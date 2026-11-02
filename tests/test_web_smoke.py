"""每个网页在三种身份 × 两种语言下都能打开，或者按规矩跳走。

只看状态码和「模板没把自己漏出来」，不看页面长什么样——布局交给浏览器检查。
它兜的是改模板时最常见的那种事故：某个分支引用了不存在的变量，页面直接 500，
而只有某种身份或某种语言才走得到那个分支。
"""
from __future__ import annotations

import pytest

BROWSE = ["/", "/calendar", "/map", "/stats"]
PUBLIC = ["/support", "/privacy", "/terms", "/donate", "/guide", "/verify-email/not-a-real-token-123456"]
ADMIN = ["/users", "/users/new", "/announcements", "/settings", "/settings/app-accounts",
         "/settings/app-accounts?tab=devices", "/monitoring", "/logs", "/crashes", "/system"]
LANGS = ["zh", "en"]


def _assert_clean(r, path):
    body = r.get_data(as_text=True)
    for leak in ("{{", "{%", "Traceback"):
        assert leak not in body, f"{path} 漏出了 {leak!r}"


@pytest.fixture
def user_client(test_app):
    """一个刚注册的普通用户。"""
    from app.auth import _REGISTER_RECORDS
    from users import load_users
    c = test_app.test_client()
    with c.session_transaction() as sess:
        sess["csrf_token"] = "test_csrf"
    _REGISTER_RECORDS.clear()
    r = c.post("/register", data={
        "csrf_token": "test_csrf", "register_username": "SmokeUser",
        "register_password": "pw1234", "terms_accepted": "1",
    })
    assert r.status_code in (302, 303)
    with c.session_transaction() as sess:
        sess["csrf_token"] = "test_csrf"
    c.user_id = next(u.id for u in load_users() if u.name == "SmokeUser")
    return c


def _browsers(client, user_client, admin_client):
    return {"anonymous": client, "user": user_client, "admin": admin_client}


@pytest.mark.parametrize("lang", LANGS)
def test_browse_pages_open_for_everyone(client, user_client, admin_client, lang):
    """收件箱一组：没登录的人以访客身份进来，所以三种身份都是 200。"""
    for who, c in _browsers(client, user_client, admin_client).items():
        c.set_cookie("h2s-lang", lang)
        for path in BROWSE:
            r = c.get(path)
            assert r.status_code == 200, f"{who} {lang} {path} → {r.status_code}"
            _assert_clean(r, path)


@pytest.mark.parametrize("lang", LANGS)
def test_public_pages_open_for_everyone(client, user_client, admin_client, lang):
    for who, c in _browsers(client, user_client, admin_client).items():
        c.set_cookie("h2s-lang", lang)
        for path in PUBLIC:
            r = c.get(path)
            assert r.status_code == 200, f"{who} {lang} {path} → {r.status_code}"
            _assert_clean(r, path)


@pytest.mark.parametrize("lang", LANGS)
def test_login_page(client, lang):
    client.set_cookie("h2s-lang", lang)
    r = client.get("/login")
    assert r.status_code == 200
    _assert_clean(r, "/login")


@pytest.mark.parametrize("lang", LANGS)
def test_admin_pages(client, user_client, admin_client, lang):
    for c in (client, user_client, admin_client):
        c.set_cookie("h2s-lang", lang)
    for path in ADMIN:
        r = admin_client.get(path)
        assert r.status_code == 200, f"admin {lang} {path} → {r.status_code}"
        _assert_clean(r, path)
        # 普通用户被送回首页，没登录的人被送去登录
        assert user_client.get(path).status_code == 302, f"user 打开了 {path}"
        r = client.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], f"anonymous 打开了 {path}"


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("section", ["", "?section=filters", "?section=notifications",
                                     "?section=account", "?section=preferences"])
def test_own_account_settings(user_client, lang, section):
    user_client.set_cookie("h2s-lang", lang)
    path = f"/users/{user_client.user_id}{section}"
    r = user_client.get(path)
    assert r.status_code == 200, f"{lang} {path} → {r.status_code}"
    _assert_clean(r, path)


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("section", ["basic", "notifications", "filters", "autobook", "profile"])
def test_admin_user_editor_sections(admin_client, user_client, lang, section):
    admin_client.set_cookie("h2s-lang", lang)
    path = f"/users/{user_client.user_id}?section={section}"
    r = admin_client.get(path)
    assert r.status_code == 200, f"{lang} {path} → {r.status_code}"
    _assert_clean(r, path)


def test_other_users_settings_are_off_limits(client, user_client, admin_client, test_app):
    """普通用户不能打开别人的账号页。"""
    from users import UserConfig, load_users, save_users
    save_users(load_users() + [UserConfig(name="SomeoneElse")])
    other = next(u.id for u in load_users() if u.name == "SomeoneElse")
    assert user_client.get(f"/users/{other}").status_code in (302, 403, 404)


@pytest.mark.parametrize("lang", LANGS)
def test_unknown_page_is_a_friendly_404(client, lang):
    client.set_cookie("h2s-lang", lang)
    r = client.get("/definitely-not-a-page")
    assert r.status_code == 404
    assert r.mimetype == "text/html"
    _assert_clean(r, "404")


def test_unknown_api_does_not_get_the_styled_page(admin_client):
    """/api/ 下保留原始响应（app/error_pages.py）：App 和脚本不该拿到一整页 HTML 外壳。"""
    r = admin_client.get("/api/definitely-not-an-endpoint")
    assert r.status_code == 404
    assert "public.css" not in r.get_data(as_text=True)
