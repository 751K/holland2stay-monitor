"""
路由：用户管理（列表 / 新增 / 编辑 / 删除 / 测试通知 / 启用切换 / 优先级调整）

挂载的 endpoint
- GET      /users                  → users_list
- GET/POST /users/new              → user_new
- GET/POST /users/<user_id>        → user_edit
- POST     /users/<user_id>/delete → user_delete
- POST     /users/<user_id>/test   → user_test_notify
- POST     /users/<user_id>/toggle → user_toggle
- POST     /users/<user_id>/move   → user_move
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import sys

from config import KNOWN_SOURCES, source_display_name, APPLICANT_GENDERS, APPLICANT_TITLES, known_city_names
from users import get_user, load_users, update_users

from app.auth import (
    admin_required,
    check_test_notify_rate,
    current_user_id,
    is_admin,
    is_user,
    record_test_notify,
    self_or_admin_required,
)
from app.csrf import csrf_required
from app.db import storage
from app.forms.user_form import build_user_from_form, build_user_from_form_self
from app.i18n import DEFAULTS, get_lang, localize_options
from app.process_ctrl import write_reload_request
from config import ENERGY_LABELS, energy_rank
from translations import tr

logger = logging.getLogger(__name__)



def _xior_building_options() -> list[dict]:
    """当前监控中的 Xior 楼栋，供用户表单选择要配哪栋楼的账号。

    只列 XIOR_CITIES 里配了的——没在监控的楼永远不会产出候选，给它配账号
    没有意义，只会把表单撑长。
    """
    try:
        from config import load_config
        from scrapers.xior import XiorScraper
        cfg = load_config()
        out = []
        for c in cfg.xior_cities:
            meta = XiorScraper.BUILDINGS.get(c.key) or {}
            out.append({"key": c.key, "display": meta.get("display") or c.name})
        return sorted(out, key=lambda x: x["display"])
    except Exception:
        return []

def _run_async(coro: Any) -> Any:
    """安全运行 async 协程，兼容已有 event loop（Gunicorn gevent/asyncio worker）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 已有 running loop：在新线程中跑独立的 event loop
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _energy_rank_or_99(label: str) -> int:
    """能耗排序辅助，未知标签排最后。"""
    r = energy_rank(label)
    return r if r is not None else 99


def _request_monitor_reload(what: str) -> None:
    """让 monitor 丢掉用户快照。**只给绕过 update_users 的写入路径用。**

    走 ``users.update_users`` 的路径（新建 / 编辑 / 删除 / 停用 / 邮箱验证）
    已经在那里统一发过了，不要重复调用。这里剩下的是优先级调整——它直接
    改表（``Storage.reorder_user`` / ``reorder_users_bulk``），不经过
    ``update_users``，所以得自己发。

    失败只警告不抛：改动已经落库了，重载请求写不下去不该让「保存成功」
    变成 500。
    """
    try:
        write_reload_request()
    except OSError:
        logger.warning(
            "写热重载请求失败（%s），改动将在 monitor 下次重启后生效",
            what, exc_info=True,
        )


def _log_user_change(action: str, user: "UserConfig") -> None:  # noqa: F821
    """记录用户配置变更到日志。"""
    channels = [ch for ch in ("imessage", "telegram", "whatsapp", "email") if ch in user.notification_channels]
    ab = user.auto_book
    ab_info = ""
    if ab and ab.enabled:
        ab_info = f" 自动预订=开启(dry={ab.dry_run} 取消={ab.cancel_enabled} 支付={ab.payment_method})"
    f = user.listing_filter
    filters = []
    if f.max_rent is not None: filters.append(f"租金≤{f.max_rent:.0f}")
    if f.min_area is not None: filters.append(f"面积≥{f.min_area:.0f}m²")
    if f.min_floor is not None: filters.append(f"楼层≥{f.min_floor}")
    if f.allowed_cities: filters.append(f"城市={f.allowed_cities}")
    if f.allowed_types: filters.append(f"房型={f.allowed_types}")
    if f.allowed_energy: filters.append(f"能耗≥{f.allowed_energy}")
    filter_str = " ".join(filters) if filters else "无过滤"
    logger.info(
        "用户%s「%s」(id=%s) — 启用=%s 通知=%s 渠道=%s 过滤=[%s]%s",
        action, user.name, user.id,
        user.enabled, user.notifications_enabled,
        channels or "无", filter_str, ab_info,
    )


