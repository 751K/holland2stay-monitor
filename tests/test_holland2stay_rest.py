"""
scrapers/holland2stay_rest.py —— REST 快速通道的查询构造与差分逻辑

不发真实请求：替换 ``RestWatcher._session``，按队列返回预设响应。
"""
from __future__ import annotations

import re

import pytest

import scrapers.holland2stay_rest as rest
from scrapers.holland2stay_rest import RestWatcher, _is_residence


class _Resp:
    def __init__(self, status: int, payload=None) -> None:
        self.status_code = status
        self._payload = payload
        self.content = b"x" * 100

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _item(slug: str, name: str | None = None, price: float = 900.0) -> dict:
    return {
        "id": hash(slug) & 0xFFFF,
        "name": name or slug.replace("-", " ").title(),
        "url": f"https://account.holland2stay.com/{slug}.html",
        "price_info": {"final_price": price},
    }


class _Session:
    """``plan`` 是响应队列：每次请求弹出一个，只剩一个时一直返回它。"""

    def __init__(self) -> None:
        self.plan: list[_Resp] = []
        self.urls: list[str] = []
        self.proxies: list[dict] = []

    def set(self, *resps: _Resp) -> None:
        self.plan = list(resps)

    def items(self, avail: list[dict]) -> None:
        self.set(_Resp(200, {"items": avail}))

    def get(self, url, timeout=None, proxies=None):
        self.urls.append(url)
        self.proxies.append(dict(proxies or {}))
        q = self.plan
        return q.pop(0) if len(q) > 1 else q[0]

    def close(self) -> None:
        pass


def _make(monkeypatch, city_ids=(29,), **kw) -> RestWatcher:
    monkeypatch.setattr(rest.time, "sleep", lambda s: None)
    w = RestWatcher(city_ids, **kw)
    w._session = _Session()
    return w


@pytest.fixture
def watcher(monkeypatch):
    return _make(monkeypatch)


def _param(url: str, group: int, key: str) -> str:
    m = re.search(rf"\[filter_groups\]\[{group}\]\[filters\]\[0\]\[{key}\]=([^&]*)", url)
    assert m, url
    return m.group(1)


# --------------------------------------------------------------------------- #
# 过滤
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", [
    "Kastanjelaan 1-135",          # 曾被 "kast" 滤掉
    "Boschdijk 525-0101 Room 5",
    "Parentstraat 2",
])
def test_residences_pass_filter(name):
    assert _is_residence(name)


@pytest.mark.parametrize("name", ["Storage 1", "Parking 10", "Locker 3", ""])
def test_non_residences_are_filtered(name):
    assert not _is_residence(name)


# --------------------------------------------------------------------------- #
# 查询构造
# --------------------------------------------------------------------------- #
def test_only_bookable_is_queried(watcher):
    watcher._session.items([])
    watcher.snapshot()
    [url] = watcher._session.urls
    assert _param(url, 0, "field") == "available_to_book"
    assert _param(url, 0, "value") == "179"
    assert _param(url, 0, "condition_type") == "eq"


def test_single_city_uses_eq(watcher):
    watcher._session.items([])
    watcher.snapshot()
    [url] = watcher._session.urls
    assert _param(url, 1, "field") == "city"
    assert _param(url, 1, "value") == "29"
    assert _param(url, 1, "condition_type") == "eq"


def test_multiple_cities_share_one_in_query(monkeypatch):
    w = _make(monkeypatch, city_ids=[25, "29", 29])
    w._session.items([])
    w.snapshot()
    [url] = w._session.urls
    assert _param(url, 1, "value") == "25,29"
    assert _param(url, 1, "condition_type") == "in"


def test_no_city_is_rejected():
    with pytest.raises(ValueError):
        RestWatcher([])


# --------------------------------------------------------------------------- #
# 翻页
# --------------------------------------------------------------------------- #
def test_full_page_fetches_next(monkeypatch):
    w = _make(monkeypatch, page_size=2)
    s = w._session
    s.set(_Resp(200, {"items": [_item("a-1"), _item("a-2")]}),
          _Resp(200, {"items": [_item("a-3")]}))
    snap = w.snapshot()
    assert sorted(snap) == ["a-1", "a-2", "a-3"]
    assert [_p for _p in (re.search(r"currentPage\]=(\d+)", u).group(1) for u in s.urls)] == ["1", "2"]


def test_failed_later_page_discards_snapshot(monkeypatch):
    w = _make(monkeypatch, page_size=2)
    s = w._session
    s.set(_Resp(200, {"items": [_item("a-1"), _item("a-2")]}), _Resp(500))
    assert w.snapshot() is None
    assert w.last_error == "HTTP 500"


def test_too_many_pages_is_a_failure(monkeypatch):
    w = _make(monkeypatch, page_size=1)
    w._session.items([_item("a-1")])           # 每页都满
    assert w.snapshot() is None
    assert len(w._session.urls) == rest._MAX_PAGES
    assert "页" in w.last_error


# --------------------------------------------------------------------------- #
# 差分
# --------------------------------------------------------------------------- #
def test_first_success_only_primes(watcher):
    watcher._session.items([_item("a-1")])
    assert watcher.poll() == []
    assert watcher.primed and watcher.known_count == 1


def test_new_id_is_reported_with_name_and_price(watcher):
    s = watcher._session
    s.items([])
    watcher.poll()
    s.items([_item("kastanjelaan-1-135", "Kastanjelaan 1-135", 1076.0)])
    [rec] = watcher.poll()
    assert rec["id"] == "kastanjelaan-1-135"
    assert rec["name"] == "Kastanjelaan 1-135"
    assert rec["price"] == 1076.0


