"""管理员用户编辑页的自动预订与申请人档案：守的是「填了能存进去」，不是长相。

这一页所有编辑器都在同一个 <form> 里，build_user_from_form 按整份 POST 重建配置。
模板里的 name 和解析器读的名字对不上是**完全静默**的：用户填了、保存了、页面不报错，
库里却是空的。所以这里断言的都是渲染出来的表单里有没有那个字段，以及档案缺项能不能
被看见；面板怎么排、什么颜色交给浏览器检查。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: 注册了 booker 的 source → 自动预订分组里对应条目的 key（account_view._autobook_rows）。
#: 注册了 booker 却没有填凭据的入口，那个 booker 就是够不着的。
BOOKER_ROWS = {
    "holland2stay": "h2s",
    "xior": "xior",
    "ourdomain": "ourdomain",
    "ourcampus": "ourcampus",
    "plaza": "plaza",
}


@pytest.fixture
def form_html(admin_client) -> str:
    r = admin_client.get("/users/new")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_every_registered_booker_has_an_entry(form_html):
    from bookers import BOOKER_REGISTRY
    assert set(BOOKER_ROWS) == set(BOOKER_REGISTRY), (
        "注册了新 booker 就要在自动预订分组里给它一个填凭据的条目")
    for key in BOOKER_ROWS.values():
        assert f'data-panel="autobook:{key}"' in form_html, f"自动预订分组里没有 {key} 的条目"


def test_every_credential_field_the_parser_reads_exists_in_the_form(form_html):
    """2026-09-17 加 OurCampus 面板时把模板里的 name 改错一个字母，所有测试仍然全绿——
    因为持久化测试是直接 POST 字段名，绕过了渲染出来的表单。"""
    src = (ROOT / "app" / "forms" / "user_form.py").read_text(encoding="utf-8")
    names = set(re.findall(r'["\'](AUTO_BOOK_[A-Z0-9_]*(?:EMAIL|PASSWORD|USERNAME))["\']', src))
    assert names, "没在 user_form.py 里找到任何凭据字段名，这条守卫失效了"
    missing = sorted(n for n in names if f'name="{n}"' not in form_html)
    assert not missing, f"解析器读这些字段，但表单里没有同名输入框: {missing}"


def test_booking_filter_fields_are_in_the_form(form_html):
    for name in ("AUTO_BOOK_MAX_RENT", "AUTO_BOOK_ALLOWED_CITIES", "AUTO_BOOK_ALLOWED_ENERGY"):
        assert f'name="{name}"' in form_html, f"预订条件缺 {name}"


def test_id_document_upload_is_submittable(form_html):
    """证件随整份表单一起提交：缺 enctype 时 request.files 永远是空的，其余字段照常保存。"""
    form = re.search(r'<form method="post" action="[^"]*" enctype="multipart/form-data"[^>]*id="account-form"', form_html)
    assert form, "编辑表单没有 multipart enctype，证件会静默不上传"
    assert re.search(r'<input type="file"[^>]*name="AUTO_BOOK_ID_DOC"', form_html)


def test_screening_consent_posts_true(form_html):
    m = re.search(r'<input type="checkbox" name="AUTO_BOOK_SCREENING_CONSENT" value="true"[^>]*>', form_html)
    assert m, "背景调查授权的复选框不见了"
    assert "aria-labelledby" in m.group(0), "开关本身没有文字，得挂上说明的 id"


def test_placeholders_are_marked_as_examples(form_html):
    """「China」「12」没有前缀时看上去像已经填好的值。"""
    for example in ("China", "5612 AB", "12"):
        bare = re.findall(rf'placeholder="{re.escape(example)}"', form_html)
        assert not bare, f"占位 {example!r} 没有「例：/ e.g.」前缀，会被看成已填的值"


def test_every_missing_field_is_counted_in_a_group():
    """档案按组显示「缺 N 项」。某个字段没归进任何组，缺了它也没人看得见。"""
    from bookers.applicant_profile import ApplicantProfile
    from app.services.account_view import PROFILE_GROUPS
    grouped = {f for fields in PROFILE_GROUPS.values() for f in fields}
    missing = set(ApplicantProfile().missing_fields())
    assert missing <= grouped, f"没有归组的缺项: {sorted(missing - grouped)}"


def test_incomplete_profile_shows_what_is_missing(admin_client):
    from config import AutoBookConfig
    from bookers.applicant_profile import ApplicantProfile
    from users import UserConfig, load_users, save_users

    u = UserConfig(name="prof-incomplete")
    u.auto_book = AutoBookConfig(applicant_profile=ApplicantProfile(nationality="China"))
    save_users(load_users() + [u])
    uid = next(x.id for x in load_users() if x.name == "prof-incomplete")
    html = admin_client.get(f"/users/{uid}?section=profile").get_data(as_text=True)
    assert re.search(r"\d+ missing|缺 \d+ 项", html), "档案不完整却看不出缺什么"
    assert re.search(r"\d+ incomplete|缺 \d+ 组", html), "档案分组的摘要没报缺几组"


def test_translation_keys_are_unique():
    """``user_form_profile`` 曾被定义两次，后一条把「申请人档案」覆盖成了「个人资料」。
    dict 字面量里重复的 key 不会报错，只会静默取最后一条。"""
    src = (ROOT / "translations.py").read_text()
    keys = re.findall(r'^\s*"([a-z0-9_]+)":\s*\{', src, re.M)
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"translations.py 里有重复 key，后一条会静默覆盖前一条: {dupes}"
