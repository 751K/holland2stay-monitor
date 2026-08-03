"""Xior 按楼栋凭据测试。

2026-08-03 实测发现 **Xior 是一栋楼一个账号**：每栋楼是独立的 RENTCafe
property 门户，各有自己的 host、property 代码和 myOlePropertyId，登录页原话
是「your <楼栋名> Guest Account」。cookie 不跨主机，账号也不互通。

原来的单对 ``xior_email`` / ``xior_password`` 建立在「Xior 一个账号」这个错误
认知上，对不上真实结构。

这里守的最关键一条：**找不到该楼凭据时绝不回退到别楼的**。拿 A 楼账号去 B 楼
门户登录必然失败，而失败会计入 RENTCafe 的 IP 级尝试限制（连续失败锁 30 分钟）
——等于用一次注定失败的请求去消耗真正需要它的额度。
"""
from __future__ import annotations

import json

import pytest

from config import AutoBookConfig
from users import UserConfig, _ab_from_dict, _user_to_row


def _cfg(**kw) -> AutoBookConfig:
    return AutoBookConfig(**kw)


class TestLookup:
    def test_returns_the_matching_building(self):
        ab = _cfg(xior_accounts={"p1": {"email": "a@x.com", "password": "pw1"}})
        assert ab.xior_account_for("p1") == ("a@x.com", "pw1")

    def test_never_falls_back_to_another_building(self):
        """核心契约：拿 A 楼账号去 B 楼登录必然失败，还会烧掉 IP 尝试额度。"""
        ab = _cfg(xior_accounts={"p1": {"email": "a@x.com", "password": "pw1"}})
        assert ab.xior_account_for("p2") == ("", "")

    def test_unknown_building_is_empty(self):
        assert _cfg().xior_account_for("p1") == ("", "")

    def test_key_is_whitespace_tolerant(self):
        ab = _cfg(xior_accounts={"p1": {"email": "a@x.com", "password": "pw1"}})
        assert ab.xior_account_for("  p1  ") == ("a@x.com", "pw1")

    def test_empty_key(self):
        ab = _cfg(xior_accounts={"p1": {"email": "a@x.com", "password": "pw1"}})
        assert ab.xior_account_for("") == ("", "")


class TestLegacyFallback:
    def test_legacy_pair_used_when_no_per_building_config(self):
        """存量用户是在「Xior 一个账号」的认知下填的，不能直接作废。"""
        ab = _cfg(xior_email="old@x.com", xior_password="pw0")
        assert ab.xior_account_for("p1") == ("old@x.com", "pw0")

    def test_legacy_ignored_once_per_building_config_exists(self):
        """那对存量值只可能对某一栋楼有效，无法判断是哪栋。
        用户一旦开始按楼配置，就该完全以按楼配置为准。"""
        ab = _cfg(
            xior_email="old@x.com", xior_password="pw0",
            xior_accounts={"p1": {"email": "a@x.com", "password": "pw1"}},
        )
        assert ab.xior_account_for("p1") == ("a@x.com", "pw1")
        assert ab.xior_account_for("p2") == ("", ""), "不能再退回存量值"


class TestConfiguredBuildings:
    def test_lists_only_fully_configured(self):
        ab = _cfg(xior_accounts={
            "p1": {"email": "a@x.com", "password": "pw1"},
            "p2": {"email": "b@x.com", "password": ""},     # 缺密码
            "p3": {"email": "", "password": "pw3"},          # 缺邮箱
        })
        assert ab.xior_buildings() == ["p1"]

    def test_empty(self):
        assert _cfg().xior_buildings() == []

    def test_sorted(self):
        ab = _cfg(xior_accounts={
            k: {"email": "a@x.com", "password": "p"} for k in ("p9", "p1", "p5")
        })
        assert ab.xior_buildings() == ["p1", "p5", "p9"]


