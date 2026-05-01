"""
notifier.py — 多渠道通知系统
==============================
职责
----
格式化通知消息并通过指定渠道发送。支持 iMessage、Telegram Bot、WhatsApp（Twilio）。

设计模式
--------
- `BaseNotifier`：抽象基类，定义统一的发送接口；子类只需实现 `_send(text)` 和 `close()`
- `MultiNotifier`：聚合多个渠道，并发发送，任意一个成功即返回 True
- 消息格式化函数（`_format_*`）与渠道完全解耦，纯文本输出（不用 Markdown）

调用方式
--------
monitor.py 通过 `create_user_notifier(user)` 工厂函数为每个用户创建 MultiNotifier，
然后调用高层方法（`send_new_listing` / `send_status_change` 等）。

渠道实现说明
------------
- **iMessage**：通过 macOS `osascript` 调用 Messages.app，仅限 macOS，异步 subprocess
- **Telegram** ：同步 curl_cffi POST，通过 `run_in_executor` 在线程池中执行
- **WhatsApp** ：Twilio REST API，同步 curl_cffi POST，同上

依赖
----
- `curl_cffi.requests`（Telegram/WhatsApp 的 HTTP 请求）
- `models.Listing`
- `users.UserConfig`（仅在 `create_user_notifier` 中延迟 import，避免循环依赖）
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import curl_cffi.requests as req

from models import Listing

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# 抽象基类
# ------------------------------------------------------------------ #

class BaseNotifier(ABC):
    """
    所有通知渠道的抽象基类。

    子类契约
    --------
    - 必须实现 `_send(text: str) -> bool`：发送纯文本消息，成功返回 True
    - 必须实现 `close() -> None`：释放资源（HTTP Session 等）
    - 高层方法（send_new_listing 等）负责消息格式化，子类无需关心

    线程安全
    --------
    所有 public 方法均为 async，在 asyncio 事件循环中调用。
    子类若需同步 HTTP，应在 `_send` 内通过 `run_in_executor` 转入线程池。
    """

    async def send_new_listing(self, listing: Listing) -> bool:
        """发送新房源上架通知。"""
        return await self._send(_format_new(listing))

    async def send_status_change(
        self, listing: Listing, old_status: str, new_status: str
    ) -> bool:
        """发送房源状态变更通知（如 lottery → 可直接预订）。"""
        return await self._send(_format_status_change(listing, old_status, new_status))

    async def send_heartbeat(self, total_in_db: int, fresh_count: int) -> bool:
        """
        发送监控心跳消息，默认每 HEARTBEAT_EVERY 轮发一次。

        Parameters
        ----------
        total_in_db  : 数据库当前房源总数
        fresh_count  : 自监控启动以来累计的轮询轮数
        """
        msg = (
            f"💓 监控心跳\n"
            f"数据库房源总数：{total_in_db}\n"
            f"本次累计轮询：{fresh_count} 轮"
        )
        return await self._send(msg)

    async def send_error(self, message: str) -> bool:
        """发送监控异常告警（抓取失败等）。"""
        return await self._send(f"⚠️ 监控异常\n{message}")

    async def send_booking_success(self, listing: Listing, detail: str, pay_url: str = "") -> bool:
        """
        发送自动预订成功通知。

        Parameters
        ----------
        listing : 已预订的房源
        detail  : 备用消息文本（pay_url 为空时作为兜底显示）
        pay_url : idealCheckOut 返回的直链付款 URL（account.holland2stay.com）
        """
        return await self._send(_format_booking_success(listing, detail, pay_url))

    async def send_booking_failed(self, listing: Listing, reason: str) -> bool:
        """发送自动预订失败通知，含失败原因和手动预订链接。"""
        return await self._send(_format_booking_failed(listing, reason))

    @abstractmethod
    async def _send(self, text: str) -> bool: ...

    @abstractmethod
    async def close(self) -> None: ...


# ------------------------------------------------------------------ #
# 多渠道聚合
# ------------------------------------------------------------------ #

class MultiNotifier(BaseNotifier):
    """
    将同一条消息并发发往多个渠道，任意一个成功即返回 True。

    用途
    ----
    每个 UserConfig 对应一个 MultiNotifier 实例，
    由 `create_user_notifier()` 工厂函数根据用户配置的渠道列表构建。

    Parameters
    ----------
    notifiers : 子渠道列表（IMessageNotifier / TelegramNotifier / WhatsAppNotifier）
    enabled   : 对应 UserConfig.notifications_enabled，False 时静默丢弃所有消息
    """

    def __init__(self, notifiers: list[BaseNotifier], enabled: bool = True) -> None:
        self._notifiers = notifiers
        self._enabled = enabled

    async def _send(self, text: str) -> bool:
        if not self._enabled:
            logger.debug("通知已全局关闭，跳过发送")
            return False
        if not self._notifiers:
            logger.debug("未配置任何通知渠道")
            return False
        results = await asyncio.gather(
            *[n._send(text) for n in self._notifiers],
            return_exceptions=True,
        )
        return any(r is True for r in results)

    async def close(self) -> None:
        await asyncio.gather(*[n.close() for n in self._notifiers])


# ------------------------------------------------------------------ #
# iMessage（macOS）
# ------------------------------------------------------------------ #

class IMessageNotifier(BaseNotifier):
    """
    通过 macOS Messages.app 发送 iMessage。

    实现方式
    --------
    调用系统 `osascript` 执行 AppleScript，通过 Messages.app 发送。
    仅限 macOS；在 Linux/Windows 上调用会因 `osascript` 不存在而失败。

    Parameters
    ----------
    recipient : iMessage 收件人，手机号（如 "+31612345678"）或 Apple ID 邮箱

    注意
    ----
    消息中的换行符在 AppleScript 中有特殊含义，_build_applescript() 负责转义。
    """

    def __init__(self, recipient: str) -> None:
        self._recipient = recipient

    async def _send(self, text: str) -> bool:
        script = _build_applescript(self._recipient, text)
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                logger.error("iMessage 发送失败: %s", stderr.decode().strip())
                return False
            logger.debug("iMessage 发送成功 → %s", self._recipient)
            return True
        except asyncio.TimeoutError:
            logger.error("iMessage 发送超时")
            return False
        except FileNotFoundError:
            logger.error("osascript 未找到，请确认在 macOS 上运行")
            return False
        except Exception as e:
            logger.error("iMessage 发送异常: %s", e)
            return False

    async def close(self) -> None:
        pass


# ------------------------------------------------------------------ #
# Telegram Bot
# ------------------------------------------------------------------ #

class TelegramNotifier(BaseNotifier):
    """
    通过 Telegram Bot API 发送消息。

    配置步骤
    --------
    1. 向 @BotFather 发 /newbot，获取 Bot Token
    2. 向 bot 发任意一条消息
    3. 访问 https://api.telegram.org/bot<TOKEN>/getUpdates 获取 chat_id

    实现方式
    --------
    `_send()` 通过 `run_in_executor` 将同步 `_post()` 转为异步。
    每次发送都新建 curl_cffi Session（TCP 连接不复用）。

    Parameters
    ----------
    token   : Bot Token，格式 "123456789:AABBccdd..."
    chat_id : 目标会话 ID，数字字符串（私聊）或 "@channel_name"（频道）
    """

    _API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id

    async def _send(self, text: str) -> bool:
        url = self._API.format(token=self._token)
        try:
            loop = asyncio.get_event_loop()
            ok = await loop.run_in_executor(None, lambda: self._post(url, text))
            return ok
        except Exception as e:
            logger.error("Telegram 发送异常: %s", e)
            return False

    def _post(self, url: str, text: str) -> bool:
        with req.Session(impersonate="chrome110") as session:
            resp = session.post(
                url,
                json={"chat_id": self._chat_id, "text": text},
                timeout=15,
            )
            if not resp.ok:
                logger.error("Telegram 发送失败 %d: %s", resp.status_code, resp.text[:200])
                return False
            logger.debug("Telegram 发送成功 → %s", self._chat_id)
            return True

    async def close(self) -> None:
        pass


# ------------------------------------------------------------------ #
# WhatsApp（Twilio）
# ------------------------------------------------------------------ #

class WhatsAppNotifier(BaseNotifier):
    """
    通过 Twilio API 发送 WhatsApp 消息。

    前置条件
    --------
    需要 Twilio 付费账号，并在 Twilio 控制台配置 WhatsApp Sandbox 或正式号码。

    Parameters
    ----------
    account_sid  : Twilio Account SID
    auth_token   : Twilio Auth Token
    from_number  : 发送方 WhatsApp 号码，格式 "whatsapp:+14155238886"
    to_number    : 接收方 WhatsApp 号码，格式 "whatsapp:+31612345678"
    """

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
    ) -> None:
        self._sid = account_sid
        self._token = auth_token
        self._from = from_number
        self._to = to_number

    async def _send(self, text: str) -> bool:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/Messages.json"
        try:
            loop = asyncio.get_event_loop()
            ok = await loop.run_in_executor(None, lambda: self._post(url, text))
            return ok
        except Exception as e:
            logger.error("WhatsApp 发送异常: %s", e)
            return False

    def _post(self, url: str, text: str) -> bool:
        with req.Session(impersonate="chrome110") as session:
            resp = session.post(
                url,
                data={"From": self._from, "To": self._to, "Body": text},
                auth=(self._sid, self._token),
                timeout=15,
            )
            if not resp.ok:
                logger.error("WhatsApp 发送失败 %d: %s", resp.status_code, resp.text[:200])
                return False
            logger.debug("WhatsApp 发送成功 → %s", self._to)
            return True

    async def close(self) -> None:
        pass


# ------------------------------------------------------------------ #
# 工厂函数
# ------------------------------------------------------------------ #

def create_user_notifier(user) -> BaseNotifier:
    """
    根据 UserConfig 创建该用户对应的 MultiNotifier。

    Parameters
    ----------
    user : UserConfig 实例（延迟 import users 避免循环依赖）

    Returns
    -------
    MultiNotifier，内含用户配置中所有有效渠道的子 Notifier 实例。
    若某渠道配置不完整（缺少 token 等），记录警告并跳过该渠道。
    若 notifications_enabled=False，返回的 MultiNotifier 会静默丢弃所有消息。

    调用时机
    --------
    monitor.py 启动时以及 SIGHUP 热重载后调用一次；web.py 通知测试路由按需调用。
    """
    from users import UserConfig  # 延迟导入避免循环
    notifiers: list[BaseNotifier] = []

    for channel in user.notification_channels:
        ch = channel.strip().lower()
        if ch == "imessage":
            if user.imessage_recipient:
                notifiers.append(IMessageNotifier(user.imessage_recipient))
                logger.info("[%s] 通知渠道: iMessage → %s", user.name, user.imessage_recipient)
            else:
                logger.warning("[%s] iMessage 渠道缺少收件人，跳过", user.name)
        elif ch == "telegram":
            if user.telegram_token and user.telegram_chat_id:
                notifiers.append(TelegramNotifier(user.telegram_token, user.telegram_chat_id))
                logger.info("[%s] 通知渠道: Telegram → chat_id=%s", user.name, user.telegram_chat_id)
            else:
                logger.warning("[%s] Telegram 渠道 TOKEN 或 CHAT_ID 为空，跳过", user.name)
        elif ch == "whatsapp":
            if user.twilio_sid and user.twilio_token and user.twilio_from and user.twilio_to:
                notifiers.append(
                    WhatsAppNotifier(user.twilio_sid, user.twilio_token, user.twilio_from, user.twilio_to)
                )
                logger.info("[%s] 通知渠道: WhatsApp → %s", user.name, user.twilio_to)
            else:
                logger.warning("[%s] WhatsApp 渠道 Twilio 参数不完整，跳过", user.name)
        else:
            logger.warning("[%s] 未知通知渠道: %s", user.name, channel)

    return MultiNotifier(notifiers, enabled=user.notifications_enabled)


# ------------------------------------------------------------------ #
# AppleScript 构建
# ------------------------------------------------------------------ #

def _build_applescript(recipient: str, message: str) -> str:
    """
    构造用于 Messages.app 的 AppleScript 字符串。

    转义规则
    --------
    先转义反斜杠（\\ → \\\\），再转义双引号（" → \\"），
    顺序不能颠倒，否则已转义的反斜杠会被二次处理。

    Parameters
    ----------
    recipient : iMessage 收件人（手机号或 Apple ID 邮箱）
    message   : 要发送的纯文本消息

    Returns
    -------
    可直接传给 `osascript -e` 的 AppleScript 字符串
    """
    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'tell application "Messages"\n'
        f'  send "{escaped}" to buddy "{recipient}"'
        f' of (first service whose service type = iMessage)\n'
        f'end tell'
    )


# ------------------------------------------------------------------ #
# 消息格式化（纯文本，无 Markdown）
# ------------------------------------------------------------------ #

def _format_new(l: Listing) -> str:
    """格式化新房源上架通知，含完整特征信息和直链。"""
    status_icon = "🎰" if "lottery" in l.status.lower() else "✅"
    fm = l.feature_map()
    lines = [
        f"{status_icon} 新房源上架",
        f"",
        f"🏠 {l.name}",
        f"📌 状态：{l.status}",
        f"💰 租金：{l.price_display}/月",
        f"📅 可入住：{l.available_from or '未知'}",
        f"",
    ]
    if fm:
        lines += [
            f"🛏 类型：{fm.get('type', '—')}",
            f"📐 面积：{fm.get('area', '—')}",
            f"👤 入住：{fm.get('occupancy', '—')}",
            f"🏢 楼层：{fm.get('floor', '—')}",
            f"⚡ 能耗：{fm.get('energy_label', '—')}",
            f"",
        ]
    lines.append(f"🔗 {l.url}")
    return "\n".join(lines)


def _format_status_change(l: Listing, old: str, new: str) -> str:
    icon = "🚀" if "book" in new.lower() else "🔄"
    return "\n".join([
        f"{icon} 状态变更",
        f"",
        f"🏠 {l.name}",
        f"📌 {old} → {new}",
        f"💰 租金：{l.price_display}/月",
        f"📅 可入住：{l.available_from or '未知'}",
        f"",
        f"🔗 {l.url}",
    ])


def _format_booking_success(l: Listing, detail: str, pay_url: str = "") -> str:
    lines = [
        f"🛒 自动预订成功！",
        f"",
        f"🏠 {l.name}",
        f"💰 租金：{l.price_display}/月",
        f"📅 入住：{l.available_from or '待定'}",
        f"",
        f"⚡ 点击链接立即付款（有时限，请尽快）：",
        f"",
        pay_url if pay_url else detail,
        f"",
        f"⚠️ 链接直达支付页面，无需登录。",
    ]
    return "\n".join(lines)


def _format_booking_failed(l: Listing, reason: str) -> str:
    return "\n".join([
        f"❌ 自动预订失败",
        f"",
        f"🏠 {l.name}",
        f"💰 租金：{l.price_display}/月",
        f"",
        f"原因：{reason}",
        f"🔗 请手动预订：{l.url}",
    ])
