"""
users.py — 多用户配置管理
==========================
职责
----
- 定义 `UserConfig` dataclass，包含单个用户的全部配置：
  通知渠道凭证、房源过滤条件、自动预订配置
- 提供 `load_users` / `save_users` / `update_users` 兼容接口

存储格式
--------
运行时存储在 SQLite `user_configs` 表。

旧版的两个一次性迁移（`data/users.json` 导入 SQLite、清理被回填的平台凭据）
已在 v2.2 删除，只保留启动检查 :func:`check_legacy_migrations`：没跑过迁移的
旧库会被拒绝启动，提示先升级到 v2.1.x。

与 config.py 的分工
--------------------
- config.py / .env  → 全局参数（轮询间隔、城市、数据库路径）
- users.py / SQLite user_configs → 每用户独立配置（通知、过滤、预订）

依赖
----
标准库 + filters.py（ListingFilter, AutoBookConfig）+ storage。Web 进程通过
``storage.set_storage_factory`` 注入请求内共用的连接。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field, fields as dc_fields
from typing import TYPE_CHECKING, Callable, Optional, TypeVar

from config import DATA_DIR
from filters import AutoBookConfig, ListingFilter
from crypto import decrypt, encrypt

if TYPE_CHECKING:
    from bookers.applicant_profile import ApplicantProfile

logger = logging.getLogger(__name__)

#: 旧版用户文件。不再读取，只用来在启动检查里识别「没迁移过的旧部署」。
USERS_FILE = DATA_DIR / "users.json"
#: v2.1 及以前的两个一次性迁移在库里留下的完成标记，见 :func:`check_legacy_migrations`。
USERS_MIGRATION_META_KEY = "users_storage_migrated_v1"
RENTCAFE_CRED_CLEANUP_META_KEY = "rentcafe_creds_unbackfilled_v1"
DEFAULT_EMAIL_SMTP_PORT = 587
_T = TypeVar("_T")


# ------------------------------------------------------------------ #
# 数据模型
# ------------------------------------------------------------------ #

@dataclass
class UserConfig:
    """
    单个用户的全部配置。

    字段说明
    --------
    name                  : 用户显示名，仅用于日志和 Web 面板
    id                    : 8 位十六进制 UUID 前缀，自动生成，作为内部唯一标识
    enabled               : False 时该用户的通知和自动预订全部跳过

    通知配置
    --------
    notifications_enabled : 该用户的通知总开关（独立于 enabled）
    notification_channels : 启用的渠道列表，支持 "imessage" / "telegram" / "whatsapp" / "email"
    imessage_recipient    : iMessage 收件人（手机号或 Apple ID 邮箱，macOS only）
    telegram_token        : Telegram Bot Token（格式 "123456789:AAB..."）
    telegram_chat_id      : Telegram Chat ID（数字字符串，向 bot 发消息后从 getUpdates 获取）
    email_smtp_host       : SMTP 主机（如 smtp.gmail.com）
    email_smtp_port       : SMTP 端口（常见：587 / 465）
    email_smtp_security   : SMTP 安全模式：starttls / ssl / none
    email_username        : SMTP 登录用户名（可选；若留空则不登录）
    email_password        : SMTP 登录密码 / App Password
    email_from            : 发件人邮箱
    email_to              : 收件人邮箱，支持逗号分隔多个地址
    twilio_sid            : Twilio Account SID（WhatsApp 渠道）
    twilio_token          : Twilio Auth Token
    twilio_from           : 发送方 WhatsApp 号码，格式 "whatsapp:+14155238886"
    twilio_to             : 接收方 WhatsApp 号码，格式 "whatsapp:+31612345678"

    过滤条件
    --------
    listing_filter        : 通知过滤条件，不满足的房源不发通知
                            is_empty() 时所有房源都通知

    自动预订
    --------
    auto_book             : 自动预订配置，enabled=False 时不触发预订
                            内含独立的 listing_filter（可比通知条件更严格）
    """

    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    enabled: bool = True

    notifications_enabled: bool = True
    language: str = "en"          # 推送语言：en / zh
    notification_channels: list[str] = field(default_factory=list)
    imessage_recipient: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    # email_mode: "shared" → 走 admin 在 .env 配的 Resend；用户只填 email_to
    #             "custom" → 走用户自填的 SMTP 字段（兼容旧行为）
    # 默认 shared，老用户经 SQLite 迁移自动改 custom（见 _base.py）。
    email_mode: str = "shared"
    # email_verified: shared 模式专用——email_to 是否通过 double opt-in 验证。
    # custom 模式忽略此字段（用户自负责）。修改 email_to 会自动重置为 False。
    email_verified: bool = False
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_smtp_security: str = "starttls"
    email_username: str = ""
    email_password: str = ""
    email_from: str = ""
    email_to: str = ""
    twilio_sid: str = ""
    twilio_token: str = ""
    twilio_from: str = ""
    twilio_to: str = ""

    listing_filter: ListingFilter = field(default_factory=ListingFilter)
    auto_book: AutoBookConfig = field(default_factory=AutoBookConfig)

    # iOS / 第三方客户端登录凭证 -------------------------------------
    # 与 notification_channels 完全独立：这里只用于 App 登录鉴权，
    # 不影响通知/抓取/预订任何现有逻辑。
    #
    # app_password_hash : bcrypt(password).decode()；空字符串=未设置
    # app_login_enabled : False 时即使 hash 存在也拒绝 App 登录
    #                     默认 False（fail-closed，老用户不会意外打开 App 入口）
    app_password_hash: str = ""
    app_login_enabled: bool = False


# ------------------------------------------------------------------ #
# 内部：反序列化辅助
# ------------------------------------------------------------------ #

def _lf_from_dict(d: dict) -> ListingFilter:
    """从 dict 构造 ListingFilter，缺失字段使用默认值。"""
    return ListingFilter(
        max_rent=d.get("max_rent"),
        min_area=d.get("min_area"),
        min_floor=d.get("min_floor"),
        allowed_occupancy=d.get("allowed_occupancy", []),
        allowed_types=d.get("allowed_types", []),
        allowed_neighborhoods=d.get("allowed_neighborhoods", []),
        allowed_buildings=d.get("allowed_buildings", []),
        allowed_contract=d.get("allowed_contract", []),
        allowed_tenant=d.get("allowed_tenant", []),
        allowed_offer=d.get("allowed_offer", []),
        allowed_cities=d.get("allowed_cities", []),
        allowed_sources=d.get("allowed_sources", []),
        allowed_finishing=d.get("allowed_finishing", []),
        allowed_energy=d.get("allowed_energy", "") if isinstance(d.get("allowed_energy", ""), str) else "",
    )


def _xior_accounts_from_dict(raw) -> dict[str, dict[str, str]]:
    """反序列化按楼栋的 Xior 账号，逐条解密密码。

    结构不对的条目直接丢弃而不是抛异常——一个坏条目不该让整个用户配置加载失败，
    那会连带停掉这个用户的全部通知。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, acct in raw.items():
        if not isinstance(acct, dict):
            continue
        email = str(acct.get("email") or "").strip()
        if not email:
            continue
        out[str(key).strip()] = {
            "email": email,
            "password": decrypt(acct.get("password", "")),
        }
    return out


