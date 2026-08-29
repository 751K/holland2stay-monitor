"""
用户配置页新增「通知语言」。

``UserConfig.language`` 一直存在，房源通知与验证邮件都按它出文案，但**面板上
没有任何入口**——2026-08-29 线上 62 个用户全部是 ``en``，不是因为大家都选了
英文，是因为没人能改。App 上报的 ``device_tokens.language`` 是每设备一份的
另一个字段，管的是推送，管不到邮件。

这批用例盯两件事：改得动，以及**没提交这个字段时不会把已有值冲掉**。后者是
这类「新增字段」最常见的回归：旧版页面或 API 提交的表单不带 LANGUAGE，一个
``form.get("LANGUAGE", "en")`` 就会把所有人悄悄改回英文。
"""
from __future__ import annotations

import re

import pytest

from app.forms.user_form import _language


BASE = {
    "csrf_token": "test_csrf",
    "enabled": "true",
    "NOTIFICATIONS_ENABLED": "true",
    "NOTIFICATION_CHANNELS": "",
}


def _post_new(admin_client, name, **extra):
    data = dict(BASE, name=name)
    data.update(extra)
    return admin_client.post("/users/new", data=data, follow_redirects=False)


def _user(name):
    from users import load_users
    return next(u for u in load_users() if u.name == name)


class _Existing:
    def __init__(self, language):
        self.language = language


# ── 取值规则 ────────────────────────────────────────────────────

class TestLanguageField:
    @pytest.mark.parametrize("raw,want", [
        ("zh", "zh"), ("en", "en"),
        ("ZH", "zh"), ("zh-CN", "zh"), ("en_US", "en"),
    ])
    def test_accepted_values(self, raw, want):
        assert _language({"LANGUAGE": raw}, _Existing("en")) == want

    def test_missing_field_keeps_the_existing_value(self):
        """表单不带这个字段时沿用旧值，而不是回落到 en。

        回落的表现是：用户选了中文，之后任何一次不带该字段的保存（旧页面、
        API、脚本）都会把它悄悄改回英文，而且没有任何地方会报错。
        """
        assert _language({}, _Existing("zh")) == "zh"

    @pytest.mark.parametrize("raw", ["", "  ", "fr", "de", "xx", "123"])
    def test_unknown_value_keeps_the_existing_value(self, raw):
        assert _language({"LANGUAGE": raw}, _Existing("zh")) == "zh"

    def test_no_existing_falls_back_to_english(self):
        """新建用户没有 existing，此时按 UserConfig 的默认值。"""
        assert _language({}, None) == "en"
        assert _language({"LANGUAGE": "fr"}, None) == "en"


# ── 两个构造器都要接上 ──────────────────────────────────────────

class TestBothBuilders:
    def test_admin_builder(self):
        import app.forms.user_form as m
        import inspect

        src = inspect.getsource(m.build_user_from_form)
        assert "language=_language(" in src

    def test_self_builder(self):
        """自助模式是白名单——不显式列出就改不动，而这是本人的偏好。"""
        import app.forms.user_form as m
        import inspect

        src = inspect.getsource(m.build_user_from_form_self)
        assert "language=_language(" in src


# ── 端到端 ──────────────────────────────────────────────────────

class TestRoundTrip:
    def test_new_user_with_chinese(self, admin_client):
        _post_new(admin_client, "LangZh", LANGUAGE="zh")
        assert _user("LangZh").language == "zh"

    def test_new_user_defaults_to_english(self, admin_client):
        _post_new(admin_client, "LangDefault")
        assert _user("LangDefault").language == "en"

    def test_editing_without_the_field_does_not_reset(self, admin_client):
        """这条是整批里最要紧的：编辑一次不该把语言冲回英文。"""
        _post_new(admin_client, "LangKeep", LANGUAGE="zh")
        uid = _user("LangKeep").id

        data = dict(BASE, name="LangKeep")          # 故意不带 LANGUAGE
        admin_client.post(f"/users/{uid}", data=data, follow_redirects=False)

        assert _user("LangKeep").language == "zh", "编辑之后语言被改回英文了"

    def test_can_switch_back(self, admin_client):
        _post_new(admin_client, "LangSwitch", LANGUAGE="zh")
        uid = _user("LangSwitch").id
        admin_client.post(f"/users/{uid}", data=dict(BASE, name="LangSwitch",
                                                     LANGUAGE="en"))
        assert _user("LangSwitch").language == "en"


# ── 页面 ────────────────────────────────────────────────────────

