"""普通用户的账号页（account.html）：分组 / 条目 / 详情工作区。

锁三件事：
- 保存时整份配置都在表单里。build_user_from_form_self 按整份 POST 重建配置，
  缺一个字段就等于把它清空（NOTIFICATIONS_ENABLED 缺失还会被当成开启）。
- ?section=&item= 决定初始选中，未知值回落而不是报错。
- 保存后回到原来的条目。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


def _login(client, name="AcctUser"):
    from app.auth import _REGISTER_RECORDS
    _REGISTER_RECORDS.clear()
    r = client.post("/register", data={
        "csrf_token": "test_csrf", "register_username": name,
        "register_password": "pw1234", "terms_accepted": "1",
    })
    assert r.status_code in (302, 303)
    client.set_cookie("h2s-lang", "en")
    from users import load_users
    return next(u for u in load_users() if u.name == name)


@pytest.fixture
def account(client, test_app):
    with test_app.app_context():
        user = _login(client)
    return client, user


def test_every_field_the_self_parser_reads_is_in_the_form():
    parser = (Path(__file__).resolve().parent.parent / "app" / "forms" / "user_form.py").read_text(encoding="utf-8")
    body = parser[parser.index("def build_user_from_form_self"):]
    body = body[:body.index("\ndef ", 10)] if "\ndef " in body[10:] else body
    read = set(re.findall(r'(?:form\.get|_fv|_iv|_lv|_secret|_sanitize_energy)\(\s*"([A-Za-z_]+)"', body))
    read |= {"LANGUAGE"}
    tpl = (Path(__file__).resolve().parent.parent / "templates" / "account.html").read_text(encoding="utf-8")
    # 复选组经由 checks() 宏渲染，字段名作为参数出现
    missing = sorted(name for name in read if f'name="{name}"' not in tpl and f"'{name}'" not in tpl)
    assert not missing, f"账号页缺这些字段，保存一次就会被清空: {missing}"


def test_notifications_off_is_submitted_explicitly():
    """未勾选的复选框不提交，而解析器把缺失当作开启——必须有 false 兜底。"""
    tpl = (Path(__file__).resolve().parent.parent / "templates" / "account.html").read_text(encoding="utf-8")
    assert '<input type="hidden" name="NOTIFICATIONS_ENABLED" value="false">' in tpl


def test_selection_comes_from_the_query(account):
    client, user = account
    html = client.get(f"/users/{user.id}?section=notifications&item=telegram").get_data(as_text=True)
    panel = re.search(r'<div class="account-panel"[^>]*data-panel="notifications:telegram"[^>]*>', html).group(0)
    assert "hidden" not in panel
    assert re.search(r'data-account-item="notifications:telegram"[^>]*aria-current="true"', html)
    # 其他面板隐藏但仍在表单里，随保存一起提交
    other = re.search(r'<div class="account-panel"[^>]*data-panel="filters:rent"[^>]*>', html).group(0)
    assert "hidden" in other
    assert 'name="MAX_RENT"' in html


def test_unknown_selection_falls_back(account):
    client, user = account
    r = client.get(f"/users/{user.id}?section=nope&item=../x")
    assert r.status_code == 200
    assert 'name="section" value="notifications"' in r.get_data(as_text=True)  # 新用户收不到通知 → 先开通知分组


def test_save_returns_to_the_same_item(account):
    client, user = account
    r = client.post(f"/users/{user.id}", data={
        "csrf_token": "test_csrf", "section": "filters", "item": "rent",
        "enabled": "true", "NOTIFICATIONS_ENABLED": "false", "MAX_RENT": "1200",
    })
    assert r.status_code == 302
    assert r.headers["Location"].endswith(f"/users/{user.id}?section=filters&item=rent")
    from users import load_users
    saved = next(u for u in load_users() if u.id == user.id)
    assert saved.listing_filter.max_rent == 1200
    assert saved.notifications_enabled is False


def test_bad_return_target_is_ignored(account):
    client, user = account
    r = client.post(f"/users/{user.id}", data={
        "csrf_token": "test_csrf", "section": "https://evil.example", "item": "rent", "enabled": "true",
    })
    assert r.headers["Location"].endswith(f"/users/{user.id}")


def test_admin_still_gets_the_full_user_form(admin_client):
    html = admin_client.get("/users/new").get_data(as_text=True)
    assert "AUTO_BOOK_ENABLED" in html and "account-workspace" not in html


def test_saved_values_missing_from_options_survive(account, test_app):
    """城市暂时没有房源时不在选项里；不渲染出来的话，保存一次就把它清掉了。"""
    client, user = account
    from users import update_users, get_user

    def change(all_users):
        get_user(all_users, user.id).listing_filter.allowed_cities = ["Atlantis"]
    update_users(change)
    html = client.get(f"/users/{user.id}?section=filters&item=location").get_data(as_text=True)
    assert 'name="ALLOWED_CITIES" value="Atlantis" checked' in html
