"""Web 端新用户注册的可达性。

这条路径曾经整个断掉且没有任何测试变红：
- ``/login`` 里的自动注册被删掉了（三条理由见 sessions.py 的注释：绕过
  terms_accepted、绕过 64 字符截断、构成用户枚举侧信道），删得对；
- 但登录页的 UI 没跟着改：按钮写着「Sign in / Register」，弹窗承诺「首次登录
  将自动创建账户」，确认之后表单仍投向 ``/login``，拿到的是「用户名或密码错误」；
- 而全站**没有任何表单指向 ``/register``**——那个端点注册了路由，UI 到不了。

于是新用户在 Web 上根本注册不了。本文件守两件事：注册这条路走得通，以及走通
之后那三条理由依然成立。
"""
import re
from pathlib import Path

import pytest

from users import load_users

ROOT = Path(__file__).resolve().parent.parent


def _js(path: Path) -> str:
    """模板源码，剥掉 ``//``、``/* */`` 与 Jinja ``{# #}`` 注释。

    必须剥：下面那几条断言要找的标识符（``/register``、``form.action``、
    ``terms_accepted``），在这个文件里上方的说明注释中全都出现过。不剥的话
    把整段实现删掉，grep 咬中的仍是注释，测试照样绿——这正是本文件要防的那
    类错，不能自己再犯一次。
    """
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"\{#.*?#\}", " ", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
    return src


@pytest.fixture(autouse=True)
def _reset_register_rate():
    """注册限流是模块级状态，跨用例累积会让后面的断言全变成 429。"""
    from app.auth import _REGISTER_RECORDS
    _REGISTER_RECORDS.clear()
    yield
    _REGISTER_RECORDS.clear()


def _csrf(client) -> str:
    client.get("/login")
    with client.session_transaction() as sess:
        return sess.get("csrf_token", "")


def _sign_out(client) -> None:
    """直接清 session。

    ``client.get("/logout")`` 在这里不起作用——logout 带 ``@csrf_required``
    且只接 POST。用它「退出」之后 session 仍是已登录的，而 ``register_user``
    开头就有 ``if session.get("authenticated"): return redirect(...)``，
    第二次注册会拿到 302 而不是 409，测试于是测了个寂寞。
    """
    with client.session_transaction() as sess:
        sess.clear()


def _names(client) -> set[str]:
    return {u.name for u in load_users()}


class TestRegistrationIsReachable:
    def test_login_page_points_at_register(self, client):
        page = client.get("/login").data.decode("utf-8", "ignore")
        assert "/register" in page
        assert 'id="register-action"' in page

    def test_confirm_flow_creates_the_account(self, client):
        """弹窗确认后表单改投 /register，带 terms_accepted=1。"""
        csrf = _csrf(client)
        before = _names(client)
        r = client.post("/register", data={
            "username": "newcomer",
            "password": "hunter22",
            "terms_accepted": "1",
            "csrf_token": csrf,
        })
        assert r.status_code == 302, r.data[:300]
        assert "newcomer" in _names(client) - before

    def test_new_user_can_sign_in_afterwards(self, client):
        csrf = _csrf(client)
        client.post("/register", data={
            "username": "roundtrip", "password": "hunter22",
            "terms_accepted": "1", "csrf_token": csrf})
        _sign_out(client)
        csrf = _csrf(client)
        r = client.post("/login", data={
            "username": "roundtrip", "password": "hunter22", "csrf_token": csrf})
        assert r.status_code == 302, "注册出来的账号登录不了"

    def test_old_field_names_still_work(self, client):
        """register_username / register_password 是老写法，测试仍在用。"""
        csrf = _csrf(client)
        r = client.post("/register", data={
            "register_username": "oldstyle", "register_password": "hunter22",
            "terms_accepted": "1", "csrf_token": csrf})
        assert r.status_code == 302
        assert "oldstyle" in _names(client)