def _profile_from_dict(raw) -> "ApplicantProfile":
    """反序列化申请人档案，解密敏感字段。

    未知 key 直接丢弃（而不是塞进构造器炸掉）——上游加字段、用户手改 JSON、
    版本回退都可能带来多余 key，一个坏 key 不该让整个用户配置加载失败。
    """
    from bookers.applicant_profile import ApplicantProfile, profile_field_is_encrypted

    if not isinstance(raw, dict):
        return ApplicantProfile()
    known = {f.name for f in dc_fields(ApplicantProfile)}
    kw = {}
    for k, v in raw.items():
        if k not in known:
            continue
        kw[k] = decrypt(v) if (profile_field_is_encrypted(k) and isinstance(v, str)) else v
    try:
        return ApplicantProfile(**kw)
    except Exception:
        logger.warning("申请人档案反序列化失败，已退回空档案", exc_info=True)
        return ApplicantProfile()


def _ab_from_dict(d: dict) -> AutoBookConfig:
    """从 dict 构造 AutoBookConfig，内含 listing_filter 嵌套反序列化。

    平台凭据不互相回退
    ------------------
    旧版本只有共用的 ``email``/``password``，新版 H2S / Xior / OurDomain 三套
    独立。当年拆分时给 Xior / OurDomain 的字段留了「缺失就回退到旧共用值」的
    兜底，**那是错的**：

    - 旧的共用凭据实际上就是 **H2S 的账号密码**（当时只有 H2S 支持自动预订），
      回填进另外两个平台不是"保住了旧配置"，而是**凭空替用户编了两个他从来
      没注册过的账号**；
    - 三个平台是三家不同公司、三个独立的 RENTCafe 租户，账号不通用；
    - 后果不是登录失败这么简单：会把用户的 H2S 密码发给第三方站点，而且
      RENTCafe 按 **IP** 记登录失败次数（连续失败锁 30 分钟），同一代理池上的
      其他用户跟着遭殃；
    - 它还把 Xior 那条「**没配该楼凭据 = 该楼不参与自动预订**」的设计短路了
      ——凭据本身就是开关，凭空造一份等于把开关焊死在"开"上。

    2026-08-04 实测：线上 7 个开了自动预订的用户，``ourdomain_email`` /
    ``xior_email`` 与 H2S 的**逐字相同**，全是这么来的。两个平台当时都没开
    （``monitor._AUTO_BOOK_SOURCES`` 里只有 holland2stay），所以没爆。

    现在缺失一律取空串 = 未配置。存量数据由 v2.1 及以前的一次性清理处理过，
    见 :func:`check_legacy_migrations`。
    """
    old_email = d.get("email", "")
    old_password = decrypt(d.get("password", ""))

    return AutoBookConfig(
        enabled=d.get("enabled", False),
        dry_run=d.get("dry_run", True),
        listing_filter=_lf_from_dict(d.get("listing_filter", {})),
        # H2S
        email=old_email,
        password=old_password,
        payment_method=d.get("payment_method", "idealcheckout_ideal"),
        # Xior：按楼栋一套账号（每栋楼是独立的 RENTCafe 门户）
        xior_accounts=_xior_accounts_from_dict(d.get("xior_accounts")),
        applicant_profile=_profile_from_dict(d.get("applicant_profile")),
        screening_consent_at=d.get("screening_consent_at", ""),
        # 存量单对字段，只在用户还没配过按楼凭据时兜底（见 xior_account_for）。
        # **不回退到 H2S 凭据**——理由见上面的 docstring。
        xior_email=d.get("xior_email", ""),
        xior_password=decrypt(d.get("xior_password", "")),
        # OurDomain
        ourdomain_email=d.get("ourdomain_email", ""),
        ourdomain_password=decrypt(d.get("ourdomain_password", "")),
        # OurCampus。与 Plaza 同理，不回退到 H2S 凭据。
        ourcampus_email=d.get("ourcampus_email", ""),
        ourcampus_password=decrypt(d.get("ourcampus_password", "")),
        # Plaza。**不回退到 H2S 凭据**——那是当年把 H2S 凭据
        # 抄给 Xior / OurDomain 留下的历史包袱，Plaza 从一开始就独立，别把新平台
        # 接进旧的回退链里。而且 Plaza 用的是用户名不是邮箱，抄过来必然登录失败，
        # 失败还会消耗上游的尝试额度。
        plaza_username=d.get("plaza_username", ""),
        plaza_password=decrypt(d.get("plaza_password", "")),
        plaza_enabled=bool(d.get("plaza_enabled", False)),
    )