def _get_all_filter_options() -> dict[str, list[str]]:
    """一次 Storage 调用取所有过滤分类值，DB 为空时按分类回退预设。
    供 user_new / user_edit 使用，避免每个分类单独开关一次连接。"""
    st = storage()
    try:
        return {
            cat: (st.get_feature_values(cat) or DEFAULTS.get(cat, []))
            for cat in DEFAULTS
        }
    except Exception:
        return {cat: vals for cat, vals in DEFAULTS.items()}
    finally:
        st.close()


#: 语言 cookie 的存活期，与侧栏那个语言开关（sessions.set_lang）保持一致。
_LANG_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def _own_language_cookie(resp, user_id: str, language: str):
    """保存**自己**的账号时，界面语言跟着表单里选的走。

    ``language`` 原本只管发出去的文案（验证邮件、房源通知），而界面语言是
    ``h2s-lang`` 这个 cookie，两者各走各的——用户在自己的页面上把语言改成英文、
    保存，界面却仍是中文，看起来像没生效。

    只对本人生效。``current_user_id()`` 对 admin / guest 返回空串，所以 admin
    编辑别人时不会把自己的界面语言换掉——那是另一个人的偏好，不是他的。

    cookie 而不是 session：界面语言本来就存在 cookie 里，写 session 会让同一个
    浏览器的匿名页面（登录页、指南）跟不上。
    """
    if language in ("zh", "en") and user_id and current_user_id() == user_id:
        resp.set_cookie("h2s-lang", language,
                        max_age=_LANG_COOKIE_MAX_AGE, samesite="Lax")
    return resp


def users_list() -> Any:
    """
    用户列表页：
    - admin：返回全部用户列表
    - user ：跳转到自己的编辑页（"我的账号"直达详情，不暴露列表壳）
    - guest / 未登录：重定向到登录页或首页
    """
    from flask import redirect, session, url_for
    from app.auth import auth_enabled

    if auth_enabled():
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        role = session.get("role")
        if role == "user":
            my_id = current_user_id()
            if my_id:
                return redirect(url_for("user_edit", user_id=my_id))
            return redirect(url_for("index"))
        if role != "admin":
            return redirect(url_for("index"))

    users = load_users()

    # 每张卡片上要显示「这个人在哪些客户端登录着」。一次取全部再按 id 分发，
    # 卡片里逐个查会变成几十次 SQLite 往返。
    st = storage()
    try:
        clients = st.get_active_clients_by_user()
    except Exception:
        # 客户端信息是卡片上的附加信息，取不到不该让整个用户列表打不开。
        logger.exception("读取活跃客户端失败")
        clients = {}
    finally:
        st.close()

    return render_template("users.html", users=users, clients=clients)


def _handle_id_document(user_id: str) -> None:
    """处理面板上传/删除的证件文件。

    平台在证件到位前拒绝保存申请表，而抢房是系统异步触发的——所以文件必须提前
    存好，没有「用完即走的透传」。存储细节（加密、大小/格式校验）在
    ``applicant_docs`` 里。

    校验不通过只 flash 提示、不中断保存：表单其余部分是用户刚填的，为一个文件
    把它们全丢掉更糟。
    """
    import applicant_docs

    if (request.form.get("AUTO_BOOK_ID_DOC_DELETE") or "") == "true":
        if applicant_docs.delete(user_id):
            flash("已删除已保存的证件", "success")
        return

    f = request.files.get("AUTO_BOOK_ID_DOC")
    if f is None or not (f.filename or "").strip():
        return
    try:
        applicant_docs.save(user_id, f.filename, f.read())
    except applicant_docs.DocumentRejected as e:
        flash(f"证件未保存：{e}", "danger")
        return
    flash("✅ 证件已加密保存", "success")


