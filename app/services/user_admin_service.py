"""管理员对用户的操作：启用 / 停用、删除。

Web 后台（app/routes/users.py）和 App 管理接口（app/routes/api_v1/admin.py）原先
各写一份，几乎逐行相同。业务动作收在这里，两边的路由只负责解析参数和把结果包成
各自的响应（flash + 重定向，或 JSON）。

monitor 的热重载请求由 ``users.update_users`` 统一发，这里不再重复。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.api_auth import invalidate_token_cache
from app.db import storage
from users import UserConfig, get_user, update_users

logger = logging.getLogger(__name__)


class UserNotFound(LookupError):
    """要操作的用户不存在（可能已被别人删掉）。"""


@dataclass(frozen=True)
class DeletedUser:
    name: str
    remaining: int
    revoked_sessions: int


def toggle_user(user_id: str) -> UserConfig:
    """翻转 ``enabled``，返回修改后的用户。下一轮 monitor 即跳过 / 恢复该用户。"""
    def _toggle(users):
        user = get_user(users, user_id)
        if user is None:
            raise UserNotFound(user_id)
        user.enabled = not user.enabled
        return user

    user = update_users(_toggle)
    logger.info("用户「%s」(id=%s) 已%s", user.name, user.id, "启用" if user.enabled else "停用")
    return user


def delete_user(user_id: str) -> DeletedUser:
    """删除用户，并连带撤销其全部 App Bearer token（避免 token 失主）。"""
    def _delete(users):
        user = get_user(users, user_id)
        if user is None:
            raise UserNotFound(user_id)
        users[:] = [u for u in users if u.id != user_id]
        return user.name, len(users)

    name, remaining = update_users(_delete)
    st = storage()
    try:
        revoked = st.revoke_user_tokens(user_id)
    finally:
        st.close()
    if revoked:
        invalidate_token_cache()
    logger.info(
        "用户「%s」已删除 (id=%s)，剩余 %d 个用户，连带撤销 %d 个 App 会话",
        name, user_id, remaining, revoked,
    )
    return DeletedUser(name, remaining, revoked)
