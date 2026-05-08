"""
notifier.py — 多渠道通知系统
==============================
职责
----
格式化通知消息并通过指定渠道发送。支持 iMessage、Telegram Bot、Email、WhatsApp（Twilio）。

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
- **Email**    ：标准库 smtplib + SMTP / STARTTLS / SMTP_SSL，同步发送
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
import re
import smtplib
import sys
from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import TYPE_CHECKING

import curl_cffi.requests as req

from config import CURL_IMPERSONATE
from models import Listing

if TYPE_CHECKING:
    from storage import Storage

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# 平台检测
# ------------------------------------------------------------------ #

def is_macos() -> bool:
    """返回 True 当且仅当当前平台为 macOS。"""
    return sys.platform == "darwin"


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

    async def send_heartbeat(self, total_in_db: int, round_count: int) -> bool:
        """
        发送监控心跳消息，默认每 HEARTBEAT_EVERY 轮发一次。

        Parameters
        ----------
        total_in_db : 数据库当前房源总数
        round_count : 自监控启动以来累计的轮询轮数
        """
        msg = (
            f"💓 监控心跳\n"
            f"数据库房源总数：{total_in_db}\n"
            f"本次累计轮询：{round_count} 轮"
        )
        return await self._send(msg)

    async def send_error(self, message: str) -> bool:
        """发送监控异常告警（抓取失败等）。"""
        return await self._send(f"⚠️ 监控异常\n{message}")

    async def send_booking_success(
        self,
        listing: Listing,
        detail: str,
        pay_url: str = "",
        contract_start_date: str = "",
    ) -> bool:
        """
        发送自动预订成功通知。

        Parameters
        ----------
        listing               : 已预订的房源
        detail                : 备用消息文本（pay_url 为空时作为兜底显示）
        pay_url               : idealCheckOut 返回的直链付款 URL
        contract_start_date   : try_book() 从 API 获取的实际合同开始日期（"YYYY-MM-DD"）；
                                优先于 listing.available_from 展示；
                                为空时回退到 listing.available_from
        """
        return await self._send(
            _format_booking_success(listing, detail, pay_url, contract_start_date)
        )

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
    notifiers : 子渠道列表（IMessageNotifier / TelegramNotifier / EmailNotifier / WhatsAppNotifier）
    enabled   : 对应 UserConfig.notifications_enabled，False 时静默丢弃所有消息
    """

    def __init__(self, notifiers: list[BaseNotifier], enabled: bool = True) -> None:
        self._notifiers = notifiers
        self._enabled = enabled

    @property
    def has_channels(self) -> bool:
        """至少有一个可用的外部通知渠道。"""
        return self._enabled and len(self._notifiers) > 0

    async def _send(self, text: str) -> bool:
        if not self._enabled:
            logger.debug("通知已全局关闭，跳过发送")
            return False
        if not self._notifiers:
            logger.debug("未配置任何通知渠道")
            return False

        async def _send_with_retry(n):
            try:
                ok = await n._send(text)
                if ok:
                    return True
            except Exception:
                pass
            # 失败后等待 3 秒重试一次
            await asyncio.sleep(3)
            try:
                return await n._send(text)
            except Exception:
                return False

        results = await asyncio.gather(
            *[_send_with_retry(n) for n in self._notifiers],
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
    curl_cffi Session 在 `__init__` 中创建并持有，`close()` 时关闭，
    复用底层 TCP 连接，避免每条消息重新握手。

    Parameters
    ----------
    token   : Bot Token，格式 "123456789:AABBccdd..."
    chat_id : 目标会话 ID，数字字符串（私聊）或 "@channel_name"（频道）
    """

    _API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._session = req.Session(impersonate=CURL_IMPERSONATE)

    async def _send(self, text: str) -> bool:
        url = self._API.format(token=self._token)
        try:
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, lambda: self._post(url, text))
            return ok
        except Exception as e:
            logger.error("Telegram 发送异常: %s", e)
            return False

    def _post(self, url: str, text: str) -> bool:
        resp = self._session.post(
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
        self._session.close()


# ------------------------------------------------------------------ #
# Email（SMTP）
# ------------------------------------------------------------------ #

class EmailNotifier(BaseNotifier):
    """
    通过 SMTP 发送纯文本邮件。

    支持的安全模式
    --------------
    - `starttls`：先明文连接，再升级到 TLS（常见端口 587）
    - `ssl`      ：直接使用 SMTPS（常见端口 465）
    - `none`     ：不加密，仅适合可信内网或本地中继
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        security: str,
        username: str,
        password: str,
        from_addr: str,
        to_addrs: str,
    ) -> None:
        self._host = smtp_host.strip()
        self._port = int(smtp_port or 587)
        self._security = _normalize_email_security(security)
        self._username = username.strip()
        self._password = password
        self._from = from_addr.strip()
        self._to = to_addrs.strip()

    async def _send(self, text: str) -> bool:
        try:
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, lambda: self._post(text))
            return ok
        except Exception as e:
            logger.error("Email 发送异常: %s", e)
            return False

    def _post(self, text: str) -> bool:
        recipients = _split_email_recipients(self._to)
        if not self._host:
            logger.error("Email 发送失败: SMTP host 为空")
            return False
        if not recipients:
            logger.error("Email 发送失败: 收件人为空")
            return False

        from_addr = self._from or self._username
        if not from_addr:
            logger.error("Email 发送失败: 发件人为空")
            return False

        msg = EmailMessage()
        msg["Subject"] = _format_email_subject(text)
        msg["From"] = from_addr
        msg["To"] = ", ".join(recipients)
        msg.set_content(text)

        try:
            if self._security == "ssl":
                with smtplib.SMTP_SSL(self._host, self._port, timeout=15) as client:
                    self._deliver(client, msg, recipients)
            else:
                with smtplib.SMTP(self._host, self._port, timeout=15) as client:
                    client.ehlo()
                    if self._security == "starttls":
                        client.starttls()
                        client.ehlo()
                    self._deliver(client, msg, recipients)
            logger.debug("Email 发送成功 → %s", ", ".join(recipients))
            return True
        except Exception as e:
            logger.error("Email 发送失败: %s", e)
            return False

    def _deliver(self, client, msg: EmailMessage, recipients: list[str]) -> None:
        if self._username:
            client.login(self._username, self._password)
        client.send_message(msg, to_addrs=recipients)

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
        self._session = req.Session(impersonate=CURL_IMPERSONATE)

    async def _send(self, text: str) -> bool:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/Messages.json"
        try:
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, lambda: self._post(url, text))
            return ok
        except Exception as e:
            logger.error("WhatsApp 发送异常: %s", e)
            return False

    def _post(self, url: str, text: str) -> bool:
        resp = self._session.post(
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
        self._session.close()


# ------------------------------------------------------------------ #
# Web 面板通知（平台无关）
# ------------------------------------------------------------------ #

class WebNotifier(BaseNotifier):
    """
    将通知写入 SQLite `web_notifications` 表，由 Web 面板 SSE 端点推送给浏览器。

    与 iMessage/Telegram 等渠道不同，WebNotifier 是全局单例（monitor.py 持有），
    不对应某个具体用户，所有事件都写入同一张表，面板统一展示。

    Parameters
    ----------
    storage : Storage 实例（monitor.py 的全局实例，而非 web.py 的只读副本）

    平台
    ----
    在 macOS / Linux / Windows / Docker 中均可运行，无系统依赖。
    """

    def __init__(self, storage: "Storage") -> None:
        self._storage = storage

    # 覆盖高层方法，直接构造结构化通知，不走 _format_*() 纯文本路径

    async def send_new_listing(self, listing: Listing) -> bool:
        status_icon = "🎰" if "lottery" in listing.status.lower() else "✅"
        self._storage.add_web_notification(
            type="new_listing",
            title=f"{status_icon} 新房源：{listing.name}",
            body=(
                f"{listing.status} · {listing.price_display}/月"
                f" · 入住 {listing.available_from or '待定'}"
            ),
            url=listing.url,
            listing_id=listing.id,
        )
        return True

    async def send_status_change(
        self, listing: Listing, old_status: str, new_status: str
    ) -> bool:
        icon = "🚀" if "book" in new_status.lower() else "🔄"
        self._storage.add_web_notification(
            type="status_change",
            title=f"{icon} 状态变更：{listing.name}",
            body=f"{old_status} → {new_status} · {listing.price_display}/月",
            url=listing.url,
            listing_id=listing.id,
        )
        return True

    async def send_heartbeat(self, total_in_db: int, round_count: int) -> bool:
        self._storage.add_web_notification(
            type="heartbeat",
            title=f"💓 监控心跳 #{round_count}",
            body=f"数据库房源总数：{total_in_db}",
        )
        return True

    async def send_error(self, message: str) -> bool:
        self._storage.add_web_notification(
            type="error",
            title="⚠️ 监控异常",
            body=message,
        )
        return True

    async def send_booking_success(
        self,
        listing: Listing,
        detail: str,
        pay_url: str = "",
        contract_start_date: str = "",
    ) -> bool:
        start = contract_start_date or listing.available_from or "待定"
        self._storage.add_web_notification(
            type="booking",
            title=f"🛒 预订成功：{listing.name}",
            body=f"入住 {start} · {listing.price_display}/月",
            url=pay_url or listing.url,
            listing_id=listing.id,
        )
        return True

    async def send_booking_failed(self, listing: Listing, reason: str) -> bool:
        self._storage.add_web_notification(
            type="booking",
            title=f"❌ 预订失败：{listing.name}",
            body=reason,
            url=listing.url,
            listing_id=listing.id,
        )
        return True

    async def _send(self, text: str) -> bool:
        # 兜底：直接调用 _send() 时写入通用通知
        self._storage.add_web_notification(type="error", title="通知", body=text)
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
    notifiers: list[BaseNotifier] = []

    for channel in user.notification_channels:
        ch = channel.strip().lower()
        if ch == "imessage":
            if not is_macos():
                logger.warning(
                    "[%s] iMessage 仅支持 macOS，当前平台（%s）已跳过。"
                    " 请改用 Telegram / Email 等渠道，或通过 Web 面板查看通知。",
                    user.name, sys.platform,
                )
            elif user.imessage_recipient:
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
        elif ch == "email":
            has_auth = bool(user.email_username or user.email_password)
            if (
                user.email_smtp_host
                and user.email_to
                and (user.email_from or user.email_username)
                and ((not has_auth) or (user.email_username and user.email_password))
            ):
                notifiers.append(
                    EmailNotifier(
                        user.email_smtp_host,
                        user.email_smtp_port,
                        user.email_smtp_security,
                        user.email_username,
                        user.email_password,
                        user.email_from,
                        user.email_to,
                    )
                )
                logger.info("[%s] 通知渠道: Email → %s", user.name, user.email_to)
            else:
                logger.warning("[%s] Email 渠道 SMTP 参数不完整，跳过", user.name)
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
    按以下顺序处理，顺序不能颠倒：
    1. 反斜杠：\\ → \\\\（必须最先处理，避免后续步骤二次转义）
    2. 双引号：" → \\"
    3. 换行符：\\n → " & return & "
       AppleScript 字符串字面量必须在一行内，换行符需用内置常量 `return`
       和字符串连接运算符 `&` 表达，例如：
         send "Hello" & return & "World" to buddy "..."

    Parameters
    ----------
    recipient : iMessage 收件人（手机号或 Apple ID 邮箱）
    message   : 要发送的纯文本消息（可含换行符）

    Returns
    -------
    可直接传给 `osascript -e` 的 AppleScript 字符串
    """
    escaped = (
        message
        .replace("\\", "\\\\")   # 1. 反斜杠（必须最先）
        .replace('"', '\\"')      # 2. 双引号
        .replace("\n", '" & return & "')  # 3. 换行符 → AppleScript return 常量
    )
    return (
        f'tell application "Messages"\n'
        f'  send "{escaped}" to buddy "{recipient}"'
        f' of (first service whose service type = iMessage)\n'
        f'end tell'
    )


def _normalize_email_security(security: str) -> str:
    value = (security or "starttls").strip().lower()
    aliases = {
        "tls": "starttls",
        "smtps": "ssl",
        "plain": "none",
    }
    value = aliases.get(value, value)
    return value if value in {"starttls", "ssl", "none"} else "starttls"


def _split_email_recipients(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;\n]+", value or "") if part.strip()]


def _format_email_subject(text: str) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "Holland2Stay 通知")
    first_line = re.sub(r"\s+", " ", first_line)
    if len(first_line) > 80:
        first_line = first_line[:77].rstrip() + "..."
    return f"[Holland2Stay] {first_line}"


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


def _format_booking_success(
    l: Listing,
    detail: str,
    pay_url: str = "",
    contract_start_date: str = "",
) -> str:
    # 优先使用 try_book() 预订时 API 返回的实际合同日期，
    # 回退顺序：contract_start_date → listing.available_from → "待定"
    # 不直接使用 l.available_from 作为第一选择：
    # 因为 listing 是监控轮询时的快照，可能与预订时 API 返回的日期存在差异。
    start = contract_start_date or l.available_from or "待定"
    lines = [
        f"🛒 自动预订成功！",
        f"",
        f"🏠 {l.name}",
        f"💰 租金：{l.price_display}/月",
        f"📅 入住：{start}",
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