def _needs_email_verification(user, previous_email: str | None = None) -> bool:
    """该不该给这个用户发收件邮箱验证邮件。

    只有 shared 模式需要：它借的是 admin 自己的发件域，必须用 double opt-in
    确认用户对收件邮箱有控制权，否则等于给任何人做代发。custom 模式用户自管
    SMTP，不走这条路。

    ``previous_email`` 只在编辑路径传：邮箱没变说明是在保存别的字段，不该重发。
    新建路径不传——那时没有「上一个邮箱」可比，有邮箱就得发。
    """
    if user.email_mode != "shared" or not user.email_to or user.email_verified:
        return False
    return previous_email is None or user.email_to != previous_email


def _flash_verification_email(user) -> None:
    """发验证邮件并把结果 flash 出去。是否该发、要不要限流由调用方决定。

    四种结果都只 flash 不抛：调用到这里时用户已经落库了，发信失败不能把
    「用户创建/保存成功」这件事一起搭进去。
    """
    from app.email_verify import EmailVerifyConfigError, send_verification_email_sync
    try:
        sent = send_verification_email_sync(
            user.id, user.name, user.email_to,
            getattr(user, "language", "en") or "en",
        )
        if sent:
            flash("📧 验证邮件已发送，请查收并点击链接确认", "success")
        else:
            flash("⚠️ 邮箱已保存，但验证邮件未能发出（服务器未配置 Resend 或临时故障），通知暂不会发到此邮箱", "warning")
    except EmailVerifyConfigError as e:
        logger.error("邮箱验证未就绪: %s", e)
        flash("⚠️ 邮箱已保存，但系统未配置 PUBLIC_BASE_URL，暂时无法发送验证邮件", "warning")
    except Exception as e:
        logger.exception("发送邮箱验证邮件异常: %s", e)
        flash("⚠️ 邮箱已保存但验证邮件发送失败，请稍后重试", "warning")


@admin_required
@csrf_required
def user_new() -> Any:
    if request.method == "POST":
        try:
            user = build_user_from_form(request.form)
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(request.url)
        try:
            def _append(users):
                if any(u.name == user.name for u in users):
                    raise ValueError(f"用户「{user.name}」已存在")
                users.append(user)

            update_users(_append)
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(request.url)
        _handle_id_document(user.id)
        _log_user_change("创建", user)
        flash(f"✅ 用户「{user.name}」已创建", "success")
        # 建的时候就填了 shared 收件邮箱，必须当场发验证邮件。
        #
        # 这里漏掉会形成一条走不出去的死路：用户建完是 email_verified=0，
        # 而下面 user_edit 只在「邮箱变了」时才发，于是他既收不到验证链接、
        # 也不会再触发发送，notifier 那边则直接跳过整个 email 渠道。
        # 线上真出过（2026-08-04），日志里只有一行「邮箱未验证，跳过」。
        #
        # user_new 是 @admin_required，不需要 user_edit 那道给普通用户的限流。
        if _needs_email_verification(user):
            _flash_verification_email(user)
        return redirect(url_for("users_list"))
    # GET：空白表单
    # 全平台归一后的城市并集。只用 KNOWN_CITIES 会漏掉 Xior 独有的
    # Wageningen / Venlo / Breda / Leeuwarden——用户既选不到，一旦设了
    # 城市筛选，这些楼盘的房源就被整体挡掉。
    city_names = known_city_names()
    opts = _get_all_filter_options()
    return render_template(
        "user_form.html", user=None, id_doc=None, device_count=0,
        xior_buildings=_xior_building_options(),
        applicant_titles=APPLICANT_TITLES,
        applicant_genders=APPLICANT_GENDERS,
        # title 会同时进 <title> 和面包屑；写死中文的话英文界面标签页上
        # 也是「新增用户」
        action=url_for("user_new"), title=tr("user_new_title", get_lang()),
        is_macos=(sys.platform == "darwin"),
        occupancy_options=localize_options("Occupancy", opts["Occupancy"]),
        type_options=localize_options("Type", opts["Type"]),
        city_options=city_names,
        # 平台清单来自 config.KNOWN_SOURCES —— 别在模板里再写死一份，
        # ourcampus 就是这么被漏了三次的
        source_options=[(k, source_display_name(k)) for k in KNOWN_SOURCES],
        contract_options=localize_options("Contract", opts["Contract"]),
        tenant_options=localize_options("Tenant", opts["Tenant"]),
        offer_options=opts["Offer"],
        finishing_options=opts["Finishing"],
        energy_options=sorted(
            [x for x in opts["Energy"] if x.upper() in ENERGY_LABELS] or ENERGY_LABELS,
            key=_energy_rank_or_99),
    )