class TestPersistence:
    def test_passwords_are_encrypted_at_rest(self):
        u = UserConfig(id="u1", name="T", auto_book=_cfg(xior_accounts={
            "p1": {"email": "a@x.com", "password": "pw-secret"},
        }))
        blob = _user_to_row(u)["auto_book_json"]
        assert "pw-secret" not in blob, "密码不能明文落库"
        assert "a@x.com" in blob, "邮箱不加密（与其他平台字段一致）"

    def test_round_trip(self):
        u = UserConfig(id="u1", name="T", auto_book=_cfg(xior_accounts={
            "p1": {"email": "a@x.com", "password": "pw1"},
            "p2": {"email": "b@x.com", "password": "pw2"},
        }))
        back = _ab_from_dict(json.loads(_user_to_row(u)["auto_book_json"]))
        assert back.xior_account_for("p1") == ("a@x.com", "pw1")
        assert back.xior_account_for("p2") == ("b@x.com", "pw2")
        assert back.xior_buildings() == ["p1", "p2"]

    @pytest.mark.parametrize("bad", [
        "notadict", None, 123, [],
        {"k": "notadict"},
        {"k": {"email": ""}},          # 邮箱为空 → 丢弃
        {"k": None},
    ])
    def test_malformed_entries_are_dropped_not_fatal(self, bad):
        """一个坏条目不该让整个用户配置加载失败——那会连带停掉该用户全部通知。"""
        ab = _ab_from_dict({"xior_accounts": bad})
        assert isinstance(ab.xior_accounts, dict)

    def test_partial_entry_kept_but_not_listed_as_configured(self):
        ab = _ab_from_dict({"xior_accounts": {"k": {"email": "e@x.com", "password": ""}}})
        assert ab.xior_account_for("k") == ("e@x.com", "")
        assert ab.xior_buildings() == [], "缺密码不算配好"


class TestBuildingKeysMatchScraper:
    def test_keys_align_with_scraper_registry(self):
        """凭据的 key 必须和 XIOR_CITIES / BUILDINGS 用同一套，否则查不到。"""
        from scrapers.xior import XiorScraper

        ab = _cfg(xior_accounts={
            k: {"email": "a@x.com", "password": "p"}
            for k in list(XiorScraper.BUILDINGS)[:2]
        })
        for k in ab.xior_buildings():
            assert k in XiorScraper.BUILDINGS


# ── 候选闸门 ────────────────────────────────────────────────────────


class _Notifier:
    has_channels = True


def _user(**ab_kw):
    from users import UserConfig
    return UserConfig(id="u1", name="U", auto_book=AutoBookConfig(enabled=True, **ab_kw))


def _listing(source, city="Amsterdam Karspeldreef"):
    from models import Listing
    return Listing(
        id="x1", name="L", status="Available to book", price_raw="€500",
        available_from="2030-01-01", features=[], url="https://e.test/1",
        city=city, source=source,
    )


class TestBuildingKeyLookup:
    def test_resolves_by_display_name(self):
        from scrapers.xior import building_key_for
        assert building_key_for(_listing("xior", "Amsterdam Karspeldreef")) == "p0196062"

    def test_unknown_display_returns_empty(self):
        from scrapers.xior import building_key_for
        assert building_key_for(_listing("xior", "Nowhere Street")) == ""

    def test_empty_city_returns_empty(self):
        from scrapers.xior import building_key_for
        assert building_key_for(_listing("xior", "")) == ""


