"""校验打不通的那一轮，不许把「已知不可订」翻成「可订」。

起因（2026-08-25，生产 ``xr_373301`` / Eindhoven Zernikestraat 1-222）
--------------------------------------------------------------------
Xior 的 WP feed 会把已经订走的单元继续挂着，所以每轮另抓一次 ``floorplans.aspx``
求权威可订集合。那个页面在 Cloudflare 后面，当天 51 次校验里 15 次没打通（403
八次、challenge 五次、代理 502 两次）。老逻辑是「拿不到就 fail-open，信 feed」，
于是同一条房源一天翻转 5 次：

    14:12  权威 []          → Occupied     ✅ 真的没了
    14:49  ⚠️ 校验打不通     → Available    ❌ 假
    14:55  权威 []          → Occupied     ✅
    15:02  权威 []          → Occupied     ✅
    15:08  ⚠️ 代理 502      → Available    ❌ 假（用户点进去是空的）

38 个用户 × 5 次 = 190 条通知，其中两次是纯噪音。**房源本身一次都没变**，变的
是校验请求通没通——判据和被判的东西不是一回事。

拦的方向只有一个
----------------
``status_unverified`` 说的是「这次报可订，但只有 feed 背书」。怎么处置取决于
手上有没有相反的证据，所以本文件的每条断言都在钉同一件事的不同侧面：

- 库里已是可订      → 不拦（fail-open 的本意就是别漏报）
- 库里 Occupied/Reserved → 拦（那是上一轮权威校验的结果，是实打实的证据）
- 库里没有这条      → 不拦（没有旧状态就没有相反证据）

**填补空白**和**推翻证据**是两回事，这条界线就是本文件存在的理由。
"""
from __future__ import annotations

import pytest

from models import Listing


def _l(id_="xr_1", status="Available to book", *, unverified=False, **kw):
    base = dict(
        id=id_, name=f"Zernikestraat {id_}", status=status,
        price_raw="€781", available_from="2030-01-01",
        features=["Area: 19 m²", "Tenant: student only"],
        url="https://x.securerc.co.uk/a.aspx", city="Eindhoven Zernikestraat",
        source="xior", status_unverified=unverified,
    )
    base.update(kw)
    return Listing(**base)


def _status(db, listing_id: str) -> str:
    row = db._conn.execute(
        "SELECT status FROM listings WHERE id=?", (listing_id,)).fetchone()
    return row["status"]


def _change_count(db, listing_id: str) -> int:
    return db._conn.execute(
        "SELECT COUNT(*) FROM status_changes WHERE listing_id=?",
        (listing_id,)).fetchone()[0]


# ── Listing 上的那一位 ────────────────────────────────────────────────

class TestTheFlagItself:
    def test_默认是关的(self):
        """存量三个 scraper 一个都不设它，默认必须是「有依据」。"""
        assert _l().status_unverified is False

    def test_不参与相等判断(self):
        """它是这一轮的取数元信息，不是房源快照的一部分。

        两条描述同一套房子的 Listing，不该因为「这轮校验通没通」而判为不等——
        diff 之外还有一堆地方拿 Listing 做集合/比较。
        """
        assert _l(unverified=True) == _l(unverified=False)