def test_known_id_is_not_reported_again(watcher):
    s = watcher._session
    s.items([_item("a-1")])
    watcher.poll()
    s.items([_item("a-1"), _item("a-2")])
    assert [r["id"] for r in watcher.poll()] == ["a-2"]
    assert watcher.poll() == []


def test_storage_and_parking_never_reported(watcher):
    s = watcher._session
    s.items([])
    watcher.poll()
    s.items([_item("t-kwd-1", "Storage 1"), _item("p-lst-10", "Parking 10")])
    assert watcher.poll() == []


# --------------------------------------------------------------------------- #
# 基线：空结果是有效结果，失败不是
# --------------------------------------------------------------------------- #
def test_empty_city_at_startup_still_primes(watcher):
    s = watcher._session
    s.items([])
    assert watcher.poll() == []
    assert watcher.primed
    s.items([_item("a-1")])
    assert [r["id"] for r in watcher.poll()] == ["a-1"]


def test_rerelease_after_sellout_is_reported(watcher):
    s = watcher._session
    s.items([_item("a-1")])
    watcher.poll()
    s.items([])
    assert watcher.poll() == []
    s.items([_item("a-1")])
    assert [r["id"] for r in watcher.poll()] == ["a-1"]


def test_failure_does_not_prime(watcher):
    watcher._session.set(_Resp(500))
    assert watcher.poll() == []
    assert not watcher.primed
    assert watcher.last_error == "HTTP 500"


def test_failure_keeps_previous_baseline(watcher):
    s = watcher._session
    s.items([_item("a-1")])
    watcher.poll()
    s.set(_Resp(500))
    assert watcher.poll() == []
    s.items([_item("a-1")])
    assert watcher.poll() == []          # 还是同一套，不算新


def test_missing_items_key_is_a_failure(watcher):
    watcher._session.set(_Resp(200, {"message": "something changed"}))
    assert watcher.snapshot() is None
    assert "items" in watcher.last_error


def test_non_json_is_a_failure(watcher):
    watcher._session.set(_Resp(200, ValueError("bad")))
    assert watcher.snapshot() is None
    assert "JSON" in watcher.last_error


# --------------------------------------------------------------------------- #
# 重试
# --------------------------------------------------------------------------- #
def test_403_is_not_retried(watcher):
    s = watcher._session
    s.set(_Resp(403))
    assert watcher.snapshot() is None
    assert len(s.urls) == 1
    assert watcher.last_error == "HTTP 403"


def test_429_backs_off_then_succeeds(watcher):
    s = watcher._session
    s.set(_Resp(429), _Resp(429), _Resp(200, {"items": [_item("a-1")]}))
    assert list(watcher.snapshot()) == ["a-1"]
    assert len(s.urls) == 3
    assert watcher.last_error is None


def test_429_exhausted_sets_last_error(watcher):
    s = watcher._session
    s.set(_Resp(429))
    assert watcher.snapshot() is None
    assert len(s.urls) == 1 + len(rest._BACKOFF_429)
    assert "429" in watcher.last_error and "重试" in watcher.last_error


def test_success_clears_last_error(watcher):
    s = watcher._session
    s.set(_Resp(500))
    watcher.snapshot()
    s.items([])
    watcher.snapshot()
    assert watcher.last_error is None


# --------------------------------------------------------------------------- #
# 出口
# --------------------------------------------------------------------------- #
class TestProxy:
    def test_without_getter_goes_direct_explicitly(self, watcher):
        """不传 proxy（手工命令行）才直连，且是显式空串，不让 curl_cffi 读 HTTPS_PROXY。"""
        watcher._session.items([])
        watcher.snapshot()
        assert watcher._session.proxies == [{"http": "", "https": ""}]

    def test_getter_url_is_used(self, monkeypatch):
        w = _make(monkeypatch, proxy=lambda: "http://u-1:p@proxy.test:80")
        w._session.items([])
        w.snapshot()
        assert w._session.proxies == [{"http": "http://u-1:p@proxy.test:80",
                                       "https": "http://u-1:p@proxy.test:80"}]

    def test_new_exit_each_poll_same_exit_across_pages(self, monkeypatch):
        n = iter(range(100))
        w = _make(monkeypatch, page_size=1, proxy=lambda: f"http://u-{next(n)}:p@proxy.test:80")
        s = w._session
        s.set(_Resp(200, {"items": [_item("a-1")]}), _Resp(200, {"items": []}))
        w.snapshot()
        assert [p["https"] for p in s.proxies] == ["http://u-0:p@proxy.test:80"] * 2
        s.items([])
        w.snapshot()
        assert s.proxies[-1]["https"] == "http://u-1:p@proxy.test:80"

    @pytest.mark.parametrize("value", ["", None])
    def test_no_proxy_available_sends_nothing(self, monkeypatch, value):
        """代理不可用时不查，也**不退回直连**。"""
        w = _make(monkeypatch, proxy=lambda: value)
        w._session.items([_item("a-1")])
        assert w.snapshot() is None
        assert w._session.urls == []
        assert "代理" in w.last_error

    def test_getter_exception_sends_nothing(self, monkeypatch):
        def boom():
            raise RuntimeError("config broken")
        w = _make(monkeypatch, proxy=boom)
        w._session.items([])
        assert w.snapshot() is None
        assert w._session.urls == []

    def test_unavailable_proxy_keeps_baseline(self, monkeypatch):
        state = {"url": "http://u-1:p@proxy.test:80"}
        w = _make(monkeypatch, proxy=lambda: state["url"])
        s = w._session
        s.items([_item("a-1")])
        w.poll()
        state["url"] = ""
        assert w.poll() == []
        state["url"] = "http://u-2:p@proxy.test:80"
        assert w.poll() == []          # a-1 还在基线里，不算新
