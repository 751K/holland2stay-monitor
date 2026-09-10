"""自动预订的「能不能通知到这个用户」这道闸。

背景：2026-09-10 线上一个真实用户，楼盘、城市、平台、凭据、开关全设对了，
自动预订却一次都没触发。原因是这道闸只认 `notifier.has_channels`——而系统里有
**两条**投递路径：

    传统渠道  create_user_notifier 从 notification_channels 建 → has_channels
    推送      mcore.push.dispatch 独立走 APNs，只看 notifications_enabled

只用 App 的用户照常收房源推送，却永远开不了自动预订。这一整个文件在此之前
**没有任何测试**，所以这个洞没有任何东西挡着。
"""
import pytest

import monitor
from config import AutoBookConfig
from models import Listing
from users import UserConfig


class FakeNotifier:
    def __init__(self, has_channels: bool):
        self.has_channels = has_channels


class FakeStorage:
    def __init__(self, devices_by_user=None, raises=False):
        self._devices = devices_by_user or {}
        self._raises = raises
        self.calls: list[str] = []

    def get_active_devices_for_user(self, user_id):
        self.calls.append(user_id)
        if self._raises:
            raise RuntimeError("db down")
        return self._devices.get(user_id, [])


def _user(uid="u1", **kw):
    u = UserConfig(name=uid, id=uid)
    u.notifications_enabled = kw.pop("notifications_enabled", True)
    u.auto_book = AutoBookConfig(enabled=kw.pop("ab_enabled", True), dry_run=True)
    return u


def _listing(lid="l1", source="holland2stay"):
    return Listing(id=lid, name="n", status="Available to book", price_raw="€ 900,00",
                   available_from=None, features=[], url="u", city="Eindhoven",
                   source=source)


class TestCanReachUser:
    def test_classic_channels_are_enough(self):
        st = FakeStorage()
        assert monitor._can_reach_user(_user(), FakeNotifier(True), st)
        assert st.calls == [], "有传统渠道就不必再查设备"

    def test_an_active_device_is_enough(self):
        """这条是那个真实用户的情形：一台 iPhone，零传统渠道。"""
        st = FakeStorage({"u1": [{"id": 1, "platform": "ios"}]})
        assert monitor._can_reach_user(_user(), FakeNotifier(False), st)

    def test_neither_means_unreachable(self):
        st = FakeStorage({})
        assert not monitor._can_reach_user(_user(), FakeNotifier(False), st)

    def test_a_storage_failure_fails_closed(self):
        """查不出来 = 不确定能不能通知 = 不下单。这道闸的意义就是这个。"""
        st = FakeStorage(raises=True)
        assert not monitor._can_reach_user(_user(), FakeNotifier(False), st)

    def test_the_criterion_is_the_shared_helper(self):
        """判据必须走 get_active_devices_for_user，不能另写 SQL。

        它的条件里有一条容易抄漏的 expires_at；tools/backfill_push_optin.py 就
        栽在照抄 WHERE 上。这条断言「确实调了那个方法」。
        """
        st = FakeStorage({"u1": [{"id": 1}]})
        monitor._can_reach_user(_user(), FakeNotifier(False), st)
        assert st.calls == ["u1"]


class TestCandidateCollection:
    def _collect(self, user, notifier, storage, listing):
        return monitor._collect_booking_candidates(
            [listing], [], [listing], [(user, notifier)], storage)

    def test_a_push_only_user_gets_candidates(self):
        """回归：这正是线上那个用户的配置，此前一条候选都产生不了。"""
        u, l = _user(), _listing()
        st = FakeStorage({"u1": [{"id": 1, "platform": "ios"}]})
        cands, _ = self._collect(u, FakeNotifier(False), st, l)
        assert cands["u1"] == [l], "只用 App 的用户也该能自动预订"

    def test_an_unreachable_user_gets_none(self):
        u, l = _user(), _listing()
        cands, _ = self._collect(u, FakeNotifier(False), FakeStorage({}), l)
        assert cands["u1"] == [], "通知不到就不该替他下单——他不知道要去付款或取消"

    def test_notifications_off_still_blocks(self):
        """总开关关着仍然挡住，这道闸没被放松。"""
        u = _user(notifications_enabled=False)
        st = FakeStorage({"u1": [{"id": 1}]})
        cands, _ = self._collect(u, FakeNotifier(True), st, _listing())
        assert cands["u1"] == []

    def test_auto_book_off_still_blocks(self):
        u = _user(ab_enabled=False)
        st = FakeStorage({"u1": [{"id": 1}]})
        cands, _ = self._collect(u, FakeNotifier(True), st, _listing())
        assert cands["u1"] == []

    def test_reachability_is_computed_once_per_user_per_round(self):
        """不能放进逐条房源的循环——那是 用户数 × 房源数 次查询。"""
        u = _user()
        st = FakeStorage({"u1": [{"id": 1}]})
        listings = [_listing(f"l{i}") for i in range(5)]
        monitor._collect_booking_candidates(
            listings, [], listings, [(u, FakeNotifier(False))], st)
        assert st.calls == ["u1"], f"每轮每人只该查一次，实际 {len(st.calls)} 次"

    def test_a_source_without_a_booker_is_not_a_candidate(self):
        """source 级的闸仍在最前面，可达性翻不过它。"""
        u = _user()
        st = FakeStorage({"u1": [{"id": 1}]})
        cands, _ = self._collect(u, FakeNotifier(True), st, _listing(source="ourcampus"))
        assert cands["u1"] == []
