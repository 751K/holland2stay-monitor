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
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_TITLE = 120
MAX_BODY = 1000


@dataclass(slots=True)
class AnnouncementResult:
    recipients: int = 0          # 目标用户数
    channel_ok: int = 0          # 外部渠道送达成功的用户数
    push_devices: int = 0        # 推送成功的设备数
    web_feed: int = 0            # 写入面板 feed 的条数
    skipped_disabled: int = 0    # 因关闭通知而跳过的用户数
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
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


async def _broadcast_async(title: str, body: str, *, dry_run: bool) -> AnnouncementResult:
    from app.db import storage as _storage
    from mcore import push as _push
    from notifier import create_user_notifier
    from users import load_users

    res = AnnouncementResult()
    users = [u for u in load_users() if u.enabled]

    for user in users:
        if not getattr(user, "notifications_enabled", True):
            res.skipped_disabled += 1
            continue
        res.recipients += 1

    if dry_run:
        return res

    st = _storage()
    try:
        # 面板 feed：一条全局记录（user_id=""），所有人可见
        try:
            st.add_web_notification(type="announcement", title=title, body=body)
            res.web_feed = 1
        except Exception as exc:
            res.errors.append(f"web feed: {exc}")

        for user in users:
            if not getattr(user, "notifications_enabled", True):
                continue

            notifier = None
            try:
                notifier = create_user_notifier(user)
                if await notifier.send_announcement(title, body):
                    res.channel_ok += 1
            except Exception as exc:
                # 单个用户失败不能中断群发——否则排在后面的人一条都收不到
                logger.warning("公告发送失败 user=%s: %s", user.name, exc)
                res.errors.append(f"{user.name}: {exc}")
            finally:
                if notifier is not None:
                    try:
                        await notifier.close()
                    except Exception:
                        pass

            try:
                res.push_devices += await _push.dispatch_announcement_to_user(
                    st, user.id, title, body,
                )
            except Exception as exc:
                logger.warning("公告推送失败 user=%s: %s", user.name, exc)
    finally:
        st.close()

    logger.info(
        "公告已发送：目标 %d 人，渠道成功 %d，推送 %d 设备，跳过（已关通知）%d",
        res.recipients, res.channel_ok, res.push_devices, res.skipped_disabled,
    )
    return res


def broadcast(title: str, body: str = "", *, dry_run: bool = False) -> AnnouncementResult:
    """同步入口，供 Flask 路由调用。

    ``dry_run=True`` 只统计送达范围、不发任何东西——群发不可撤回，发之前
    先看清楚会打扰到多少人是值得的。
    """
    title, body = _clean(title, body)
    if not title:
        raise ValueError("公告标题不能为空")
    return asyncio.run(_broadcast_async(title, body, dry_run=dry_run))