# ------------------------------------------------------------------ #
# 读写接口
# ------------------------------------------------------------------ #


def _open_storage():
    """见 ``storage.open_storage``：Web 进程里与请求共用连接，其余进程新开。"""
    from storage import open_storage

    return open_storage()


def check_legacy_migrations(st) -> None:
    """启动检查：v2.1 及以前的一次性迁移已经在这个库上跑过。

    那两个迁移已删除，代码不再读 ``users.json``，也不再清理回填的凭据。没跑过
    迁移的旧库直接拿来用会出两种安静的错：``users.json`` 里的用户不会被导入，
    monitor 读到空用户表，所有通知停掉；被回填成 Xior / OurDomain 账号的 H2S
    密码留在库里，等这两个平台开放自动预订时就会被拿去登录。所以这里拒绝启动。

    判据
    ----
    - ``users.json`` 存在，旁边却没有 ``users.json.migrated.*.bak``：旧迁移每次
      导入前都会留这份备份，没有它就是从没导入过。有备份说明导入过，之后只是
      清过库（``Storage.reset_all`` 连标记带用户一起删），按全新安装处理。
    - 库里已有用户却缺标记：v2.1 之前的库，回填的凭据还没清理。

    其余情况（全新安装、清空过的库）直接补上两个标记。

    Raises
    ------
    RuntimeError
        命中上面任一判据。
    """
    if all(st.get_meta(k, default="") == "1"
           for k in (USERS_MIGRATION_META_KEY, RENTCAFE_CRED_CLEANUP_META_KEY)):
        return
    never_imported = USERS_FILE.exists() and not any(
        USERS_FILE.parent.glob(f"{USERS_FILE.name}.migrated.*.bak"))
    if never_imported or st.list_user_config_rows():
        raise RuntimeError(
            "这个数据库来自 v2.1 之前的版本，还没有完成用户数据迁移。"
            "请先升级到 v2.1.x 并启动一次（monitor 和 Web 面板都可以），"
            "确认用户都在之后，再升级到本版本。"
        )
    for key in (USERS_MIGRATION_META_KEY, RENTCAFE_CRED_CLEANUP_META_KEY):
        st.set_meta(key, "1")


