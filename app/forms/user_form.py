"""
用户配置表单 → UserConfig 绑定
================================

把 user_form.html 提交的 ImmutableMultiDict → UserConfig dataclass
的转换从 web.py 路由层剥离出来。

依赖
----
- config.AutoBookConfig / ListingFilter
- users.UserConfig
- 标准库：uuid / logging
- werkzeug.datastructures.ImmutableMultiDict（仅类型注解）

设计要点
--------
- 数值字段（_fv / _iv）做范围校验，超界静默丢弃 + WARNING 日志
- 列表字段（_lv）兼容 checkbox 多选与逗号分隔旧格式
- 密码/令牌字段（_secret）：空提交保留旧值，避免误清空已存凭据
- payment_method 用白名单 {idealcheckout_ideal/visa/mastercard} 兜底
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Optional

from config import AutoBookConfig, ListingFilter, ENERGY_LABELS
from users import UserConfig

if TYPE_CHECKING:
    from werkzeug.datastructures import ImmutableMultiDict

logger = logging.getLogger(__name__)

# Magento setPaymentMethodOnCart 支持的支付方式代码（来自浏览器抓包）
VALID_PAYMENT_METHODS: set[str] = {
    "idealcheckout_ideal",
    "idealcheckout_visa",
    "idealcheckout_mastercard",
}
DEFAULT_PAYMENT_METHOD = "idealcheckout_ideal"


def build_user_from_form(
    form: "ImmutableMultiDict[str, str]",
    user_id: Optional[str] = None,
    existing: Optional[UserConfig] = None,
) -> UserConfig:
    """
    从表单数据构建 UserConfig。

    Parameters
    ----------
    form     : Flask ``request.form``（ImmutableMultiDict）
    user_id  : 编辑模式时传入已有 ID，新建时传 None（自动生成 8 位 hex）
    existing : 编辑模式时传入当前 UserConfig，用于在密码字段为空时
               保留旧密码而不是将其清空。
               密码字段在 GET 时不回填到 HTML，空提交 = "不修改"。
               新建模式传 None，空密码字段即存为空字符串。

    Returns
    -------
    构造完成的 UserConfig 实例（含 ListingFilter / AutoBookConfig 嵌套）
    """
    def _fv(key: str, min_val: float = 0.01, max_val: float = 50000) -> Optional[float]:
        v = form.get(key, "").strip()
        if not v:
            return None
        try:
            val = float(v)
        except ValueError:
            logger.warning("表单 [%s] 值 %r 不是合法数字，已忽略", key, v)
            return None
        if not (min_val <= val <= max_val):
            logger.warning("表单 [%s] 值 %.1f 超出范围 [%.1f, %.1f]，已忽略", key, val, min_val, max_val)
            return None
        return val

    def _iv(key: str, min_val: int = 0, max_val: int = 200) -> Optional[int]:
        v = form.get(key, "").strip()
        if not v:
            return None
        try:
            val = int(v)
        except ValueError:
            logger.warning("表单 [%s] 值 %r 不是合法整数，已忽略", key, v)
            return None
        if not (min_val <= val <= max_val):
            logger.warning("表单 [%s] 值 %d 超出范围 [%d, %d]，已忽略", key, val, min_val, max_val)
            return None
        return val

    def _lv(key: str) -> list[str]:
        # Checkbox 多选：有多个同名字段时用 getlist
        vals = form.getlist(key)
        if vals:
            return [x.strip() for x in vals if x.strip()]
        # 兼容旧的文本框输入（逗号分隔）
        v = form.get(key, "").strip()
        return [x.strip() for x in v.split(",") if x.strip()] if v else []

    def _secret(key: str, old_val: str) -> str:
        """
        密码/令牌字段的安全读取：
        - 表单字段非空 → 使用新值（用户正在更新密码）
        - 表单字段为空 → 保留 old_val（用户未动密码字段，不清除已保存的值）
        """
        v = form.get(key, "").strip()
        return v if v else old_val

    def _sanitize_energy(key: str) -> str:
        """校验能耗等级在白名单中，非法值 WARNING 后返回 ''。"""
        v = form.get(key, "").strip()
        if not v:
            return ""
        if v.upper() in ENERGY_LABELS:
            return v
        logger.warning("表单 [%s] 能耗等级 %r 不在白名单中，已忽略", key, v)
        return ""

    channels_raw = form.get("NOTIFICATION_CHANNELS", "")
    channels = [c.strip().lower() for c in channels_raw.split(",") if c.strip()]

    lf = ListingFilter(
        max_rent=_fv("MAX_RENT"),
        min_area=_fv("MIN_AREA"),
        min_floor=_iv("MIN_FLOOR"),
        allowed_occupancy=_lv("ALLOWED_OCCUPANCY"),
        allowed_types=_lv("ALLOWED_TYPES"),
        allowed_neighborhoods=_lv("ALLOWED_NEIGHBORHOODS"),
        allowed_contract=_lv("ALLOWED_CONTRACT"),
        allowed_tenant=_lv("ALLOWED_TENANT"),
        allowed_offer=_lv("ALLOWED_OFFER"),
        allowed_cities=_lv("ALLOWED_CITIES"),
        allowed_finishing=_lv("ALLOWED_FINISHING"),
        allowed_energy=_sanitize_energy("ALLOWED_ENERGY"),
    )

    ex_ab = existing.auto_book if existing else None
    raw_pm = form.get("AUTO_BOOK_PAYMENT_METHOD", DEFAULT_PAYMENT_METHOD)
    payment_method = raw_pm if raw_pm in VALID_PAYMENT_METHODS else DEFAULT_PAYMENT_METHOD

    ab = AutoBookConfig(
        enabled=form.get("AUTO_BOOK_ENABLED") == "true",
        dry_run=form.get("AUTO_BOOK_DRY_RUN", "true") != "false",
        cancel_enabled=form.get("AUTO_BOOK_CANCEL_ENABLED") == "true",
        email=form.get("AUTO_BOOK_EMAIL", ""),
        password=_secret("AUTO_BOOK_PASSWORD", ex_ab.password if ex_ab else ""),
        payment_method=payment_method,
        listing_filter=ListingFilter(
            max_rent=_fv("AUTO_BOOK_MAX_RENT"),
            min_area=_fv("AUTO_BOOK_MIN_AREA"),
            min_floor=_iv("AUTO_BOOK_MIN_FLOOR"),
            allowed_occupancy=_lv("AUTO_BOOK_ALLOWED_OCCUPANCY"),
            allowed_contract=_lv("AUTO_BOOK_ALLOWED_CONTRACT"),
            allowed_tenant=_lv("AUTO_BOOK_ALLOWED_TENANT"),
            allowed_offer=_lv("AUTO_BOOK_ALLOWED_OFFER"),
            allowed_types=_lv("AUTO_BOOK_ALLOWED_TYPES"),
            allowed_neighborhoods=_lv("AUTO_BOOK_ALLOWED_NEIGHBORHOODS"),
            allowed_cities=_lv("AUTO_BOOK_ALLOWED_CITIES"),
            allowed_finishing=_lv("AUTO_BOOK_ALLOWED_FINISHING"),
            allowed_energy=_sanitize_energy("AUTO_BOOK_ALLOWED_ENERGY"),
        ),
    )
    # iOS App 登录字段（独立处理，因为是 bcrypt hash 而不是双向加密的凭证）
    # ----------------------------------------------------------------
    # 三种用户行为：
    # 1. 不填 app_password 且不勾 app_password_clear   → 保留旧 hash
    # 2. 填了新密码                                    → bcrypt 重新哈希
    # 3. 勾了 "清除密码" checkbox                       → hash="" 强制下次登录失败
    app_login_enabled = form.get("app_login_enabled") == "true"
    # H2S 凭据 fallback —— 默认关。仅在管理员/用户显式勾选时打开；fail-closed
    # 防止 H2S 站点密码泄露被借用来登录本地账号（详见 UserConfig 注释）。
    allow_h2s_login = form.get("allow_h2s_login") == "true"
    new_app_pw = form.get("app_password", "")
    clear_app_pw = form.get("app_password_clear") == "true"

    if clear_app_pw:
        app_password_hash = ""
    elif new_app_pw:
        try:
            from users import _bcrypt_hash
            app_password_hash = _bcrypt_hash(new_app_pw)
        except RuntimeError as e:
            raise ValueError(str(e)) from e
    else:
        app_password_hash = existing.app_password_hash if existing else ""

    # name 规范化：禁止 "__" 前缀，避免与 API v1 的保留 sentinel "__admin__"
    # 冲突（同名用户会被 sentinel 分支劫持，无法登录 App）。
    raw_name = form.get("name", "").strip()
    if raw_name.startswith("__"):
        logger.warning("用户名 %r 含保留前缀 '__'，已加 'u_' 前缀防冲突", raw_name)
        raw_name = "u_" + raw_name.lstrip("_")
    new_user = UserConfig(
        id=user_id or uuid.uuid4().hex[:8],
        name=raw_name or "未命名用户",
        enabled=form.get("enabled") == "true",
        notifications_enabled=form.get("NOTIFICATIONS_ENABLED", "true") != "false",
        notification_channels=channels,
        imessage_recipient=form.get("IMESSAGE_RECIPIENT", ""),
        telegram_token=form.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=form.get("TELEGRAM_CHAT_ID", ""),
        email_smtp_host=form.get("EMAIL_SMTP_HOST", "").strip(),
        email_smtp_port=_iv("EMAIL_SMTP_PORT", min_val=1, max_val=65535) or 587,
        email_smtp_security=form.get("EMAIL_SMTP_SECURITY", "starttls").strip().lower() or "starttls",
        email_username=form.get("EMAIL_USERNAME", "").strip(),
        email_password=_secret("EMAIL_PASSWORD", existing.email_password if existing else ""),
        email_from=form.get("EMAIL_FROM", "").strip(),
        email_to=form.get("EMAIL_TO", "").strip(),
        twilio_sid=form.get("TWILIO_ACCOUNT_SID", ""),
        twilio_token=_secret("TWILIO_AUTH_TOKEN", existing.twilio_token if existing else ""),
        twilio_from=form.get("TWILIO_FROM", ""),
        twilio_to=form.get("TWILIO_TO", ""),
        listing_filter=lf,
        auto_book=ab,
        app_password_hash=app_password_hash,
        app_login_enabled=app_login_enabled,
        allow_h2s_login=allow_h2s_login,
    )
    return new_user