@self_or_admin_required
@csrf_required
def user_edit(user_id: str) -> Any:
    users = load_users()
    user = get_user(users, user_id)
    if user is None:
        flash("用户不存在", "danger")
        return redirect(url_for("users_list"))

    if request.method == "POST":
        # existing=user 确保空密码字段保留旧值，不会意外清除已保存的密码
        # admin 用全字段 builder；非 admin（user 自助）走白名单 builder，
        # 即使 POST 里塞 AUTO_BOOK_* / name / app_login_enabled 也被丢弃。
        try:
            if is_admin():
                updated = build_user_from_form(request.form, user_id=user_id, existing=user)
            else:
                updated = build_user_from_form_self(request.form, existing=user)
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(request.url)
        try:
            def _replace(users):
                current = get_user(users, user_id)
                if current is None:
                    raise LookupError("missing")
                if any(u.id != user_id and u.name == updated.name for u in users):
                    raise ValueError(f"用户「{updated.name}」已存在")
                idx = next(i for i, u in enumerate(users) if u.id == user_id)
                users[idx] = updated

            update_users(_replace)
        except LookupError:
            flash("用户不存在", "danger")
            return redirect(url_for("users_list"))
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(request.url)
        # App 密码变化（修改或清除）→ 撤销该用户的所有 Bearer token，
        # 避免泄漏的旧密码继续生效。app_login_enabled 切到 False 同理。
        pw_changed = (updated.app_password_hash != user.app_password_hash)
        login_disabled = (user.app_login_enabled and not updated.app_login_enabled)
        _handle_id_document(user_id)
        _log_user_change("更新", updated)
        if pw_changed or login_disabled:
            from app.api_auth import invalidate_token_cache
            st = storage()
            try:
                n = st.revoke_user_tokens(user_id)
            finally:
                st.close()
            if n:
                invalidate_token_cache()
                logger.info(
                    "用户「%s」(id=%s) App 凭证变更，已撤销 %d 个会话",
                    updated.name, user_id, n,
                )

        # 只有真的换了邮箱才重发；同一邮箱第二次保存（已 verified 继承）不发。
        if _needs_email_verification(updated, previous_email=user.email_to):
            # 普通用户走这条路时要限流：发验证邮件和「发测试通知」共用一个配额，
            # 否则它就成了一个不限次的对外发信接口。admin 不限。
            if not is_admin():
                allowed, reason = check_test_notify_rate(user_id)
                if not allowed:
                    flash(reason, "warning")
                    return _own_language_cookie(
                        redirect(url_for("user_edit", user_id=user_id)),
                        user_id, updated.language,
                    )
                record_test_notify(user_id)
            _flash_verification_email(updated)
        else:
            flash(f"✅ 用户「{updated.name}」已保存", "success")
        return _own_language_cookie(
            redirect(url_for("user_edit", user_id=user_id)),
            user_id, updated.language,
        )

    # 全平台归一后的城市并集。只用 KNOWN_CITIES 会漏掉 Xior 独有的
    # Wageningen / Venlo / Breda / Leeuwarden——用户既选不到，一旦设了
    # 城市筛选，这些楼盘的房源就被整体挡掉。
    city_names = known_city_names()
    opts = _get_all_filter_options()
    # 「App 推送」卡片显示已连接设备数。查不出来时按 0——那张卡会退回
    # 「安装 App 并登录即可」，比谎报一个数字好。
    try:
        st_dev = storage()
        try:
            device_count = len(st_dev.get_active_devices_for_user(user_id))
        finally:
            st_dev.close()
    except Exception:
        logger.warning("读取设备数失败 user_id=%s", user_id, exc_info=True)
        device_count = 0

    return render_template(
        "user_form.html", user=user, device_count=device_count,
        id_doc=__import__("applicant_docs").info(user_id),
        xior_buildings=_xior_building_options(),
        applicant_titles=APPLICANT_TITLES,
        applicant_genders=APPLICANT_GENDERS,
        action=url_for("user_edit", user_id=user_id),
        title=f"{tr('user_edit_title', get_lang())} · {user.name}",
        is_macos=(sys.platform == "darwin"),
        occupancy_options=localize_options("Occupancy", opts["Occupancy"]),
        type_options=localize_options("Type", opts["Type"]),
        city_options=city_names,
        # 平台清单来自 config.KNOWN_SOURCES —— 别在模板里再写死一份，
        # ourcampus 就是这么被漏了三次的
        source_options=[(k, source_display_name(k)) for k in KNOWN_SOURCES],
        contract_options=localize_options("Contract", opts["Contract"]),
        tenant_options=localize_options("Tenant", opts["Tenant"]),
        offer_options=opts["Offer"],
        finishing_options=opts["Finishing"],
        energy_options=sorted(
            [x for x in opts["Energy"] if x.upper() in ENERGY_LABELS] or ENERGY_LABELS,
            key=_energy_rank_or_99),
    )