def user_login_allowed(user_id: str) -> bool:
    """该用户是否仍可登录：账号启用，且开了 App / Web 登录。

    已登录的 Web 会话和 Bearer token 每个请求都要确认一次（admin 停用后立即
    失效），所以只读这一行的两个开关，不像 :func:`load_users` 那样读全表、
    解密所有凭据。
    """
    if not user_id:
        return False
    st = _open_storage()
    try:
        row = st.get_user_config_row(user_id)
    finally:
        st.close()
    return bool(row and row["enabled"] and row["app_login_enabled"])


def load_users() -> list[UserConfig]:
    """从 SQLite `user_configs` 加载用户列表。"""
    st = _open_storage()
    try:
        return _rows_to_users(st.list_user_config_rows())
    finally:
        st.close()


def _user_to_row(u: UserConfig) -> dict:
    """UserConfig -> SQLite row dict。敏感字段按原 users.json 规则加密。"""
    from mstorage._user_configs import UserConfigOps

    d = asdict(u)
    for field_name in ("email_password", "telegram_token", "twilio_token"):
        if d.get(field_name):
            d[field_name] = encrypt(d[field_name])
    ab = d.get("auto_book") or {}
    for _pw_key in ("password", "xior_password", "ourdomain_password",
                    "ourcampus_password", "plaza_password"):
        if ab.get(_pw_key):
            ab[_pw_key] = encrypt(ab[_pw_key])
    # 申请人档案：**默认全部加密**，例外清单见 config._PLAINTEXT_PROFILE_FIELDS。
    # 这里面有国籍、出生地、身份证号，以及 GDPR 第 10 条的犯罪记录。
    prof = ab.get("applicant_profile")
    if isinstance(prof, dict):
        from bookers.applicant_profile import profile_field_is_encrypted
        ab["applicant_profile"] = {
            k: (encrypt(v) if (profile_field_is_encrypted(k) and isinstance(v, str) and v) else v)
            for k, v in prof.items()
        }

    # 按楼栋的 Xior 账号：逐条加密密码，规则与上面那些单字段一致
    accts = ab.get("xior_accounts")
    if isinstance(accts, dict):
        ab["xior_accounts"] = {
            k: {**v, "password": encrypt(v["password"])} if (v or {}).get("password") else v
            for k, v in accts.items()
        }

    return {
        "id": d["id"],
        "name": d["name"],
        "enabled": 1 if d.get("enabled") else 0,
        "notifications_enabled": 1 if d.get("notifications_enabled") else 0,
        "notification_channels_json": UserConfigOps.dumps_json(d.get("notification_channels", [])),
        "imessage_recipient": d.get("imessage_recipient", ""),
        "telegram_token": d.get("telegram_token", ""),
        "telegram_chat_id": d.get("telegram_chat_id", ""),
        "email_mode": (d.get("email_mode") or "shared").lower(),
        "email_verified": 1 if d.get("email_verified") else 0,
        "email_smtp_host": d.get("email_smtp_host", ""),
        "email_smtp_port": int(d.get("email_smtp_port") or DEFAULT_EMAIL_SMTP_PORT),
        "email_smtp_security": d.get("email_smtp_security", "starttls"),
        "email_username": d.get("email_username", ""),
        "email_password": d.get("email_password", ""),
        "email_from": d.get("email_from", ""),
        "email_to": d.get("email_to", ""),
        "twilio_sid": d.get("twilio_sid", ""),
        "twilio_token": d.get("twilio_token", ""),
        "twilio_from": d.get("twilio_from", ""),
        "twilio_to": d.get("twilio_to", ""),
        "listing_filter_json": UserConfigOps.dumps_json(d.get("listing_filter", {})),
        "auto_book_json": UserConfigOps.dumps_json(ab),
        "app_password_hash": d.get("app_password_hash", ""),
        "app_login_enabled": 1 if d.get("app_login_enabled") else 0,
        "language": (d.get("language") or "en").lower()[:8],
    }