class TestScraperSetsIt:
    """``_to_listing``：只在「报可订」且「没拿到权威集合」时才置位。"""

    def _unit(self, status="Notice Unrented", fp_id=1109741):
        return {
            "apartmentId": "373301", "apartmentName": "1-222",
            "floorplanName": "Comfy", "floorplanId": fp_id,
            "sqm": 19, "minimumRent": 781, "maximumRent": 781, "deposit": 0,
            "availableDate": "2030-01-01", "unitStatus": status,
        }

    def _to(self, unit, ids):
        from datetime import date

        import scrapers.xior as x
        return x._to_listing(
            unit, display="Eindhoven Zernikestraat", building_url="",
            building_key="eindhoven zernikestraat",
            today=date(2029, 12, 1), bookable_floorplan_ids=ids,
        )

    def test_校验拿不到且报可订就置位(self):
        got = self._to(self._unit(), None)
        assert got.status == "Available to book"
        assert got.status_unverified is True

    def test_校验拿到了就不置位(self):
        got = self._to(self._unit(), {1109741})
        assert got.status == "Available to book"
        assert got.status_unverified is False

    def test_报_occupied_时不置位(self):
        """feed 说没有就是没有——负面结论不需要权威背书。

        这条要是反了，一条真的被订走的房源会被永远压在可订上。
        """
        got = self._to(self._unit(status="Rented"), None)
        assert got.status == "Occupied"
        assert got.status_unverified is False

    def test_被权威判掉之后不置位(self):
        """权威集合里没有它 → 降级为 Occupied，而且这个降级是有依据的。"""
        got = self._to(self._unit(), {999})
        assert got.status == "Occupied"
        assert got.status_unverified is False


# ── 存储层：拦哪个方向 ────────────────────────────────────────────────

class TestStorageHoldsTheFlip:
    def test_已知不可订不许翻成可订(self, temp_db):
        temp_db.diff([_l(status="Occupied")])
        assert _status(temp_db, "xr_1") == "Occupied"

        new, changes = temp_db.diff([_l(status="Available to book", unverified=True)])

        assert changes == [], "校验没通过却产生了状态变更事件——这就是那 190 条通知"
        assert new == []
        assert _status(temp_db, "xr_1") == "Occupied"
        assert _change_count(temp_db, "xr_1") == 0

    def test_reserved_也拦(self, temp_db):
        """Reserved 同样是「已知订不了」，别绕过去。"""
        temp_db.diff([_l(status="Reserved")])
        _, changes = temp_db.diff([_l(status="Available to book", unverified=True)])
        assert changes == []
        assert _status(temp_db, "xr_1") == "Reserved"

    def test_已经是可订的保持可订(self, temp_db):
        """这正是 fail-open 的本意：拦下来反而会造出一次假的「没了」。"""
        temp_db.diff([_l(status="Available to book")])
        _, changes = temp_db.diff([_l(status="Available to book", unverified=True)])

        assert changes == []
        assert _status(temp_db, "xr_1") == "Available to book"

    def test_新房源不拦(self, temp_db):
        """没有旧状态就没有相反证据，fail-open 填补空白是对的。"""
        new, changes = temp_db.diff([_l(status="Available to book", unverified=True)])

        assert [l.id for l in new] == ["xr_1"]
        assert _status(temp_db, "xr_1") == "Available to book"

    def test_有依据的可订照常翻转(self, temp_db):
        """回归守卫：别把整条通路一起焊死了。"""
        temp_db.diff([_l(status="Occupied")])
        _, changes = temp_db.diff([_l(status="Available to book")])

        assert [(c[1], c[2]) for c in changes] == [("Occupied", "Available to book")]
        assert _status(temp_db, "xr_1") == "Available to book"

    def test_降级方向不受影响(self, temp_db):
        """房子没了必须立刻报——这个方向从来不需要 floorplans.aspx 背书。"""
        temp_db.diff([_l(status="Available to book")])
        _, changes = temp_db.diff([_l(status="Occupied")])

        assert [(c[1], c[2]) for c in changes] == [("Available to book", "Occupied")]
        assert _status(temp_db, "xr_1") == "Occupied"

    def test_被压住时仍然更新非状态字段(self, temp_db):
        """压的是状态，不是整条记录——价格/日期该跟上还得跟上。"""
        temp_db.diff([_l(status="Occupied")])
        temp_db.diff([_l(status="Available to book", unverified=True,
                         price_raw="€999", available_from="2031-05-05")])

        row = temp_db._conn.execute(
            "SELECT status, price_raw, available_from FROM listings WHERE id=?",
            ("xr_1",)).fetchone()
        assert row["status"] == "Occupied"
        assert row["price_raw"] == "€999"
        assert row["available_from"] == "2031-05-05"

    def test_校验恢复后立刻翻转(self, temp_db):
        """压住不等于永久埋掉：下一轮校验通了就该正常报出来。

        重放 2026-08-25 那条房源的真实序列。
        """
        temp_db.diff([_l(status="Occupied")])
        for _ in range(3):                      # 连续三轮校验打不通
            _, changes = temp_db.diff([_l(status="Available to book", unverified=True)])
            assert changes == []

        _, changes = temp_db.diff([_l(status="Available to book")])   # 校验恢复
        assert [(c[1], c[2]) for c in changes] == [("Occupied", "Available to book")]

    def test_其它平台不受影响(self, temp_db):
        """H2S / OurDomain 从不置位，行为必须和改动前一模一样。"""
        h = _l(id_="h2s-1", status="Occupied", source="holland2stay")
        temp_db.diff([h])
        _, changes = temp_db.diff([_l(id_="h2s-1", status="Available to book",
                                      source="holland2stay")])
        assert [(c[1], c[2]) for c in changes] == [("Occupied", "Available to book")]


