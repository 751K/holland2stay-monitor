"""官方部署 / 自部署的判定与页面表现（app/deployment.py、templates/_self_hosted.html）。

conftest 默认把所有用例渲染成官方站；这里显式切回真实判定或自部署。
"""
from __future__ import annotations

import pytest
from flask import Flask

import app.deployment as deployment

# conftest 的 autouse fixture 会替换模块属性，先留住真实实现
_REAL = deployment.is_official_site


@pytest.mark.parametrize("host,official", [
    ("flatradar.app", True),
    ("www.flatradar.app", True),
    ("flatradar.app:443", True),
    ("localhost:8088", False),
    ("flatradar.example.com", False),
    ("[::1]:8088", False),
])
def test_host_decides(host, official, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    with Flask("probe").test_request_context("/", base_url=f"https://{host}"):
        assert _REAL() is official


def test_public_base_url_covers_a_rewritten_host(monkeypatch):
    """反代把 Host 改成内部地址时，靠 PUBLIC_BASE_URL 认出官方站。"""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://flatradar.app")
    with Flask("probe").test_request_context("/", base_url="http://127.0.0.1:8088"):
        assert _REAL() is True


def test_someone_elses_base_url_is_not_official(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://flats.example.org")
    with Flask("probe").test_request_context("/", base_url="https://flats.example.org"):
        assert _REAL() is False


@pytest.fixture
def self_hosted(monkeypatch):
    monkeypatch.setattr(deployment, "is_official_site", lambda: False)


def test_official_site_has_no_label_or_notice(admin_client):
    admin_client.set_cookie("h2s-lang", "en")
    html = admin_client.get("/").get_data(as_text=True)
    assert "Self-hosted" not in html
    assert 'id="selfhost-dialog"' not in html


@pytest.mark.parametrize("path", ["/", "/login", "/support"])
def test_self_hosted_label_in_title_and_brand(client, self_hosted, path):
    client.set_cookie("h2s-lang", "en")
    html = client.get(path).get_data(as_text=True)
    title = html[html.index("<title>"):html.index("</title>")]
    assert "Self-hosted" in title
    assert '<small class="brand-selfhost">Self-hosted</small>' in html


def test_notice_is_only_for_the_admin(client, admin_client, self_hosted):
    """免责与禁止商用的提示是给部署者看的，普通访客只看到品牌名后的小字。"""
    assert 'id="selfhost-dialog"' not in client.get("/").get_data(as_text=True)
    admin_client.set_cookie("h2s-lang", "zh")
    html = admin_client.get("/").get_data(as_text=True)
    assert 'id="selfhost-dialog"' in html
    assert "不允许商用" in html and "不对使用本站产生的任何后果承担责任" in html


# ── 自部署不替作者收钱，也不引去装连不上的 App ─────────────────────────


def test_official_site_offers_donations(client):
    client.set_cookie("h2s-lang", "en")
    assert client.get("/donate").status_code == 200
    for path in ("/support", "/", "/stats"):
        assert 'href="/donate"' in client.get(path).get_data(as_text=True), path
    assert "/donate" in client.get("/sitemap.xml").get_data(as_text=True)


def test_self_hosted_hides_donations(client, self_hosted):
    """收款码和 Sponsors 是作者本人的：别人部署的站点不显示入口，直接访问也是 404。"""
    client.set_cookie("h2s-lang", "en")
    assert client.get("/donate").status_code == 404
    for path in ("/support", "/login", "/", "/stats"):
        assert 'href="/donate"' not in client.get(path).get_data(as_text=True), path
    assert "/donate" not in client.get("/sitemap.xml").get_data(as_text=True)
    assert "/donate" not in client.get("/llms.txt").get_data(as_text=True)


def _register(client, name):
    from app.auth import _REGISTER_RECORDS
    from users import load_users
    _REGISTER_RECORDS.clear()
    r = client.post("/register", data={
        "csrf_token": "test_csrf", "register_username": name,
        "register_password": "pw1234", "terms_accepted": "1",
    })
    assert r.status_code in (302, 303)
    return next(u.id for u in load_users() if u.name == name)


@pytest.mark.parametrize("official", [True, False])
def test_app_push_only_on_the_official_site(client, admin_client, monkeypatch, official):
    """官方 App 只连 flatradar.app，推送要作者的密钥：自部署站点上这一条既装不上也收不到。"""
    monkeypatch.setattr(deployment, "is_official_site", lambda: official)
    uid = _register(client, f"AppPush{official}")
    for c, path in ((client, f"/users/{uid}?section=notifications"),
                    (admin_client, f"/users/{uid}?section=notifications")):
        html = c.get(path).get_data(as_text=True)
        assert ('data-panel="notifications:apppush"' in html) is official, path
        assert ("apps.apple.com" in html) is official, path
        assert ("app-release.apk" in html) is official, path
        if not official:
            assert 'href="/donate"' not in html