@admin_required
@csrf_required
def user_delete(user_id: str) -> Any:
    try:
        def _delete(users):
            user = get_user(users, user_id)
            name = user.name if user else user_id
            new_users = [u for u in users if u.id != user_id]
            if len(new_users) == len(users):
                raise LookupError("missing")
            users[:] = new_users
            return name, len(users)

        name, remaining_count = update_users(_delete)
    except LookupError:
        flash("用户不存在", "warning")
        return redirect(url_for("users_list"))
    # 连带撤销该用户的所有 App Bearer token
    from app.api_auth import invalidate_token_cache
    from app.db import storage
    st = storage()
    try:
        revoked = st.revoke_user_tokens(user_id)
    finally:
        st.close()
    if revoked:
        invalidate_token_cache()
    logger.info(
        "用户「%s」已删除 (id=%s)，剩余 %d 个用户，连带撤销 %d 个 App 会话",
        name, user_id, remaining_count, revoked,
    )
    flash(f"用户「{name}」已删除", "success")
    return redirect(url_for("users_list"))


@self_or_admin_required
@csrf_required
def user_test_notify(user_id: str) -> Any:
    """逐渠道发送一条测试消息，返回每个渠道的成功/失败详情。"""
    from datetime import datetime as _dt
    from notifier import (
        EmailNotifier,
        IMessageNotifier,
        ResendNotifier,
        TelegramNotifier,
        WhatsAppNotifier,
        get_shared_email_config,
    )

    users = load_users()
    user = get_user(users, user_id)
    if user is None:
        return jsonify({"ok": False, "error": "用户不存在"}), 404

    # 限流：仅对非 admin 角色生效。admin 维护时不限流。
    if not is_admin():
        allowed, reason = check_test_notify_rate(user_id)
        if not allowed:
            return jsonify({"ok": False, "error": reason}), 429
        record_test_notify(user_id)

    test_msg = (
        f"🧪 FlatRadar 监控\n\n"
        f"这是一条通知测试消息\n"
        f"发送时间：{_dt.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"通知配置正确 ✅"
    )

    results: list[dict] = []

    async def _send_and_close(notifier_obj: Any, msg: str) -> bool:
        """发送测试消息，无论成功与否都确保关闭 notifier（释放 curl_cffi Session 等）。"""
        try:
            return await notifier_obj._send(msg)
        finally:
            await notifier_obj.close()

    for channel in user.notification_channels:
        ch = channel.strip().lower()

        if ch == "imessage":
            if not user.imessage_recipient:
                results.append({"channel": "iMessage", "ok": False, "error": "收件人未配置"})
                continue
            notifier_obj = IMessageNotifier(user.imessage_recipient)
            label = f"iMessage → {user.imessage_recipient}"

        elif ch == "telegram":
            if not user.telegram_token or not user.telegram_chat_id:
                results.append({"channel": "Telegram", "ok": False, "error": "Token 或 Chat ID 未配置"})
                continue
            notifier_obj = TelegramNotifier(user.telegram_token, user.telegram_chat_id)
            label = f"Telegram → {user.telegram_chat_id}"

        elif ch == "email":
            mode = (getattr(user, "email_mode", "shared") or "shared").lower()
            if mode == "shared":
                if not user.email_to:
                    results.append({"channel": "Email (shared)", "ok": False, "error": "收件邮箱未配置"})
                    continue
                shared_ok, shared_key, shared_from = get_shared_email_config()
                if not shared_ok:
                    results.append({
                        "channel": "Email (shared)",
                        "ok": False,
                        "error": "邮件服务暂不可用" if not is_admin()
                                 else "服务器未配置 Resend (admin 需设 RESEND_API_KEY / RESEND_FROM)",
                    })
                    continue
                if not user.email_verified:
                    results.append({
                        "channel": "Email (shared)",
                        "ok": False,
                        "error": "收件邮箱未验证，请到设置页完成 double-opt-in 验证",
                    })
                    continue
                notifier_obj = ResendNotifier(shared_key, shared_from, user.email_to, user_id=user.id)
                label = f"Email (shared) → {user.email_to}"
            else:
                has_auth = bool(user.email_username or user.email_password)
                if not user.email_smtp_host or not user.email_to or not (user.email_from or user.email_username):
                    results.append({"channel": "Email", "ok": False, "error": "SMTP 主机、发件人或收件人未配置"})
                    continue
                if has_auth and not (user.email_username and user.email_password):
                    results.append({"channel": "Email", "ok": False, "error": "SMTP 用户名和密码需要同时填写"})
                    continue
                notifier_obj = EmailNotifier(
                    user.email_smtp_host,
                    user.email_smtp_port,
                    user.email_smtp_security,
                    user.email_username,
                    user.email_password,
                    user.email_from,
                    user.email_to,
                )
                label = f"Email → {user.email_to}"

        elif ch == "whatsapp":
            if not all([user.twilio_sid, user.twilio_token, user.twilio_from, user.twilio_to]):
                results.append({"channel": "WhatsApp", "ok": False, "error": "Twilio 参数不完整"})
                continue
            notifier_obj = WhatsAppNotifier(
                user.twilio_sid, user.twilio_token, user.twilio_from, user.twilio_to
            )
            label = f"WhatsApp → {user.twilio_to}"

        else:
            results.append({"channel": ch, "ok": False, "error": "未知渠道"})
            continue

        try:
            ok = _run_async(_send_and_close(notifier_obj, test_msg))
            results.append({"channel": label, "ok": ok,
                            "error": None if ok else "发送失败，请检查日志"})
        except Exception as e:
            # 这条路由是 @self_or_admin_required——普通用户点一下「测试通知」
            # 就能触发。异常文本来自 Telegram / Twilio SDK，内容不受我们控制，
            # 可能带上 token 片段或内部 URL，所以只回固定文案。
            logger.exception("user_test_notify(%s) channel=%s failed: %s",
                             user_id, ch, e)
            results.append({"channel": label, "ok": False,
                            "error": "发送失败，请检查日志"})

    # 设备推送也要测。它不在 notification_channels 里——那个字段只列外部渠道，
    # APNs / FCM 走的是 device_tokens。只测外部渠道的话，一个只用 App、没配
    # 邮件的用户点「确认能收到」会得到「未配置任何通知渠道」，而他的投递明明
    # 是好的。按钮承诺的是「确认能收到」，就得测真正在生效的那条路。
    # 总开关关着时真实投递也不会推（mcore/push.py:_user_wants_push），测试
    # 跟着不推——测试结果比真实投递乐观，比不测还糟。
    if getattr(user, "notifications_enabled", True):
        results.extend(_test_push_to_devices(user_id))

    if not results:
        return jsonify({"ok": False, "results": [], "error": "该用户未配置任何通知渠道"})

    return jsonify({"ok": any(r["ok"] for r in results), "results": results})