class TestPredicateGuardsThatDiffCannotReachToday:
    """直接打 ``_should_hold_unverified``，钉住两条 ``diff()`` 走不到的分支。

    变异测试暴露的：把这两个 guard 删掉，端到端一条都不红。原因是
    ``diff()`` 只在「库里已有这条」时才调它（``old_row`` 必然非空），而目前唯一
    会置位的 Xior 只在报可订时置位——两个 guard 都成了纵深防御。

    纵深防御不该是没测过的代码：它挡的正是「将来某个 scraper 用错这一位」，
    而那一天不会有人回来翻这段。所以这里绕开 diff 直接钉判据本身的契约。
    """

    def test_没有旧状态就不拦(self):
        """没有相反证据可推翻——填补空白和推翻证据是两回事。

        ``diff()`` 走不到（新房源另有分支），但别人直接调它时不能炸，也不能拦。
        """
        from mstorage._listings import ListingOps

        assert ListingOps._should_hold_unverified(
            None, _l(status="Available to book", unverified=True)) is False

    def test_这一位不许冻住降级(self):
        """本轮报的不是可订 → 一律不拦，哪怕这一位是开的。

        这个 guard 挡的是最坏情况：若哪天有 scraper 在报 Occupied 时也置位，
        少了它，一条真的被订走的房源会被**永久**压在可订上，再也降不回去。
        """
        from mstorage._listings import ListingOps

        # 旧状态**也不是**可订：这才是这个 guard 唯一起作用的格局。
        # 拿 old_row="Available to book" 试是试不出来的——最后一行本身就会
        # 返回 False，guard 删掉照样绿（变异测试 A4 就是这么活下来的）。
        for old_status, fresh in (("Occupied", "Reserved"),
                                  ("Reserved", "Occupied"),
                                  ("Occupied", "Occupied")):
            assert ListingOps._should_hold_unverified(
                {"status": old_status},
                _l(status=fresh, unverified=True)) is False, (
                f"{old_status} → {fresh} 被压住了——两个非可订状态之间的"
                "转移会被吞掉，房源再也降不回去")


class TestHoldIsVisible:
    def test_压住时写日志(self, temp_db, caplog):
        """压住一次翻转是看不见的操作——不留痕，下次只能靠猜。"""
        import logging

        temp_db.diff([_l(status="Occupied")])
        with caplog.at_level(logging.INFO, logger="mstorage._listings"):
            temp_db.diff([_l(status="Available to book", unverified=True)])

        msgs = [r.getMessage() for r in caplog.records]
        assert any("xr_1" in m and "Occupied" in m for m in msgs), (
            f"没留下痕迹，也没说压住之后维持的是什么状态: {msgs}")
