"""`ListingFilter.allowed_buildings` —— 「只要这栋楼里的任何一间」。

真实取值形态差别很大（2026-09-10 生产库实测，836 条里 835 条带 Building）：

    holland2stay   Laagstraat · Philips Bedrijfsschool · De Eendracht     楼盘名
    plaza          Limapad · Teteringsedijk 120 · Bogardeind 219          街道 / 街道+门牌
    magis          Novum · The Rumour                                     楼盘名
    studentexp     Amsterdam NDSM · Leiden                                城市+楼

所以匹配用子串，与其余白名单字段一致。
"""
import pytest

from config import ListingFilter, _UNIVERSAL_FILTER_DIMS, _source_supports_dim
from models import Listing


def _l(building=None, source="plaza", **kw):
    feats = [] if building is None else [f"Building: {building}"]
    base = dict(id="x", name="n", status="Available to book", price_raw="€ 900,00",
                available_from=None, features=feats, url="u", city="Eindhoven",
                source=source)
    base.update(kw)
    return Listing(**base)


class TestMatching:
    def test_the_whole_building_passes(self):
        """Plaza 的 Building 是从地址推的，Teteringsedijk 120 覆盖 B16/B22/B26——
        这正是「这栋楼里任何一间」的语义。"""
        f = ListingFilter(allowed_buildings=["Teteringsedijk 120"])
        for unit in ("Teteringsedijk 120", "Teteringsedijk 120"):
            assert f.passes(_l(unit))

    def test_another_building_is_rejected(self):
        f = ListingFilter(allowed_buildings=["Teteringsedijk 120"])
        assert not f.passes(_l("Bogardeind 219"))

    def test_matching_is_case_insensitive_substring(self):
        f = ListingFilter(allowed_buildings=["laagstraat"])
        assert f.passes(_l("Laagstraat", source="holland2stay"))

    def test_several_buildings_are_or_ed(self):
        f = ListingFilter(allowed_buildings=["Novum", "Laagstraat"])
        assert f.passes(_l("Novum", source="magis"))
        assert f.passes(_l("Laagstraat", source="holland2stay"))
        assert not f.passes(_l("De Eendracht", source="holland2stay"))


class TestFailClosed:
    def test_a_listing_without_a_building_is_rejected(self):
        """设了「只要这栋楼」，却拿到一条没有楼盘信息的房源——不能放行。

        放行的方向是错的：这个过滤器可能挂在 auto_book 上，fail-open 等于
        替用户自动申请一栋他没指定的楼。
        """
        f = ListingFilter(allowed_buildings=["Laagstraat"])
        assert not f.passes(_l(None))

    def test_an_unregistered_source_still_gets_filtered(self):
        """building 在通用维度里，所以将来的新平台**也**受它约束。

        这条钉的是**方向**。若改成逐平台登记，新平台会走 fail-open 整条跳过——
        用户勾了一栋楼，却收到新平台上所有楼的房源；挂在 auto_book 上就是
        自动申请一栋他明确排除掉的楼。
        """
        assert "building" in _UNIVERSAL_FILTER_DIMS
        assert _source_supports_dim("some_future_source", "building")
        f = ListingFilter(allowed_buildings=["Laagstraat"])
        assert not f.passes(_l("De Eendracht", source="some_future_source"))

    @pytest.mark.parametrize("source", [
        "holland2stay", "plaza", "xior", "ourdomain",
        "magis", "studentexperience", "ourcampus"])
    def test_every_current_source_supports_it(self, source):
        """七个平台都写 Building（生产实测 835/836），所以都该受约束。"""
        assert _source_supports_dim(source, "building")


class TestIntegration:
    def test_it_counts_toward_is_empty(self):
        """is_empty 是遍历 dataclass fields 的，新字段自动生效——钉住这个前提。"""
        assert ListingFilter().is_empty()
        assert not ListingFilter(allowed_buildings=["X"]).is_empty()

    def test_it_ands_with_the_other_conditions(self):
        f = ListingFilter(allowed_buildings=["Laagstraat"], max_rent=800.0)
        assert not f.passes(_l("Laagstraat", source="holland2stay",
                               price_raw="€ 1.200,00")), "楼对了但超预算，仍要拒"
        assert f.passes(_l("Laagstraat", source="holland2stay",
                           price_raw="€ 700,00"))

    def test_an_empty_list_does_not_filter(self):
        assert ListingFilter(allowed_buildings=[]).passes(_l(None))


class TestOptionsEndpoint:
    def test_api_buildings_returns_values_from_the_db(self, admin_client):
        r = admin_client.get("/api/buildings")
        assert r.status_code == 200
        assert "buildings" in r.get_json()

    def test_the_panel_renders_both_dropdowns(self, admin_client):
        html = admin_client.get("/users/new").get_data(as_text=True)
        assert 'name="ALLOWED_BUILDINGS"' in html or 'id="building-dropdown"' in html
        assert ('name="AUTO_BOOK_ALLOWED_BUILDINGS"' in html
                or 'id="ab-building-dropdown"' in html), \
            "自动预订那一侧也要有——「只抢这栋楼」正是它的用法"

    def test_it_round_trips_through_the_form(self, admin_client):
        admin_client.post("/users/new", data={
            "name": "bld-ui", "csrf_token": "test_csrf",
            "AUTO_BOOK_ALLOWED_BUILDINGS": "Teteringsedijk 120",
            "ALLOWED_BUILDINGS": "Laagstraat",
        }, headers={"X-CSRF-Token": "test_csrf"}, follow_redirects=True)

        from users import load_users
        u = next(x for x in load_users() if x.name == "bld-ui")
        assert u.listing_filter.allowed_buildings == ["Laagstraat"]
        assert u.auto_book.listing_filter.allowed_buildings == ["Teteringsedijk 120"]
