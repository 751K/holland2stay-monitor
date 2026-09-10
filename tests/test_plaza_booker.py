"""bookers/plaza.py —— Plaza 自动应征

侦察见 docs/PLAZA.md。这里盯的是三件**错了不会报错**的事（模块 docstring 里的
三个坑），以及「没验证过的东西不许当成功报给用户」。

全部用假 session，**一个真实请求都不发**。
"""
import pytest

from config import AutoBookConfig
from models import Listing
from users import UserConfig

import bookers.plaza as bp
from bookers import BOOKER_REGISTRY, dispatch_book
from bookers.base import BookingRequest


# ── 假 session ──────────────────────────────────────────────────────
class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.content = (text or "").encode()

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """按 URL 关键字返回预置响应，并记录每次调用的 (url, data, json, headers)。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple] = []

    def post(self, url, data=None, json=None, timeout=None, headers=None):
        return self._dispatch("POST", url, data, json, headers)

    def get(self, url, timeout=None, headers=None):
        return self._dispatch("GET", url, None, None, headers)

    def _dispatch(self, method, url, data, json, headers):
        self.calls.append((url, data, json, headers or {}, method))
        for key, resp in self.routes.items():
            if key in url:
                return resp(self) if callable(resp) else resp
        raise AssertionError(f"没有为 {url} 预置响应")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(payload):
    return FakeResp(200, payload)


def _reaction(dth=False, **kw):
    rd = {"loggedin": True, "kanReageren": True, "action": "add",
          "redenMagNietReagerenCode": None,
          "url": "?add=11882&dwellingID=16613"}
    rd.update(kw)
    return _ok({"result": {
        "reactionData": rd,
        # 服务端返回的原始 model。DTH 那批的 modelCategorie 整个是 null，
        # 判据只能是这个布尔。
        "model": {"advertentieSluitenNaEersteReactie": dth,
                  "modelCategorie": None if dth else {"code": "reactiedatum"}},
    }})


def _active(object_ids):
    return _ok({"result": {"items": [{"object": {"id": str(i)}} for i in object_ids]}})


TOKEN_OK = _ok({"access_token": "tok"})
SESSION_OK = FakeResp(200, {})


#: 真实的 react 响应（2026-09-10 抓包）：**顶层没有 result**。
REACT_OK = _ok({"success": True, "reactionId": 999001,
                "reactionData": {"kanReageren": False,
                                 "redenMagNietReagerenCode": "WINKEL-REACTIE-DUBBEL"},
                "messages": ["<strong>Thank you for your response!</strong>"]})

#: 提交信封端点（GET，返回 form 不是 result）。
ENVELOPE_OK = _ok({"form": {"id": "Portal_Form_SubmitOnly",
                            "elements": {"__hash__": {"initialData": "h" * 32,
                                                      "type": "hash"}}}})


def _routes(**over):
    r = {"oauth/token": TOKEN_OK,
         "loginbyservice": SESSION_OK,
         "getobject": _reaction(),
         "getformsubmitonlyconfiguration": ENVELOPE_OK,
         "react/format": REACT_OK,
         "getactievereacties": _active(["16613"])}
    r.update(over)
    return r


@pytest.fixture
def req():
    u = UserConfig(name="t")
    u.auto_book = AutoBookConfig(plaza_username="user1", plaza_password="pw")
    listing = Listing(id="pz_16613", name="Bomansplaats 67, Eindhoven",
                      status="Available to book", price_raw="€ 967,75",
                      available_from=None, features=[],
                      url="https://plaza.newnewnew.space/x",
                      city="Eindhoven", source="plaza")
    return BookingRequest(listing=listing, user=u)


def _run(monkeypatch, request, routes):
    sess = FakeSession(routes)
    monkeypatch.setattr(bp.PlazaBooker, "_session", staticmethod(lambda: sess))
    return bp.PlazaBooker().book(request), sess


class TestRegistration:
    def test_plaza_is_registered_and_enabled_at_source_level(self):
        """2026-09-10 端到端验证通过后进的这个元组。见 docs/PLAZA.md §6。"""
        import monitor
        assert BOOKER_REGISTRY["plaza"] is bp.PlazaBooker
        assert "plaza" in monitor._AUTO_BOOK_SOURCES

    def test_but_it_is_off_for_a_user_by_default(self):
        """进了元组不等于对谁都开——用户侧还有一道显式开关，默认关。"""
        assert AutoBookConfig().plaza_enabled is False

    def test_dispatch_routes_to_it(self, monkeypatch, req):
        monkeypatch.setattr(bp.PlazaBooker, "book",
                            lambda self, r: "routed")
        assert dispatch_book(req) == "routed"


class TestObjectId:
    def test_the_pz_prefix_is_stripped(self, monkeypatch, req):
        """站点端点只认裸 id；带着 scraper 加的 pz_ 前缀发过去是未知对象。"""
        _, sess = _run(monkeypatch, req, _routes())
        getobject = [c for c in sess.calls if "getobject" in c[0]]
        assert getobject and getobject[0][1] == {"id": "16613"}

    def test_an_id_without_the_prefix_is_left_alone(self):
        assert bp._object_id("16613") == "16613"
        assert bp._object_id("pz_16613") == "16613"


class TestPreflight:
    def test_kan_reageren_false_never_sends_the_write(self, monkeypatch, req):
        """预检不过时**绝不能**发 react。这是整个文件最重要的一条。"""
        res, sess = _run(monkeypatch, req, _routes(
            getobject=_reaction(kanReageren=False,
                                redenMagNietReagerenCode="WINKEL-REACTIE-NIETMEERGEPUBLICEERD")))
        assert not res.success and res.phase == "race_lost"
        assert not [c for c in sess.calls if "react/format" in c[0]]

    def test_an_unknown_reason_code_also_blocks_and_is_surfaced(self, monkeypatch, req):
        """未知码 fail-safe：同样不发写请求，并把原码带出来。

        侦察时只见过一个码，没有完整清单——把未知码当成「可以应征」是
        错向危险一侧。
        """
        res, sess = _run(monkeypatch, req, _routes(
            getobject=_reaction(kanReageren=False,
                                redenMagNietReagerenCode="WINKEL-IETS-ANDERS")))
        assert not res.success
        assert not [c for c in sess.calls if "react/format" in c[0]]
        assert "WINKEL-IETS-ANDERS" in res.message

    def test_no_session_is_caught_even_though_kan_reageren_is_true(self, monkeypatch, req):
        """未登录时 ``kanReageren`` **仍然是 true**（2026-09-10 实测）。

        两个字段答的是不同问题：kanReageren = 这条广告开不开放应征（与你是谁无关），
        loggedin = 这个会话认不认你。所以 kanReageren 根本不是会话检查——只查它的
        实现会带着一个无效会话一路走到 POST react。

        这条测试就是钉住「loggedin 那道检查不能被删」。
        """
        res, sess = _run(monkeypatch, req, _routes(
            getobject=_reaction(loggedin=False, kanReageren=True)))
        assert res.phase == "auth_failed", "未登录必须报成认证失败"
        assert not [c for c in sess.calls if "react/format" in c[0]], \
            "没有会话就绝不能发写请求"


class TestDryRun:
    def test_dry_run_stops_before_the_write(self, monkeypatch, req):
        req.dry_run = True
        res, sess = _run(monkeypatch, req, _routes())
        assert res.success and res.phase == "dry_run" and res.dry_run
        assert not [c for c in sess.calls if "react/format" in c[0]]


class TestReactPayload:
    def test_the_server_given_params_are_replayed_verbatim(self, monkeypatch, req):
        """原样回传，不自己拼。

        DTH 那批的 url 多一个 ``redirect=1``——手拼成 {add, dwellingID} 会把它漏掉，
        而漏参数多半不报错，只会落到一个不对的分支里。
        """
        res, sess = _run(monkeypatch, req, _routes(
            getobject=_reaction(url="?add=10600&redirect=1&dwellingID=15676"),
            getactievereacties=_active(["16613"])))
        react = [c for c in sess.calls if "react/format" in c[0]]
        assert react, "没有发出 react 请求"
        assert react[0][1] == {
            "__id__": "Portal_Form_SubmitOnly", "__hash__": "h" * 32,
            "add": "10600", "redirect": "1", "dwellingID": "15676"}

    def test_an_unparseable_url_is_an_error_not_an_empty_post(self, monkeypatch, req):
        res, sess = _run(monkeypatch, req, _routes(getobject=_reaction(url="")))
        assert not res.success
        assert not [c for c in sess.calls if "react/format" in c[0]]


class TestContentType:
    def test_portal_calls_are_form_encoded(self, monkeypatch, req):
        """发 JSON 会得到 200 + 没有 result + 零报错。所以编码必须钉住。"""
        _, sess = _run(monkeypatch, req, _routes())
        checked = 0
        for url, data, js, headers, method in sess.calls:
            if method != "POST":
                continue
            if "/portal/object/" in url or "getactievereacties" in url:
                assert js is None, f"{url} 不能用 json="
                assert headers.get("Content-Type") == "application/x-www-form-urlencoded"
                checked += 1
        assert checked >= 3, f"只检查到 {checked} 个 portal POST，这条几乎空过了"

    def test_a_response_without_result_raises_instead_of_looking_empty(self, monkeypatch, req):
        """只有 sAngularServiceData、没有 result = 请求发错了，不是「没有数据」。

        断言要盯**我们自己写的那句提示**，不能只判 "result" 在不在 message 里——
        去掉那道检查之后会抛 ``KeyError: 'result'``，异常文本里同样有 "result"，
        于是测试照样绿。第一版就是这么写的，被变异测试抓出来。
        """
        res, sess = _run(monkeypatch, req, _routes(
            getobject=_ok({"sAngularServiceData": "[]"})))
        assert not res.success
        assert "form-urlencoded" in res.message, (
            f"要给出「Content-Type 发错了」这条线索，实际拿到：{res.message!r}")
        assert not [c for c in sess.calls if "react/format" in c[0]]


class TestVerification:
    def test_success_requires_the_reaction_to_show_up(self, monkeypatch, req):
        """react 的响应形状从没验证过，所以只认回查的结果。"""
        res, _ = _run(monkeypatch, req, _routes(
            getactievereacties=_active(["99999"])))
        assert not res.success, "回查里没有这条，不能报成功"
        assert res.phase == "unknown_error"

    def test_a_confirmed_reaction_is_success(self, monkeypatch, req):
        res, _ = _run(monkeypatch, req, _routes())
        assert res.success and res.phase == "success"

    def test_the_react_response_body_is_never_trusted(self, monkeypatch, req):
        """react 返回一个看起来失败的 body，但回查显示进去了 → 仍算成功。"""
        res, _ = _run(monkeypatch, req, _routes(
            **{"react/format": _ok({"result": {"success": False}})}))
        assert res.success


class TestCredentials:
    def test_missing_credentials_never_touch_the_network(self, monkeypatch, req):
        req.user.auto_book.plaza_password = ""

        def _boom():
            raise AssertionError("不该建 session")

        monkeypatch.setattr(bp.PlazaBooker, "_session", staticmethod(_boom))
        res = bp.PlazaBooker().book(req)
        assert not res.success and res.phase == "not_configured"

    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_rejected_credentials_are_auth_failed(self, monkeypatch, req, status):
        res, _ = _run(monkeypatch, req,
                      _routes(**{"oauth/token": FakeResp(status, {})}))
        assert res.phase == "auth_failed"

    def test_credentials_are_encrypted_at_rest(self):
        """三个仓库都是 public，密码绝不能明文进库。

        直接调 ``_user_to_row``，不做 hasattr 兜底——兜底会在函数改名时静默跳过，
        而这条测试守的正是"改名之后没人回来同步加密清单"那种情况。
        """
        import json

        from crypto import decrypt
        from users import _user_to_row

        u = UserConfig(name="t")
        u.auto_book = AutoBookConfig(plaza_username="user1", plaza_password="pw")
        ab = json.loads(_user_to_row(u)["auto_book_json"])
        assert ab["plaza_password"] != "pw", "明文进库了"
        assert decrypt(ab["plaza_password"]) == "pw"
        assert ab["plaza_username"] == "user1", "用户名不加密（与其余平台的邮箱一致）"

    def test_load_decrypts(self):
        from crypto import encrypt
        from users import _ab_from_dict
        ab = _ab_from_dict({"plaza_username": "user1",
                            "plaza_password": encrypt("pw")})
        assert (ab.plaza_username, ab.plaza_password) == ("user1", "pw")


class TestCandidateGating:
    """两道闸：显式开关 + 凭据。缺任何一个都不产生候选。

    Plaza 比另外三个平台多一道，是因为它的应征一次 POST 就落地——H2S 下单后还要
    付款，两个 RENTCafe 停在存草稿，都还有一步在用户手里。所以「填了凭据」不能顺带
    等于「授权系统替我应征」。
    """

    def test_credentials_alone_are_not_enough(self, req):
        """有凭据、没打开开关 → 不产生候选。这条是新增那道闸的全部意义。"""
        assert req.user.auto_book.plaza_username and req.user.auto_book.plaza_password
        assert req.user.auto_book.plaza_enabled is False
        import monitor
        assert not monitor._can_auto_book(req.user, req.listing)

    def test_the_switch_alone_is_not_enough(self, req):
        """打开了开关、没凭据 → 也不产生候选。

        否则每条新房源都会跑一次注定失败的登录，而失败会消耗上游的尝试额度。
        """
        import monitor
        req.user.auto_book.plaza_enabled = True
        req.user.auto_book.plaza_password = ""
        assert not monitor._can_auto_book(req.user, req.listing)

    def test_both_together_produce_a_candidate(self, req):
        import monitor
        req.user.auto_book.plaza_enabled = True
        assert monitor._can_auto_book(req.user, req.listing)

    def test_a_source_not_in_the_tuple_is_still_blocked(self, req, monkeypatch):
        """source 级的闸仍在最前面，用户开关翻不过它。"""
        import monitor
        monkeypatch.setattr(monitor, "_AUTO_BOOK_SOURCES", ("holland2stay",))
        req.user.auto_book.plaza_enabled = True
        assert not monitor._can_auto_book(req.user, req.listing)


class TestPanelRoundTrip:
    """面板里填的凭据要真的存得进去。

    模板有输入框、表单不解析它，是一种**完全静默**的失败：用户填了、保存了、
    页面也没报错，但库里是空的，booker 永远拿不到凭据。变异测试第一次跑的时候
    这个洞就没被抓到，所以补这一条。
    """

    def test_credentials_typed_in_the_panel_are_persisted(self, admin_client):
        r = admin_client.post("/users/new", data={
            "name": "plaza-ui", "csrf_token": "test_csrf",
            "AUTO_BOOK_PLAZA_USERNAME": "zoeker1",
            "AUTO_BOOK_PLAZA_PASSWORD": "pw-plaza",
        }, headers={"X-CSRF-Token": "test_csrf"}, follow_redirects=True)
        assert r.status_code == 200

        from users import load_users
        u = next((x for x in load_users() if x.name == "plaza-ui"), None)
        assert u is not None
        assert u.auto_book.plaza_username == "zoeker1"
        assert u.auto_book.plaza_password == "pw-plaza"
        assert u.auto_book.plaza_enabled is False, "没勾开关就不该开"

    def test_the_switch_round_trips(self, admin_client):
        admin_client.post("/users/new", data={
            "name": "plaza-switch", "csrf_token": "test_csrf",
            "AUTO_BOOK_PLAZA_USERNAME": "zoeker9",
            "AUTO_BOOK_PLAZA_PASSWORD": "pw",
            "AUTO_BOOK_PLAZA_ENABLED": "true",
        }, headers={"X-CSRF-Token": "test_csrf"}, follow_redirects=True)

        from users import load_users
        u = next(x for x in load_users() if x.name == "plaza-switch")
        assert u.auto_book.plaza_enabled is True

        page = admin_client.get(f"/users/{u.id}").get_data(as_text=True)
        assert 'name="AUTO_BOOK_PLAZA_ENABLED" value="true"' in page, \
            "开关状态没有回填，用户再保存一次就会被关掉"

    def test_the_password_is_not_echoed_back_to_the_page(self, admin_client):
        admin_client.post("/users/new", data={
            "name": "plaza-echo", "csrf_token": "test_csrf",
            "AUTO_BOOK_PLAZA_USERNAME": "zoeker2",
            "AUTO_BOOK_PLAZA_PASSWORD": "pw-secret-echo",
        }, headers={"X-CSRF-Token": "test_csrf"}, follow_redirects=True)

        from users import load_users
        u = next(x for x in load_users() if x.name == "plaza-echo")
        page = admin_client.get(f"/users/{u.id}").get_data(as_text=True)
        assert "pw-secret-echo" not in page
        assert "zoeker2" in page, "用户名该回填（它不是密码）"

    def test_an_empty_password_field_keeps_the_stored_one(self, admin_client):
        """编辑时留空 = 不改密码，与其余平台一致；否则每次改别的都会清空凭据。"""
        admin_client.post("/users/new", data={
            "name": "plaza-keep", "csrf_token": "test_csrf",
            "AUTO_BOOK_PLAZA_USERNAME": "zoeker3",
            "AUTO_BOOK_PLAZA_PASSWORD": "pw-keep",
        }, headers={"X-CSRF-Token": "test_csrf"}, follow_redirects=True)

        from users import load_users
        u = next(x for x in load_users() if x.name == "plaza-keep")
        admin_client.post(f"/users/{u.id}", data={
            "name": "plaza-keep", "csrf_token": "test_csrf",
            "AUTO_BOOK_PLAZA_USERNAME": "zoeker3",
            "AUTO_BOOK_PLAZA_PASSWORD": "",
        }, headers={"X-CSRF-Token": "test_csrf"}, follow_redirects=True)

        u2 = next(x for x in load_users() if x.name == "plaza-keep")
        assert u2.auto_book.plaza_password == "pw-keep"


class TestSubmitEnvelope:
    """``__id__`` / ``__hash__`` —— 2026-09-10 抓到真实请求才发现的。

    实现的第一版只回传 ``reactionData.url`` 里的参数，会漏掉这两个。「原样回传
    服务端给的参数」是必要条件、不是充分条件：信封来自另一个端点。
    """

    def test_the_envelope_is_included(self, monkeypatch, req):
        _, sess = _run(monkeypatch, req, _routes())
        body = [c for c in sess.calls if "react/format" in c[0]][0][1]
        assert body.get("__id__") == "Portal_Form_SubmitOnly"
        assert body.get("__hash__") == "h" * 32

    def test_it_comes_from_a_get_on_its_own_endpoint(self, monkeypatch, req):
        _, sess = _run(monkeypatch, req, _routes())
        env = [c for c in sess.calls if "getformsubmitonlyconfiguration" in c[0]]
        assert env, "没有去取提交信封"
        assert env[0][4] == "GET", "这个端点是 GET，不是 POST"

    def test_it_is_fetched_before_every_submit_not_cached(self, monkeypatch, req):
        """令牌会轮换（formService 里有「响应带 formHash 就替换」那段），所以现取。"""
        booker = bp.PlazaBooker()
        sess = FakeSession(_routes())
        monkeypatch.setattr(bp.PlazaBooker, "_session", staticmethod(lambda: sess))
        booker.book(req)
        booker.book(req)
        n = len([c for c in sess.calls if "getformsubmitonlyconfiguration" in c[0]])
        assert n == 2, f"两次提交应当各取一次信封，实际 {n} 次"

    def test_an_incomplete_envelope_blocks_the_write(self, monkeypatch, req):
        res, sess = _run(monkeypatch, req, _routes(
            getformsubmitonlyconfiguration=_ok({"form": {"id": "X", "elements": {}}})))
        assert not res.success
        assert not [c for c in sess.calls if "react/format" in c[0]], \
            "信封不全就不该发写请求——发出去也是白发"

    def test_the_listing_params_win_on_a_key_clash(self, monkeypatch, req):
        """信封在前、房源参数在后。哪天键名撞上，以服务端给这条房源的那份为准。"""
        _, sess = _run(monkeypatch, req, _routes(
            getobject=_reaction(url="?__id__=OVERRIDE&add=1&dwellingID=2")))
        body = [c for c in sess.calls if "react/format" in c[0]][0][1]
        assert body["__id__"] == "OVERRIDE"


class TestReactResponseShape:
    def test_a_successful_react_has_no_result_key_and_must_not_raise(self, monkeypatch, req):
        """真实响应顶层是 success / reactionData / reactionId / messages，**没有 result**。

        把 ``result`` 写死成必须存在的话，一次**成功**的应征会被当成传输错误抛出来
        ——比失败更糟：房子应征上了，用户收到的是「失败」，于是既不会去站点确认，
        也不会撤回。
        """
        res, _ = _run(monkeypatch, req, _routes())
        assert res.success and res.phase == "success"

    def test_an_explicit_failure_is_reported_as_save_rejected(self, monkeypatch, req):
        res, _ = _run(monkeypatch, req, _routes(
            **{"react/format": _ok({"success": False, "messages": ["nee"]})}))
        assert not res.success and res.phase == "save_rejected"


class TestAlreadyReacted:
    def test_dubbel_is_success_not_an_error(self, monkeypatch, req):
        """``WINKEL-REACTIE-DUBBEL`` = 这个账号已经应征过。

        期望的终态已经成立。报失败的话上层会不停重试一条已经到手的候选。
        """
        res, sess = _run(monkeypatch, req, _routes(
            getobject=_reaction(kanReageren=False,
                                redenMagNietReagerenCode="WINKEL-REACTIE-DUBBEL")))
        assert res.success and res.phase == "success"
        assert not [c for c in sess.calls if "react/format" in c[0]], \
            "已经应征过就不必再发一次"


class TestAlreadyAppliedIsNotAWithdrawal:
    """已应征的房源不能被当成「可以应征」——否则会替用户撤单。

    2026-09-10 端到端第一次跑就撞上了。在那之前**这个文件里 34 条测试全绿**，
    因为替身里 ``action`` 永远是 ``"add"``：单元测试再多，也测不出一个从没在替身里
    出现过的服务端状态。这几条就是补那个洞。

    真实数据（已应征的 16626）：

        action: "remove"   kanReageren: true
        url: "?remove=999001&dwellingID=16626"
    """

    def test_a_remove_action_never_posts(self, monkeypatch, req):
        """最关键的一条：原样回传这个 url = POST 一个 remove = 撤销用户的应征。"""
        res, sess = _run(monkeypatch, req, _routes(
            getobject=_reaction(action="remove", kanReageren=True,
                                url="?remove=999001&dwellingID=16626")))
        assert not [c for c in sess.calls if "react/format" in c[0]], \
            "已经应征过却发了写请求——这会撤销用户已有的应征"
        assert res.success and res.phase == "success"

    def test_kan_reageren_alone_is_not_enough(self, monkeypatch, req):
        """把 kanReageren 单独拎出来看，它对这两种状态给的是同一个答案。"""
        add = _reaction(action="add", kanReageren=True, url="?add=1&dwellingID=2")
        rem = _reaction(action="remove", kanReageren=True, url="?remove=9&dwellingID=2")
        both = [_ok({"result": {"reactionData": {**r._payload["result"]["reactionData"]}}})
                for r in (add, rem)]
        assert all(x._payload["result"]["reactionData"]["kanReageren"] for x in both), \
            "前提：两种状态的 kanReageren 都是 true，所以它区分不了"

    def test_an_inconsistent_url_is_refused(self, monkeypatch, req):
        """``action`` 说 add 但参数里是 remove —— 两个信号打架时不动。"""
        res, sess = _run(monkeypatch, req, _routes(
            getobject=_reaction(action="add", kanReageren=True,
                                url="?remove=999001&dwellingID=16626")))
        assert not res.success
        assert not [c for c in sess.calls if "react/format" in c[0]]

    def test_an_unknown_action_is_refused(self, monkeypatch, req):
        res, sess = _run(monkeypatch, req, _routes(
            getobject=_reaction(action="hospiteren", kanReageren=True)))
        assert not res.success and res.phase == "unknown_error"
        assert not [c for c in sess.calls if "react/format" in c[0]]

    def test_a_normal_add_still_goes_through(self, monkeypatch, req):
        """反证：修完之后正常应征这条路没被堵死。"""
        res, sess = _run(monkeypatch, req, _routes())
        assert res.success and res.phase == "success"
        assert [c for c in sess.calls if "react/format" in c[0]]


class TestDryRunExercisesTheWholePathExceptTheWrite:
    def test_dry_run_fetches_the_envelope(self, monkeypatch, req):
        """不取信封的 dry_run 会说「可以应征」而真跑在下一步就死——等于没验。"""
        req.dry_run = True
        res, sess = _run(monkeypatch, req, _routes())
        assert res.success and res.phase == "dry_run"
        assert [c for c in sess.calls if "getformsubmitonlyconfiguration" in c[0]], \
            "dry_run 没有验证信封拿不拿得到"
        assert not [c for c in sess.calls if "react/format" in c[0]]

    def test_dry_run_fails_when_the_envelope_is_unavailable(self, monkeypatch, req):
        req.dry_run = True
        res, _ = _run(monkeypatch, req, _routes(
            getformsubmitonlyconfiguration=_ok({"form": {"id": "X", "elements": {}}})))
        assert not res.success, "信封拿不到就不该说「可以应征」"

    def test_dry_run_on_an_already_applied_listing_short_circuits(self, monkeypatch, req):
        """已应征的那条仍然提前返回——它不需要信封，也不该去取。"""
        req.dry_run = True
        res, sess = _run(monkeypatch, req, _routes(
            getobject=_reaction(action="remove", url="?remove=1&dwellingID=2")))
        assert res.success and res.phase == "success"
        assert not [c for c in sess.calls if "getformsubmitonlyconfiguration" in c[0]]


class TestDthIsNeverAutoSubmitted:
    """DTH 一律不自动应征——刻意的产品决定，不是没做完。

    确认框那句「geen andere aanbieding meer krijgt」的范围站点没有说明（3595 条
    gettranslations 全搜过，除确认框本身没有第二处）。事实未知时两边代价不对称：
    不应征只是少自动化一类当前够不着的房源，应征则可能自动放弃用户手上全部其它
    offer。所以不按。
    """

    def test_a_dth_listing_is_declined(self, monkeypatch, req):
        res, sess = _run(monkeypatch, req, _routes(getobject=_reaction(dth=True)))
        assert not res.success and res.phase == "unsupported"
        assert not [c for c in sess.calls if "react/format" in c[0]]

    def test_even_in_dry_run(self, monkeypatch, req):
        """dry_run 也要挡：否则「试运行说能订」会诱导用户去开真跑。"""
        req.dry_run = True
        res, _ = _run(monkeypatch, req, _routes(getobject=_reaction(dth=True)))
        assert res.phase == "unsupported"

    def test_even_when_the_site_says_it_can_be_booked(self, monkeypatch, req):
        """站点说能应征也不按——这不是能力问题，是决定。"""
        res, sess = _run(monkeypatch, req, _routes(
            getobject=_reaction(dth=True, kanReageren=True, action="add")))
        assert res.phase == "unsupported"
        assert not [c for c in sess.calls if "react/format" in c[0]]

    def test_the_message_says_why(self, monkeypatch, req):
        """phase=unsupported 会被 book_with_fallback 过滤掉，所以 message 是给日志
        和排查的人看的——必须说清是「刻意不按」而不是「坏了」。"""
        res, _ = _run(monkeypatch, req, _routes(getobject=_reaction(dth=True)))
        assert "definitief" in res.message and "自己决定" in res.message

    def test_a_normal_listing_is_unaffected(self, monkeypatch, req):
        res, sess = _run(monkeypatch, req, _routes(getobject=_reaction(dth=False)))
        assert res.success
        assert [c for c in sess.calls if "react/format" in c[0]]

    def test_the_criterion_is_the_boolean_not_the_label(self):
        """label 是本地化的：荷兰语 "Boeken"、英文 "Reply"。按文案判在英文界面翻车。"""
        assert bp.is_dth({"model": {"advertentieSluitenNaEersteReactie": True}})
        assert not bp.is_dth({"model": {"advertentieSluitenNaEersteReactie": False}})
        # modelCategorie 为 null 正是 DTH 的特征，不能拿它当判据
        assert bp.is_dth({"model": {"advertentieSluitenNaEersteReactie": True,
                                    "modelCategorie": None}})
        assert not bp.is_dth({"reactionData": {"label": "Boeken"}}), \
            "label 不是判据"
        assert not bp.is_dth({})

    def test_the_gate_sits_before_the_preflight(self, monkeypatch, req):
        """一条**当前不可应征**的 DTH，仍要报 unsupported，不能报 race_lost。

        这条钉的是**闸的位置**，不是闸本身。挪到预检之后同样不会提交 DTH——所以
        「有没有提交」类的断言全都抓不到（变异测试实测：挪位置之后 52 条全绿）。
        真正的区别在 phase：``race_lost`` 会被 ``monitor._collect_booking_candidates``
        放进重试队列，下一轮仍 Available 就再试一次——于是一条我们**永远不打算按**
        的 DTH 会被无限重试。``unsupported`` 则被 ``book_with_fallback`` 直接滤掉。
        """
        res, sess = _run(monkeypatch, req, _routes(
            getobject=_reaction(dth=True, kanReageren=False,
                                redenMagNietReagerenCode="WINKEL-REACTIE-NIETMEERGEPUBLICEERD")))
        assert res.phase == "unsupported", (
            f"DTH 闸必须在预检之前，否则会走成 {res.phase}，进重试队列无限重试")
        assert not [c for c in sess.calls if "react/format" in c[0]]

    def test_an_already_applied_dth_also_reports_unsupported(self, monkeypatch, req):
        """同理：已应征的 DTH 也不该走成 success——我们从没替他按过，不认这个功。"""
        res, _ = _run(monkeypatch, req, _routes(
            getobject=_reaction(dth=True, action="remove",
                                url="?remove=1&dwellingID=2")))
        assert res.phase == "unsupported"