class TestForm:
    def test_select_is_rendered(self, admin_client):
        html = admin_client.get("/users/new").get_data(as_text=True)
        assert 'name="LANGUAGE"' in html
        assert 'value="zh"' in html and 'value="en"' in html

    def test_current_value_is_selected(self, admin_client):
        _post_new(admin_client, "LangShown", LANGUAGE="zh")
        uid = _user("LangShown").id
        html = admin_client.get(f"/users/{uid}").get_data(as_text=True)
        sel = re.search(r'name="LANGUAGE".*?</select>', html, re.S).group(0)
        assert re.search(r'value="zh"[^>]*selected', sel), "中文没有被选中"
        assert not re.search(r'value="en"[^>]*selected', sel)

    def test_label_is_just_language(self):
        from translations import TRANSLATIONS

        assert TRANSLATIONS["user_form_language"]["zh"] == "语言"
        assert TRANSLATIONS["user_form_language"]["en"] == "Language"
        assert "user_form_language_hint" not in TRANSLATIONS, "说明那行没删干净"

    def test_options_are_endonyms(self, admin_client):
        """语言名一律用它自己的语言写。

        英文界面上写 "Chinese" 的话，只看得懂中文的人反而找不到自己那一项。
        """
        for ui in ("zh", "en"):
            html = admin_client.get(f"/users/new?lang={ui}").get_data(as_text=True)
            sel = re.search(r'name="LANGUAGE".*?</select>', html, re.S).group(0)
            assert "English" in sel and "中文" in sel, ui
            assert "Chinese" not in sel

    def test_reaches_the_self_service_view(self, client, admin_client):
        """自助视图用的是同一个模板，这里确认那条分支也画得出来。"""
        from app.auth import _REGISTER_RECORDS
        _REGISTER_RECORDS.clear()
        client.post("/register", data={
            "csrf_token": "test_csrf", "register_username": "LangSelf",
            "register_password": "pw1234", "terms_accepted": "1",
        })
        uid = _user("LangSelf").id
        html = client.get(f"/users/{uid}").get_data(as_text=True)
        assert 'name="LANGUAGE"' in html


# ── 写入通道 ────────────────────────────────────────────────────

class TestUpdateSetCoversEveryColumn:
    """``ON CONFLICT DO UPDATE SET`` 原先是逐列手写的，加列时漏掉了 ``language``。

    症状极安静：新建用户选中文是对的（走 INSERT），编辑时改成英文、点保存、
    提示「已保存」，值纹丝不动——因为 UPDATE 分支根本没有这一列。加「通知语言」
    入口时正好撞上，否则这个字段做出来也是死的。

    判据改成从 ``USER_CONFIG_COLUMNS`` 推导，这一类漏写从此不可能发生。
    """

    def test_every_column_except_the_key_is_updated(self):
        from mstorage._user_configs import USER_CONFIG_COLUMNS, _UPDATE_SET

        for col in USER_CONFIG_COLUMNS:
            if col in ("id", "created_at"):
                continue
            assert f"{col}=excluded.{col}" in _UPDATE_SET, f"{col} 更新时会被跳过"

    def test_key_and_created_at_are_excluded(self):
        """``id`` 是冲突键；``created_at`` 必须保留首次写入的值。"""
        from mstorage._user_configs import _UPDATE_SET

        assert "id=excluded.id" not in _UPDATE_SET
        assert "created_at=excluded.created_at" not in _UPDATE_SET

    def test_set_clause_is_derived_not_hand_written(self):
        import inspect

        import mstorage._user_configs as m

        src = inspect.getsource(m)
        assert "for c in USER_CONFIG_COLUMNS" in src, "SET 子句又改回手写了"

    def test_language_survives_an_update_at_the_storage_layer(self, temp_db):
        """端到端那条走的是路由；这条把同一件事钉在存储层，不依赖表单。"""
        base = {c: "" for c in
                __import__("mstorage._user_configs", fromlist=["x"]).USER_CONFIG_COLUMNS}
        base.update(id="u1", name="U", enabled=1, notifications_enabled=1,
                    email_smtp_port=587, email_verified=0, app_login_enabled=0,
                    allow_h2s_login=0, sort_order=0, language="zh")
        temp_db.replace_user_config_rows(list([base]))
        assert temp_db.list_user_config_rows()[0]["language"] == "zh"

        base["language"] = "en"
        temp_db.replace_user_config_rows(list([base]))
        assert temp_db.list_user_config_rows()[0]["language"] == "en", \
            "更新时 language 被跳过了"


# ── 界面语言跟着走 ──────────────────────────────────────────────

