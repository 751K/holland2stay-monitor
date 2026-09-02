"""房源 CRUD：diff、标记已通知、面板列表、filter 辅助查询。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import canonical_city
from models import (
    STATUS_AVAILABLE,
    Listing,
    canonical_feature,
    is_sentinel_available_from,
)

logger = logging.getLogger(__name__)

#: 「消失多久算不再可订」/「消失多久算彻底没了」的默认小时数。
#:
#: 四个平台、所有状态**统一一套**。曾经按 (source, 状态类) 分开配过，最后收掉
#: 了：那些差别描述的是「feed 会不会保留下架房源」，而实测下来四个平台的终态
#: 都是**从 feed 里消失**——只有 Xior 的 feed 里真有 Occupied，其余三个平台的
#: 终态基本全靠推。既然消失是共同的下架信号，就不该有四套判据。
DEFAULT_RESERVED_HOURS = 0.5
DEFAULT_OCCUPIED_HOURS = 2.0

#: 2 小时不是随手取的：**H2S 官方的付款限时就是 2 小时**。
#:
#: 这个数同时管住两种 Reserved，而且都成立：
#:
#: - 我们推出来的（消失了但还没到终态）——2 小时够久，足以排除单次抓取抖动；
#: - **平台自己报的**（有人下单未付款）——一条已经消失超过 2 小时的 Reserved，
#:   付款窗口必然已经关闭：要么付成了，要么作废了；作废的话它会以「可订」重新
#:   出现在 feed 里，而我们没看到它。所以判 Occupied 是对的。
#:
#: 把历史状态变更配对起来量「一条 Reserved 持续多久」，得到的间隔远大于 2 小时，
#: 看起来和付款限时矛盾——但那个量法是错的。它量的是「第一次看到它 Reserved」
#: 到「它以可订回来」的间隔，中间大部分时间它根本不在 feed 里
#: （``available_to_book`` 过滤器把 Reserved 挡掉了，只有状态刚翻转那一两轮
#: 因为索引没跟上才漏出来）。也就是说那不是付款窗口，是「预留 + 作废 + 重新
#: 上架」的整个周期。这类回归会产生 ``Occupied → 可订``，而那是**真的重新可订
#: 了**，本来就该通知。


#: 城市筛选统一走这个表达式，而不是直接读 city_normalized。
#:
#: 归一值由写入路径填、启动时回填。但只要有哪条写入路径漏了它，那条房源就会
#: 从所有城市筛选里**整个消失**——查不到、也不报错。退回原始 city 至少让它按
#: 自己的字面值可查；对 H2S（占绝大多数）来说那本来就是正确的城市名。
_CITY_EXPR = "COALESCE(NULLIF(city_normalized,''), city)"
_CITY_EXPR_L = "COALESCE(NULLIF(l.city_normalized,''), l.city)"



# ── 粘性 feature ────────────────────────────────────────────────────
#
# 有些 feature 不是列表抓取产出的，而是**另发一次详情请求**补齐的
# （scrapers/holland2stay.py 的详情补齐：Building / Tenant / Neighborhood /
# MinIncome，来自 GetProductDetail —— 白名单主查询的字段集里没有它们）。
#
# 补齐是按预算 + 限速跨轮渐进的，还会因 429 中断，进程重启后缓存也清零。
# 于是同一条房源在「补到了」和「还没补到」之间来回：而 diff() 每轮整体覆盖
# features，没补到的那轮就会把上一轮存好的值**抹掉**。
# 实测表现：部署后仪表盘的「楼盘」列大面积变回 '—'，90 分钟后才慢慢长回来。
#
# 判据：对这些 key 而言，**抓取侧没给 ≠ 上游没有了**，只是这轮没去问。
# 所以新值缺失时保留旧值；新值存在时正常覆盖（上游真改了要能跟上）。
#
# 只对这几个 key 生效。普通字段（Type / Area / Status 派生的那些）仍然整体覆盖
# ——那些是每轮都拿得到的，缺失就是真的没有了。
_STICKY_FEATURE_KEYS = frozenset({
    "Building", "Tenant", "Neighborhood", "MinIncome",
})


def _merge_sticky_features(
    fresh_features: list[str],
    old_features_json: "str | None",
) -> list[str]:
    """新 features 缺了粘性 key 时，从旧值里补回来。"""
    if not old_features_json:
        return list(fresh_features)

    fresh_keys = {
        f.split(":", 1)[0].strip() for f in fresh_features if ":" in f
    }
    missing = set(_STICKY_FEATURE_KEYS) - fresh_keys
    if not missing:
        return list(fresh_features)

    try:
        old = json.loads(old_features_json)
    except (json.JSONDecodeError, TypeError):
        return list(fresh_features)
    if not isinstance(old, list):
        return list(fresh_features)

    merged = list(fresh_features)
    for item in old:
        if not isinstance(item, str) or ":" not in item:
            continue
        key = item.split(":", 1)[0].strip()
        if key in missing:
            merged.append(item)
            missing.discard(key)
    return merged


def _sticky_available_from(fresh: "str | None", old: "str | None") -> "str | None":
    """入住日期是粘性的：抓不到的时候保留上一次抓到的真值。

    为什么需要
    ----------
    H2S 的房源在「可订」阶段有真实的 available_from，一旦转成 Reserved，上游
    的 next_contract_startdate 就变成 2050-01-01 哨兵——scrapers/holland2stay.py
    认出哨兵后返回 None，而这里原来是无条件写回库，于是那个真日期被 None 冲掉，
    界面上只剩一个「—」。

    2026-08-28 线上实测：H2S 445 条里 76 条没有日期，其中 Reserved 47 条中占了
    45 条；而 Available to book 的 19 条一条都不缺。23 条能从 status_changes 里
    证明它们曾经是「可订」——也就是说那个日期确实存在过，是被我们自己删掉的。

    判据
    ----
    只在**新值为空**时保留旧值。上游给了新日期就用新的——修正、重新放盘都应该
    覆盖得了。空值的含义是「这一轮没拿到」，不是「这个房子没有入住日」，两者不
    是一回事。

    哨兵在两侧都按「空」处理。scrapers 层已经认过一次，这里是落库前的最后一道：
    没有它的话，抓取层的过滤哪天回退，哨兵进了库就会被粘性逻辑当成「已知」永远
    锁住——那正是这个函数本来要防的事情的反面。线上 2026-08-28 就有 72 条
    ``2050-01-01`` 留在库里，是加过滤之前写进去的，之后再没被抓到过。

    对四个平台都生效。今天只有 H2S 会产生空值（其余三个线上 0 条），但「未知不
    该覆盖已知」和平台无关，写成 H2S 专属反而是在赌上游行为不变。
    """
    fresh = (fresh or "").strip()
    if fresh and not is_sentinel_available_from(fresh):
        return fresh
    old = (old or "").strip()
    if not old or is_sentinel_available_from(old):
        return None
    return old


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days(minutes: float) -> float:
    """分钟 → 儒略日的天数差，喂给 ``julianday('now') - ?``。"""
    return float(minutes) / 1440.0


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _booking_hold_minutes() -> int:
    raw = os.environ.get("BOOKING_STATUS_HOLD_MINUTES", "120")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 120


class ListingOps:
    """依赖 self._conn / self._tz（由 StorageBase.__init__ 提供）。"""

    # ── diff（核心）──────────────────────────────────────────────────

    def diff(
        self, fresh: list[Listing]
    ) -> tuple[list[Listing], list[tuple[Listing, str, str]]]:
        now = _now_iso()
        now_dt = datetime.now(timezone.utc)
        new_listings: list[Listing] = []
        status_changes: list[tuple[Listing, str, str]] = []

        cur = self._conn.cursor()
        with self._conn:
            ids = [l.id for l in fresh]
            existing: dict[str, dict] = {}
            if ids:
                placeholders = ",".join("?" * len(ids))
                rows = cur.execute(
                    f"""SELECT id, status, status_is_inferred, status_hold_until,
                               features, available_from
                        FROM listings WHERE id IN ({placeholders})""",
                    ids,
                ).fetchall()
                existing = {r["id"]: dict(r) for r in rows}

            for listing in fresh:
                old_row = existing.get(listing.id)
                old_status = old_row["status"] if old_row is not None else None
                # 抓取产不出的「粘性」字段要保留，见 _merge_sticky_features
                features_json = json.dumps(
                    _merge_sticky_features(
                        listing.features,
                        old_row.get("features") if old_row else None,
                    ),
                    ensure_ascii=False,
                )
                # 同理：抓不到日期时保留上一次的真值，见 _sticky_available_from。
                #
                # 就地改 listing 而不是另起一个局部变量：diff() 返回的就是这批
                # 对象，monitor 拿它们去发通知（notifier.py 读
                # listing.available_from）。同一个值留两个名字，迟早只同步一处，
                # 届时表现是「网页上有日期、推送里是 ?」——同一件事两个说法。
                listing.available_from = _sticky_available_from(
                    listing.available_from,
                    old_row.get("available_from") if old_row else None,
                )

                if old_status is None:
                    # P0: 写入 source 字段。老的 INSERT 不传 source 时
                    # 走 schema 默认值 'holland2stay'，但 Listing.source 已
                    # 在 scrapers 层强制赋值，这里直接传，更显式。
                    cur.execute(
                        """INSERT INTO listings
                           (id, name, status, price_raw, available_from,
                            features, url, city, first_seen, last_seen, notified, last_status,
                            source, city_normalized)
                           VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
                        (
                            listing.id, listing.name, listing.status,
                            listing.price_raw, listing.available_from,
                            features_json,
                            listing.url, listing.city, now, now, listing.status,
                            listing.source, canonical_city(listing.city or ""),
                        ),
                    )
                    new_listings.append(listing)
                else:
                    hold_unverified = self._should_hold_unverified(old_row, listing)
                    if hold_unverified:
                        # 压住一次状态翻转是**看不见**的操作——日志里不留痕，
                        # 下次再问「为什么这条没报可订」就只能靠猜。
                        logger.info(
                            "[%s] %s 报可订但本轮没通过权威校验，维持 %s"
                            "（不翻转、不通知）",
                            listing.source, listing.id, old_status,
                        )
                    if (hold_unverified
                            or self._should_keep_booking_hold(old_row, listing.status, now_dt)):
                        cur.execute(
                            """UPDATE listings
                               SET name=?, price_raw=?, available_from=?,
                                   features=?, last_seen=?, source=?
                               WHERE id=?""",
                            (
                                listing.name, listing.price_raw, listing.available_from,
                                features_json,
                                now, listing.source, listing.id,
                            ),
                        )
                        continue

                    # 来自 API 的真实数据：复位 status_is_inferred=0，
                    # 撤销之前 mark_stale_listings 可能打过的"推测"标记。
                    # source 在 UPDATE 时也带上——理论上 listing 的 source 永不变，
                    # 但显式写入更稳（防止历史数据 backfill 默认值不一致）。
                    cur.execute(
                        """UPDATE listings
                           SET name=?, status=?, price_raw=?, available_from=?,
                               features=?, last_seen=?, last_status=?,
                               status_is_inferred=0, status_hold_until='', source=?
                           WHERE id=?""",
                        (
                            listing.name, listing.status, listing.price_raw,
                            listing.available_from,
                            features_json,
                            now, listing.status, listing.source, listing.id,
                        ),
                    )
                    if old_status != listing.status:
                        cur.execute(
                            """INSERT INTO status_changes
                               (listing_id, old_status, new_status, changed_at)
                               VALUES (?,?,?,?)""",
                            (listing.id, old_status, listing.status, now),
                        )
                        status_changes.append((listing, old_status, listing.status))

        return new_listings, status_changes

    @staticmethod
    def _should_hold_unverified(old_row: dict | None, listing: Listing) -> bool:
        """校验不可用的这一轮，不许把「已知不可订」翻成「可订」。

        为什么只拦这一个方向
        --------------------
        ``status_unverified`` 说的是「这次报可订，但只有上游 feed 背书」。怎么
        处置取决于**我们手上有没有相反的证据**：

        - 库里已经是可订 → 保持可订。这正是 fail-open 的本意（别漏报真房源），
          拦下来反而会造出一次假的「没了」。
        - 库里是 Occupied / Reserved → **压住**。这个状态是上一轮权威校验的结果，
          是实打实的证据；拿一份已知会滞后的 feed 去推翻它，等于让房源状态取决于
          「校验请求通没通」，而不是取决于房子。
        - 库里没这条（新房源）→ 不拦。没有旧状态就没有相反证据，fail-open
          填补空白是对的；这跟推翻证据是两回事。

        代价是真有新一轮上架、而校验恰好挂了时，播报会晚一到两轮（约 6–12
        分钟）。对照组是 2026-08-25：xr_373301 一天翻转 5 次、发出 190 条通知，
        其中两次「又可订」纯粹是校验打不通造出来的，点进去是空的。

        不写 status_changes、不发通知——被压住的这一轮**什么都没变**，
        走的是和 ``_should_keep_booking_hold`` 同一条「只更新非状态字段」的路。
        """
        if not getattr(listing, "status_unverified", False):
            return False
        if not old_row:
            return False
        if (listing.status or "").strip().lower() != STATUS_AVAILABLE:
            return False
        return (old_row.get("status") or "").strip().lower() != STATUS_AVAILABLE

    @staticmethod
    def _should_keep_booking_hold(
        old_row: dict | None,
        fresh_status: str,
        now: datetime,
    ) -> bool:
        if not old_row:
            return False
        if old_row.get("status") != "Reserved":
            return False
        if int(old_row.get("status_is_inferred") or 0) != 1:
            return False
        if fresh_status.lower() != "available to book":
            return False
        hold_until = _parse_iso(old_row.get("status_hold_until"))
        if hold_until is None:
            return False
        if hold_until.tzinfo is None:
            hold_until = hold_until.replace(tzinfo=timezone.utc)
        return hold_until > now

    def mark_listing_reserved_after_booking(self, listing_id: str) -> bool:
        """自动预订成功后，把本地状态暂时保持为 Reserved。"""
        now_dt = datetime.now(timezone.utc)
        hold_until = now_dt + timedelta(minutes=_booking_hold_minutes())
        with self._conn:
            cur = self._conn.execute(
                """UPDATE listings
                   SET status='Reserved',
                       last_status='Reserved',
                       status_is_inferred=1,
                       status_hold_until=?,
                       last_seen=?
                   WHERE id=?""",
                (hold_until.isoformat(), now_dt.isoformat(), listing_id),
            )
        return bool(cur.rowcount)

    # ── 通知回执 ────────────────────────────────────────────────────

    # ── 未投递事件的重放 ────────────────────────────────────────────
    #
    # ``notified`` 曾经是**只写**的：全仓库没有任何 SELECT 读它，只有两条
    # UPDATE 把它置 1。于是它看起来像一本 at-least-once 的账，实际没人对账。
    #
    # 而 ``diff()`` 检测变更的**副作用就是覆盖掉用来检测的那个旧状态**。两者
    # 叠加的后果：diff 提交之后、通知发出去之前进程死掉（崩溃 / 部署 / OOM），
    # 那批事件永久丢失——下一轮 diff 看到 old_status == new_status，什么也不产出。
    # 触发条件很日常：2026-08-20 一天之内部署了 12 次。
    #
    # 下面这组方法让 notified=0 真的有人读。三个必须同时成立的前提见
    # tests/test_notification_replay.py::TestNoNotificationStorm。

    #: 重放的时间窗（分钟）。超窗的不再发——房子多半已经没了，推过去只是打扰。
    PENDING_WINDOW_MINUTES = 90

    #: 单轮重放条数上限。异常情况下（比如迁移出岔子）不至于一次性炸开。
    PENDING_BATCH_LIMIT = 50

    def _row_to_listing(self, r) -> Listing:
        """把 listings 行还原成能直接喂给 notifier 的 Listing。

        必须是完整对象而不是半成品 row：通知模板要读 name / status / price /
        url / features，缺一条就会在**重放路径**上炸——而那条路径平时不跑，
        炸了也不会有人立刻发现。
        """
        try:
            feats = json.loads(r["features"]) if r["features"] else []
        except (json.JSONDecodeError, TypeError):
            feats = []
        keys = r.keys()
        return Listing(
            id=r["id"],
            name=r["name"] or r["id"],
            status=r["status"] or "",
            price_raw=r["price_raw"],
            available_from=r["available_from"],
            features=feats if isinstance(feats, list) else [],
            url=r["url"] or "",
            city=r["city"] or "",
            source=(r["source"] if "source" in keys else "") or "",
            sku=(r["sku"] if "sku" in keys else "") or "",
        )

    def pending_new_listings(
        self,
        within_minutes: int | None = None,
        limit: int | None = None,
    ) -> list[Listing]:
        """还没走完通知阶段的新房源（时间窗内、按条数封顶）。

        ⚠️ 比较必须走 ``julianday``，不能拿字符串比。
        ``first_seen`` 存的是 ``_now_iso()`` 的带时区 ISO
        （``2026-09-02T10:30:06.916515+00:00``），而 ``datetime('now','-N minutes')``
        返回空格分隔、无时区的 ``2026-09-02 09:00:06``。直接比字符串时第 10 位是
        ``T``(0x54) 对空格(0x20)，**同一 UTC 日期内恒为真**——90 分钟的窗口退化成
        「当天 UTC 零点起」，实测 9 小时前的行仍判为窗口内，跨天才失效。

        后果不是少推，是**多推**：投递失败后的重放会把最多一整天的旧事件重新捞
        出来。配套的 ``retire_stale_pending`` 用的是同一个比较的补集（``<=``），
        于是当天的积压永远归档不掉，0 池只增不减。

        `mstorage/_map_calendar.py` 对 ``last_seen`` 早就踩过同一个坑并改用
        julianday，只是这几处没跟着改。
        """
        win = self.PENDING_WINDOW_MINUTES if within_minutes is None else within_minutes
        lim = self.PENDING_BATCH_LIMIT if limit is None else limit
        rows = self._conn.execute(
            """SELECT * FROM listings
                WHERE notified = 0
                  AND julianday(first_seen) > julianday('now') - ?
                ORDER BY first_seen ASC LIMIT ?""",
            (_days(win), int(lim)),
        ).fetchall()
        return [self._row_to_listing(r) for r in rows]

    def pending_status_changes(
        self,
        within_minutes: int | None = None,
        limit: int | None = None,
    ) -> list[tuple[Listing, str, str]]:
        """还没走完通知阶段的状态变更，形状与 ``diff()`` 的第二个返回值一致。"""
        win = self.PENDING_WINDOW_MINUTES if within_minutes is None else within_minutes
        lim = self.PENDING_BATCH_LIMIT if limit is None else limit
        rows = self._conn.execute(
            """SELECT sc.listing_id, sc.old_status, sc.new_status, l.*
                FROM status_changes sc
                JOIN listings l ON l.id = sc.listing_id
                WHERE sc.notified = 0
                  AND julianday(sc.changed_at) > julianday('now') - ?
                ORDER BY sc.changed_at ASC LIMIT ?""",
            (_days(win), int(lim)),
        ).fetchall()
        return [
            (self._row_to_listing(r), r["old_status"] or "", r["new_status"] or "")
            for r in rows
        ]

    def retire_stale_pending(self, within_minutes: int | None = None) -> int:
        """把超出时间窗的积压直接标记成已处理，返回条数。

        没有这一步的话 0 池只增不减：超窗的行永远选不中、也永远不被清掉，
        每轮都要白扫一遍。
        """
        win = self.PENDING_WINDOW_MINUTES if within_minutes is None else within_minutes
        with self._conn:
            c1 = self._conn.execute(
                """UPDATE listings SET notified = 1
                    WHERE notified = 0
                      AND julianday(first_seen) <= julianday('now') - ?""",
                (_days(win),),
            ).rowcount
            c2 = self._conn.execute(
                """UPDATE status_changes SET notified = 1
                    WHERE notified = 0
                      AND julianday(changed_at) <= julianday('now') - ?""",
                (_days(win),),
            ).rowcount
        return (c1 or 0) + (c2 or 0)

    def mark_notified(self, listing_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE listings SET notified=1 WHERE id=?", (listing_id,)
            )

    def mark_notified_batch(self, listing_ids: list[str]) -> None:
        if not listing_ids:
            return
        with self._conn:
            placeholders = ",".join("?" for _ in listing_ids)
            self._conn.execute(
                f"UPDATE listings SET notified=1 WHERE id IN ({placeholders})",
                listing_ids,
            )

    def mark_status_change_notified(self, listing_id: str) -> None:
        with self._conn:
            self._conn.execute(
                """UPDATE status_changes SET notified=1
                   WHERE listing_id=? AND notified=0""",
                (listing_id,),
            )

    def mark_status_change_notified_batch(self, listing_ids: list[str]) -> None:
        if not listing_ids:
            return
        with self._conn:
            placeholders = ",".join("?" for _ in listing_ids)
            self._conn.execute(
                f"""UPDATE status_changes SET notified=1
                   WHERE listing_id IN ({placeholders}) AND notified=0""",
                listing_ids,
            )

    # ── 状态收敛：从 feed 里消失 = 唯一的下架信号 ────────────────────
    #
    # 四个平台的终态都是**从 feed 里消失**，而不是被报成 Occupied（只有 Xior
    # 的 feed 里真有 Occupied，且覆盖不全）。平台不会说「这套没了」，只是不再
    # 返回它。所以「多久没见到」是我们唯一的判据。
    #
    # 两段
    # ----
    # 消失 ``reserved_hours``  → Reserved（推测）：够强到不该再说「可订」
    # 消失 ``occupied_hours``  → Occupied（推测）：终态
    #
    # 中间那一站是有意的。「没见到了」够强到不该再当可订，但不足以断言
    # 「已出租」，直接跳终态是把推断当事实：判错时房源从面板上彻底消失，等
    # feed 恢复再出现会产生 ``Occupied → 可订``，用户收到一批假的「重新上架」。
    # 落在 Reserved 上代价小得多——它本来就是过渡态，``Reserved → 可订`` 在
    # H2S 上是最常见的迁移之一，语义就是「别人的预留没成」。而且
    # ``Listing.is_available`` 不含 Reserved，「不再显示为可订」这件正事第一段
    # 就办到了。
    #
    # 不写 status_changes：推测转换不触发通知 / auto_book。
    _STALE_AVAILABLE_STATUSES = (
        "Available to book",
        "Available in lottery",
        "Unknown",
    )
    _STALE_INTERMEDIATE_STATUS = "Reserved"

    def mark_stale_listings(
        self,
        cities: Optional[list[str]] = None,
        source_city_pairs: Optional[list[tuple[str, str]]] = None,
        monitored_pairs: Optional[list[tuple[str, str]]] = None,
        orphan_days: int = 30,
        reserved_hours: float = DEFAULT_RESERVED_HOURS,
        occupied_hours: float = DEFAULT_OCCUPIED_HOURS,
        full_lifecycle_sources: Optional[set[str]] = None,
    ) -> int:
        """按「多久没见到」收敛 listing 状态。

        三条路径
        --------
        1. **消失 ``reserved_hours``** → ``Reserved`` + ``status_is_inferred=1``。
           范围由 ``cities`` / ``source_city_pairs`` 限定——传的是"本轮完整扫描
           成功的城市"，只有确认扫全了才敢说"没见到 = 没了"。

           ``full_lifecycle_sources`` 里的 source **跳过这一步**，直接走第 2 条：
           它们的 feed 已经覆盖了「已预留」，消失因此不再有歧义（详见该参数）。

        2. **Reserved 消失 ``occupied_hours``** → ``Occupied``（终态）。
           不分「我们推的」和「平台报的」——H2S 官方付款限时就是 2 小时，
           一条消失超过 2 小时的 Reserved，付款窗口必然已经关闭（详见模块顶部
           ``DEFAULT_OCCUPIED_HOURS`` 的说明）。此前平台报的 Reserved 不参与
           收敛，一批消失了好几个月的记录因此永远卡着，现在也走这条路清掉。

        3. **已经掉出监控范围的 (source, city)**：按 ``orphan_days`` 老化。
           第 1 条的范围限定有个副作用：一旦某个城市被移出监控，它就再也不会
           出现在"完整扫描"名单里，于是**永远不会被收敛**。改一次监控城市就攒
           一批鬼影，最后一次见到已是几个月前，却还挂着"可订"。

           这条路径收敛 ``status != 'Occupied'`` 的全部状态：一个已经完全不再
           观察的城市，我们手上任何非 Occupied 的状态都同样无从核实。

        执行顺序是**第 2 条先跑**。反过来的话，一条消失很久的房源会在同一次
        调用里被连改两次（先 Reserved 再 Occupied），返回的行数把它算两遍，
        「本轮收敛了几条」就不再等于「几条房源变了状态」。

        Parameters
        ----------
        cities : 限定当前仍在监控的城市；传入空列表时不更新任何 listing
        source_city_pairs : 限定 source + city 组合，用于多源同名城市隔离
        monitored_pairs : **当前配置里**全部 (source, city) 目标。注意这和
            ``source_city_pairs`` 不是一回事：后者是"本轮扫全了的"，前者是
            "配置里有的"。分片和节流会让一个正常监控的城市这轮不出现，拿它
            当孤儿判据会误杀。传 None / 空表示不知道监控范围 → **跳过孤儿
            收敛**（fail-open：宁可留着鬼影，也不能因为一次配置读取失败就
            把整库判死）。
        full_lifecycle_sources : feed 已覆盖「已预留」状态的 source 集合。

            **「从 feed 里消失」的含义取决于 feed 覆盖了什么。** feed 只含
            可订/抽签时，消失是有歧义的——可能被人下单了，也可能彻底没了，所以
            先推 Reserved 留出付款窗口。feed 也含 Reserved 时，消失就没有歧义：
            它已经掉出我们跟踪的全部状态。此时再推一次 Reserved，是凭空造一个
            平台从没说过的状态，还会把 ``status_is_inferred=1`` 打在一条本可以
            如实上报的房源上。

            由 ``Config.sources_with_full_lifecycle()`` 从实际配置推出，不写死
            平台名——``AVAILABILITY_FILTERS`` 是可改的。

        orphan_days : 掉出监控范围后的宽限期；默认 30 天。取长是为了防误伤
            ——临时关一天再打开的城市不该被判死。
        reserved_hours / occupied_hours : 见模块顶部 ``DEFAULT_*`` 的说明。

        Returns
        -------
        本次实际更新的行数（幂等：到终态的不会被重复命中）
        """
        city_filter = [c for c in (cities or []) if c]
        source_city_filter = [
            (source, city)
            for source, city in (source_city_pairs or [])
            if source and city
        ]
        if (
            (cities is not None or source_city_pairs is not None)
            and not city_filter
            and not source_city_filter
        ):
            return 0

        now = datetime.now(timezone.utc)

        def _cutoff(hours: float) -> str:
            # 下限 15 分钟：配成 0 会把整个监控范围里的房源当场判死，而这种
            # 配置错误在日志里看不出来——只会表现成「房源突然全没了」。
            return (now - timedelta(hours=max(0.25, float(hours)))).isoformat()

        # 范围子句：城市名 和 / 或 (source, city) 组合。
        scope_clauses: list[str] = []
        scope_params: list = []
        if city_filter:
            scope_clauses.append("city IN (" + ",".join("?" * len(city_filter)) + ")")
            scope_params.extend(city_filter)
        if source_city_filter:
            pair_clause = " OR ".join(
                "(source = ? AND city = ?)" for _ in source_city_filter
            )
            scope_clauses.append(f"({pair_clause})")
            for source, city in source_city_filter:
                scope_params.extend([source, city])
        scope_sql = (
            " AND (" + " OR ".join(scope_clauses) + ")" if scope_clauses else ""
        )

        def _run(target: str, where: str, prm: list) -> int:
            cur = self._conn.execute(
                f"UPDATE listings "
                f"SET status='{target}', last_status='{target}', status_is_inferred=1 "
                f"WHERE {where}",
                prm,
            )
            return cur.rowcount or 0

        avail_placeholders = ",".join("?" * len(self._STALE_AVAILABLE_STATUSES))

        with self._conn:
            # ① Reserved 消失够久 → 终态。先跑，理由见上面的「执行顺序」。
            #    不分是谁说的：官方付款限时 2 小时，消失超过它就必然已经落定。
            n = _run(
                "Occupied",
                f"last_seen < ? AND status = ?{scope_sql}",
                [_cutoff(occupied_hours), self._STALE_INTERMEDIATE_STATUS,
                 *scope_params],
            )

            # ② 消失了 → 不再当可订。走哪条取决于**该 source 的 feed 覆盖了
            #    什么**，见 full_lifecycle_sources 的说明。
            full = sorted(s for s in (full_lifecycle_sources or ()) if s)
            if full:
                src_ph = ",".join("?" * len(full))
                # ②a feed 含 Reserved：消失没有歧义，直接判终态。中间再插一个
                #     推测的 Reserved，是凭空造一个平台从没说过的状态。
                n += _run(
                    "Occupied",
                    f"last_seen < ? AND status IN ({avail_placeholders}) "
                    f"AND source IN ({src_ph}){scope_sql}",
                    [_cutoff(occupied_hours), *self._STALE_AVAILABLE_STATUSES,
                     *full, *scope_params],
                )
                not_full = f" AND source NOT IN ({src_ph})"
                not_full_params = list(full)
            else:
                not_full, not_full_params = "", []

            # ②b feed 只含可订/抽签：消失是有歧义的，先推 Reserved 留出付款窗口。
            n += _run(
                "Reserved",
                f"last_seen < ? AND status IN ({avail_placeholders})"
                f"{not_full}{scope_sql}",
                [_cutoff(reserved_hours), *self._STALE_AVAILABLE_STATUSES,
                 *not_full_params, *scope_params],
            )

            # ③ 孤儿：已经掉出监控范围的，按更长的宽限期直接判终态。
            monitored = [
                (source, city)
                for source, city in (monitored_pairs or [])
                if source and city
            ]
            if monitored:
                not_monitored = " AND ".join(
                    "NOT (source = ? AND city = ?)" for _ in monitored
                )
                prm: list = [
                    (now - timedelta(days=max(1, int(orphan_days)))).isoformat()
                ]
                for source, city in monitored:
                    prm.extend([source, city])
                n += _run(
                    "Occupied",
                    f"last_seen < ? AND status <> 'Occupied' AND {not_monitored}",
                    prm,
                )
        return n

    # ── 基础查询 ────────────────────────────────────────────────────

    def detail_feature_snapshot(self, source: str) -> dict[str, dict[str, str]]:
        """库里已有的补齐值：``{listing_id: {key: value}}``，只含粘性那几个 key。

        抓取侧在**进程启动时**拿它回填详情缓存。没有这一步的话，重启后
        ``_DETAIL_CACHE`` 归零，几十条早就补齐过的房源会被重新问一遍详情——
        而上游的限流是按速率算的，那一串请求撞出 429 之后，本轮真正需要补齐的
        **新房源**就轮不上了。2026-08-25 实测：部署两分钟后进来的
        ``beukenlaan-143-093`` 就这么少了 Building/Tenant，而它带着残缺的
        feature 直接发出了通知，勾了租客条件的用户被 fail-closed 拒掉，且补齐
        之后不会补发。

        只回填**有值**的 key。详情里本来就没有的（空结果）不进快照，重启后照旧
        去问一次——那类房源很少，代价小于「永远不再核实」。
        """
        out: dict[str, dict[str, str]] = {}
        try:
            rows = self._conn.execute(
                "SELECT id, features FROM listings "
                "WHERE source = ? AND features IS NOT NULL AND features <> ''",
                (source,),
            ).fetchall()
        except Exception:
            logger.debug("读取 detail_feature_snapshot 失败（已忽略）", exc_info=True)
            return out

        for r in rows:
            try:
                feats = json.loads(r["features"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(feats, list):
                continue
            got: dict[str, str] = {}
            for item in feats:
                if not isinstance(item, str) or ":" not in item:
                    continue
                key, value = item.split(":", 1)
                key, value = key.strip(), value.strip()
                if key in _STICKY_FEATURE_KEYS and value:
                    got[key] = value
            if got:
                out[r["id"]] = got
        return out

    def get_distinct_cities(self) -> list[str]:
        """筛选下拉用的城市列表——归一后的值。

        用原始 city 的话，下拉里会同时出现「Amsterdam」和「Amsterdam Diemen」
        「Amsterdam Naritaweg」，看着像三个城市，实际是一个城市加两个楼盘；
        用户勾了其中一个就漏掉另外两个。
        """
        rows = self._conn.execute(
            f"SELECT DISTINCT {_CITY_EXPR} AS c FROM listings "
            f"WHERE c != '' ORDER BY c"
        ).fetchall()
        return [r[0] for r in rows]

    def get_distinct_sources(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT source FROM listings WHERE source != '' ORDER BY source"
        ).fetchall()
        return [r[0] for r in rows]

    def count_all(self, city: Optional[str] = None) -> int:
        if city:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM listings WHERE {_CITY_EXPR} = ?",
                (canonical_city(city),),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM listings").fetchone()
        return row[0] if row else 0

    def get_listing(self, listing_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM listings WHERE id=?", (listing_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── 面板查询 ────────────────────────────────────────────────────

    def get_all_listings(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        city: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        q = "SELECT * FROM listings WHERE 1=1"
        params: list = []
        if status:
            q += " AND status = ?"
            params.append(status)
        if search:
            # 同时匹配 `name`（地址）和 `features` 里 "Building: ..." 这一项的楼盘名。
            # features 是 JSON 数组形如 ["Type: Studio", "Building: The Docks", ...]，
            # LIKE '%Building: %<search>%' 受限在 building 条目附近，避免误命中
            # 其它特征里的同名字符串（如 Neighborhood）。SQLite LIKE 对 ASCII
            # 默认不区分大小写，跟原有 name LIKE 行为一致，无须 COLLATE。
            q += " AND (name LIKE ? OR features LIKE ?)"
            params.append(f"%{search}%")
            params.append(f"%Building: %{search}%")
        if city:
            # 按归一后的城市筛：传进来的可能是城市名，也可能是存量配置里的
            # 楼盘名，两边都过一遍 canonical_city 才能对上。
            q += f" AND {_CITY_EXPR} = ?"
            params.append(canonical_city(city))
        if source:
            q += " AND source = ?"
            params.append(source)
        q += " ORDER BY first_seen DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(q, params).fetchall()]

    def get_recent_changes(
        self, hours: int = 48, city: Optional[str] = None
    ) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        if city:
            rows = self._conn.execute(
                f"""SELECT sc.*, l.name, l.url, l.price_raw, l.source
                   FROM status_changes sc
                   JOIN listings l ON l.id = sc.listing_id
                   WHERE sc.changed_at > ? AND {_CITY_EXPR_L} = ?
                   ORDER BY sc.changed_at DESC""",
                (since, canonical_city(city)),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT sc.*, l.name, l.url, l.price_raw, l.source
                   FROM status_changes sc
                   JOIN listings l ON l.id = sc.listing_id
                   WHERE sc.changed_at > ?
                   ORDER BY sc.changed_at DESC""",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_new_since(
        self, hours: int = 24, city: Optional[str] = None
    ) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        if city:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM listings WHERE first_seen > ? "
                f"AND {_CITY_EXPR} = ?",
                (since, canonical_city(city)),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM listings WHERE first_seen > ?", (since,)
            ).fetchone()
        return row[0] if row else 0

    def count_changes_since(
        self, hours: int = 24, city: Optional[str] = None
    ) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        if city:
            row = self._conn.execute(
                f"""SELECT COUNT(*) FROM status_changes sc
                   JOIN listings l ON l.id = sc.listing_id
                   WHERE sc.changed_at > ? AND {_CITY_EXPR_L} = ?""",
                (since, canonical_city(city)),
            ).fetchone()
        else:
            row = self._conn.execute(
                """SELECT COUNT(*) FROM status_changes sc
                   JOIN listings l ON l.id = sc.listing_id
                   WHERE sc.changed_at > ?""",
                (since,),
            ).fetchone()
        return row[0] if row else 0

    # ── 面板筛选辅助 ────────────────────────────────────────────────

    def get_distinct_statuses(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT status FROM listings ORDER BY status"
        ).fetchall()
        return [r[0] for r in rows]

    def count_by_status(
        self,
        city: Optional[str] = None,
    ) -> dict[str, int]:
        """Return {status_lower: count} for the dashboard filter chips."""
        if city:
            rows = self._conn.execute(
                f"SELECT status, COUNT(*) FROM listings WHERE {_CITY_EXPR} = ? "
                f"GROUP BY status",
                (canonical_city(city),),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM listings GROUP BY status"
            ).fetchall()
        return {r[0].lower(): r[1] for r in rows}

    def get_feature_values(
        self,
        category: str,
        cities: Optional[list[str]] = None,
    ) -> list[str]:
        pattern = f"{category}:%"
        if cities:
            placeholders = ",".join("?" * len(cities))
            rows = self._conn.execute(
                f"""SELECT DISTINCT ltrim(substr(value, instr(value, ':') + 1)) AS val
                    FROM listings, json_each(features)
                    WHERE value LIKE ? AND city IN ({placeholders})
                    ORDER BY val""",
                [pattern, *cities],
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT DISTINCT ltrim(substr(value, instr(value, ':') + 1)) AS val
                   FROM listings, json_each(features)
                   WHERE value LIKE ?
                   ORDER BY val""",
                (pattern,),
            ).fetchall()
        # 归一 + 去重：上游对同一种合同既写 Indefinite 又写 Onbepaalde tijd，
        # 不合并的话筛选下拉里会并排出现两个同义选项，用户勾了其中一个就
        # 收不到另一半房源（见 models.FEATURE_SYNONYMS）。
        seen: dict[str, None] = {}
        for r in rows:
            if r[0]:
                seen.setdefault(canonical_feature(r[0]), None)
        return list(seen)