class TestTheThreeReasonsStillHold:
    """删掉自动注册的三条理由不能因为这次修复而失效。"""

    def test_terms_are_still_required(self, client):
        """理由一：terms_accepted 是同意条款的凭证，不能替用户默认同意。"""
        csrf = _csrf(client)
        before = _names(client)
        r = client.post("/register", data={
            "username": "no-terms", "password": "hunter22", "csrf_token": csrf})
        assert r.status_code == 400
        assert _names(client) == before, "没同意条款却建了号"

    def test_username_is_still_truncated_to_64(self, client):
        """理由二：64 字符截断。"""
        csrf = _csrf(client)
        long_name = "n" * 100
        client.post("/register", data={
            "username": long_name, "password": "hunter22",
            "terms_accepted": "1", "csrf_token": csrf})
        created = [n for n in _names(client) if n.startswith("nn")]
        assert created, "账号没建出来"
        assert all(len(n) == 64 for n in created), [len(n) for n in created]

    def test_login_still_hides_whether_a_user_exists(self, client, test_credentials):
        """理由三：用户枚举侧信道。

        ``/login`` 对「用户不存在」和「密码错误」必须给出同样的响应——状态码
        和提示文案都要一致，否则一次 POST 就能判断任意用户名是否注册过。
        """
        csrf = _csrf(client)
        client.post("/register", data={
            "username": "exists-already", "password": "hunter22",
            "terms_accepted": "1", "csrf_token": csrf})
        _sign_out(client)

        csrf = _csrf(client)
        wrong_pw = client.post("/login", data={
            "username": "exists-already", "password": "WRONG", "csrf_token": csrf})
        csrf = _csrf(client)
        no_such = client.post("/login", data={
            "username": "definitely-not-a-user", "password": "WRONG", "csrf_token": csrf})

        assert wrong_pw.status_code == no_such.status_code
        a = wrong_pw.data.decode("utf-8", "ignore")
        b = no_such.data.decode("utf-8", "ignore")
        assert ("用户名或密码错误" in a) == ("用户名或密码错误" in b)

    def test_login_never_creates_an_account(self, client):
        """/login 本身仍然一个号都不许建。"""
        csrf = _csrf(client)
        before = _names(client)
        client.post("/login", data={
            "username": "should-not-exist", "password": "hunter22", "csrf_token": csrf})
        assert _names(client) == before

    def test_duplicate_name_is_rejected(self, client):
        csrf = _csrf(client)
        client.post("/register", data={
            "username": "taken", "password": "hunter22",
            "terms_accepted": "1", "csrf_token": csrf})
        _sign_out(client)
        csrf = _csrf(client)
        r = client.post("/register", data={
            "username": "taken", "password": "different",
            "terms_accepted": "1", "csrf_token": csrf})
        assert r.status_code == 409


class TestTheConfirmButtonActuallyRetargetsTheForm:
    """确认弹窗必须把表单**改投** ``/register``。

    这一条是整组测试里最要紧的：其余断言都直接 POST ``/register``，压根碰不到
    那段 JS，而坏掉的恰恰是 JS——弹窗承诺自动注册，确认之后表单仍投向
    ``/login``，于是拿到「用户名或密码错误」。变异测试里把 ``form.action = …``
    那一行删掉，前九条断言全绿。
    """

    @property
    def _confirm_login(self) -> str:
        src = _js(ROOT / "templates" / "login.html")
        start = src.index("function confirmLogin()")
        return src[start:src.index("\n}", start)]

    def test_confirm_switches_the_form_action(self):
        body = self._confirm_login
        assert "form.action" in body, "确认之后表单没有改投，仍会 POST /login"
        assert "register-action" in body, "改投的目标不是页面上那个 register URL"

    def test_confirm_sends_terms_accepted(self):
        """条款同意的凭证就在这一步产生——弹窗正文写着同意条款。"""
        body = self._confirm_login
        assert "terms_accepted" in body
        assert "'1'" in body or '"1"' in body

    def test_the_plain_submit_path_still_goes_to_login(self):
        """已注册用户不该被弹窗打扰，也不该被改投到 /register。

        断言写成"不含任何对 .action 的赋值"，而不是"不含 ``form.action``"：
        后者太字面，``getElementById('login-form').action = '/register'`` 这种
        写法从旁边绕过去，变异测试实测漏掉过一次。
        """
        src = _js(ROOT / "templates" / "login.html")
        start = src.index("function submitLoginForm()")
        body = src[start:src.index("\n}", start)]
        assert not re.search(r"\.action\s*=", body), "普通登录路径不该改写 action"
        assert "register" not in body, "普通登录路径不该提到 register"

    def test_form_default_action_is_login(self):
        src = _js(ROOT / "templates" / "login.html")
        assert "url_for('login')" in src
