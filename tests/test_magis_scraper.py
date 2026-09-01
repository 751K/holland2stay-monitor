"""
Magis（magisrealestate.com）接入。

fixtures 是 2026-09-01 从生产站点真实抓的：

- ``magis_for_rent_all.html``       ``/for-rent?only_available=0`` → 12 条
- ``magis_for_rent_available.html`` ``/for-rent``（默认）           → 4 条

两份都留着，因为**「只抓可租的」正是这次要避开的形状**：状态变更通知的原料是
「同一个单元从可租变成不可租」，只抓可租的就只能看见「消失」，那是有歧义的。
留一份默认响应，是为了让「有人把 only_available 参数弄丢了」这件事看得见。

这批用例盯的是解析器对真实 markup 的行为，以及三处最容易悄悄错掉的地方：
按位置取字段、价格口径、以及新 source 在各处注册表里的登记。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from scrapers.magis import (
    CITIES,
    FINISHING_MAP,
    TENANT_MAP,
    MagisScraper,
    _euro,
    _fmt_euro,
    _parse_card,
    _parse_date,
    _split_cards,
)

_FIX = Path(__file__).parent / "fixtures"


def _page(name: str = "magis_for_rent_all.html") -> str:
    return (_FIX / name).read_text(encoding="utf-8")


def _all_listings(name: str = "magis_for_rent_all.html"):
    return [l for url, b, u, seg in _split_cards(_page(name))
            if (l := _parse_card(url, b, u, seg))]


# ── 真实 markup ────────────────────────────────────────────────

class TestAgainstRealMarkup:
    def test_every_card_parses(self):
        """12 张卡片一张都不能丢。

        解析失败率是 ``complete`` 的判据，而 incomplete 会让 monitor 跳过状态
        收敛——一直丢几条不会报错，只会让房源永远停在旧状态。
        """
        cards = _split_cards(_page())
        assert len(cards) == 12
        assert len(_all_listings()) == 12

    def test_statuses(self):
        by_status = {}
        for l in _all_listings():
            by_status.setdefault(l.status, []).append(l.id)
        assert set(by_status) == {"Available to book", "Occupied"}
        assert len(by_status["Available to book"]) == 4

    def test_cities_all_five_are_reachable(self):
        got = {l.city for l in _all_listings()}
        assert got <= set(CITIES)
        assert "Eindhoven" in got, "主力城市一条都没解析出来"

    def test_ids_are_stable_and_clean(self):
        """id 是数据库主键，不能带 URL 编码。

        单元号里有空格（``2 F329`` → ``2%20F329``），不 unquote 的话主键、通知
        标题、深链里全是 ``%20``，而且它会跟着 URL 编码的写法变化而变化。
        """
        ids = [l.id for l in _all_listings()]
        assert len(set(ids)) == len(ids), "id 有重复"
        for i in ids:
            assert i.startswith("mg_"), i
            assert "%" not in i and " " not in i, i

    def test_field_extraction_is_positional_free(self):
        """字段按模式认，不按行号。

        同一批里有的卡片多一行设施（"External storage room"），有的多一行租客
        徽标（"Students only"），后面的字段会整体顶掉一位。按行号取会把面积读成
        设施名，而那不会抛异常——只会让面积筛选静默失准。
        """
        by_id = {l.id: l for l in _all_listings()}
        # 带设施行的
        wing = by_id["mg_the-wing_7L"].feature_map()
        assert wing["area"] == "36.43 m²"
        assert wing["floor"] == "3"
        # 带租客徽标的（楼盘名在「•」前一行，不是 lines[1]）
        zern = by_id["mg_zernikestraat_8"]
        assert zern.feature_map()["building"] == "Zernikestraat"
        assert zern.feature_map()["tenant"] == "student only"

    def test_ground_floor_is_zero_not_missing(self):
        """Ground floor 要变成 0。

        楼层筛选是「≥ N 层」，缺值 fail-closed。认不出就把一层的房源整体挡在外面，
        而站点上一层的单元不少（12 条里 3 条）。
        """
        floors = [l.feature_map().get("floor") for l in _all_listings()]
        assert floors.count("0") == 3
        assert all(f is not None for f in floors)

    def test_finishing_lands_in_the_project_vocabulary(self):
        """装修是**整体相等**匹配的维度，取值必须落在词表里。

        站点写 "Not furnished" / "Fully furnished"，而项目的四档是
        Unfurnished / Semi furnished / Furnished / Fully furnished。原样写进去的话
        "Not furnished" 谁也匹配不上，用户勾 Unfurnished 一条都收不到。
        """
        from config import (_FIN_FULLY, _FIN_FURNISHED, _FIN_SEMI,
                            _FIN_UNFURNISHED)

        canonical = {_FIN_FULLY, _FIN_FURNISHED, _FIN_SEMI, _FIN_UNFURNISHED}
        vals = {l.feature_map().get("furnishing") for l in _all_listings()}
        vals.discard(None)
        assert vals, "一条装修档位都没解析出来"
        assert vals <= canonical, vals
        assert set(FINISHING_MAP.values()) <= canonical

    def test_padded_is_not_mistaken_for_furnishing(self):
        """"Padded (floor and curtains)" 说的是铺装，不是家具。

        混进来会造出词表之外的取值，用户勾任何一档都匹配不上。
        """
        assert "padded" not in " ".join(FINISHING_MAP).lower()
        for l in _all_listings():
            assert "added" not in (l.feature_map().get("furnishing") or "")

    def test_energy_label_when_present(self):
        labels = [l.feature_map().get("energy_label") for l in _all_listings()]
        assert set(filter(None, labels)) <= {"A", "A+", "A++", "A+++", "B", "C", "D"}
        assert any(labels), "能耗标签一条都没解析出来"

    def test_available_from_is_iso(self):
        """入住日期必须是 ISO。

        日历与筛选都按字符串比大小，混进 "October 1st, 2026" 会排到所有 ISO 日期
        之后，而不会报错。
        """
        for l in _all_listings():
            if l.available_from:
                assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", l.available_from), l.available_from
        assert sum(1 for l in _all_listings() if l.available_from) == 4


# ── 价格口径 ────────────────────────────────────────────────────

class TestAllInPrice:
    def test_price_includes_service_costs(self):
        """报到手价，与 H2S / Xior 同一口径。

        卡片同时给出基础租金和服务费的确切金额，12 条实测全都有——不像
        OurDomain / OurCampus 那样只能标注。只报基础租金会让同一个租金上限下，
        Magis 的房源显得比实际便宜两三百欧。
        """
        by_id = {l.id: l for l in _all_listings()}
        l = by_id["mg_novum_2-F329"]
        fm = l.feature_map()
        assert fm["base rent"] == "€933"          # 932,93
        assert fm["service costs amount"] == "€122"   # 121,51
        assert l.price_raw == "€1.054"            # 932.93 + 121.51 = 1054.44
        assert l.price_value == 1054.0

    def test_every_listing_has_both_parts(self):
        for l in _all_listings():
            fm = l.feature_map()
            assert fm.get("base rent"), l.id
            assert fm.get("service costs amount"), l.id
            assert l.price_value and l.price_value > 0, l.id

    def test_no_rent_basis_note_when_all_in(self):
        """到手价的房源不该带「基础租金」标注。

        ``models.rent_basis_note`` 靠 ``Service costs`` 这个 key 决定要不要在价格
        旁边加一句「另计服务费」。到手价还挂着那句话是自相矛盾的。
        """
        for l in _all_listings():
            assert l.rent_basis_note == "", l.id

    def test_falls_back_to_base_rent_and_says_so(self):
        """服务费认不出时只报基础租金，并标注口径。

        静默按基础租金报会让这条在同一个上限下显得便宜——而这正是 v1.28.0 修的
        那类问题。
        """
        seg = ('<a href="https://magisrealestate.com/for-rent/x/1">'
               "<div>Available</div><div>X</div><div>•</div><div>studio 1</div>"
               "<div>Eindhoven</div><div>, Somestreet</div><div>€ 900,00</div></a>")
        l = _parse_card("https://x/1", "x", "1", seg)
        assert l.price_raw == "€900"
        assert l.feature_map().get("service_costs")
        assert l.rent_basis_note.startswith("base rent")


# ── 小函数 ──────────────────────────────────────────────────────

class TestNumberAndDateParsing:
    @pytest.mark.parametrize("raw,want", [
        ("932,93", 932.93), ("1.090,76", 1090.76), ("60,00", 60.0),
        ("1.159,40", 1159.40), ("", None), ("abc", None),
    ])
    def test_euro(self, raw, want):
        """荷兰记法：点是千分位，逗号是小数点。

        不能用 models.parse_float——它要同时容忍英式与欧式，对「只有逗号」的
        ``932,93`` 会当成千分位读成 93293，而那是三个数量级的错。
        """
        assert _euro(raw) == want

    def test_euro_delegates_to_the_shared_parser(self):
        """站点的欧式记法由 models.parse_float 处理，这里不另写一份。

        第一版在这里手写了「点当千分位、逗号当小数点」，理由是通用解析器会把
        ``932,93`` 读成 93293——实测不成立，它两种记法都对。多一份解析器就多一处
        会分叉的地方，而分叉的表现是价格差三个数量级。
        """
        from models import parse_float

        for raw in ("932,93", "1.090,76", "60,00", "1.159,40"):
            assert _euro(raw) == parse_float(raw) != None, raw

    @pytest.mark.parametrize("raw,want", [
        ("October 1st, 2026", "2026-10-01"),
        ("October 6th, 2026", "2026-10-06"),
        ("March 2nd, 2027", "2027-03-02"),
        ("May 23rd, 2026", "2026-05-23"),
        ("", None), ("soon", None), ("Smarch 1st, 2026", None),
    ])
    def test_date(self, raw, want):
        assert _parse_date(raw) == want

    def test_fmt_euro(self):
        assert _fmt_euro(1054.44) == "€1.054"
        assert _fmt_euro(900.0) == "€900"


# ── 抓取形状 ────────────────────────────────────────────────────

class TestScrapeShape:
    def _scraper(self, page):
        s = MagisScraper()
        s._page = page
        return s

    def _task(self, city):
        from scrapers.base import ScrapeTask
        return ScrapeTask(source="magis", city_key=city.lower(), city_display=city)

    def test_filters_by_city(self):
        s = self._scraper(_page())
        eindhoven = s.scrape(self._task("Eindhoven"))
        assert eindhoven.complete
        assert len(eindhoven.listings) == 3
        assert {l.city for l in eindhoven.listings} == {"Eindhoven"}

    def test_one_fetch_serves_every_city(self):
        """整批只发一次 HTTP。

        Magis 一次返回全站，按城市各发一次既没必要，也把对同一个页面的请求频率
        乘上城市数——五城就是五倍。
        """
        s = MagisScraper()
        calls = []
        s._fetch = lambda: (calls.append(1), _page())[1]
        with s.batch_session():
            for city in ("Eindhoven", "Tilburg", "Rijswijk"):
                s.scrape(self._task(city))
        assert len(calls) == 1, f"发了 {len(calls)} 次请求"

    def test_batch_session_drops_the_cache_afterwards(self):
        """缓存只在一个批次内有效，否则下一轮拿到的是上一轮的页面。"""
        s = MagisScraper()
        s._fetch = lambda: _page()
        with s.batch_session():
            s.scrape(self._task("Eindhoven"))
            assert s._page is not None
        assert s._page is None

    def test_zero_cards_is_incomplete_not_empty(self):
        """一张卡都切不出来 ≠ 这一轮没有房。

        当成空结果会让存量房源被整体收敛成 Occupied 并发一批假通知——而站点改版
        或返回错误页时正是这个形状。
        """
        s = self._scraper("<html><body>maintenance</body></html>")
        r = s.scrape(self._task("Eindhoven"))
        assert r.listings == []
        assert r.complete is False

    def test_mostly_unparseable_is_incomplete(self):
        """过半卡片认不出时也不认这一轮。

        结构变了的典型表现就是大部分卡片解析失败，而此时「抓到几条」比「一条
        没抓到」更危险——它看起来像正常结果。
        """
        good = ('<a href="https://magisrealestate.com/for-rent/x/1">'
                "<div>Available</div><div>X</div><div>•</div><div>studio 1</div>"
                "<div>Eindhoven</div><div>€ 900,00</div></a>")
        bad = "".join(
            f'<a href="https://magisrealestate.com/for-rent/y/{i}"><div>nothing</div></a>'
            for i in range(3)
        )
        s = self._scraper(good + bad)
        r = s.scrape(self._task("Eindhoven"))
        assert len(r.listings) == 1
        assert r.complete is False

    def test_source_is_stamped(self):
        for l in _all_listings():
            assert l.source == "magis"

    def test_default_page_would_miss_the_occupied_ones(self):
        """守住 only_available=0 这个参数的价值。

        默认响应只有 4 条可租的。参数一旦弄丢，所有已出租的单元会在同一轮里
        「消失」，被收敛逻辑当成状态变更——一次性发出一批假通知。
        """
        assert len(_all_listings("magis_for_rent_available.html")) == 4
        assert len(_all_listings()) == 12

    def test_request_asks_for_everything(self):
        from scrapers.magis import LIST_PARAMS

        assert LIST_PARAMS.get("only_available") == "0"


# ── 注册 ────────────────────────────────────────────────────────

class TestRegistration:
    def test_in_the_scraper_registry(self):
        from scrapers import SCRAPER_REGISTRY, get_scraper

        assert SCRAPER_REGISTRY["magis"] is MagisScraper
        assert isinstance(get_scraper("magis"), MagisScraper)

    def test_known_source_and_display_name(self):
        from config import KNOWN_SOURCES, source_display_name

        assert "magis" in KNOWN_SOURCES
        assert source_display_name("magis") == "Magis"

    def test_filter_dimensions(self):
        """登记了什么就必须真的抓得到，反之亦然。"""
        from config import sources_supporting_dim

        for dim in ("floor", "type", "finishing", "energy"):
            assert "magis" in sources_supporting_dim(dim), dim
        # tenant 不登记：站点只在部分房源上打徽标，「没有徽标」的含义没有证据
        assert "magis" not in sources_supporting_dim("tenant")

    def test_registered_dimensions_are_actually_populated(self):
        """能力表说支持，房源就得真的带上这个字段。

        只改能力表不改抓取，等于把 fail-open 换成 fail-closed——用户勾了条件反而
        一条都收不到。
        """
        listings = _all_listings()
        for key in ("floor", "type", "furnishing"):
            filled = sum(1 for l in listings if l.feature_map().get(key))
            assert filled == len(listings), f"{key} 只有 {filled}/{len(listings)} 条"

    def test_short_badge_everywhere(self):
        """通知、Jinja 过滤器、前端 JS 三处各有一份表，缺一处就显示成 Magis 以外的东西。"""
        import notifier
        from app.jinja_filters import source_short

        assert notifier._source_short("magis") == "MG"
        assert source_short("magis") == "MG"
        js = (Path(__file__).parent.parent / "static" / "app.js").read_text(encoding="utf-8")
        assert "magis: 'MG'" in js
        assert "magis: 'Magis'" in js

    def test_city_registry_and_tasks(self):
        from config import KNOWN_MAGIS_CITIES

        keys = {c["key"] for c in KNOWN_MAGIS_CITIES}
        assert len(KNOWN_MAGIS_CITIES) == 5
        assert "eindhoven" in keys
        # 注册表里的城市名必须和解析出来的对得上，否则按城市筛会全部落空
        assert {c["city"] for c in KNOWN_MAGIS_CITIES} == set(CITIES)

    def test_env_key_is_registered(self):
        from env_registry import tier_of

        # runtime：和其余三个 *_CITIES 同一档，由面板的「抓取目标」页管理，
        # 值落在 SQLite 的 app_settings 里而不是 .env。
        assert tier_of("MAGIS_CITIES") == "runtime", (
            "MAGIS_CITIES 没登记，启动审计会把它报成「不认识的键」"
        )
        from env_registry import tier_of as _t
        for peer in ("CITIES", "OURDOMAIN_CITIES", "XIOR_CITIES"):
            assert _t(peer) == "runtime", f"{peer} 换档了，MAGIS_CITIES 要跟着走"

    def test_no_auto_booking(self):
        """只通知，不预订——下单流程未侦察，ToS 暴露面未评估。"""
        import monitor
        import inspect

        src = inspect.getsource(monitor)
        assert '"magis"' not in src or "AUTO_BOOK" not in src.split('"magis"')[0][-200:]


# ── 设置面板 ────────────────────────────────────────────────────

class TestSettingsPanel:
    """新平台要在面板上真的看得见、改得动。

    登记进 env_registry 的 runtime 档意味着「面板正在运行时改写它」——只登记不接
    界面，那个档位就是句空话，而这正是本会话早些时候 MAP_MAX_AGE_DAYS 踩过的坑。
    """

    def test_platform_toggle_is_rendered(self, admin_client):
        """平台开关从 KNOWN_SOURCES 渲染，加了 source 就该自动出现。"""
        html = admin_client.get("/settings").get_data(as_text=True)
        assert 'name="source_selected" value="magis"' in html

    def test_city_picker_is_rendered(self, admin_client):
        html = admin_client.get("/settings").get_data(as_text=True)
        assert 'name="magis_city_selected"' in html
        for key in ("eindhoven", "tilburg", "rijswijk",
                    "amersfoort", "s-hertogenbosch"):
            assert f",{key}\"" in html, key

    def test_all_cities_are_checked_when_unset(self, admin_client):
        """未配置时全部勾上。

        空 = 全部（与 XIOR_CITIES 同一约定）。一个都不勾会让用户以为默认什么都
        不抓，然后去手动勾一遍——那反而把「空」写成了一份显式清单。
        """
        import re

        html = admin_client.get("/settings").get_data(as_text=True)
        boxes = re.findall(
            r'<input type="checkbox" name="magis_city_selected"[^>]*>', html)
        assert len(boxes) == 5
        assert all("checked" in b for b in boxes), boxes

    def test_saving_writes_the_env_key(self, admin_client, test_app):
        """勾选真的落到 MAGIS_CITIES 上。

        断言落在**存下来的值**上，而不是「保存成功」——这个区块最容易的错法是
        表单字段名和 getlist 的名字对不上，那时页面照样提示已保存，值一个都没
        写进去。设置写的是 SQLite 的 app_settings（runtime 档），不是 .env。
        """
        r = admin_client.post("/settings", data={
            "csrf_token": "test_csrf",
            "source_selected": ["holland2stay", "magis"],
            "magis_city_selected": ["Eindhoven,eindhoven", "Tilburg,tilburg"],
        }, follow_redirects=False)
        assert r.status_code in (302, 303), r.status_code

        with test_app.app_context():
            from app.db import storage
            st = storage()
            try:
                saved = st.get_app_setting("MAGIS_CITIES")
            finally:
                st.close()
        assert saved == "Eindhoven,eindhoven|Tilburg,tilburg"

    def test_saved_selection_comes_back_checked(self, admin_client):
        """存进去的要再读得出来，否则用户每次打开设置都看到全选。"""
        admin_client.post("/settings", data={
            "csrf_token": "test_csrf",
            "source_selected": ["holland2stay", "magis"],
            "magis_city_selected": ["Eindhoven,eindhoven"],
        })
        import re

        html = admin_client.get("/settings").get_data(as_text=True)
        boxes = re.findall(
            r'<input type="checkbox" name="magis_city_selected" value="([^"]+)"([^>]*)>',
            html)
        checked = {v.split(",")[-1] for v, attrs in boxes if "checked" in attrs}
        assert checked == {"eindhoven"}, checked

    def test_empty_is_not_reported_as_no_target(self):
        """空 = 全部，所以不能进「平台开着却没有目标」那张警告表。

        XIOR_CITIES 同理，注释里写着。把 magis 混进去会在默认配置下常驻一条
        「Magis 不会抓取任何房源」——而它其实在抓全部五城。
        """
        import inspect

        from app.routes import settings as mod

        src = inspect.getsource(mod)
        table = src[src.index("_no_target_when_empty"):]
        table = table[:table.index("}")]
        assert "MAGIS_CITIES" not in table
        assert "XIOR_CITIES" not in table

    def test_target_registry_knows_magis(self):
        """面板保存的值要能通过校验，否则启动审计会报「未知楼盘」。"""
        from target_config import parse_targets

        got, problems = parse_targets(
            "MAGIS_CITIES", "Eindhoven,eindhoven|Tilburg,tilburg")
        assert got == [("Eindhoven", "eindhoven"), ("Tilburg", "tilburg")]
        assert not problems

    def test_unknown_magis_key_is_flagged(self):
        from target_config import parse_targets

        _, problems = parse_targets("MAGIS_CITIES", "Utrecht,utrecht")
        assert problems, "写了一个不存在的城市却没有报出来"


def test_platform_dropdown_lists_every_known_source(admin_client):
    """用户表单的「平台」多选要列出 KNOWN_SOURCES 的全部条目。

    三个页面各自构造 ``source_options``（用户新建、用户编辑、全局设置）。加了
    source 却漏掉其中一处，表现是那一页选不到新平台——而另外两页是好的，很容易
    看成「偶尔没刷新」。
    """
    import re

    from config import KNOWN_SOURCES, source_display_name

    for path in ("/users/new", "/settings"):
        html = admin_client.get(path).get_data(as_text=True)
        for key in KNOWN_SOURCES:
            assert f'value="{key}"' in html, f"{path} 缺 {key}"
        assert source_display_name("magis") in html, path


def test_direct_fallback_really_bypasses_the_proxy():
    """代理全部冷却时要真的直连。

    curl 拿到 ``proxies={}`` 会回落到 HTTP_PROXY / HTTPS_PROXY 环境变量——也就是
    回到那个刚被判定为失效的代理，于是「降级直连」从来没有真的直连过。生产容器里
    这两个变量常年指着 webshare，而 webshare 欠费时一直回 402。

    2026-08-26 ourdomain 踩过同一个坑并留下并排实测：``{"http":"","https":""}``
    → 200，``{}`` → 402。2026-09-01 接入 magis 时又踩了一次，是部署后在生产上
    实跑才发现的——本地跑不出来，因为本地没有那两个环境变量。
    """
    import inspect

    from net import NO_PROXY_CURL

    import scrapers.magis as m

    src = inspect.getsource(m.MagisScraper._fetch)
    assert "NO_PROXY_CURL" in src, "代理为空时传的不是 NO_PROXY_CURL"
    assert not re.search(r"else\s*\{\s*\}", src), "还留着 proxies={} 那种写法"
    assert NO_PROXY_CURL == {"http": "", "https": ""}