def _row_to_user(row: dict) -> UserConfig:
    """SQLite row dict -> UserConfig。SQLite 字段固定，直接构造 dataclass。"""
    try:
        channels = json.loads(row.get("notification_channels_json") or "[]")
    except Exception:
        channels = []
    try:
        listing_filter = json.loads(row.get("listing_filter_json") or "{}")
    except Exception:
        listing_filter = {}
    try:
        auto_book = json.loads(row.get("auto_book_json") or "{}")
    except Exception:
        auto_book = {}

    email_password = row.get("email_password", "")
    telegram_token = row.get("telegram_token", "")
    twilio_token = row.get("twilio_token", "")
    auto_book_data = auto_book if isinstance(auto_book, dict) else {}

    if email_password:
        email_password = decrypt(email_password)
    if telegram_token:
        telegram_token = decrypt(telegram_token)
    if twilio_token:
        twilio_token = decrypt(twilio_token)
    ab = _ab_from_dict(auto_book_data)

    return UserConfig(
        id=row.get("id", ""),
        name=row.get("name", ""),
        enabled=bool(row.get("enabled")),
        notifications_enabled=bool(row.get("notifications_enabled")),
        notification_channels=channels if isinstance(channels, list) else [],
        imessage_recipient=row.get("imessage_recipient", ""),
        telegram_token=telegram_token,
        telegram_chat_id=row.get("telegram_chat_id", ""),
        email_mode=(
            (row.get("email_mode") or "").lower()
            or ("custom" if row.get("email_smtp_host") else "shared")
        ),
        email_verified=bool(row.get("email_verified")),
        email_smtp_host=row.get("email_smtp_host", ""),
        email_smtp_port=int(row.get("email_smtp_port") or DEFAULT_EMAIL_SMTP_PORT),
        email_smtp_security=row.get("email_smtp_security", "starttls"),
        email_username=row.get("email_username", ""),
        email_password=email_password,
        email_from=row.get("email_from", ""),
        email_to=row.get("email_to", ""),
        twilio_sid=row.get("twilio_sid", ""),
        twilio_token=twilio_token,
        twilio_from=row.get("twilio_from", ""),
        twilio_to=row.get("twilio_to", ""),
        listing_filter=_lf_from_dict(listing_filter if isinstance(listing_filter, dict) else {}),
        auto_book=ab,
        app_password_hash=row.get("app_password_hash", ""),
        app_login_enabled=bool(row.get("app_login_enabled")),
        language=(row.get("language") or "en").lower()[:8],
    )


