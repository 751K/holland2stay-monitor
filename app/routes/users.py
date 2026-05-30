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

from config import KNOWN_CITIES
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
from app.i18n import DEFAULTS, localize_options
from config import ENERGY_LABELS, energy_rank

logger = logging.getLogger(__name__)


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
    return render_template("users.html", users=users)


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
        _log_user_change("创建", user)
        flash(f"✅ 用户「{user.name}」已创建", "success")
        return redirect(url_for("users_list"))
    # GET：空白表单
    city_names = sorted(c["name"] for c in KNOWN_CITIES)
    opts = _get_all_filter_options()
    return render_template(
        "user_form.html", user=None,
        action=url_for("user_new"), title="新增用户",
        is_macos=(sys.platform == "darwin"),
        occupancy_options=localize_options("Occupancy", opts["Occupancy"]),
        type_options=localize_options("Type", opts["Type"]),
        city_options=city_names,
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

        # shared 模式下 email_to 变化 → 发验证邮件。custom 模式跳过（用户自管）。
        # 同一邮箱第二次保存（已 verified 继承）不会发；只有真的换了邮箱才发。
        if (
            updated.email_mode == "shared"
            and updated.email_to
            and not updated.email_verified
            and updated.email_to != user.email_to  # 真的改了，不是其他字段保存
        ):
            if not is_admin():
                allowed, reason = check_test_notify_rate(user_id)
                if not allowed:
                    flash(reason, "warning")
                    return redirect(url_for("user_edit", user_id=user_id))
                record_test_notify(user_id)
            from app.email_verify import EmailVerifyConfigError, send_verification_email_sync
            try:
                sent = send_verification_email_sync(
                    updated.id, updated.name, updated.email_to,
                )
                if sent:
                    flash("📧 验证邮件已发送到新邮箱，请查收并点击链接确认", "success")
                else:
                    flash("⚠️ 邮箱已保存，但验证邮件未能发出（服务器未配置 Resend 或临时故障），通知暂不会发到此邮箱", "warning")
            except EmailVerifyConfigError as e:
                logger.error("邮箱验证未就绪: %s", e)
                flash("⚠️ 邮箱已保存，但系统未配置 PUBLIC_BASE_URL，暂时无法发送验证邮件", "warning")
            except Exception as e:
                logger.exception("发送邮箱验证邮件异常: %s", e)
                flash("⚠️ 邮箱已保存但验证邮件发送失败，请稍后重试", "warning")
        else:
            flash(f"✅ 用户「{updated.name}」已保存", "success")
        return redirect(url_for("user_edit", user_id=user_id))

    city_names = sorted(c["name"] for c in KNOWN_CITIES)
    opts = _get_all_filter_options()
    return render_template(
        "user_form.html", user=user,
        action=url_for("user_edit", user_id=user_id),
        title=f"编辑用户 · {user.name}",
        is_macos=(sys.platform == "darwin"),
        occupancy_options=localize_options("Occupancy", opts["Occupancy"]),
        type_options=localize_options("Type", opts["Type"]),
        city_options=city_names,
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
            results.append({"channel": label, "ok": False, "error": str(e)})

    if not results:
        return jsonify({"ok": False, "results": [], "error": "该用户未配置任何通知渠道"})

    return jsonify({"ok": any(r["ok"] for r in results), "results": results})


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


def register(app: Flask) -> None:
    app.add_url_rule("/users",                       endpoint="users_list",       view_func=users_list,       methods=["GET"])
    app.add_url_rule("/users/new",                   endpoint="user_new",         view_func=user_new,         methods=["GET", "POST"])
    app.add_url_rule("/users/<user_id>",             endpoint="user_edit",        view_func=user_edit,        methods=["GET", "POST"])
    app.add_url_rule("/users/<user_id>/delete",      endpoint="user_delete",      view_func=user_delete,      methods=["POST"])
    app.add_url_rule("/users/<user_id>/test",        endpoint="user_test_notify", view_func=user_test_notify, methods=["POST"])
    app.add_url_rule("/users/<user_id>/toggle",      endpoint="user_toggle",      view_func=user_toggle,      methods=["POST"])
    app.add_url_rule("/users/<user_id>/move",       endpoint="user_move",        view_func=user_move,       methods=["POST"])
