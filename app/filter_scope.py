"""过滤维度的适用范围文案：某个筛选条件实际对哪些平台生效。

从 config.py 拆出——这是给界面看的提示语（含中英文），不属于配置。
数据来自 ``mcore.sources`` 的维度能力表。
"""
from __future__ import annotations

from mcore.sources import KNOWN_SOURCES, source_display_name, sources_supporting_dim


def dim_scope_note(dim: str, lang: str = "zh") -> str:
    """维度的适用范围提示；全平台通用时返回空串（无需提示）。"""
    supported = sources_supporting_dim(dim)
    if not supported or len(supported) == len(KNOWN_SOURCES):
        return ""
    names = "、" if lang == "zh" else ", "
    listed = names.join(source_display_name(s) for s in supported)
    if lang == "zh":
        return f"仅对 {listed} 生效；其余平台不提供该属性，其房源不受此条件影响"
    return (f"Applies to {listed} only — other platforms do not report this "
            f"attribute, so their listings are unaffected by this filter")


#: 徽标里最多点名几个平台，多出来的折成「等 N 家」。见 dim_scope_badge。
_BADGE_MAX_NAMES = 2


def dim_scope_badge(dim: str, lang: str = "zh") -> str:
    """紧凑版的适用范围标记，给列表页那种横排筛选栏用。

    完整的一句话在那里会把整行撑散，但完全不提又回到「用户不知道」的老问题，
    所以留一个短标记，完整说明放 tooltip。

    **一定要点名平台，不要只报个数。** 原先「仅 3 个平台」既不说是哪 3 个，也
    不说缺谁，读的人只能去开 tooltip；2026-08-21 实际被误读成「和 Holland2Stay
    有关」，而那三个维度恰恰是 Holland2Stay 全都支持的。

    规则是**说短的那一边**——哪边字少就报哪边，信息量一样而字数更省：

        缺的少   → 「Xior 除外」
        支持的少 → 「仅 Holland2Stay」

    比的是**渲染后的字数**，不是平台个数。两者不等价：``occupancy`` 是 3 支持 /
    3 不支持，按个数是平手（旧实现平手时报支持方，得到 34 字），按字数则缺失方
    32 字更短。平台名长短差得很远（"Xior" 4 字 vs "Student Experience" 18 字），
    数个数会挑错边。

    名字数封顶
    ----------
    只列前 ``_BADGE_MAX_NAMES`` 个，其余折成「等 N 家」。这条是随平台数增长的
    刹车：六个平台时中间分裂的维度两边各三个名字，不封顶就是三十多字，塞在
    10px 的 label 后面会把整行顶散（templates/listings.html 的筛选栏）。

    折叠**保留点名**，只是名字不全：「Xior、Magis 等 3 家除外」既说了是谁，也说了
    有几个。这与「不要只报个数」的规矩不冲突——当年被误读的是「仅 3 个平台」那种
    一个名字都没有的写法（2026-08-21），完整清单一直在 tooltip 里。
    """
    supported = sources_supporting_dim(dim)
    if not supported or len(supported) == len(KNOWN_SOURCES):
        return ""
    missing = [s for s in KNOWN_SOURCES if s not in supported]

    def _render(sources: list[str], *, positive: bool) -> tuple[bool, str]:
        """→ (是否折叠过, 徽标文本)。"""
        sep = "、" if lang == "zh" else ", "
        names = [source_display_name(s) for s in sources]
        folded = len(names) > _BADGE_MAX_NAMES
        if folded:
            shown = sep.join(names[:_BADGE_MAX_NAMES])
            listed = (f"{shown} 等 {len(names)} 家" if lang == "zh"
                      else f"{shown} +{len(names) - _BADGE_MAX_NAMES} more")
        else:
            listed = sep.join(names)
        if positive:
            return folded, (f"仅 {listed}" if lang == "zh" else f"{listed} only")
        # 中文不在「家」后面再空一格；平台名以拉丁字母收尾时才需要那个空格。
        gap = "" if lang == "zh" and listed[-1:] in ("家", "）") else " "
        return folded, (f"{listed}{gap}除外" if lang == "zh"
                        else f"Except {listed}")

    # 排序键先看折没折叠：折叠是**丢信息**（清单不全），只有在不折叠那边明显更长
    # 时才值得。finishing 就是这个情形——支持方折叠后 30 字、缺失方完整 31 字，
    # 按纯字数会挑走那个信息不全的，差一个字不值当。
    return min(_render(supported, positive=True),
               _render(missing, positive=False),
               key=lambda r: (r[0], len(r[1])))[1]

