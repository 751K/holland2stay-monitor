"""地图深链（``/map?focus=<id>``）、按 id 定位、以及用户筛选。

这一轮改动要守住的三件事
------------------------
**一、「看不到」有三种，不能合并成一句「没找到」。** 深链点过去而地图上没有
那套房，可能是：库里没这个 id（链接过期）、有但还没解析出坐标、有坐标但被
14 天新鲜度窗口或用户筛选挡在视图之外。用户面对这三种能做的事完全不同，
所以 ``/api/map/locate`` 必须把它们分开报。

**二、地址推导只能有一份。** 地址是 geocode 缓存的**主键**。全量查询和按 id
查询若各推各的，深链会查不到那套房早就缓存好的坐标——而且不报错，只是表现成
「这套房没有位置」。

**三、地图此前完全无视用户自己的筛选。** ``/api/map`` 一律走
``get_map_payload()`` 不传 user，列表页按用户条件筛过，地图上却是全量。
``/api/v1/map`` 早就按角色区分了，Web 这边是漏的。
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
STATIC = TEMPLATES.parent / "static"


def _js(path: Path) -> str:
    """模板 / JS 文件的源码，剥掉 ``//``、``/* */`` 与 Jinja ``{# #}`` 注释。

    和 ``_code`` 同一个理由，而且更容易中招：这个文件里几乎每处 grep 断言，
    上方都跟着一段引用了同一个标识符的说明注释。实测踩过一次——
    ``test_map_and_calendar_both_guard`` 第一版把 calendar.html 里的哨兵判断
    整句删掉，测试**照样是绿的**，因为我自己写的注释里有一句「见 app.js
    isSentinelDate」，grep 咬中的是它。
    """
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"\{#.*?#\}", " ", src, flags=re.S)      # Jinja 注释
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)        # 块注释
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)          # 整行 // 注释
    return src


def _code(fn) -> str:
    """函数源码，剥掉注释**和 docstring**。

    这个文件里的断言是「源码里必须/不许出现某段写法」，而解释性文字常常原样
    引用那段写法——不剥的话咬中的是说明而不是代码。docstring 也要剥：本文件
    的 test_shares_the_scraper_judgement 第一版就是被 available_display 自己
    docstring 里的 "2050" 咬中的（tests/test_scraper_blast_radius.py 的注释
    里记着同一个坑今天已经踩过三次）。
    """
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.split("\n"))


def _seed(st, listing_id="l1", *, status="Available to book", days_ago=0,
          name="Kastanjelaan 1-639", city="Eindhoven", features='[]'):
    st.conn.execute(
        """INSERT OR REPLACE INTO listings
           (id, name, status, price_raw, available_from, url, city, source,
            features, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now', ?))""",
        (listing_id, name, status, "€1444", "2026-07-01", "https://example.test/x",
         city, "holland2stay", features, "-%d days" % days_ago),
    )
    st.conn.commit()


class TestLookupByIdIgnoresFreshnessWindow:
    def test_stale_listing_is_still_findable(self, temp_db, monkeypatch):
        """深链要能定位到已经下架的房源。

        用户是从房源列表点过来的——那一套确实存在，只是过了 14 天窗口。
        窗口是为了让地图别被几个月前的终态塞满，不是为了让人查不到。
        """
        monkeypatch.setenv("MAP_MAX_AGE_DAYS", "14")
        _seed(temp_db, "old", days_ago=90, status="Occupied")
        assert temp_db.get_map_listings() == [], "前提：它确实被窗口挡住了"
        assert temp_db.get_map_listing_by_id("old") is not None

    def test_missing_id_is_none(self, temp_db):
        assert temp_db.get_map_listing_by_id("nope") is None

    def test_empty_id_is_none(self, temp_db):
        """空串不能碰巧匹配上某一行。"""
        _seed(temp_db, "l1")
        assert temp_db.get_map_listing_by_id("") is None