class TestInterfaceFollows:
    """保存自己的账号时界面语言跟着表单走。

    ``UserConfig.language`` 原本只管发出去的文案，界面语言是 ``h2s-lang``
    cookie，两者各走各的——用户在自己的页面上把语言改成英文、保存，界面却仍是
    中文，看起来像没生效。
    """

    def _register(self, client, name="LangSelf"):
        from app.auth import _REGISTER_RECORDS
        _REGISTER_RECORDS.clear()
        with client.session_transaction() as sess:
            sess.clear()
            sess["csrf_token"] = "test_csrf"
        client.post("/register", data={
            "csrf_token": "test_csrf", "register_username": name,
            "register_password": "pw1234", "terms_accepted": "1",
        })
        return _user(name).id

    def _lang_cookie(self, resp):
        for h in resp.headers.getlist("Set-Cookie"):
            if h.startswith("h2s-lang="):
                return h.split("=", 1)[1].split(";", 1)[0]
        return None

    def test_saving_my_own_switches_the_interface(self, client):
        uid = self._register(client)
        r = client.post(f"/users/{uid}", data=dict(BASE, LANGUAGE="zh"),
                        follow_redirects=False)
        assert self._lang_cookie(r) == "zh", "界面语言没跟着切"

    def test_and_back_again(self, client):
        uid = self._register(client)
        client.post(f"/users/{uid}", data=dict(BASE, LANGUAGE="zh"))
        r = client.post(f"/users/{uid}", data=dict(BASE, LANGUAGE="en"),
                        follow_redirects=False)
        assert self._lang_cookie(r) == "en"

    def test_the_page_really_comes_back_in_that_language(self, client):
        """判据落在渲染出来的页面上，而不只是「cookie 写了」。"""
        uid = self._register(client)
        r = client.post(f"/users/{uid}", data=dict(BASE, LANGUAGE="zh"),
                        follow_redirects=True)
        assert '<html lang="zh"' in r.get_data(as_text=True)

        r = client.post(f"/users/{uid}", data=dict(BASE, LANGUAGE="en"),
                        follow_redirects=True)
        assert '<html lang="en"' in r.get_data(as_text=True)

    def test_admin_editing_someone_else_keeps_their_own_interface(
            self, client, admin_client):
        """别人的偏好不是 admin 的偏好。

        admin 改一个中文用户的语言，自己的界面不该跟着变成中文。
        """
        uid = self._register(client, "SomeoneElse")
        r = admin_client.post(f"/users/{uid}", data=dict(BASE, name="SomeoneElse",
                                                         LANGUAGE="zh"),
                              follow_redirects=False)
        assert self._lang_cookie(r) is None, "admin 的界面语言被别人的设置改掉了"
        assert _user("SomeoneElse").language == "zh", "但那个人的语言要真的存下来"

    def test_cookie_follows_even_when_the_field_was_absent(self, client):
        """表单不带该字段时沿用旧值，cookie 也写这个旧值。

        侧栏那个语言开关是一次性的浏览覆盖，账号里这个才是长期设置——保存自己的
        账号时以账号里的为准。两者不一致地留着才是坏的：界面中文、发出去的邮件
        英文，而用户以为自己已经全设成中文了。
        """
        uid = self._register(client)
        r = client.post(f"/users/{uid}", data=dict(BASE), follow_redirects=False)
        assert self._lang_cookie(r) == "en", "沿用的旧值仍应写回，保持两者一致"


def test_cookie_helper_rejects_junk(test_app):
    """``_own_language_cookie`` 自己校验取值，不假设调用方已经校验过。

    今天它的入参只来自 ``_language()``（已白名单），所以这道检查删掉行为不变。
    但它写的是 ``h2s-lang``——``get_lang()`` 直接拿它去决定整站语言，一个脏值
    会让所有页面回落到协商结果，而没有任何地方会报错。判据放在写入点上，
    而不是靠「上游应该已经清洗过」。
    """
    from flask import redirect

    from app.routes.users import _own_language_cookie

    with test_app.test_request_context("/"):
        from flask import session
        session["role"] = "user"
        session["user_id"] = "u1"
        session["authenticated"] = True

        good = _own_language_cookie(redirect("/"), "u1", "zh")
        assert any(h.startswith("h2s-lang=zh")
                   for h in good.headers.getlist("Set-Cookie"))

        for junk in ("fr", "", "zh-CN", "'; DROP", "en-US"):
            resp = _own_language_cookie(redirect("/"), "u1", junk)
            assert not any(h.startswith("h2s-lang")
                           for h in resp.headers.getlist("Set-Cookie")), junk
