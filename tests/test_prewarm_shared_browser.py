"""预登录借用常驻浏览器：借来的不能当自己的用。

这一族错误全是**静默**的。关掉全进程共用的浏览器不会抛异常、不会进日志，下一个
用它的人只是拿到一个空壳，然后"莫名其妙"又过了一次 CF 挑战——而那 16.5 秒正是这
整件事要省掉的东西。

分工（见 mcore/warm_browser.py）：
    过 CF   16.5s  属于浏览器  → 共用
    登录    ~1.5s  属于用户    → 各自
"""
from __future__ import annotations

import threading

import pytest

import booker
from mcore.prewarm import PrewarmCache


class FakeFetcher:
    def __init__(self) -> None:
        self.closed = 0
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def close(self) -> None:
        self.closed += 1

    def __exit__(self, *a):
        self.close()


@pytest.fixture
def no_real_login(monkeypatch):
    """把登录换成一个不发网络请求的桩，返回它记下的调用。"""
    calls = []

    def _login(fetcher, email, password):
        calls.append((fetcher, email))
        return f"tok-{email}"

    monkeypatch.setattr(booker, "login", _login)
    return calls


class TestBorrowing:

    def test_borrowing_does_not_open_a_browser(self, no_real_login, monkeypatch):
        """借来的整个意义就在于不开新浏览器、不重过挑战。"""
        opened = []
        monkeypatch.setattr(booker, "BrowserFetcher",
                            lambda *a, **kw: opened.append(1))

        shared = FakeFetcher()
        ps = booker.create_prewarmed_session(
            "a@b.c", "pw", shared=(shared, threading.RLock()))

        assert opened == [], "借了常驻还另开了一个浏览器"
        assert ps.fetcher is shared
        assert ps.owns_fetcher is False
        assert ps.use_lock is not None

    def test_without_a_lane_it_opens_its_own(self, no_real_login, monkeypatch):
        """没有常驻时必须还能走老路，否则常驻建不起来就全盘瘫痪。"""
        made = FakeFetcher()
        made.__enter__ = lambda: made          # type: ignore[assignment]
        monkeypatch.setattr(booker, "BrowserFetcher", lambda *a, **kw: made)

        ps = booker.create_prewarmed_session("a@b.c", "pw")
        assert ps.fetcher is made
        assert ps.owns_fetcher is True
        assert ps.use_lock is None

    def test_each_user_gets_its_own_token_on_one_browser(
            self, no_real_login, monkeypatch):
        """一个浏览器，N 个 token——这就是「不需要 9 个 Chrome」的那句话。"""
        monkeypatch.setattr(booker, "BrowserFetcher",
                            lambda *a, **kw: pytest.fail("不该开新浏览器"))
        shared, lock = FakeFetcher(), threading.RLock()

        a = booker.create_prewarmed_session("a@x.c", "pw", shared=(shared, lock))
        b = booker.create_prewarmed_session("b@x.c", "pw", shared=(shared, lock))

        assert a.fetcher is b.fetcher is shared
        assert a.token != b.token
        assert shared.closed == 0


