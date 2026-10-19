"""app/services/announcement_service.py — 管理员公告群发

为什么单独有个「公告」类型
--------------------------
在此之前系统里唯一能群发的东西是 ``send_error``，它会顶着 **"Monitor Error"**
的标题送到用户手机上。用它发「近期在扩展房源覆盖，可能有额外推送」这种说明，
既误导用户（看起来像故障），又稀释真告警的可信度——告警一旦经常不是故障，
下次真出事就没人当回事了。

公告和告警的区别是**谁发起的**：告警是系统检测到异常自动发的，公告是管理员
主动写的。所以公告不走告警的那套 dedup / 限流 / 节流。

送达范围
--------
每个 ``notifications_enabled`` 的用户：外部渠道（iMessage / Telegram / Email /
WhatsApp）+ 推送（APNs / FCM）+ Web 面板 feed。关掉通知的用户一个都不碰——
他们明确表达过不想被打扰，公告不是绕过这个意愿的理由。

逐用户投递记录
--------------
每次群发落一行 ``announcements``，再给每个目标用户的每个渠道落一行
``announcement_deliveries``。发送前先写 ``pending``，所以进程中途被杀也能
知道还有谁没处理；失败/未完成的行可以用 :func:`retry_failed` 精确补发：
只重试 ``failed`` 与 ``pending``，`ok` 的用户不会被再打扰一次，``skipped``
（没有渠道、没有设备、通知关闭）也不会被当成失败反复重试。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_TITLE = 120
MAX_BODY = 1000

#: 渠道名。落库值，重试与统计都按它分组。
CHANNEL_EXTERNAL = "external"
CHANNEL_PUSH = "push"


@dataclass(slots=True)
class AnnouncementResult:
    announcement_id: int = 0     # 本次群发/重试对应的公告 id
    recipients: int = 0          # 目标用户数（重试时 = 被重试的用户数）
    channel_ok: int = 0          # 外部渠道送达成功的用户数
    push_devices: int = 0        # 推送成功的设备数
    web_feed: int = 0            # 写入面板 feed 的条数
    skipped_disabled: int = 0    # 因关闭通知而跳过的用户数
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "announcement_id": self.announcement_id,
            "recipients": self.recipients,
            "channel_ok": self.channel_ok,
            "push_devices": self.push_devices,
            "web_feed": self.web_feed,
            "skipped_disabled": self.skipped_disabled,
            "errors": list(self.errors),
        }


def _clean(title: str, body: str) -> tuple[str, str]:
    title = (title or "").strip()[:MAX_TITLE]
    body = (body or "").strip()[:MAX_BODY]
    return title, body


def _user_has_devices(st, user_id: str) -> bool | None:
    """用户当前有没有可推送设备；查询失败返回 None（按「失败」处理）。

    区分「没有设备」与「有设备但一条都没推出去」很重要：前者是 ``skipped``，
    重试没有意义；后者是 ``failed``，正是补发要处理的情况。查询本身失败时
    不能猜成 skipped——那会把真正的失败永久藏起来。
    """
    try:
        return bool(st.get_active_devices_for_user(user_id))
    except Exception:
        logger.exception("查询公告目标设备失败 user_id=%s", user_id)
        return None


def _record(
    st,
    announcement_id: int,
    user_id: str,
    channel: str,
    status: str,
    *,
    devices: int = 0,
    error: str = "",
) -> None:
    """写投递结果。落库失败不能中断群发，只记日志。"""
    try:
        st.upsert_announcement_delivery(
            announcement_id, user_id, channel, status,
            devices=devices, error=error,
        )
    except Exception:
        logger.warning(
            "公告投递结果落库失败 announcement=%s user=%s channel=%s",
            announcement_id, user_id, channel, exc_info=True,
        )


async def _deliver_user(
    st,
    announcement_id: int,
    user,
    title: str,
    body: str,
    res: AnnouncementResult,
    channels: tuple[str, ...],
) -> None:
    """给一个用户投递指定渠道，并逐渠道记录结果。"""
    if CHANNEL_EXTERNAL in channels:
        await _deliver_external(st, announcement_id, user, title, body, res)
    if CHANNEL_PUSH in channels:
        await _deliver_push(st, announcement_id, user, title, body, res)


async def _deliver_external(
    st, announcement_id: int, user, title: str, body: str,
    res: AnnouncementResult,
) -> None:
    from notifier import create_user_notifier

    notifier = None
    try:
        notifier = create_user_notifier(user)
        if not getattr(notifier, "has_channels", True):
            # 只用 App 推送的用户没有外部渠道，这不是失败
            _record(st, announcement_id, user.id, CHANNEL_EXTERNAL,
                    "skipped", error="no_channels")
        elif await notifier.send_announcement(title, body):
            _record(st, announcement_id, user.id, CHANNEL_EXTERNAL, "ok")
            res.channel_ok += 1
        else:
            _record(st, announcement_id, user.id, CHANNEL_EXTERNAL,
                    "failed", error="渠道未送达")
            res.errors.append(f"{user.name}: 外部渠道未送达")
    except Exception as exc:
        # 单个用户失败不能中断群发——否则排在后面的人一条都收不到
        logger.warning("公告发送失败 user=%s: %s", user.name, exc)
        _record(st, announcement_id, user.id, CHANNEL_EXTERNAL,
                "failed", error=str(exc))
        res.errors.append(f"{user.name}: {exc}")
    finally:
        if notifier is not None:
            try:
                await notifier.close()
            except Exception:
                pass


async def _deliver_push(
    st, announcement_id: int, user, title: str, body: str,
    res: AnnouncementResult,
) -> None:
    from mcore import push as _push

    try:
        delivered = await _push.dispatch_announcement_to_user(
            st, user.id, title, body,
        )
        res.push_devices += delivered
        if delivered > 0:
            _record(st, announcement_id, user.id, CHANNEL_PUSH,
                    "ok", devices=delivered)
            return
        # 一条都没推出去：有设备就是 failed（值得补发），没设备是 skipped
        if _user_has_devices(st, user.id) is False:
            _record(st, announcement_id, user.id, CHANNEL_PUSH,
                    "skipped", error="no_devices")
        else:
            _record(st, announcement_id, user.id, CHANNEL_PUSH,
                    "failed", error="推送未送达")
    except Exception as exc:
        logger.warning("公告推送失败 user=%s: %s", user.name, exc)
        _record(st, announcement_id, user.id, CHANNEL_PUSH,
                "failed", error=str(exc))


async def _broadcast_async(
    title: str, body: str, *, dry_run: bool,
) -> AnnouncementResult:
    from app.db import storage as _storage
    from users import load_users

    res = AnnouncementResult()
    users = [u for u in load_users() if u.enabled]

    targeted = []
    for user in users:
        if not getattr(user, "notifications_enabled", True):
            res.skipped_disabled += 1
            continue
        targeted.append(user)
        res.recipients += 1

    if dry_run:
        return res

    st = _storage()
    try:
        res.announcement_id = st.create_announcement(
            title, body,
            recipients=res.recipients,
            skipped_disabled=res.skipped_disabled,
        )
        # 先把每个目标用户的每个渠道写成 pending，再逐个更新结果。
        # 没有这一步，进程在群发中途被杀时，剩余用户没有任何行——重试也无从
        # 知道「该发给谁」。pending 就是「这次群发答应过要投递，但还没有结论」。
        st.seed_announcement_deliveries(
            res.announcement_id,
            [u.id for u in targeted],
            (CHANNEL_EXTERNAL, CHANNEL_PUSH),
        )

        # 面板 feed：一条全局记录（user_id=""），所有人可见
        try:
            st.add_web_notification(type="announcement", title=title, body=body)
            res.web_feed = 1
        except Exception as exc:
            res.errors.append(f"web feed: {exc}")

        for user in targeted:
            await _deliver_user(
                st, res.announcement_id, user, title, body, res,
                (CHANNEL_EXTERNAL, CHANNEL_PUSH),
            )
    finally:
        st.close()

    logger.info(
        "公告已发送：目标 %d 人，渠道成功 %d，推送 %d 设备，跳过（已关通知）%d",
        res.recipients, res.channel_ok, res.push_devices, res.skipped_disabled,
    )
    return res


async def _retry_async(
    announcement_id: int, *, dry_run: bool,
) -> AnnouncementResult:
    from app.db import storage as _storage
    from users import load_users

    res = AnnouncementResult(announcement_id=int(announcement_id))
    st = _storage()
    try:
        ann = st.get_announcement(announcement_id)
        if ann is None:
            raise ValueError("公告不存在")

        failed = (
            st.list_announcement_deliveries(announcement_id, status="failed")
            + st.list_announcement_deliveries(announcement_id, status="pending")
        )
        by_user: dict[str, set[str]] = {}
        for row in failed:
            by_user.setdefault(row["user_id"], set()).add(row["channel"])
        if not by_user:
            return res

        if dry_run:
            res.recipients = len(by_user)
            return res

        living = {
            u.id: u for u in load_users()
            if getattr(u, "enabled", True)
        }
        for user_id, channels in by_user.items():
            user = living.get(user_id)
            if user is None or not getattr(user, "notifications_enabled", True):
                reason = (
                    "user_missing_or_disabled" if user is None
                    else "notifications_disabled"
                )
                for channel in sorted(channels):
                    _record(st, announcement_id, user_id, channel,
                            "skipped", error=reason)
                res.skipped_disabled += 1
                continue

            res.recipients += 1
            await _deliver_user(
                st, announcement_id, user, ann["title"], ann["body"], res,
                tuple(sorted(channels)),
            )
        return res
    finally:
        st.close()


def broadcast(title: str, body: str = "", *, dry_run: bool = False) -> AnnouncementResult:
    """同步入口，供 Flask 路由调用。

    ``dry_run=True`` 只统计送达范围、不发任何东西——群发不可撤回，发之前
    先看清楚会打扰到多少人。
    """
    title, body = _clean(title, body)
    if not title:
        raise ValueError("公告标题不能为空")
    return asyncio.run(_broadcast_async(title, body, dry_run=dry_run))


def retry_failed(announcement_id: int, *, dry_run: bool = False) -> AnnouncementResult:
    """补发某次公告里 ``failed`` 与 ``pending`` 的投递。

    ``pending`` 是群发中途进程被杀留下的「还没有结论」的行；它和 failed 一样
    需要重试。已成功、以及按设计跳过的用户都不会被再次打扰。传入 ``dry_run``
    时只统计会重试多少个用户。
    """
    return asyncio.run(_retry_async(announcement_id, dry_run=dry_run))
