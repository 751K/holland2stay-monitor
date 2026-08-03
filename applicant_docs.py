"""applicant_docs.py — 申请人证件文件的加密落盘存储

为什么要存
----------
RENTCafe 在申请表上有一项必填文档 ``ID/Passport Upload*``，**服务端在它上传前
拒绝保存任何内容**（2026-08-03 实测）。而自动预订是**异步**的——系统在房源出现
的那一刻才触发，可能是用户配置完几小时甚至几天以后。所以不存在「用完即走的
透传」：要代传，文件就必须先留在系统里。

这是一个明确的取舍，由用户在知情后决定（当前部署只服务少量熟人，自动预订未
对外开放）。取舍既然做了，实现上就把能降的风险降到底：

- **不进数据库。** 用户配置每轮都要加载，几 MB 的护照扫描件塞进去会被反复读进
  内存。文件单独落盘，只有真正要上传的那一刻才读。
- **静态加密。** 复用 ``DATA_ENCRYPTION_KEY``（Fernet），和密码/token 同一把钥匙。
- **一人一份，删得掉。** 面板上能看到存了什么、能一键删除。

为什么在这里重复平台的校验规则
------------------------------
大小/扩展名/文件名的限制抄自上传控件自己的 JS。**在面板上就拦下来**，用户当场
就知道；等到抢房那一刻才发现文件不合规，房子已经没了。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: 抄自上传控件的 JS（rcLoadContent.ashx?contentclass=PropertySiteImageUpload）
MAX_BYTES = 5_242_880          # 5 MiB
MAX_NAME_LEN = 100
ALLOWED_EXT = frozenset({
    ".gif", ".jpeg", ".png", ".jpg", ".pjpeg", ".bmp", ".x-png",
    ".pdf", ".doc", ".docx", ".xlsx",
})
#: 平台不接受的文件名字符
_BAD_NAME_RE = re.compile(r'[\\/:*?"<>|]')

#: 文件名和内容之间的分隔符（文件名里不可能出现 NUL）
_SEP = b"\x00"


class DocumentRejected(ValueError):
    """文件不符合平台的限制。消息直接给用户看。"""


def _dir() -> Path:
    from config import DATA_DIR

    d = Path(DATA_DIR) / "applicant_docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(user_id: str) -> Path:
    # user_id 是系统生成的十六进制 id，但仍然过一遍白名单——绝不能让它参与
    # 路径拼接时带上 ".." 或分隔符。
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(user_id))
    if not safe:
        raise ValueError("user_id 不合法")
    return _dir() / f"{safe}.bin"


def validate(filename: str, data: bytes) -> None:
    """按平台规则校验；不合规抛 :class:`DocumentRejected`。"""
    name = (filename or "").strip()
    if not name:
        raise DocumentRejected("没有文件名。")
    if len(name) > MAX_NAME_LEN:
        raise DocumentRejected(f"文件名超过 {MAX_NAME_LEN} 个字符。")
    if _BAD_NAME_RE.search(name):
        raise DocumentRejected('文件名不能包含 \\ / : * ? " < > | 这些字符。')
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext not in ALLOWED_EXT:
        raise DocumentRejected(
            "平台只接受这些格式：" + "、".join(sorted(e[1:] for e in ALLOWED_EXT))
        )
    if not data:
        raise DocumentRejected("文件是空的。")
    if len(data) > MAX_BYTES:
        raise DocumentRejected(
            f"文件 {len(data) / 1048576:.1f} MB，超过平台上限 5 MB。"
        )


def save(user_id: str, filename: str, data: bytes) -> None:
    """加密存下某个用户的证件（每人一份，覆盖旧的）。"""
    from crypto import _get_cipher

    validate(filename, data)
    payload = filename.strip().encode() + _SEP + data
    token = _get_cipher().encrypt(payload)
    p = _path(user_id)
    # 先写临时文件再改名：中途崩掉不会留下半个文件，导致抢房时读出损坏内容。
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(token)
    tmp.replace(p)
    logger.info("已存证件 user=%s file=%s (%d 字节)", user_id, filename, len(data))


def load(user_id: str) -> tuple[str, bytes] | None:
    """读回 ``(文件名, 内容)``；没有则返回 None。

    解密失败也返回 None 并记一条 error——换过 ``DATA_ENCRYPTION_KEY`` 时不该
    让整条预订链路崩掉，当成「没有证件」处理即可（后果是保存被拒，会如实
    告诉用户）。
    """
    from crypto import _get_cipher

    p = _path(user_id)
    if not p.exists():
        return None
    try:
        payload = _get_cipher().decrypt(p.read_bytes())
    except Exception:
        logger.error("证件解密失败 user=%s（DATA_ENCRYPTION_KEY 换过？）", user_id)
        return None
    name, _, data = payload.partition(_SEP)
    return name.decode(errors="replace"), data


def info(user_id: str) -> tuple[str, int] | None:
    """给面板显示用的 ``(文件名, 字节数)``；没有则 None。"""
    got = load(user_id)
    return (got[0], len(got[1])) if got else None


def delete(user_id: str) -> bool:
    """删掉某个用户的证件。返回是否真的删了。"""
    p = _path(user_id)
    if not p.exists():
        return False
    p.unlink()
    logger.info("已删除证件 user=%s", user_id)
    return True


def has(user_id: str) -> bool:
    return _path(user_id).exists()