def _test_push_to_devices(user_id: str) -> list[dict]:
    """给该用户的活跃设备发一条测试推送。没有设备时返回空列表（不是失败）。"""
    from app.db import storage

    st = storage()
    try:
        devices = st.get_active_devices_for_user(user_id)
        if not devices:
            return []
        from mcore import push as _push

        sent = _run_async(_push.dispatch_announcement_to_user(
            st, user_id, "🧪 FlatRadar", "通知配置正确 ✅",
        ))
    except Exception as e:
        logger.exception("测试推送失败 user_id=%s", user_id)
        return [{"channel": "设备推送", "ok": False, "error": "发送失败，请检查日志"}]
    finally:
        st.close()

    n = len(devices)
    return [{
        "channel": f"设备推送（{n}）" if n > 1 else "设备推送",
        "ok": sent > 0,
        # 登记了设备但一条都没发出去，最常见的原因是 token 已经失效（换机、
        # 重装、长期未打开）。说出设备数才能让用户看懂 "0/2" 是什么意思。
        "error": None if sent > 0 else f"{n} 台设备均未送达",
    }]


@admin_required
@csrf_required
def user_move(user_id: str) -> Any:
    """调整用户在自动预订中的优先级（上移/下移）。"""
    direction = (request.form.get("direction") or "").strip().lower()
    if direction not in ("up", "down"):
        flash("无效的移动方向", "warning")
        return redirect(url_for("users_list"))

    from app.db import storage
    st = storage()
    try:
        ok = st.reorder_user(user_id, direction)
    finally:
        st.close()

    if ok:
        direction_label = "上移" if direction == "up" else "下移"
        _request_monitor_reload("调整优先级")
        flash(f"用户优先级已{direction_label}", "success")
    else:
        flash("已在边界，无法移动", "info")
    return redirect(url_for("users_list"))


