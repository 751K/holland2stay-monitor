"""邮件配额把通知丢掉时要告警，而且判据是**丢了几条**，不是「用满了没」。

起因（2026-08-25 生产）
----------------------
当天 26 条通知被 Resend 配额挡下（``fl1p`` 18 条、``qijunhuang1221`` 8 条），
全部是 per-user 触顶，全局那条一次没到。而这件事**只存在于日志里**——面板看
不出来、没有告警、用户那边更不知道。后果正是「用户以为没房源，其实是没发出去」。

为什么不拿「计数触顶」当判据
--------------------------
一个用户当天正好用到 20/20、之后再没有房源要推给他，那什么都没丢；被挡下来的
那一条才是他本该收到却没收到的。触顶是状态，拒发是事件，告警要报的是后者。

这条区分是本文件的重点，也是本项目反复修过的那类错——判据和被判的东西不是
一回事。``test_at_the_limit_but_nothing_dropped_stays_quiet`` 就是钉它的。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mcore import watchdog


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _alerts(storage):
    return [a for a in watchdog.evaluate(storage) if a.key.startswith("email_quota")]


@pytest.fixture
def limits(monkeypatch):
    import notifier
    monkeypatch.setattr(notifier, "RESEND_GLOBAL_DAILY_LIMIT", 80, raising=False)
    monkeypatch.setattr(notifier, "RESEND_PER_USER_DAILY_LIMIT", 20, raising=False)


class TestWhenItFires:
    def test_quiet_when_nothing_dropped(self, temp_db, limits):
        assert _alerts(temp_db) == []

    def test_at_the_limit_but_nothing_dropped_stays_quiet(self, temp_db, limits):
        """用满 20/20 却一条都没丢 → 不该响。

        这正是「触顶」和「丢了东西」的区别。拿触顶当判据的话，一个刚好用完额度
        又恰好没有新房源的用户，每天都会让 admin 收到一条无事发生的告警。
        """
        for _ in range(20):
            temp_db.record_email_send(_day(), "u1")
        assert _alerts(temp_db) == []

    def test_fires_once_something_is_actually_dropped(self, temp_db, limits):
        temp_db.record_email_reject(_day(), "u1")
        got = _alerts(temp_db)
        assert len(got) == 1
        assert got[0].key == "email_quota_user"
        assert got[0].level == watchdog.LEVEL_WARN

    def test_yesterdays_rejections_do_not_fire_today(self, temp_db, limits):
        """配额按 UTC 日切窗，昨天丢的今天已经无从补救，报了只是噪音。"""
        temp_db.record_email_reject("2020-01-01", "u1")
        assert _alerts(temp_db) == []


class TestLevels:
    def test_global_exhaustion_is_down_not_warn(self, temp_db, limits):
        """全局用尽 = 所有人的邮件都停了，和「某几个人超了」不是一个严重度。"""
        for _ in range(80):
            temp_db.record_email_send(_day(), "")
        temp_db.record_email_reject(_day(), "u1")
        got = _alerts(temp_db)
        assert len(got) == 1
        assert got[0].key == "email_quota_global"
        assert got[0].level == watchdog.LEVEL_DOWN

    def test_per_user_only_when_global_has_headroom(self, temp_db, limits):
        for _ in range(30):
            temp_db.record_email_send(_day(), "")
        temp_db.record_email_reject(_day(), "u1")
        assert _alerts(temp_db)[0].key == "email_quota_user"

    def test_never_both_at_once(self, temp_db, limits):
        """两条说的是同一件事，一起发会让恢复判定跟着糊。"""
        for _ in range(80):
            temp_db.record_email_send(_day(), "")
        temp_db.record_email_reject(_day(), "u1")
        assert len(_alerts(temp_db)) == 1


class TestBodyIsActionable:
    def _fire(self, temp_db, names=(("u1", "fl1p", 18), ("u2", "qijun", 8))):
        import json
        rows = []
        for uid, name, cnt in names:
            for _ in range(cnt):
                temp_db.record_email_reject(_day(), uid)
            rows.append({
                "id": uid, "name": name, "enabled": 1, "notifications_enabled": 1,
                "notification_channels_json": json.dumps(["email"]),
                "listing_filter_json": "{}", "auto_book_json": "{}",
            })
        temp_db.replace_user_config_rows(rows)
        return _alerts(temp_db)[0]

    def test_names_the_users_not_their_ids(self, temp_db, limits):
        """报 id 等于让人再去查一次库，那和翻日志没区别。"""
        body = self._fire(temp_db).body
        assert "fl1p" in body and "qijun" in body
        assert "u1" not in body and "u2" not in body

    def test_says_how_many_were_dropped(self, temp_db, limits):
        import re
        body = self._fire(temp_db).body
        assert re.search(r"今天已有 26 条通知发不出去", body), body

    def test_sorted_by_damage(self, temp_db, limits):
        body = self._fire(temp_db).body
        assert body.index("fl1p") < body.index("qijun"), "18 条的排在 8 条后面了"

    def test_unknown_user_is_still_counted(self, temp_db, limits):
        """用户删了不代表通知没丢——不能静默吞掉。"""
        temp_db.record_email_reject(_day(), "ghost")
        body = _alerts(temp_db)[0].body
        assert "ghost" in body

    def test_says_what_to_do(self, temp_db, limits):
        body = self._fire(temp_db).body
        assert "过滤条件" in body

    def test_global_alert_body_says_the_same_things(self, temp_db, limits):
        """两条告警各有一份正文，很容易只测其中一条。

        变异测试确认过：只改 DOWN 那条的 f-string，上面那些用例全绿——它们打的
        都是 WARN 分支。丢了多少、是谁、怎么办，两条都得说。
        """
        import re

        for _ in range(80):
            temp_db.record_email_send(_day(), "")
        alert = self._fire(temp_db)
        assert alert.key == "email_quota_global"
        assert re.search(r"今天已有 26 条通知发不出去", alert.body), alert.body
        assert "fl1p" in alert.body
        assert "RESEND_GLOBAL_DAILY_LIMIT" in alert.body


class TestRecordingPath:
    """拒发那一笔真的会被记下来——接线断了上面全部用例照样绿。"""

    def test_notifier_records_on_rejection(self, temp_db, monkeypatch):
        import notifier

        monkeypatch.setattr(notifier, "_open_storage_for_quota", lambda: temp_db)
        monkeypatch.setattr(temp_db, "close", lambda: None, raising=False)
        notifier.record_resend_rejected("u1")

        total, per_user = temp_db.email_reject_counts(_day())
        assert total == 1 and per_user == {"u1": 1}

    def test_rejection_site_calls_it(self):
        """AST 守卫：配额拒发那个分支里必须有这次记账。

        它和 return False 只隔一行，删掉不会有任何测试变红——除非在这里钉住。
        """
        import ast
        import inspect
        import textwrap

        import notifier

        src = textwrap.dedent(inspect.getsource(notifier.ResendNotifier._send))
        calls = {
            n.func.id for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "record_resend_rejected" in calls, (
            "拒发分支没记账，告警永远不会响")

    def test_send_and_reject_counters_are_separate(self, temp_db):
        """两个 scope 不能互相污染，否则「丢了几条」会被发送量顶上去。"""
        temp_db.record_email_send(_day(), "u1")
        temp_db.record_email_send(_day(), "u1")
        temp_db.record_email_reject(_day(), "u1")

        g, u = temp_db.get_email_send_counts(_day(), "u1")
        total, per_user = temp_db.email_reject_counts(_day())
        assert (g, u) == (2, 2)
        assert (total, per_user) == (1, {"u1": 1})

    def test_anonymous_rejection_counts_globally_only(self, temp_db):
        """验证邮件那类不归属用户，只进 global——加总 per_user 会漏掉它们。"""
        temp_db.record_email_reject(_day(), "")
        total, per_user = temp_db.email_reject_counts(_day())
        assert total == 1 and per_user == {}

    def test_pruning_covers_the_new_scopes(self, temp_db):
        temp_db.record_email_reject("2020-01-01", "u1")
        temp_db.prune_old_email_send_counters(keep_days=30)
        assert temp_db.email_reject_counts("2020-01-01") == (0, {})