class TestAddressDerivationIsShared:
    """地址是 geocode 缓存的主键，两条路径必须推出同一个字符串。"""

    @pytest.mark.parametrize("features,name", [
        ('[]', "Kastanjelaan 1-639"),                       # 回退到 name+city
        ('["Address: Wenckebachweg 51, 1096 AN Amsterdam"]', "Diemen #6017"),
    ])
    def test_two_paths_agree(self, temp_db, features, name):
        _seed(temp_db, "l1", features=features, name=name)
        full = temp_db.get_map_listings(max_age_days=0)[0]
        one = temp_db.get_map_listing_by_id("l1")
        assert one["address"] == full["address"]
        assert one == full, "两条路径应当产出完全相同的条目"

    def test_there_is_only_one_implementation(self):
        """钉住「只有一份」这个前提本身。

        哪天有人在 get_map_listing_by_id 里另写一遍地址拼接，这条会红——
        那正是 bug 出现的时刻，而不是它被发现的时刻。
        """
        from mstorage._map_calendar import MapCalendarOps

        for fn in (MapCalendarOps.get_map_listings,
                   MapCalendarOps.get_map_listing_by_id):
            src = _code(fn)
            assert "row_to_map_entry" in src
            assert "Netherlands" not in src, f"{fn.__name__} 里又拼了一遍地址"


class TestLocateEndpoint:
    """三种「看不到」分开报。"""

    def test_found_with_coords(self, admin_client, monkeypatch):
        from app.db import storage

        st = storage()
        try:
            _seed(st, "l1")
            entry = st.get_map_listing_by_id("l1")
            st.cache_coords(entry["address"], 51.44, 5.47)
        finally:
            st.close()
        d = admin_client.get("/api/map/locate?id=l1").get_json()
        assert d["ok"] is True
        assert d["listing"]["lat"] == 51.44 and d["listing"]["lng"] == 5.47

    def test_found_but_not_geocoded(self, admin_client):
        """有房源没坐标——地图上确实没有这个点，但链接没坏。"""
        from app.db import storage

        st = storage()
        try:
            _seed(st, "l2")
        finally:
            st.close()
        d = admin_client.get("/api/map/locate?id=l2").get_json()
        assert d["ok"] is False
        assert d["reason"] == "no_coords"
        assert d["listing"]["id"] == "l2", "认得出是哪一套，前端才能说清楚"

    def test_unknown_id(self, admin_client):
        d = admin_client.get("/api/map/locate?id=ghost").get_json()
        assert d == {"ok": False, "reason": "not_found"}

    def test_no_coords_and_not_found_are_different(self, admin_client):
        """核心断言：这两种绝不能报成同一句。

        合并之后，「等管理员解析地址」和「这个链接作废了」在界面上长得一模
        一样，而用户能做的事完全相反。
        """
        from app.db import storage

        st = storage()
        try:
            _seed(st, "l3")
        finally:
            st.close()
        a = admin_client.get("/api/map/locate?id=l3").get_json()
        b = admin_client.get("/api/map/locate?id=ghost").get_json()
        assert a["reason"] != b["reason"]

    def test_blank_id_is_rejected(self, admin_client):
        r = admin_client.get("/api/map/locate?id=")
        assert r.status_code == 400

    def test_requires_login(self, client):
        assert client.get("/api/map/locate?id=l1").status_code == 401


