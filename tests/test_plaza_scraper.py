"""Plaza (newnewnew.space) scraper 的解析、过滤与完整性判据。

fixture ``plaza_getallobjects.json`` 是 2026-09-02 ``getallobjects`` 的原始响应：
55 条里 53 条住宅、2 条停车位，其中 4 条在德国。也就是说荷兰住宅 49 条——这三个
数字之间的差额正是本 scraper 要过滤掉的东西，下面逐个守住。
"""
import collections
import copy
import json
from pathlib import Path

import pytest

from scrapers.base import ScrapeNetworkError, ScrapeTask
from scrapers.plaza import (
    ALLOCATION_LABELS,
    CITIES,
    HOUSING_CATEGORY,
    NL_LAND_ID,
    PlazaScraper,
    _floor,
    _fmt_euro,
    _parse_object,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads((FIXTURES / "plaza_getallobjects.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def parsed(payload):
    items, complete = PlazaScraper()._parse_all(payload)
    assert complete
    return items


class TestFiltering:
    def test_only_dutch_housing_survives(self, payload, parsed):
        raw = payload["result"]
        housing = [r for r in raw if (r["dwellingType"] or {}).get("categorie") == HOUSING_CATEGORY]
        nl_housing = [r for r in housing
                      if str((r.get("land") or {}).get("id")) == NL_LAND_ID]
        assert len(raw) == 55 and len(housing) == 53 and len(nl_housing) == 49
        assert len(parsed) == 49

    def test_parking_is_dropped(self, payload):
        """同一个端点还返回停车位与储藏间——站点自己的房源页也把它们筛掉了。

        不筛的话用户会收到「€50 的房源」推送，而那是个车位。
        """
        parking = [r for r in payload["result"]
                   if (r["dwellingType"] or {}).get("categorie") == "voorVoertuig"]
        assert parking, "fixture 里应当有停车位，否则这条测试空过"
        assert all(_parse_object(r) is None for r in parking)

    def test_foreign_listings_are_dropped(self, payload):
        """德国 Bochum 的房源不该进荷兰用户的库。"""
        foreign = [r for r in payload["result"]
                   if str((r.get("land") or {}).get("id")) != NL_LAND_ID]
        assert foreign, "fixture 里应当有非荷兰房源，否则这条测试空过"
        assert all(_parse_object(r) is None for r in foreign)
        assert "Bochum" not in {x.city for x in _parsed_cities(foreign)}

    def test_country_is_judged_by_land_id_not_region_text(self, payload):
        """国别判据是 ``land.id`` 这个主键，不是 ``regio.name`` 的展示文案。

        文案改一次（"Nederland - Utrecht" → "NL - Utrecht"）就会让按前缀判断的
        实现静默漏掉全部荷兰房源。这里把文案改掉，结果必须不变。
        """
        mangled = copy.deepcopy(payload)
        for r in mangled["result"]:
            if r.get("regio"):
                r["regio"]["name"] = "XX - " + (r["regio"].get("name") or "")
        items, complete = PlazaScraper()._parse_all(mangled)
        assert complete and len(items) == 49


def _parsed_cities(rows):
    return [x for x in (_parse_object(r) for r in rows) if x]


class TestFieldMapping:
    def test_first_come_listing(self, parsed):
        item = next(x for x in parsed if x.id == "pz_15676")
        assert item.name == "Limapad 16, Utrecht"
        assert item.city == "Utrecht"
        assert item.price_raw == "€867,75"
        assert item.available_from == "2026-08-10"
        assert item.source == "plaza"
        assert item.url.endswith("/aanbod/huurwoningen/details/15676-limapad-16-utrecht")

        fm = dict(f.split(": ", 1) for f in item.features)
        assert fm["Allocation"] == ALLOCATION_LABELS["dth"]
        assert fm["Type"] == "Studio"
        assert fm["Area"] == "16 m²"
        assert fm["Floor"] == "0"
        assert fm["Tenant"] == "student only"
        assert fm["Net rent"] == "€653"
        assert fm["Registration"] == "required to respond (paid account)"

    def test_price_is_the_all_in_rent_not_the_net_rent(self, parsed):
        """``price_raw`` 报 totalRent（到手价），netRent 只写进 features。

        报反了会让房源在同一个租金上限下显得便宜两百多欧——实测这条
        totalRent €867,75 / netRent €653。
        """
        item = next(x for x in parsed if x.id == "pz_15676")
        fm = dict(f.split(": ", 1) for f in item.features)
        assert item.price_raw == "€867,75" and fm["Net rent"] == "€653"

    def test_allocation_model_is_recorded_for_every_listing(self, parsed):
        """分配模型必须每条都有——它决定用户该多快动手。"""
        for item in parsed:
            fm = dict(f.split(": ", 1) for f in item.features)
            assert fm.get("Allocation"), f"{item.id} 没有 Allocation"

    def test_allocation_split_matches_the_raw_flags(self, payload, parsed):
        """31 条 DTH + 18 条 reactiedatum = 49 条荷兰住宅。

        （全部住宅 53 条里 DTH 31 / reactiedatum 22，差额是德国那 4 条，它们
        全是 reactiedatum。)
        """
        want_dth = sum(
            1 for r in payload["result"]
            if (r["dwellingType"] or {}).get("categorie") == HOUSING_CATEGORY
            and str((r.get("land") or {}).get("id")) == NL_LAND_ID
            and (r.get("model") or {}).get("advertentieSluitenNaEersteReactie"))
        got = collections.Counter(
            next(f.split(": ", 1)[1] for f in x.features if f.startswith("Allocation: "))
            for x in parsed)
        assert want_dth == 31
        assert got[ALLOCATION_LABELS["dth"]] == 31
        assert got[ALLOCATION_LABELS["reactiedatum"]] == 18

    def test_allocation_wording_comes_from_the_site_not_the_field_name(self):
        """两处文案必须说出站点自己说的那两件事，别再从字段名直译。

        2026-09-02 第一版就是直译 ``advertentieSluitenNaEersteReactie``，写成
        「回应之后广告即关闭」——漏掉了它其实是**当场成交**，也漏掉了接受即
        **放弃其它 offer**；同时把 reactiedatum 说成「到点后再分配」，而站点管它
        叫「Snelle reageerder」，明写「不设等候名单」。两处都影响用户该多快动手。
        """
        dth = ALLOCATION_LABELS["dth"]
        assert "books it outright" in dth and "forfeits other offers" in dth
        assert "no waiting list" in ALLOCATION_LABELS["reactiedatum"]
        # 推送没价值的那几类也要有文案——将来它们出现时得看得出来
        for k in ("loting", "inschrijfduur"):
            assert "speed does not help" in ALLOCATION_LABELS[k]


class TestBuilding:
    """Plaza 不发布楼盘名，这一列是从地址结构推导的。"""

    def test_every_listing_gets_one(self, parsed):
        """不推导的话「楼盘」列全是「—」——2026-09-02 上线后就是这个样子。"""
        for item in parsed:
            fm = dict(f.split(": ", 1) for f in item.features)
            assert fm.get("Building"), f"{item.id} 没有 Building"

    def test_addition_means_the_number_marks_the_building(self, parsed):
        """有附加号：门牌标楼、附加号标单元 → 楼盘 = 街道 + 门牌。"""
        fm = dict(f.split(": ", 1)
                  for f in next(x for x in parsed if x.name.startswith("Bogardeind")).features)
        assert fm["Building"] == "Bogardeind 219"

    def test_no_addition_means_the_number_is_the_unit(self, parsed):
        """无附加号：门牌本身就是单元 → 楼一级只到街道。

        Limapad 那 32 条分布在六个邮编（3584 SR/ST/SV/SW/SX/SZ，同一综合体的不同
        入口），只有街道这一级能把它们归到一起。
        """
        limapad = [x for x in parsed if x.name.startswith("Limapad")]
        assert len(limapad) == 32
        for item in limapad:
            fm = dict(f.split(": ", 1) for f in item.features)
            assert fm["Building"] == "Limapad"

    def test_the_source_really_has_no_building_field(self, payload):
        """推导是因为**上游确实没有**，不是因为懒得找。

        这条钉住那个前提：哪天 Plaza 开始发楼盘名，它会失败，提醒改用真实字段。
        """
        rows = [r for r in payload["result"]
                if (r["dwellingType"] or {}).get("categorie") == HOUSING_CATEGORY
                and str((r.get("land") or {}).get("id")) == NL_LAND_ID]
        assert all(not (r.get("neighborhood") or {}).get("name") for r in rows)
        assert all(not r.get("projectID") for r in rows)
        assert all(not r.get("verzameladvertentieID") for r in rows)
        assert all("complex" not in r for r in rows)
        # quarter / municipality 只是城市名，不能当楼盘
        for r in rows:
            city = (r.get("city") or {}).get("name")
            assert (r.get("quarter") or {}).get("name") == city

    def test_rule_is_structural_not_data_dependent(self):
        """判据是地址结构，不是「同街道有几条」。

        按数据分布判会让同一栋楼今天一条、明天五条时标签翻来覆去。
        """
        from scrapers.plaza import _building
        assert _building("Bogardeind", 219, "D21") == "Bogardeind 219"
        assert _building("Limapad", 16, "") == "Limapad"
        assert _building("Limapad", 16, None) == "Limapad"
        assert _building("", 16, "A") == ""


class TestTenant:
    def test_student_only_comes_from_the_listing_not_a_site_wide_claim(self, payload):
        """租客维度逐条读 doelgroepen，不是整站断言。

        同一批里 student 与 regulier 并存，一刀切会把非学生盘也标成学生盘——
        那正是 Xior 2026-08-21 在 finishing 上栽过的那种错。
        """
        rows = [r for r in payload["result"]
                if (r["dwellingType"] or {}).get("categorie") == HOUSING_CATEGORY
                and str((r.get("land") or {}).get("id")) == NL_LAND_ID]
        codes = {tuple(sorted(d.get("code") for d in (r.get("doelgroepen") or [])))
                 for r in rows}
        assert len(codes) > 1, "fixture 里应当有不止一种 doelgroepen，否则这条空过"

        for r in rows:
            item = _parse_object(r)
            fm = dict(f.split(": ", 1) for f in item.features)
            only_student = {d.get("code") for d in (r.get("doelgroepen") or [])} == {"student"}
            assert (fm.get("Tenant") == "student only") is only_student

    def test_mixed_target_group_is_left_unlabelled(self, payload):
        """混合标记的房源不写 Tenant——让该维度对它们 fail-open。

        写成 student only 是替站点断言「只有学生能租」，写成别的又没有对应取值。
        不写才是诚实的：我们不知道。
        """
        row = next(r for r in payload["result"]
                   if (r["dwellingType"] or {}).get("categorie") == HOUSING_CATEGORY
                   and str((r.get("land") or {}).get("id")) == NL_LAND_ID
                   and {d.get("code") for d in (r.get("doelgroepen") or [])} != {"student"})
        fm = dict(f.split(": ", 1) for f in _parse_object(row).features)
        assert "Tenant" not in fm


class TestHelpers:
    @pytest.mark.parametrize("label,want", [
        ("Begane grond", "0"), ("1e verdieping", "1"), ("3e verdieping", "3"),
        ("9e verdieping", "9"), ("", ""), ("Zolder", ""), (None, ""),
    ])
    def test_floor_parsing(self, label, want):
        assert _floor(label) == want

    @pytest.mark.parametrize("value,want", [
        (867.75, "€867,75"), (653, "€653"), (653.0, "€653"), (1366.25, "€1366,25"),
    ])
    def test_euro_formatting(self, value, want):
        assert _fmt_euro(value) == want


class TestCompleteness:
    def test_html_instead_of_json_is_a_network_error(self, monkeypatch):
        """路由没了时站点返回整页 HTML 而不是 JSON（实测 /api/v1/* 的 404 是
        158 KB HTML）。那是「接口没了」，不是「没房源」，必须上抛。"""
        class _Resp:
            status_code = 200
            content = b"<html>" * 100

            def json(self):
                raise ValueError("not json")

        class _Session:
            def __enter__(self): return self
            def __exit__(self, *_e): return False
            def post(self, *_a, **_kw): return _Resp()

        import curl_cffi.requests as req
        monkeypatch.setattr(req, "Session", lambda **_kw: _Session())
        with pytest.raises(ScrapeNetworkError, match="不是 JSON"):
            PlazaScraper()._fetch()

    def test_proxy_failure_is_recognisable_as_one(self, monkeypatch):
        """代理故障包成 ScrapeNetworkError 并保住 __cause__。

        裸抛会掉进 dispatcher 的通用 except Exception，被记成「未预期异常」，
        本 source 就永远不参与代理冷却判定（2026-09-02 在 magis 与
        studentexperience 上实测过这个洞）。
        """
        from scrapers.base import is_proxy_error

        class _ProxyError(Exception):
            pass

        class _Session:
            def __enter__(self): return self
            def __exit__(self, *_e): return False
            def post(self, *_a, **_kw):
                raise _ProxyError("Failed to perform, curl: (56) CONNECT tunnel "
                                  "failed, response 402.")

        import curl_cffi.requests as req
        monkeypatch.setattr(req, "Session", lambda **_kw: _Session())
        with pytest.raises(ScrapeNetworkError) as ei:
            PlazaScraper()._fetch()
        assert is_proxy_error(ei.value)

    def test_missing_config_probe_marks_incomplete(self, payload):
        """``sAngularServiceData`` 里的门户配置有房没房都返回，是结构探针。

        读不到就说明拿到的不是一份真的接口响应（改版、网关、登录墙）。此时若把
        空 result 当成「没房源」，存量会被整体收敛成 Occupied 并发一批假下架通知。
        """
        broken = dict(payload)
        broken["sAngularServiceData"] = "[]"
        items, complete = PlazaScraper()._parse_all(broken)
        assert items == [] and complete is False

    def test_empty_result_with_a_healthy_probe_is_complete(self, payload):
        """探针在、result 为空 → 站点确实没在架房源，不该判不完整。

        先到先得那 31 条广告在首个回应后即关闭，短时间清空是可能的。
        """
        empty = dict(payload)
        empty["result"] = []
        items, complete = PlazaScraper()._parse_all(empty)
        assert items == [] and complete is True

    def test_result_of_wrong_type_marks_incomplete(self, payload):
        broken = dict(payload)
        broken["result"] = {"oops": 1}
        items, complete = PlazaScraper()._parse_all(broken)
        assert items == [] and complete is False

    def test_mostly_unparseable_marks_incomplete(self, payload):
        """过半荷兰住宅认不出 = 上游字段改了。

        此时「抓到几条」比「一条没抓到」更危险：它看起来像正常结果。
        """
        mangled = copy.deepcopy(payload)
        n = 0
        for r in mangled["result"]:
            if ((r["dwellingType"] or {}).get("categorie") == HOUSING_CATEGORY
                    and str((r.get("land") or {}).get("id")) == NL_LAND_ID):
                r["totalRent"] = None   # 关键字段
                n += 1
                if n > 25:
                    break
        items, complete = PlazaScraper()._parse_all(mangled)
        assert complete is False


class TestCitySplit:
    def test_dispatch_by_display_name(self, payload, monkeypatch):
        s = PlazaScraper()
        monkeypatch.setattr(s, "_fetch", lambda: payload)
        with s.batch_session():
            utr = s.scrape(ScrapeTask(source="plaza", city_key="x", city_display="Utrecht"))
            ein = s.scrape(ScrapeTask(source="plaza", city_key="y", city_display="Eindhoven"))
            bre = s.scrape(ScrapeTask(source="plaza", city_key="z", city_display="Breda"))
        assert len(utr.listings) == 32
        assert len(ein.listings) == 1
        assert bre.listings == []          # 已登记但当前无房源，不是错误
        assert all(r.complete for r in (utr, ein, bre))

    def test_one_fetch_serves_every_city(self, payload, monkeypatch):
        """整批只发一次 HTTP。

        分多次抓不但把请求频率乘上城市数，而且两次之间库存可能变化——先到先得那
        31 条在首个回应后即关闭，拼出来的快照会自相矛盾。
        """
        calls = []
        s = PlazaScraper()
        monkeypatch.setattr(s, "_fetch", lambda: (calls.append(1), payload)[1])
        with s.batch_session():
            for city in ("Utrecht", "Amsterdam", "Delft"):
                s.scrape(ScrapeTask(source="plaza", city_key=city.lower(), city_display=city))
        assert len(calls) == 1

    def test_unregistered_city_is_reported_not_silently_dropped(self, payload, monkeypatch, caplog):
        """站点上架新城市时，日志里必须看得见。

        清单是快照，一定会漂。未登记城市的房源不会分派给任何 task——那本身没办法
        避免，但**必须留下痕迹**，否则这些房源就是凭空消失。
        """
        mangled = copy.deepcopy(payload)
        for r in mangled["result"]:
            if (r.get("city") or {}).get("name") == "Utrecht":
                r["city"]["name"] = "Zwolle"
        assert "Zwolle" not in CITIES

        s = PlazaScraper()
        monkeypatch.setattr(s, "_fetch", lambda: mangled)
        with caplog.at_level("WARNING"):
            with s.batch_session():
                s.scrape(ScrapeTask(source="plaza", city_key="a", city_display="Amsterdam"))
        assert "Zwolle" in caplog.text


class TestRegistration:
    def test_source_is_registered_end_to_end(self):
        from config import KNOWN_SOURCES, SOURCE_DISPLAY_NAMES
        from scrapers import SCRAPER_REGISTRY
        assert "plaza" in KNOWN_SOURCES
        assert SOURCE_DISPLAY_NAMES["plaza"] == "Plaza"
        assert SCRAPER_REGISTRY["plaza"] is PlazaScraper

    def test_registered_cities_cover_what_the_feed_returns(self, parsed):
        """当前在架的城市必须都在 KNOWN_PLAZA_CITIES 里。

        这条不能防止将来漂移（feed 会变，fixture 不会），但能挡住「加了城市却忘了
        登记」这一半。真正的漂移由生产日志里的 WARNING 兜。
        """
        from config import KNOWN_PLAZA_CITIES
        registered = {c["name"] for c in KNOWN_PLAZA_CITIES}
        assert {x.city for x in parsed} <= registered

    def test_energy_is_not_registered(self):
        """energyLabel 在 fixture 里 53 条全是空对象，登记了只会 fail-closed 误杀。"""
        from config import sources_supporting_dim
        assert "plaza" not in sources_supporting_dim("energy")

    def test_tenant_is_not_a_site_wide_assumption(self):
        """Plaza 不进 SOURCE_ASSUMED_FEATURES——它的租客信息是逐条抓来的。"""
        from config import SOURCE_ASSUMED_FEATURES, sources_supporting_dim
        assert "plaza" not in SOURCE_ASSUMED_FEATURES
        assert "plaza" in sources_supporting_dim("tenant")

    def test_not_treated_as_full_lifecycle(self):
        """feed 只列在架房源，「消失」有歧义，走 Reserved → Occupied 两跳。"""
        import os
        from config import load_config
        os.environ["SOURCES"] = "plaza"
        try:
            assert "plaza" not in load_config().sources_with_full_lifecycle()
        finally:
            os.environ.pop("SOURCES", None)