class TestNeverCloseWhatYouBorrowed:

    def test_close_if_owned_spares_the_shared_browser(self, no_real_login):
        shared = FakeFetcher()
        ps = booker.create_prewarmed_session(
            "a@b.c", "pw", shared=(shared, threading.RLock()))
        ps.close_if_owned()
        assert shared.closed == 0, "把全进程共用的浏览器关掉了"

    def test_close_if_owned_does_close_its_own(self, no_real_login, monkeypatch):
        made = FakeFetcher()
        made.__enter__ = lambda: made          # type: ignore[assignment]
        monkeypatch.setattr(booker, "BrowserFetcher", lambda *a, **kw: made)
        ps = booker.create_prewarmed_session("a@b.c", "pw")
        ps.close_if_owned()
        assert made.closed == 1, "自己的浏览器漏了"

    def test_invalidate_spares_the_shared_browser(self, no_real_login):
        """用户被禁用 / 改邮箱都会走到 invalidate。

        它无差别 close() 的话，一次「把某个用户停掉」就顺手废了所有人的下单通道，
        而操作的人完全想不到。
        """
        shared = FakeFetcher()
        cache = PrewarmCache()
        cache.set("u1", booker.create_prewarmed_session(
            "a@b.c", "pw", shared=(shared, threading.RLock())))

        cache.invalidate("u1")
        assert "u1" not in cache
        assert shared.closed == 0

    def test_clear_spares_the_shared_browser(self, no_real_login):
        """CF 熔断会调 clear()。那一刻常驻是唯一还通着的路，不能连它一起丢。"""
        shared = FakeFetcher()
        cache = PrewarmCache()
        for uid in ("u1", "u2"):
            cache.set(uid, booker.create_prewarmed_session(
                f"{uid}@b.c", "pw", shared=(shared, threading.RLock())))

        cache.clear()
        assert len(cache) == 0
        assert shared.closed == 0


class TestBorrowLane:

    def test_a_dead_lane_is_not_borrowed(self, monkeypatch):
        """借到一个死浏览器比借不到更糟：老路至少会自己重过挑战。"""
        from mcore import warm_browser

        f = FakeFetcher()
        f.alive = False
        monkeypatch.setattr(warm_browser.warm_lane, "_fetcher", f)
        assert PrewarmCache._borrow_lane() is None

    def test_no_lane_yields_none(self, monkeypatch):
        from mcore import warm_browser
        monkeypatch.setattr(warm_browser.warm_lane, "_fetcher", None)
        assert PrewarmCache._borrow_lane() is None

    def test_a_healthy_lane_is_borrowed_with_its_lock(self, monkeypatch):
        from mcore import warm_browser

        f = FakeFetcher()
        monkeypatch.setattr(warm_browser.warm_lane, "_fetcher", f)
        got = PrewarmCache._borrow_lane()
        assert got is not None
        assert got[0] is f
        assert got[1] is warm_browser.warm_lane.lock

    def test_borrowing_does_not_hold_the_lock(self, monkeypatch):
        """只读一个属性和 is_alive()。为这个去排队等别人整笔下单是错的。"""
        from mcore import warm_browser

        monkeypatch.setattr(warm_browser.warm_lane, "_fetcher", FakeFetcher())
        PrewarmCache._borrow_lane()
        assert warm_browser.warm_lane.lock.acquire(blocking=False), "借完没放锁"
        warm_browser.warm_lane.lock.release()


def _listing():
    from models import Listing
    l = Listing(id="x1", name="X 1", status="Available to book",
                price_raw="€1", available_from="", features={},
                url="http://x", city="Eindhoven", source="holland2stay")
    l.sku = "sku-1"
    return l


def _free_for_other_threads(lock) -> bool:
    """**必须从另一个线程问。**

    ``RLock`` 是可重入的：本线程持着它的时候，本线程再 acquire 照样成功。所以在
    调用 try_book 的同一个线程里问「锁放了吗」，答案永远是「放了」——这条断言会
    在锁根本没释放时也照样绿。第一版就是这么写的，把 finally 里的 release 整段
    删掉，26 条测试全过。
    """
    got = []

    def _probe():
        ok = lock.acquire(timeout=0.5)
        got.append(ok)
        if ok:
            lock.release()      # RLock 只认自己的线程，必须在这里放

    t = threading.Thread(target=_probe)
    t.start()
    t.join()
    return bool(got and got[0])