def _users_to_rows(users: list[UserConfig]) -> list[dict]:
    return [_user_to_row(u) for u in users]


def _rows_to_users(rows: list[dict]) -> list[UserConfig]:
    return [_row_to_user(r) for r in rows]


def save_users(users: list[UserConfig]) -> None:
    """将用户列表完整覆盖写入 SQLite `user_configs`。"""
    st = _open_storage()
    conn = st.conn
    try:
        conn.execute("BEGIN IMMEDIATE")
        st.replace_user_config_rows_unlocked(_users_to_rows(users))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        st.close()


def _invalidate_monitor_snapshot() -> None:
    """写热重载请求，让 monitor 丢掉它手里的用户快照。

    monitor 的 ``user_notifiers`` 是 ``list[(UserConfig, Notifier)]``——**整个
    UserConfig 在进程启动时被快照下来**，只在启动和热重载时重建（见
    ``monitor._build_user_notifiers``）。任何一次用户配置写入都让那份快照过期。

    绑在 ``update_users`` 里而不是让调用方各自记得，是因为让调用方记得已经
    失败过了：2026-08-07 查出来时，全仓库 12 处写用户配置的地方只有
    ``api_v1/me.py`` 一处发了这个请求。后果按严重性排：

    - 改收件邮箱：monitor 继续发旧地址，而面板「发送测试通知」是现场
      new 一个 notifier、发新地址——**两个邮箱同时在收**。Resend 投递记录里
      能看到一次心跳同时发给了一个已不存在于任何 user_config 的地址。
    - 删除用户 / 停用用户：被删被停的人继续收通知。
    - 邮箱验证通过：``email_verified`` 落库了，但 monitor 快照里还是 0，
      notifier 直接跳过整个 email 渠道——用户点了验证链接却依然收不到信。
    - 筛选条件 / 自动预订开关：改了不生效。

    这些以前之所以「过一会儿自己好了」，靠的是别处（设置页、部署重启）偶然
    触发了一次重载；安静的日子里能拖一整天。

    这是缓存失效，不是层级倒置：``update_users`` 是用户配置**唯一**的写入
    口，失效动作就该贴在写入点上。``app.process_ctrl`` 只依赖标准库和
    ``config.DATA_DIR``，没有 Flask 依赖，也不反向依赖本模块。

    失败只记日志：用户数据已经提交了，通知不到 monitor 不该让写入看起来失败。
    monitor 重启后总会重读。
    """
    try:
        from app.process_ctrl import write_reload_request
        write_reload_request()
    except Exception:
        logger.warning("写热重载请求失败，用户配置改动将在 monitor 下次重启后生效", exc_info=True)


