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


class TestStartupSummary:
    """启动摘要那几条警告——它们此前经常在说不成立的事。

    运维日志里的警告要是经常是假的，真的那条就没人看了。所以这里盯的不是「有没有
    警告」，而是「该不该有」。
    """

    def test_a_plaza_only_user_is_not_nagged_about_h2s(self):
        """线上真实案例：allowed_sources=['plaza'] 的用户每次启动吃两条
        「未填写 H2S 账号」——他根本不抓 H2S。"""
        u = _user()
        u.auto_book.listing_filter.allowed_sources = ["plaza"]
        u.auto_book.plaza_username = "zoeker"
        u.auto_book.plaza_password = "pw"
        u.auto_book.plaza_enabled = True
        assert monitor._autobook_targets(u) == ["plaza"]
        assert monitor._autobook_credential_gaps(u) == []

    def test_a_plaza_user_without_credentials_is_told_so(self):
        u = _user()
        u.auto_book.listing_filter.allowed_sources = ["plaza"]
        gaps = monitor._autobook_credential_gaps(u)
        assert len(gaps) == 1 and "Plaza" in gaps[0]

    def test_credentials_filled_but_switch_off_is_its_own_message(self):
        """填了凭据没勾开关，是最容易「以为在跑」的状态，要单独说。"""
        u = _user()
        u.auto_book.listing_filter.allowed_sources = ["plaza"]
        u.auto_book.plaza_username = "zoeker"
        u.auto_book.plaza_password = "pw"
        u.auto_book.plaza_enabled = False
        gaps = monitor._autobook_credential_gaps(u)
        assert len(gaps) == 1 and "开关" in gaps[0]

    def test_no_source_filter_means_every_supported_platform(self):
        u = _user()
        assert monitor._autobook_targets(u) == sorted(monitor._AUTO_BOOK_SOURCES)

    def test_a_source_without_a_booker_is_not_warned_about(self, monkeypatch):
        """allowed_sources 可以写还没实现 booker 的平台。据此警告「没填凭据」
        会让人去填一个填了也没用的东西。"""
        u = _user()
        u.auto_book.listing_filter.allowed_sources = ["ourcampus"]
        assert monitor._autobook_targets(u) == []
        assert monitor._autobook_credential_gaps(u) == []

    def test_h2s_gap_is_reported_when_it_is_targeted(self):
        u = _user()
        u.auto_book.listing_filter.allowed_sources = ["holland2stay"]
        gaps = monitor._autobook_credential_gaps(u)
        assert len(gaps) == 1 and "H2S" in gaps[0]

    def test_the_channel_warning_uses_the_same_criterion_as_the_gate(self):
        """摘要里那条「没有通知渠道」必须和真正的闸同一判据。

        不同判据的后果是：闸放行了，日志却在说这个用户配置不全（此前就是这样），
        或者反过来——日志说没问题，实际一条候选都不产生。
        """
        import inspect
        src = inspect.getsource(monitor.main_loop)
        assert "_can_reach_user(user, notifier, storage)" in src, \
            "摘要应当调 _can_reach_user，而不是自己判 notification_channels"
        assert "elif not user.notification_channels:" not in src, \
            "旧判据还在——它把只用 App 的用户误报成配置不全"