class TestMapAppliesUserFilter:
    def test_web_endpoint_passes_the_user(self):
        """``/api/map`` 必须把当前用户传下去。

        用 grep 而不是端到端跑一遍：构造一个带 listing_filter 的 user session
        需要拉起整条 users/加密链路，而这里要守的其实只有一件事——那个参数
        别再是空的。``/api/v1/map`` 早就传了，Web 这边漏了一年。
        """
        from app.routes import map_routes

        src = _code(map_routes.api_map)
        assert "get_map_payload(_current_map_user())" in src, "又变回不传 user 了"

    def test_helper_returns_none_for_admin_and_guest(self, app_ctx):
        """admin / guest 没有 UserConfig，必须是 None（= 不额外筛），不是抛。"""
        from flask import session

        from app.routes.map_routes import _current_map_user
        from web import app

        for role in ("admin", "guest"):
            with app.test_request_context("/api/map"):
                session["authenticated"] = True
                session["role"] = role
                assert _current_map_user() is None

    def test_helper_degrades_instead_of_raising(self, app_ctx, monkeypatch):
        """取用户失败时返回 None。地图少一层筛选是退化，整页 500 才是故障。"""
        import users
        from flask import session

        from app.routes.map_routes import _current_map_user
        from web import app

        monkeypatch.setattr(users, "load_users",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        with app.test_request_context("/api/map"):
            session["authenticated"] = True
            session["role"] = "user"
            session["user_id"] = "u1"
            assert _current_map_user() is None


class TestListingsEntryPoint:
    """列表页那个「在地图上查看」的入口。"""

    def _html(self) -> str:
        return _js(TEMPLATES / "listings.html")

    def test_both_views_link_to_the_map(self):
        html = self._html()
        assert html.count("?focus={{ l.id | urlencode }}") == 2, (
            "表格行和移动端卡片各要一个；只加一处等于一半的用户没有这个入口")

    def test_id_is_url_encoded(self):
        """房源 id 里有 ``#``、空格的平台不止一个（OurDomain 的 unit 名就是）。
        不转义的话 ``#`` 之后整段会被当成锚点丢掉。"""
        assert "l.id | urlencode" in self._html()

    def test_the_pin_is_not_inside_the_card_link(self):
        """移动端整张卡片是一个 <a>，<a> 里不能再套 <a>。

        套了的话浏览器会把它拆成两个相邻链接，图钉失效——点哪儿都跳外部
        预订页。这条断言直接看结构：图钉必须出现在卡片 </a> 之后。
        """
        html = self._html()
        card = html.index('class="listing-card-item"')
        close = html.index("</a>", html.index("lc-footer", card))
        pin = html.index('class="lc-map-pin"', card)
        assert pin > close, "图钉嵌在了卡片 <a> 里面"

    def test_pin_has_an_accessible_name(self):
        html = self._html()
        assert html.count("aria-label=\"{{ _('view_on_map') }}\"") == 2


class TestSharedFrontendHelpers:
    """分桶与散开只有一份实现，在 static/app.js。"""

    def _appjs(self) -> str:
        return _js(STATIC / "app.js")

    def _mapjs(self) -> str:
        return _js(TEMPLATES / "map.html")

    def test_map_uses_the_shared_helpers(self):
        m = self._mapjs()
        assert "window.statusBucket" in m
        assert "window.mapDisplayCoord" in m

    def test_map_does_not_reimplement_bucketing(self):
        """map.html 里不许再出现第二套判据。

        这个仓库有过 sourceLabel 在 map / calendar / stats 各写一份、
        三份都漏掉 ourcampus 的先例。
        """
        assert "indexOf('lottery')" not in self._mapjs(), \
            "map.html 里又写了一份分桶判据"

    def test_unknown_status_gets_its_own_bucket(self):
        """认不出的状态不能掉进默认隐藏的 occupied 档。

        掉进去的话，新平台冒出的新状态会从地图上**静默消失**——正是
        「把不知道当成一个确定的答案」那个形状。
        """
        js = self._appjs()
        i = js.index("window.statusBucket")
        body = js[i:js.index("};", i)]
        assert "return 'other'" in body, "认不出的状态没有自己的一档"
        assert body.rstrip().rstrip("}").rstrip().endswith("return 'other';"), (
            "兜底分支必须是 'other'，不是 'occupied'")

    def test_other_is_on_by_default(self):
        """而且默认是**开**的，否则等于没分出来。"""
        m = self._mapjs()
        i = m.index('data-bucket="other"')
        assert 'aria-pressed="true"' in m[i:i + 120]

    def test_occupied_is_off_by_default(self):
        """生产实测 235 条里 117 条是 Occupied——默认全开等于把地图淹掉。"""
        m = self._mapjs()
        for bucket, expected in (("occupied", "false"), ("reserved", "false"),
                                 ("book", "true"), ("lottery", "true")):
            i = m.index('data-bucket="%s"' % bucket)
            assert 'aria-pressed="%s"' % expected in m[i:i + 120], bucket


class TestTranslations:
    @pytest.mark.parametrize("key", [
        "view_on_map", "map_st_book", "map_st_lottery", "map_st_reserved",
        "map_st_occupied", "map_st_other", "map_stacked_note", "map_uncached_note",
        "map_focus_out_of_view", "map_focus_no_coords", "map_focus_not_found",
        "map_filter_city", "map_filter_source", "map_max_rent", "map_min_area",
        "map_reset", "map_showing", "map_of", "map_filter_all", "map_focus_dismiss",
    ])
    def test_key_exists_in_both_languages(self, key):
        from translations import TRANSLATIONS

        assert key in TRANSLATIONS, f"translations.py 缺 {key}"
        assert TRANSLATIONS[key].get("zh"), f"{key} 缺中文"
        assert TRANSLATIONS[key].get("en") is not None, f"{key} 缺英文"

    def test_placeholder_survives_translation(self):
        """带 {n} 的文案，两种语言都得留着那个占位符——
        少一个就会在界面上显示成字面量而不是数字。"""
        from translations import TRANSLATIONS

        for key in ("map_stacked_note", "map_uncached_note"):
            for lang in ("zh", "en"):
                assert "{n}" in TRANSLATIONS[key][lang], f"{key}/{lang} 丢了 {{n}}"


class TestSentinelDateNeverShownAsADate:
    """``2050-01-01`` 是 H2S 的「入住日未定」哨兵，不是日期。

    scraper 认得它、存储层认得它、booker 也拦得住它——唯独界面把它原样显示成
    一个日期。用户读到的是「2050 年可入住」，像一个（荒唐的）事实，而它的意思
    其实是「不知道」。日历更进一步：那套房会被排进 2050 年那一格。
    """

    @pytest.mark.parametrize("value,expected", [
        ("2050-01-01", "—"),
        ("2099-12-31", "—"),      # 哨兵换个年份也要认
        ("", "—"),
        (None, "—"),
        ("2026-07-01", "2026-07-01"),
        ("2049-12-31", "2049-12-31"),   # 边界另一侧是真日期
    ])
    def test_filter(self, value, expected):
        from app.jinja_filters import available_display

        assert available_display(value) == expected

    def test_filter_is_registered(self):
        from web import app

        assert "available_display" in app.jinja_env.filters

    def test_shares_the_scraper_judgement(self):
        """判据必须来自 models，不能在 filter 里另写一个 2050。

        models 里那段注释已经记着「两处各写一个 2050 迟早分叉」——界面是第三处。
        """
        from app import jinja_filters

        src = _code(jinja_filters.available_display)
        assert "is_sentinel_available_from" in src
        assert "2050" not in src, "又写死了一个 2050"

    @pytest.mark.parametrize("template", ["listings.html", "index.html"])
    def test_templates_use_the_filter(self, template):
        html = _js(TEMPLATES / template)
        assert "l.available_from or '—'" not in html, "还在直接显示原值"
        assert "available_display" in html

    def test_js_helper_matches_python(self):
        """JS 侧（地图弹窗 / 日历）用同一套年份判据。"""
        js = _js(STATIC / "app.js")
        assert "window.isSentinelDate" in js
        from models import SENTINEL_AVAILABLE_FROM_YEAR

        assert ("window.SENTINEL_AVAILABLE_FROM_YEAR = %d" % SENTINEL_AVAILABLE_FROM_YEAR) in js

    def test_map_and_calendar_both_guard(self):
        """两个 JS 页面都要挡，只挡一个等于没挡——日历那处更严重。"""
        for name in ("map.html", "calendar.html"):
            assert "isSentinelDate" in _js(TEMPLATES / name), f"{name} 没挡哨兵日期"


class TestSpreadIsComputedOnce:
    """同址散开的几何只在服务端算一次，三端照着画。

    这几行圆环本身不难，难的是它会被抄三遍。抄三遍的表现不是崩溃，是**同一套房
    在 Web、iOS、Android 上显示在三个不同的位置**——没有任何地方会报错，也没有
    任何人会去比对。
    """

    def test_payload_carries_display_coords(self, temp_db, monkeypatch):
        from app.services.listing_service import spread_stacked_coords

        rows = [{"id": "b", "lat": 52.336693, "lng": 4.926876},
                {"id": "a", "lat": 52.336693, "lng": 4.926876},
                {"id": "solo", "lat": 51.4, "lng": 5.4}]
        spread_stacked_coords(rows)
        for r in rows:
            assert "display_lat" in r and "display_lng" in r and "stack_n" in r

    def test_single_listing_keeps_its_true_position(self):
        from app.services.listing_service import spread_stacked_coords

        r = {"id": "solo", "lat": 51.4, "lng": 5.4}
        spread_stacked_coords([r])
        assert (r["display_lat"], r["display_lng"]) == (51.4, 5.4)
        assert r["stack_n"] == 1

    def test_stacked_listings_get_distinct_positions(self):
        """核心断言：九套重合的房源摊成九个不同的点。

        不摊开的话，iOS 那边它们在**任何缩放**下都归同一个网格 cell，点击展开又
        被 boundingRegion 的 minSpan 兜成固定视野——那九套一套都碰不到。
        """
        from app.services.listing_service import spread_stacked_coords

        rows = [{"id": "od_%d" % i, "lat": 52.336693, "lng": 4.926876}
                for i in range(9)]
        spread_stacked_coords(rows)
        seen = {(round(r["display_lat"], 9), round(r["display_lng"], 9)) for r in rows}
        assert len(seen) == 9
        assert all(r["stack_n"] == 9 for r in rows)

    def test_positions_are_stable_across_calls(self):
        """输入顺序变了，每套房还是落在圈上同一个位置——否则刷新一次就跳一次。"""
        from app.services.listing_service import spread_stacked_coords

        mk = lambda: [{"id": "od_%d" % i, "lat": 52.3, "lng": 4.9} for i in range(6)]
        a, b = mk(), list(reversed(mk()))
        spread_stacked_coords(a)
        spread_stacked_coords(b)
        pos = lambda rows: {r["id"]: (round(r["display_lat"], 9), round(r["display_lng"], 9))
                            for r in rows}
        assert pos(a) == pos(b)

    def test_spread_stays_within_the_building(self):
        """散开幅度要小到还在同一栋楼的范围内——位置是近似值，不是随便挪。"""
        import math

        from app.services.listing_service import spread_stacked_coords

        rows = [{"id": "od_%d" % i, "lat": 52.336693, "lng": 4.926876}
                for i in range(10)]
        spread_stacked_coords(rows)
        for r in rows:
            d = math.hypot((r["display_lat"] - 52.336693) * 111_320,
                           (r["display_lng"] - 4.926876) * 111_320
                           * math.cos(math.radians(52.336693)))
            assert d < 40, "偏移 %.0f m，超出一栋楼的尺度" % d

    def test_rows_without_coords_are_left_alone(self):
        """没有坐标的行不该被凭空造出一个位置。"""
        from app.services.listing_service import spread_stacked_coords

        r = {"id": "x"}
        spread_stacked_coords([r])
        assert "display_lat" not in r

    def test_locate_result_also_carries_the_fields(self, admin_client):
        """兜底那条也要带全字段，客户端不必因为字段缺失去猜。"""
        from app.db import storage

        st = storage()
        try:
            _seed(st, "l9")
            entry = st.get_map_listing_by_id("l9")
            st.cache_coords(entry["address"], 51.44, 5.47)
        finally:
            st.close()
        d = admin_client.get("/api/map/locate?id=l9").get_json()
        assert d["listing"]["display_lat"] == 51.44
        assert d["listing"]["stack_n"] == 1

    def test_javascript_does_not_reimplement_the_geometry(self):
        """JS 侧不许再出现圆环几何。抄第二遍就是分叉的开始。"""
        js = _js(STATIC / "app.js") + _js(TEMPLATES / "map.html")
        assert "Math.PI" not in js, "JS 里又算了一遍散开几何"
        assert "spreadStackedCoords" not in js

    def test_v1_exposes_locate(self):
        """iOS / Android 走 v1，Web 走 /api/map——两条路由同一个 service。"""
        from app.routes.api_v1 import map as v1_map

        src = _code(v1_map._locate)
        assert "locate_map_listing" in src
