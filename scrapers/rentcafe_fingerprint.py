"""RENTCafe / SecureRC 的 TLS 指纹冷却状态。

OurDomain、OurCampus、Xior 的抓取和 RentCafe 预订打的是同一个 SecureRC，指纹
是不是「被烧了」对两边是同一个事实，所以共用这一份进程内状态——一边记下的
403，另一边下一次就会避开。

从 scrapers/ourdomain.py 拆出；那边保留旧的下划线名字作别名。
"""
from __future__ import annotations

import os
import re
import time

from env_utils import env_int


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    return env_int(name, default, minimum=min_value, maximum=max_value)


# SecureRC（OurDomain / OurCampus / Xior 的 floorplans.aspx）专用 TLS 指纹池。
#
# **按实测挑，不按「新」挑。** 2026-09-15 从生产容器经轮换代理实测（curl_cffi
# 0.16.3），每个指纹 3 个独立会话，每个会话打 OC 页面 GET + 单元表 POST、
# OD 页面 GET + 单元表 GET：
#
#   全过       chrome150  safari2601  safari184  safari184_ios  safari18_0
#              safari180  safari180_ios  safari17_2_ios  safari172_ios  safari170
#   偶发 403   firefox147  firefox144  firefox135  safari260_ios  safari18_0_ios
#   全部 403   chrome146  chrome145  chrome142  chrome136  chrome133a  edge101
#              chrome131_android  safari260；chrome131 / chrome124 页面过、POST 403
#
# **桌面 Chrome 只有 chrome150 过，146 及以前全部 403。** 推测是 Cloudflare 把
# 明显落后于当前稳定版的 Chrome 当可疑——若成立，Chrome 指纹几个月就会过期，
# 而 Safari 老版本一直能用。所以 Safari 打底，chrome150 只占一个位置。旧池排
# 第一的正是 chrome136，生产日志里每次冷启动 OC 都是 chrome136 403 → chrome131
# 403 → safari18_0 才过。
#
# 这是某一天的快照，WAF 策略会变。重测方法：临时脚本对每个候选各开几个新会话
# 打上面四个端点（不要在生产进程里跑——冷却状态是进程级的，测试会污染它）。
# 默认 OURDOMAIN_WAF_RETRIES=4 只取前 4 个，所以前 4 个必须是全过的。
_DEFAULT_IMPERSONATES: tuple[str, ...] = (
    "safari2601",         # macOS Safari 26.0.1 —— curl_cffi 里最新的 Safari
    "chrome150",          # 桌面 Chrome 150，唯一实测能过的桌面 Chrome（需 curl_cffi ≥ 0.16）
    "safari184",          # macOS Safari 18.4
    "safari18_0",         # macOS Safari 18.0
    # ── 兜底：全过但更旧，或偶发 403 ──
    "safari184_ios",      # iOS Safari 18.4
    "safari180_ios",      # iOS Safari 18.0
    "firefox147",         # Firefox 147（NSS 栈，换一个家族；偶发 403 所以排后）
    "safari17_2_ios",
)


# ────────────────────────────────────────────────────────────────────
# TLS 指纹状态（进程级，跨 scrape() 调用持久化）
# ────────────────────────────────────────────────────────────────────
#
# 痛点
# ----
# SecureRC（OurDomain 用的 RentCafe + Cloudflare）做 per-fingerprint 跟踪：
# 同一指纹短时间内重复打它，会进入"挑战中"状态返 403。原实现每次
# scrape() 都从 chrome131 重新试，等于把"被烧"的指纹反复用——chrome131
# / chrome124 看起来"特别容易 block"其实就是因为它们总是被最先试。
#
# 改进
# ----
# - 成功通过的指纹记录 last_good_at，下次 scrape() 从它开始
# - 403 失败的指纹标记 cooldown_until = now + 30 min，期内不再优先用
# - 排序：[上次 last_good] → [未冷却的] → [冷却中的兜底]
# - 全员冷却时仍可用——冷却中的会被放到队尾，至少给条出路
#
# 这是进程内存，monitor 重启清空（重启后等于"忘掉之前的烧"，从配置
# 顺序重新探）。够用——TLS 指纹热度本身就是分钟级现象。
_FINGERPRINT_COOLDOWN_SEC = 1800  # 30 分钟


_FINGERPRINT_STATE: dict[str, dict[str, float]] = {}
# 例：{"chrome131": {"last_good_at": 1234.5, "cooldown_until": 0.0}}


def _mark_fingerprint_good(impersonate: str) -> None:
    state = _FINGERPRINT_STATE.setdefault(impersonate, {})
    state["last_good_at"] = time.monotonic()
    state["cooldown_until"] = 0.0  # 成功 = 解除冷却


def _mark_fingerprint_blocked(impersonate: str) -> None:
    state = _FINGERPRINT_STATE.setdefault(impersonate, {})
    state["cooldown_until"] = time.monotonic() + _FINGERPRINT_COOLDOWN_SEC


def _is_in_cooldown(impersonate: str) -> bool:
    state = _FINGERPRINT_STATE.get(impersonate, {})
    until = state.get("cooldown_until", 0.0)
    return until > time.monotonic()


def _impersonate_attempts() -> list[str]:
    """
    生成本轮要尝试的 TLS 指纹列表，**按状态智能排序**。

    顺序逻辑
    --------
    1. 上次成功用过的指纹（last_good_at 最新）排首位——稳态下基本只用 1 个
    2. 未在 cooldown 期的、按配置原顺序
    3. cooldown 中的兜底（万一其它全失败，至少能挣扎一下）

    Returns
    -------
    list[str]，长度受 OURDOMAIN_WAF_RETRIES 限制。
    """
    raw = os.environ.get("OURDOMAIN_IMPERSONATES", "")
    configured = [p.strip() for p in re.split(r"[,|]", raw) if p.strip()]
    # 不再把全局池随机抽的一个插到最前：全局池是给 Plaza / Magis / Student
    # Experience 这些不挑指纹的平台用的，里面有桌面 Chrome，而 SecureRC 此刻
    # 一律 403（见 _DEFAULT_IMPERSONATES 的实测）。插在最前等于每次冷启动先白
    # 扔一次必然 403 的请求，还顺手把它记进冷却。
    candidates = configured or list(_DEFAULT_IMPERSONATES)
    unique = list(dict.fromkeys(candidates))
    retries = _env_int("OURDOMAIN_WAF_RETRIES", min(4, len(unique)), min_value=1, max_value=8)

    # ── 按状态分桶 ─────────────────────────────────────────────
    # cool 必须**先**算并从 last_good 里排除：``_mark_fingerprint_blocked`` 只写
    # cooldown_until、不清 last_good_at，所以「成功过 + 正在冷却」的指纹会同时
    # 落进两个桶。下面 dict.fromkeys 保留首次出现的位置，它就会被排到最前——
    # 冷却对最该冷却的那个指纹完全失效，每轮白扔一次必然 403 的尝试。
    cool = [imp for imp in unique if _is_in_cooldown(imp)]
    last_good = sorted(
        (
            imp for imp in unique
            if imp not in cool
            and _FINGERPRINT_STATE.get(imp, {}).get("last_good_at", 0)
        ),
        key=lambda imp: _FINGERPRINT_STATE[imp].get("last_good_at", 0),
        reverse=True,  # 最近成功的排最前
    )
    fresh = [imp for imp in unique if imp not in last_good and imp not in cool]

    # 合并：last_good → fresh → cooldown（去重保序）
    ordered = list(dict.fromkeys([*last_good, *fresh, *cool]))
    return ordered[:retries]