class TestLaneLockIsAlwaysReleased:
    """``try_book`` 中途抛异常也必须放锁。

    不放的话整条下单通道被永久焊死：之后每一次借用都卡在 login 那行，而 monitor
    的 2 秒上限会把它们统统判成「预登录没赶上」，于是**表现出来就是常驻从此再没
    生效过**——没有异常、没有日志，只有一个悄悄退回老路的系统。
    """

    def _run(self, monkeypatch, *, blow_up: bool):
        shared, lock = FakeFetcher(), threading.RLock()
        ps = booker.PrewarmedSession(
            fetcher=shared, token="tok", created_at=0.0,
            token_expiry=1e18, email="a@b.c",
            owns_fetcher=False, use_lock=lock,
        )
        listing = _listing()

        def _boom(*a, **kw):
            raise RuntimeError("占房接口炸了")

        # 整条下单链路都换成桩：这里测的是「锁有没有放」，不是下单本身。
        monkeypatch.setattr(booker, "create_booking",
                            _boom if blow_up else (lambda *a, **kw: "cart-1"))
        monkeypatch.setattr(booker, "remember_our_reservation", lambda *a, **kw: None)
        monkeypatch.setattr(booker, "set_payment_method", lambda *a, **kw: None)
        monkeypatch.setattr(booker, "_fetch_checkout_agreements", lambda *a, **kw: None)
        monkeypatch.setattr(booker, "place_order", lambda *a, **kw: "ORD-1")
        monkeypatch.setattr(booker, "_ideal_checkout", lambda *a, **kw: "http://pay")
        booker.try_book(listing, "a@b.c", "pw", prewarmed=ps)
        return lock

    def test_released_after_a_failure(self, monkeypatch):
        lock = self._run(monkeypatch, blow_up=True)
        assert _free_for_other_threads(lock), "下单失败后没放锁，通道被焊死"

    def test_released_after_a_success(self, monkeypatch):
        lock = self._run(monkeypatch, blow_up=False)
        assert _free_for_other_threads(lock), "下单成功后没放锁，通道被焊死"

    def test_the_shared_browser_survives_a_failure(self, monkeypatch):
        shared, lock = FakeFetcher(), threading.RLock()
        ps = booker.PrewarmedSession(
            fetcher=shared, token="tok", created_at=0.0, token_expiry=1e18,
            email="a@b.c", owns_fetcher=False, use_lock=lock,
        )
        listing = _listing()
        monkeypatch.setattr(booker, "create_booking",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("炸")))
        booker.try_book(listing, "a@b.c", "pw", prewarmed=ps)
        assert shared.closed == 0, "下单失败顺手把常驻关了"


class TestProgrammingErrorsAreNotTransient:
    """签名对不上是**代码错了**，不是「这次没建成」。

    2026-09-08 本仓自己踩过：给 create_prewarmed_session 加了个关键字参数，测试
    里的桩没跟上，TypeError 被 ``except Exception`` 收成一句 WARNING「预登录失败，
    回退正常登录路径」。系统照常跑、房源照常抓不到，日志读起来像是 CF 又不高兴了。
    """

    def _create(self, monkeypatch, exc):
        from unittest.mock import MagicMock

        import mcore.prewarm as m

        def _boom(*a, **kw):
            raise exc

        monkeypatch.setattr(m, "create_prewarmed_session", _boom)
        user = MagicMock()
        user.name = "A"
        user.auto_book.email = "a@x.c"
        user.auto_book.password = "pw"
        return PrewarmCache().create(user)

    @pytest.mark.parametrize("exc", [
        TypeError("got an unexpected keyword argument 'shared'"),
        AttributeError("'NoneType' object has no attribute 'fetcher'"),
    ])
    def test_code_errors_are_logged_as_errors(self, monkeypatch, caplog, exc):
        import logging
        with caplog.at_level(logging.DEBUG, logger="monitor"):
            assert self._create(monkeypatch, exc) is None
        errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errs, "代码错误被降级成了 WARNING，会淹在网络噪音里"
        assert errs[0].exc_info is not None, "没带堆栈就查不出是哪一行"

    def test_network_errors_stay_warnings(self, monkeypatch, caplog):
        """网络抖动是常态，报 ERROR 会把 errors.log 冲垮，真问题就看不见了。"""
        import logging
        with caplog.at_level(logging.DEBUG, logger="monitor"):
            assert self._create(monkeypatch, ConnectionError("代理超时")) is None
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_it_still_does_not_take_the_round_down(self, monkeypatch):
        """一个笔误不该让整轮监控停摆——返回 None，下单退回老路。"""
        assert self._create(monkeypatch, TypeError("boom")) is None