class TestAutoBookGate:
    def test_h2s_still_allowed(self):
        from monitor import _can_auto_book
        assert _can_auto_book(_user(), _listing("holland2stay")) is True

    def test_xior_is_gated_off_until_flow_is_implemented(self):
        """预订流程还是没走过流程硬猜的草稿，放开等于拿真实账号提交半懂的表单。"""
        from monitor import _AUTO_BOOK_SOURCES, _can_auto_book
        assert "xior" not in _AUTO_BOOK_SOURCES
        u = _user(xior_accounts={"p0196062": {"email": "a@x.com", "password": "p"}})
        assert _can_auto_book(u, _listing("xior")) is False

    def test_credential_check_works_once_source_is_enabled(self, monkeypatch):
        """把闸门打开后，凭据判定必须按楼栋生效。"""
        import monitor
        monkeypatch.setattr(monitor, "_AUTO_BOOK_SOURCES", ("holland2stay", "xior"))

        has = _user(xior_accounts={"p0196062": {"email": "a@x.com", "password": "p"}})
        assert monitor._can_auto_book(has, _listing("xior", "Amsterdam Karspeldreef")) is True
        # 另一栋楼没配 → 不产生候选，且**不回退**到已配那栋的凭据
        assert monitor._can_auto_book(has, _listing("xior", "Amsterdam Naritaweg")) is False
        # 完全没配
        assert monitor._can_auto_book(_user(), _listing("xior")) is False
        # 楼栋反查不出来 → 跳过而不是猜
        assert monitor._can_auto_book(has, _listing("xior", "Nowhere Street")) is False

    def test_unknown_source_is_rejected(self):
        from monitor import _can_auto_book
        assert _can_auto_book(_user(), _listing("ourcampus")) is False


# ── 表单解析 ────────────────────────────────────────────────────────


def _form(**kw):
    from werkzeug.datastructures import ImmutableMultiDict
    items = []
    for k, v in kw.items():
        for x in (v if isinstance(v, list) else [v]):
            items.append((k, x))
    return ImmutableMultiDict(items)


class TestFormParsing:
    def test_parses_parallel_arrays(self):
        from app.forms.user_form import parse_xior_accounts
        f = _form(
            AUTO_BOOK_XIOR_KEY=["p1", "p2"],
            AUTO_BOOK_XIOR_EMAIL=["a@x.com", "b@x.com"],
            AUTO_BOOK_XIOR_PW=["pw1", "pw2"],
        )
        assert parse_xior_accounts(f) == {
            "p1": {"email": "a@x.com", "password": "pw1"},
            "p2": {"email": "b@x.com", "password": "pw2"},
        }

    def test_blank_password_keeps_existing(self):
        """密码不回填到 HTML，空提交只能理解为「不改」——否则用户每次编辑
        别的字段都会把密码清空。"""
        from app.forms.user_form import parse_xior_accounts
        ex = AutoBookConfig(xior_accounts={"p1": {"email": "a@x.com", "password": "old"}})
        f = _form(AUTO_BOOK_XIOR_KEY="p1", AUTO_BOOK_XIOR_EMAIL="a@x.com",
                  AUTO_BOOK_XIOR_PW="")
        assert parse_xior_accounts(f, ex)["p1"]["password"] == "old"

    def test_new_password_overrides(self):
        from app.forms.user_form import parse_xior_accounts
        ex = AutoBookConfig(xior_accounts={"p1": {"email": "a@x.com", "password": "old"}})
        f = _form(AUTO_BOOK_XIOR_KEY="p1", AUTO_BOOK_XIOR_EMAIL="a@x.com",
                  AUTO_BOOK_XIOR_PW="new")
        assert parse_xior_accounts(f, ex)["p1"]["password"] == "new"

    def test_blank_email_deletes_the_building(self):
        from app.forms.user_form import parse_xior_accounts
        ex = AutoBookConfig(xior_accounts={"p1": {"email": "a@x.com", "password": "old"}})
        f = _form(AUTO_BOOK_XIOR_KEY="p1", AUTO_BOOK_XIOR_EMAIL="", AUTO_BOOK_XIOR_PW="")
        assert parse_xior_accounts(f, ex) == {}

    def test_removed_row_is_dropped(self):
        """前端删掉的行不再提交 → 结果里就没有它。"""
        from app.forms.user_form import parse_xior_accounts
        ex = AutoBookConfig(xior_accounts={
            "p1": {"email": "a@x.com", "password": "pw1"},
            "p2": {"email": "b@x.com", "password": "pw2"},
        })
        f = _form(AUTO_BOOK_XIOR_KEY="p1", AUTO_BOOK_XIOR_EMAIL="a@x.com",
                  AUTO_BOOK_XIOR_PW="")
        out = parse_xior_accounts(f, ex)
        assert set(out) == {"p1"}

    def test_empty_form(self):
        from app.forms.user_form import parse_xior_accounts
        assert parse_xior_accounts(_form()) == {}

    def test_ragged_arrays_do_not_crash(self):
        """浏览器理论上不会发出参差数组，但坏掉的客户端会。"""
        from app.forms.user_form import parse_xior_accounts
        f = _form(AUTO_BOOK_XIOR_KEY=["p1", "p2"], AUTO_BOOK_XIOR_EMAIL=["a@x.com"])
        assert parse_xior_accounts(f) == {"p1": {"email": "a@x.com", "password": ""}}

    def test_whitespace_only_key_skipped(self):
        from app.forms.user_form import parse_xior_accounts
        f = _form(AUTO_BOOK_XIOR_KEY="   ", AUTO_BOOK_XIOR_EMAIL="a@x.com",
                  AUTO_BOOK_XIOR_PW="p")
        assert parse_xior_accounts(f) == {}