@admin_required
@csrf_required
def user_toggle(user_id: str) -> Any:
    """快速开关用户启用状态。"""
    try:
        def _toggle(users):
            for u in users:
                if u.id == user_id:
                    u.enabled = not u.enabled
                    return u
            raise LookupError("missing")

        user = update_users(_toggle)
        logger.info("用户「%s」(id=%s) 已%s", user.name, user.id, "启用" if user.enabled else "停用")
    except LookupError:
        flash("用户不存在", "warning")
    return redirect(url_for("users_list"))


@admin_required
@csrf_required
def api_users_reorder() -> Any:
    """批量更新用户抢房优先级顺序（拖拽排序）。"""
    data = request.get_json(silent=True) or {}
    order: list[str] = data.get("order", [])
    if not order or not isinstance(order, list):
        return jsonify({"ok": False, "error": "缺少 order 数组"}), 400

    from app.db import storage
    st = storage()
    try:
        st.reorder_users_bulk(order)
    finally:
        st.close()

    logger.info("用户优先级已批量更新（%d 位用户）", len(order))
    _request_monitor_reload("批量调整优先级")
    return jsonify({"ok": True, "count": len(order)})


def register(app: Flask) -> None:
    app.add_url_rule("/users",                       endpoint="users_list",       view_func=users_list,       methods=["GET"])
    app.add_url_rule("/users/new",                   endpoint="user_new",         view_func=user_new,         methods=["GET", "POST"])
    app.add_url_rule("/users/<user_id>",             endpoint="user_edit",        view_func=user_edit,        methods=["GET", "POST"])
    app.add_url_rule("/users/<user_id>/delete",      endpoint="user_delete",      view_func=user_delete,      methods=["POST"])
    app.add_url_rule("/users/<user_id>/test",        endpoint="user_test_notify", view_func=user_test_notify, methods=["POST"])
    app.add_url_rule("/users/<user_id>/toggle",      endpoint="user_toggle",      view_func=user_toggle,      methods=["POST"])
    app.add_url_rule("/users/<user_id>/move",       endpoint="user_move",        view_func=user_move,       methods=["POST"])
    app.add_url_rule("/api/users/reorder",         endpoint="api_users_reorder", view_func=api_users_reorder, methods=["POST"])