class TestOnlyOneUserBorrowsTheLane:
    """常驻只给本轮优先级最高的那个用户,其余照旧各开各的。

    为什么不是「排队」：那个队列在改动之前根本不存在——每个用户各开一个浏览器，
    完全并行。而 ``threading`` 的锁随便唤醒一个等待者，既不 FIFO 更不认
    ``sort_order``。用户排序是用户在设置页明确拖出来的东西，不该被一把锁的唤醒
    顺序悄悄改写。

    所以做法是不排队：第一名用常驻（1.5s），其余走老路（各自开浏览器），
    **没有人比改动之前差**。
    """

    def _user(self, uid):
        from unittest.mock import MagicMock
        u = MagicMock()
        u.id = uid
        u.name = uid
        u.auto_book.email = f"{uid}@x.c"
        u.auto_book.password = "pw"
        return u

    def test_the_second_user_does_not_borrow(self, no_real_login, monkeypatch):
        import mcore.prewarm as m

        shared = FakeFetcher()
        monkeypatch.setattr(m.PrewarmCache, "_borrow_lane",
                            staticmethod(lambda: (shared, threading.RLock())))
        own = FakeFetcher()
        own.__enter__ = lambda: own              # type: ignore[assignment]
        monkeypatch.setattr(booker, "BrowserFetcher", lambda *a, **kw: own)

        cache = m.PrewarmCache()
        first = cache.create(self._user("u1"), may_borrow_lane=True)
        second = cache.create(self._user("u2"), may_borrow_lane=False)

        assert first.fetcher is shared and first.owns_fetcher is False
        assert second.fetcher is own and second.owns_fetcher is True
        assert second.use_lock is None, "第二个人也绑上了常驻，下单时会排队"

    def test_lane_in_use_sees_a_cached_borrower(self, no_real_login, monkeypatch):
        """跨轮也要认：上一轮借出去还缓存着，这一轮就不能再借给别人。"""
        import mcore.prewarm as m

        cache = m.PrewarmCache()
        assert cache.lane_in_use() is False
        cache.set("u1", booker.create_prewarmed_session(
            "a@b.c", "pw", shared=(FakeFetcher(), threading.RLock())))
        assert cache.lane_in_use() is True

    def test_an_own_browser_session_does_not_count_as_using_the_lane(
            self, no_real_login, monkeypatch):
        import mcore.prewarm as m

        own = FakeFetcher()
        own.__enter__ = lambda: own              # type: ignore[assignment]
        monkeypatch.setattr(booker, "BrowserFetcher", lambda *a, **kw: own)
        cache = m.PrewarmCache()
        cache.set("u1", booker.create_prewarmed_session("a@b.c", "pw"))
        assert cache.lane_in_use() is False


class TestLanePriorityWiring:

    def test_the_lane_goes_to_the_first_user_in_sort_order(self):
        """``user_notifiers`` 是 sort_order 序（list_user_config_rows 的 ORDER BY），
        所以「循环里第一个」必须等于「优先级最高的」——靠的是不打乱这个循环。
        """
        import inspect

        import monitor
        src = inspect.getsource(monitor.run_once)
        body = src[src.index("def _start_prewarm_for_candidates"):]
        body = body[:body.index("async def _wait_for_candidate_prewarms")]
        assert "lane_offered = False" in body, "没有「本轮已经给出去了」这个标记"
        assert "may_borrow_lane=may_borrow" in body, "创建时没传借用开关"
        assert "prewarm_cache.lane_in_use()" in body, "没考虑跨轮还缓存着的借用者"
        # 循环本身不能被排序/打乱——一 reorder，「第一个」就不再是优先级最高的
        loop_at = body.index("for user, _ in user_notifiers:")
        assert "sorted(" not in body[loop_at:loop_at + 200]