class TestFormIntegration:
    def test_build_user_keeps_legacy_pair(self):
        """表单已不再提供单对入口；直接清空会让还没按楼重配的老用户失去凭据。"""
        from app.forms.user_form import build_user_from_form
        from users import UserConfig
        ex = UserConfig(id="u1", name="U", auto_book=AutoBookConfig(
            xior_email="old@x.com", xior_password="oldpw"))
        f = _form(NAME="U", AUTO_BOOK_XIOR_KEY="p1",
                  AUTO_BOOK_XIOR_EMAIL="a@x.com", AUTO_BOOK_XIOR_PW="pw1")
        u = build_user_from_form(f, user_id="u1", existing=ex)
        assert u.auto_book.xior_email == "old@x.com"
        assert u.auto_book.xior_accounts["p1"]["email"] == "a@x.com"
        # 有按楼配置后，别的楼不再回退到存量值
        assert u.auto_book.xior_account_for("p2") == ("", "")


class TestFormRendering:
    """保存后必须能回填——否则用户每次编辑都得重填一遍所有楼栋。"""

    def _post(self, admin_client, **extra):
        data = {"name": "xior-ui", "csrf_token": "test_csrf", **extra}
        return admin_client.post("/users/new", data=data,
                                 headers={"X-CSRF-Token": "test_csrf"},
                                 follow_redirects=True)

    def test_saved_accounts_render_as_rows(self, admin_client, monkeypatch):
        import app.routes.users as ur
        monkeypatch.setattr(ur, "_xior_building_options", lambda: [
            {"key": "p0196062", "display": "Amsterdam Karspeldreef"},
            {"key": "p0195855", "display": "Eindhoven Zernikestraat"},
        ])
        r = self._post(
            admin_client,
            AUTO_BOOK_XIOR_KEY="p0196062",
            AUTO_BOOK_XIOR_EMAIL="a@x.com",
            AUTO_BOOK_XIOR_PW="pw1",
        )
        assert r.status_code == 200

        from users import load_users
        u = next((x for x in load_users() if x.name == "xior-ui"), None)
        assert u is not None, "用户未创建"
        assert u.auto_book.xior_accounts == {
            "p0196062": {"email": "a@x.com", "password": "pw1"}
        }

        page = admin_client.get(f"/users/{u.id}").get_data(as_text=True)
        assert "a@x.com" in page, "已保存的邮箱应回填到行里"
        assert 'value="p0196062"' in page, "楼栋 key 应作为隐藏字段回填"
        assert "pw1" not in page, "密码绝不能回填到 HTML"
        # 已配的楼不该再出现在「添加」下拉里
        add_block = page.split('id="xior-add-select"', 1)[1].split("</select>", 1)[0]
        assert "p0196062" not in add_block
        assert "p0195855" in add_block, "未配置的楼仍应可选"

    def test_no_monitored_buildings_shows_hint(self, admin_client, monkeypatch):
        import app.routes.users as ur
        monkeypatch.setattr(ur, "_xior_building_options", lambda: [])
        page = admin_client.get("/users/new").get_data(as_text=True)
        assert "XIOR_CITIES" in page, "没有监控楼栋时应提示去配 XIOR_CITIES"
