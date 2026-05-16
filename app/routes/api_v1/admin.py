"""
API v1 管理端点（admin only）
=====================================

iOS App admin role 远程做"应急运维"用的子集：
- 看一眼有几个用户、谁挂了
- 把某个用户暂时禁掉 / 删掉
- 启停 / 重载监控进程

不包含
------
- 新建用户 / 编辑通知渠道凭证 / 自动预订配置 —— 这些字段太多、密码处理
  复杂，依然走 Web 后台。手机上做错代价高。
- 全局 `.env` 编辑、关闭 Web 自身（``/api/shutdown`` 现有 Web 端有，
  iOS 没必要暴露这种"自杀"操作）。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys

from flask import Blueprint, jsonify, request

from app import api_auth, api_errors as _err
from app.api_auth import invalidate_token_cache
from app.db import storage
from app.process_ctrl import (
    monitor_pid,
    supervisorctl_available,
    supervisorctl_monitor,
    write_reload_request,
)
from config import BASE_DIR
from users import get_user, load_users, update_users

logger = logging.getLogger(__name__)


# ── Users ──────────────────────────────────────────────────────────


def _sync_app_user_or_raise(user) -> None:
    st = storage()
    try:
        existing = st.get_app_user_by_name(user.name)
        if existing is not None and existing.get("id") != user.id:
            current_ids = {u.id for u in load_users()}
            existing_id = existing.get("id") or ""
            if existing_id in current_ids:
                raise ValueError(
                    f"App 登录账号 {user.name!r} 已绑定到另一个用户"
                )
            st.delete_app_user(existing_id)
        st.sync_app_user_from_config(user)
    finally:
        st.close()


def _summarize_user(u, app_token_count: int) -> dict:
    """脱敏 + 摘要：给 iOS 列表用，省去 Web 表单字段（密码 / SMTP / Twilio 等）。"""
    lf = u.listing_filter
    return {
        "id": u.id,
        "name": u.name,
        "enabled": u.enabled,
        "notifications_enabled": u.notifications_enabled,
        "channel_count": len(u.notification_channels),
        "channels": u.notification_channels,
        "app_login_enabled": u.app_login_enabled,
        "has_app_password": bool(u.app_password_hash),
        "active_devices": app_token_count,
        "auto_book_enabled": u.auto_book.enabled,
        "filter_summary": {
            "max_rent": lf.max_rent,
            "min_area": lf.min_area,
            "min_floor": lf.min_floor,
            "cities": lf.allowed_cities,
            "energy": lf.allowed_energy,
            "filter_active": not lf.is_empty(),
        },
    }


def _users_list():
    try:
        users = load_users()
    except RuntimeError as e:
        return _err.err_server_error(e, "用户配置文件损坏")
    st = storage()
    try:
        # 顺手统计每个 user 当前活跃设备数（active tokens）
        token_counts: dict[str, int] = {}
        rows = st.list_app_tokens()
        for r in rows:
            uid = r.get("user_id") or ""
            if uid:
                token_counts[uid] = token_counts.get(uid, 0) + 1
    finally:
        st.close()
    items = [_summarize_user(u, token_counts.get(u.id, 0)) for u in users]
    return _err.ok({"items": items, "total": len(items)})


def _user_toggle(user_id: str):
    """翻转 enabled —— 立刻生效（下一轮 monitor.run_once 跳过该用户）。"""
    try:
        def _toggle(users):
            user = get_user(users, user_id)
            if user is None:
                raise LookupError("missing")
            user.enabled = not user.enabled
            return user

        user = update_users(_toggle)
    except RuntimeError as e:
        return _err.err_server_error(e, "用户配置文件损坏")
    except LookupError:
        return _err.err_not_found("用户不存在")
    try:
        _sync_app_user_or_raise(user)
    except Exception as e:
        logger.exception("同步 app_users 失败，回滚 toggle user=%s", user_id)
        try:
            update_users(_toggle)
        except Exception:
            logger.exception("回滚 users.json toggle 失败 user=%s", user_id)
        return _err.err_server_error(e, "用户状态保存失败")
    logger.info("admin toggled user=%s enabled=%s", user.name, user.enabled)
    return _err.ok({"id": user.id, "enabled": user.enabled})


def _user_delete(user_id: str):
    """删除用户 + 连带撤销其 App Bearer token（避免 token 失主）。"""
    try:
        def _delete(users):
            user = get_user(users, user_id)
            name = user.name if user else user_id
            new_users = [u for u in users if u.id != user_id]
            if len(new_users) == len(users):
                raise LookupError("missing")
            users[:] = new_users
            return name

        name = update_users(_delete)
    except RuntimeError as e:
        return _err.err_server_error(e, "用户配置文件损坏")
    except LookupError:
        return _err.err_not_found("用户不存在")

    st = storage()
    try:
        revoked = st.revoke_user_tokens(user_id)
        st.delete_app_user(user_id)
    finally:
        st.close()
    if revoked:
        invalidate_token_cache()
    logger.info("admin deleted user=%s; revoked %d App sessions", name, revoked)
    return _err.ok({"deleted": True, "name": name, "revoked_sessions": revoked})


# ── Monitor process control ────────────────────────────────────────


def _terminate(pid: int) -> None:
    """跨平台终止进程；优先 SIGTERM，Windows 直接 taskkill。"""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            check=False, capture_output=True,
        )
        return
    os.kill(pid, signal.SIGTERM)


def _monitor_status():
    pid = monitor_pid()
    st = storage()
    try:
        last_scrape = st.get_meta("last_scrape_at", default="")
        last_count = st.get_meta("last_scrape_count", default="")
    finally:
        st.close()
    return _err.ok({
        "running": pid is not None,
        "pid": pid,
        "last_scrape": last_scrape,
        "last_count": last_count,
    })


def _monitor_start():
    if monitor_pid() is not None:
        return _err.err_validation("监控已在运行")
    try:
        if supervisorctl_available():
            r = supervisorctl_monitor("start")
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout or "supervisorctl start failed").strip())
            return _err.ok({"started": True, "method": "supervisor"})
        if getattr(sys, "frozen", False):
            subprocess.Popen(
                [sys.executable, "--run-monitor"],
                cwd=str(BASE_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                [sys.executable, str(BASE_DIR / "monitor.py")],
                cwd=str(BASE_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        logger.exception("monitor start 失败")
        return _err.err_server_error(e, "启动失败")
    return _err.ok({"started": True})


def _monitor_stop():
    pid = monitor_pid()
    if pid is None:
        return _err.err_validation("监控未在运行")
    try:
        if supervisorctl_available():
            r = supervisorctl_monitor("stop")
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout or "supervisorctl stop failed").strip())
            return _err.ok({"stopped": True, "pid": pid, "method": "supervisor"})
        _terminate(pid)
    except Exception as e:
        logger.exception("monitor stop 失败")
        return _err.err_server_error(e, "停止失败")
    return _err.ok({"stopped": True, "pid": pid})


def _monitor_reload():
    """触发监控热重载（重读 users.json / .env）。"""
    pid = monitor_pid()
    if pid is None:
        return _err.err_validation("监控未在运行")

    # Windows 没有可靠 SIGHUP → 走文件触发；POSIX 优先信号，失败再 fallback
    if os.name == "nt" or not hasattr(signal, "SIGHUP"):
        try:
            write_reload_request()
            return _err.ok({"reload": True, "method": "file"})
        except Exception as e:
            return _err.err_server_error(e, "写 reload 请求失败")

    try:
        os.kill(pid, signal.SIGHUP)
        return _err.ok({"reload": True, "method": "signal"})
    except Exception:
        try:
            write_reload_request()
            return _err.ok({"reload": True, "method": "file"})
        except Exception as e:
            return _err.err_server_error(e, "reload 失败")


# ── Registration ───────────────────────────────────────────────────


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/admin/users",
        endpoint="admin_users_list",
        view_func=api_auth.bearer_required(("admin",))(_users_list),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/admin/users/<string:user_id>/toggle",
        endpoint="admin_user_toggle",
        view_func=api_auth.bearer_required(("admin",))(_user_toggle),
        methods=["POST"],
    )
    bp.add_url_rule(
        "/admin/users/<string:user_id>",
        endpoint="admin_user_delete",
        view_func=api_auth.bearer_required(("admin",))(_user_delete),
        methods=["DELETE"],
    )
    bp.add_url_rule(
        "/admin/monitor/status",
        endpoint="admin_monitor_status",
        view_func=api_auth.bearer_required(("admin",))(_monitor_status),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/admin/monitor/start",
        endpoint="admin_monitor_start",
        view_func=api_auth.bearer_required(("admin",))(_monitor_start),
        methods=["POST"],
    )
    bp.add_url_rule(
        "/admin/monitor/stop",
        endpoint="admin_monitor_stop",
        view_func=api_auth.bearer_required(("admin",))(_monitor_stop),
        methods=["POST"],
    )
    bp.add_url_rule(
        "/admin/monitor/reload",
        endpoint="admin_monitor_reload",
        view_func=api_auth.bearer_required(("admin",))(_monitor_reload),
        methods=["POST"],
    )