def update_users(mutator: Callable[[list[UserConfig]], _T]) -> _T:
    """
    在 SQLite 事务内执行 read-modify-write。

    mutator 会收到最新用户列表，可以原地修改，并返回调用方需要的结果。
    只有 mutator 成功返回后才写回；抛异常时事务回滚。

    提交成功后会请求 monitor 热重载——见 ``_invalidate_monitor_snapshot``。
    不经过本函数的写入（``Storage.reorder_user`` 之类直接改表的）必须自己发。
    """
    st = _open_storage()
    conn = st.conn
    try:
        conn.execute("BEGIN IMMEDIATE")
        users = _rows_to_users(st.list_user_config_rows())
        result = mutator(users)
        st.replace_user_config_rows_unlocked(_users_to_rows(users))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        st.close()
    # 必须在 commit 之后、close 之后：monitor 收到请求会立刻去读库，
    # 在事务里发等于让它读到未提交的旧值。
    _invalidate_monitor_snapshot()
    return result


def get_user(users: list[UserConfig], user_id: str) -> Optional[UserConfig]:
    """
    在用户列表中按 id 查找单个用户。

    Parameters
    ----------
    users   : 已加载的用户列表
    user_id : UserConfig.id（8 位十六进制字符串）

    Returns
    -------
    对应的 UserConfig，不存在时返回 None
    """
    return next((u for u in users if u.id == user_id), None)


def get_user_by_name(
    users: list[UserConfig], name: str
) -> Optional[UserConfig]:
    """
    在用户列表中按 name 查找用户（精确匹配，大小写敏感）。

    App 端登录时用户输入 name + password，这里复用同一查找语义。
    name 不是唯一约束（UserConfig.id 才是），但 Web 后台编辑表单的
    保存路径校验过同名冲突，实际不会撞名。
    """
    return next((u for u in users if u.name == name), None)


# ------------------------------------------------------------------ #
# App 登录密码 —— bcrypt 包装
# ------------------------------------------------------------------ #
# 设计原则
# - 仅 UserConfig.app_password_hash 字段相关；不影响现有 email_password /
#   telegram_token 等业务凭证（这些字段是双向加密，要解密后传给第三方
#   API；密码是单向哈希，永远不需要解密）。
# - bcrypt 是 lazy import：只有调用 set/verify 的路径才需要装 bcrypt，
#   监控进程/抓取进程没必要为这层依赖买单（虽然实测开销很小）。
# - 失败永远 fail-closed：异常视为校验失败，不要泄漏 bcrypt 内部错误细节。


def _bcrypt_hash(plaintext: str) -> str:
    """纯哈希函数：bcrypt(plaintext) → ascii hash。空串返回空串。

    bcrypt 只处理密码的前 72 字节；更长的部分会被静默截断/抛错。直接拦下来，
    让调用方回 4xx，而不是在 ``hashpw`` 里变 500。
    """
    if not plaintext:
        return ""
    if len(plaintext.encode("utf-8")) > 72:
        raise ValueError("密码最长 72 字节（UTF-8）")
    try:
        import bcrypt
    except ImportError:
        raise RuntimeError("bcrypt 未安装，无法设置 App 密码。请 pip install bcrypt。") from None
    return bcrypt.hashpw(
        plaintext.encode("utf-8"), bcrypt.gensalt()
    ).decode("ascii")


def set_app_password(user: UserConfig, plaintext: str) -> None:
    """
    给用户设置 App 登录密码。

    传空串视为"清除密码"（同时 app_login_enabled 不变，但 verify 必失败）。
    """
    user.app_password_hash = _bcrypt_hash(plaintext)


def verify_app_password(user: UserConfig, plaintext: str) -> bool:
    """
    校验 App 登录密码。

    返回 True 仅当：
    - user.enabled is True
    - user.app_login_enabled is True
    - user.app_password_hash 非空
    - bcrypt.checkpw 通过

    任何分支异常一律返回 False（fail-closed）。
    """
    if not user.enabled or not user.app_login_enabled or not user.app_password_hash:
        return False
    if not plaintext:
        return False
    try:
        import bcrypt
        return bcrypt.checkpw(
            plaintext.encode("utf-8"),
            user.app_password_hash.encode("ascii"),
        )
    except Exception:
        logger.warning("verify_app_password 异常 (user=%s)", user.name)
        return False
