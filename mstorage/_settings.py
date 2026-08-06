"""app_settings 的 SQLite 持久化——系统级运维配置。

为什么不继续用 .env
-------------------
``runtime`` 类的键（轮询节奏、监控范围）由 Web 面板在**运行时**改写，而 ``.env``
同时还是人手写的部署产物。一份文件两个写入者，代价已经摆在代码里：
``app/env_writer.py`` 为多 worker 并发写加了一把锁，``config.write_env_key()``
不能用 ``dotenv.set_key()``（内部 ``os.replace()`` 会断 Docker 的 bind mount）。

搬进这里之后 ``.env`` 退回只读，并且顺带拿到两样文件给不了的东西：
**改动有时间戳和操作者**，以及**一次写入即时可见**，不必重写文件再发信号。

为什么不用现成的 meta 表
------------------------
``meta`` 存的是程序自己的运行状态（心跳时间、节流时间戳、uptime 采样），会被
清理逻辑和调试随手改写。配置是人的意图，两者混在一张表里，「谁把 CHECK_INTERVAL
改成 20 的」将无从追查。
"""

from __future__ import annotations

from mstorage._tokens import _utc_now_iso


class AppSettingOps:
    """系统配置的读写。键名的权威清单在 ``env_registry.py``。"""

    def get_app_setting(self, key: str) -> str | None:
        """取一个配置值；没有则 None。

        空串是**有效值**（例如「不监控任何 Xior 楼栋」），因此用 None 而不是空串
        表示「没设过」。
        """
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def all_app_settings(self) -> dict[str, str]:
        return {
            r[0]: r[1]
            for r in self._conn.execute("SELECT key, value FROM app_settings")
        }

    def app_settings_meta(self) -> dict[str, tuple[str, str]]:
        """``key -> (updated_at, updated_by)``，供面板显示「谁在什么时候改的」。"""
        return {
            r[0]: (r[1] or "", r[2] or "")
            for r in self._conn.execute(
                "SELECT key, updated_at, updated_by FROM app_settings"
            )
        }

    def set_app_setting(self, key: str, value: str, *, updated_by: str = "") -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO app_settings (key, value, updated_at, updated_by)
                   VALUES (?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value,
                       updated_at=excluded.updated_at,
                       updated_by=excluded.updated_by""",
                (key, value, _utc_now_iso(), updated_by),
            )

    def set_app_settings(self, values: dict[str, str], *, updated_by: str = "") -> int:
        """批量写入，**同一个事务**。

        面板一次保存要改十几个键，逐条提交的话中途崩溃会留下一半新一半旧的配置
        ——例如 MIN_INTERVAL 已经写进去而 PEAK_INTERVAL 还是旧的，两者组合起来
        可能是个非法区间。
        """
        if not values:
            return 0
        now = _utc_now_iso()
        with self._conn:
            self._conn.executemany(
                """INSERT INTO app_settings (key, value, updated_at, updated_by)
                   VALUES (?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value,
                       updated_at=excluded.updated_at,
                       updated_by=excluded.updated_by""",
                [(k, v, now, updated_by) for k, v in values.items()],
            )
        return len(values)

    def delete_app_setting(self, key: str) -> bool:
        """删掉一个配置，使其回落到代码默认值。返回是否真的删了东西。"""
        with self._conn:
            cur = self._conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
        return cur.rowcount > 0
