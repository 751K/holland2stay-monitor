"""
地图只显示近期还被抓到过的房源；坐标由监控进程周期补齐。

地图此前不带任何时间条件。``Occupied`` 是老化收敛的**终态**，那些行永远留在
库里，于是几个月前就从 feed 里消失的单元仍然钉在图上。2026-08-28 线上 628 条
里有 270 条超过 30 天没被抓到过，全部是 Occupied——真正的噪音全在终态一侧，
Reserved 与可订的房源没有一条陈旧。

坐标解析此前只有管理员在地图页手动点才会跑，新抓到的房源在有人想起来点一下
之前都不在图上。现改为监控进程按自己的计时器每半小时补一批。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from mcore import geocode


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _insert(st, lid: str, *, days_ago: float, name="Kastanjelaan 1, Eindhoven",
            status="Occupied"):
    st.conn.execute(
        """INSERT INTO listings
           (id, name, status, price_raw, available_from, features, url, city,
            first_seen, last_seen, notified, last_status, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)""",
        (lid, name, status, "€900", "2026-09-01", "[]", f"https://x/{lid}",
         "Eindhoven", _iso(days_ago + 1), _iso(days_ago), status, "holland2stay"),
    )
    st.conn.commit()


# ── 时间过滤 ────────────────────────────────────────────────────

class TestMapFreshness:
    def test_stale_listing_is_hidden(self, temp_db):
        st = temp_db
        _insert(st, "fresh", days_ago=1)
        _insert(st, "stale", days_ago=60)

        ids = {l["id"] for l in st.get_map_listings()}
        assert ids == {"fresh"}, "陈旧房源仍然出现在地图上"

    def test_zero_disables_the_filter(self, temp_db):
        """0 表示不过滤——留一条退路，出问题时能一眼看出是不是这道过滤造成的。"""
        st = temp_db
        _insert(st, "ancient", days_ago=400)
        assert st.get_map_listings() == []
        assert len(st.get_map_listings(max_age_days=0)) == 1

    @pytest.mark.parametrize("days_ago,visible", [
        (0.0, True), (13.0, True), (13.9, True), (14.5, False), (60.0, False),
    ])
    def test_boundary(self, temp_db, days_ago, visible):
        st = temp_db
        _insert(st, "x", days_ago=days_ago)
        assert bool(st.get_map_listings()) is visible

    def test_comparison_is_not_string_based(self, temp_db):
        """比较必须走 julianday。

        last_seen 存的是带时区的 ISO（2026-08-14T09:00:00+00:00），而
        datetime('now','-14 days') 返回空格分隔、无时区的形式。直接比字符串时
        第 10 位是 'T'(0x54) 对空格(0x20)，于是边界那一天的房源**无论几点**都
        判为「新」。差一天看不出来，但它是那种永远不会报错的错。

        这里放一条刚过阈值几小时的房源：字符串比较会让它通过，julianday 不会。
        """
        st = temp_db
        _insert(st, "just-over", days_ago=14.2)
        assert st.get_map_listings() == [], "边界日的陈旧房源漏了出去"

    def test_env_override(self, temp_db, monkeypatch):
        st = temp_db
        _insert(st, "m", days_ago=20)
        assert st.get_map_listings() == []
        monkeypatch.setenv("MAP_MAX_AGE_DAYS", "30")
        assert len(st.get_map_listings()) == 1

    def test_garbage_env_falls_back_to_default(self, temp_db, monkeypatch):
        """环境变量写错时按默认值走，而不是把过滤整个关掉。

        反过来（认不出就不过滤）会让一个拼错的变量名悄悄恢复到修之前的行为。
        """
        st = temp_db
        _insert(st, "old", days_ago=60)
        monkeypatch.setenv("MAP_MAX_AGE_DAYS", "abc")
        assert st.get_map_listings() == []


# ── 坐标补齐 ────────────────────────────────────────────────────

class TestGeocodeMissing:
    def test_only_geocodes_uncached(self, temp_db):
        st = temp_db
        _insert(st, "a", days_ago=1, name="Kastanjelaan 1, Eindhoven")
        _insert(st, "b", days_ago=1, name="Beukenlaan 2, Eindhoven")
        addrs = [l["address"] for l in st.get_map_listings()]
        st.cache_coords(addrs[0], 51.4, 5.4)

        called: list[str] = []
        with patch.object(geocode, "geocode_one",
                          lambda a: called.append(a) or (51.5, 5.5)):
            done, failed = geocode.geocode_missing(st)

        assert called == [addrs[1]], "把已经有坐标的地址又解析了一遍"
        assert (done, failed) == (1, 0)

    def test_respects_the_batch_limit(self, temp_db):
        """上限是必须的：第一次跑会有几百个地址，不设限就把一个抓取轮次拖成几分钟。"""
        st = temp_db
        for i in range(10):
            _insert(st, f"l{i}", days_ago=1, name=f"Street {i}, Eindhoven")

        called: list[str] = []
        with patch.object(geocode, "geocode_one",
                          lambda a: called.append(a) or (51.5, 5.5)):
            geocode.geocode_missing(st, limit=3)

        assert len(called) == 3

    def test_no_pending_means_no_network(self, temp_db):
        """没有待解析地址时不产生任何外部请求。

        补齐是半小时一次，稳态下绝大多数次都该走这一条路径。
        """
        st = temp_db
        _insert(st, "a", days_ago=1)
        for l in st.get_map_listings():
            st.cache_coords(l["address"], 51.4, 5.4)

        def _boom(_a):
            raise AssertionError("没有待解析地址却发起了请求")

        with patch.object(geocode, "geocode_one", _boom):
            assert geocode.geocode_missing(st) == (0, 0)

    def test_one_failure_does_not_stop_the_batch(self, temp_db):
        """一条解析不出的地址不该让后面几十条也拿不到坐标。"""
        st = temp_db
        for i in range(3):
            _insert(st, f"l{i}", days_ago=1, name=f"Street {i}, Eindhoven")

        def _flaky(addr):
            if "Street 1" in addr:
                raise RuntimeError("photon down")
            return (51.5, 5.5)

        with patch.object(geocode, "geocode_one", _flaky):
            done, failed = geocode.geocode_missing(st)

        assert (done, failed) == (2, 1)

    def test_failure_reason_is_not_raw_exception_text(self, temp_db):
        """进度接口是 @api_login_required——普通用户读得到。

        异常文本来自对 Photon 的 HTTP 调用，会带出服务地址，所以只归类。
        """
        st = temp_db
        _insert(st, "l", days_ago=1)
        addrs = [l["address"] for l in st.get_map_listings()]

        with patch.object(geocode, "geocode_one",
                          lambda a: (_ for _ in ()).throw(
                              RuntimeError("https://photon.komoot.io leaked"))):
            _, _, errors = geocode.geocode_addresses(st, addrs, sleep=lambda _s: None)

        assert errors[0]["reason"] == "geocoding request failed"
        assert "photon" not in errors[0]["reason"].lower()
        assert errors[0]["address"] == addrs[0], "地址要留着，用户得知道是哪条失败"

    def test_stale_listings_are_not_geocoded(self, temp_db):
        """地图不显示的房源也不该消耗解析额度。

        geocode_missing 走 get_map_listings，因此这条是免费搭上的——但它值得
        钉住：哪天有人为了「补全历史坐标」把这里换成全表查询，额度就会被几百条
        永远不会显示的房源吃掉。
        """
        st = temp_db
        _insert(st, "fresh", days_ago=1, name="Fresh Street 1, Eindhoven")
        _insert(st, "stale", days_ago=90, name="Stale Street 9, Eindhoven")

        called: list[str] = []
        with patch.object(geocode, "geocode_one",
                          lambda a: called.append(a) or (51.5, 5.5)):
            geocode.geocode_missing(st)

        assert len(called) == 1
        assert "Fresh" in called[0]


# ── 两个调用方共用同一份实现 ────────────────────────────────────

def test_web_and_monitor_share_one_implementation():
    """解析逻辑只有一份。

    它此前住在 app/routes/map_routes.py 里；监控进程不 import app.*，照抄一份
    过去就会有两份慢慢分叉的实现——而分叉的表现是「手动点能解析出来、自动跑
    解析不出来」，没有任何地方会报错。
    """
    import inspect

    import app.routes.map_routes as mr
    import monitor

    assert "geocode_addresses" in inspect.getsource(mr)
    assert "photon.komoot.io" not in inspect.getsource(mr), \
        "map_routes 里又出现了一份 Photon 调用"
    assert "geocode_missing" in inspect.getsource(monitor)


def test_geocode_does_not_ride_on_the_heartbeat():
    """坐标补齐必须有自己的计时器。

    心跳块里已经挂了几个 prune，而整块被 ``if heartbeat_interval_sec > 0`` 罩着
    ——把 HEARTBEAT_INTERVAL_MINUTES 设成 0 是「别给我发心跳」，那是通知偏好，
    不该顺带让地图停止补坐标。两件不相干的事共用一个开关，关掉其中一个的人
    不会知道自己也关掉了另一个。

    判据是两个 ``if`` 同级：守卫坐标补齐的那一行，缩进必须等于守卫心跳的那一行，
    而不是更深。盯缩进是笨办法，但「挪回心跳块里」正是这件事最可能的复发方式。
    """
    import inspect
    import re

    import monitor

    src = inspect.getsource(monitor)
    assert "_GEOCODE_INTERVAL_SEC" in src, "坐标补齐没有独立的间隔常量"

    def _indent_of(pattern: str) -> int:
        for line in src.split("\n"):
            if re.search(pattern, line) and line.lstrip().startswith("if "):
                return len(line) - len(line.lstrip())
        raise AssertionError(f"找不到 {pattern} 的守卫行")

    geo = _indent_of(r"last_geocode_time >= _GEOCODE_INTERVAL_SEC")
    hb = _indent_of(r"heartbeat_interval_sec > 0")
    assert geo == hb, (
        f"坐标补齐的守卫缩进 {geo}，心跳是 {hb}——它被挪进心跳块了，"
        "关掉心跳通知会连带停掉坐标补齐"
    )
