"""展开的多选面板必须压在后面的卡片之上。

2026-09-01 反馈「平台选不到 Magis」，起初以为是列表没渲染出第五项。实际是层叠：
``.card`` 上有 ``backdrop-filter``，那让每张卡都成为**层叠上下文**——面板的
``z-index:999`` 因此被困在自己那张卡里，够不着卡外面；而后面的卡是更晚的兄弟、
同样是层叠上下文，于是整个盖上来。卡是半透明的，所以选项**看得见、点不到**。

浏览器里的命中栈（1600px 宽）：

    Magis 那一行   DIV.section-header → DIV.card → INPUT（被压在下面）
    Xior  那一行   INPUT → LABEL → DIV.ms-dropdown（正常）

**只在页面拉宽之后犯。** ``.form-row`` 是 auto-fit：窄屏时字段各占一行，平台那格
离卡底还远，面板伸不出本卡；拉宽后字段并排，那一格贴着卡底，面板才探进下一张卡。

所以它和「平台从四个变五个」没有关系——列表变长只是让第五项恰好落进重叠区，把一个
一直存在的问题暴露出来。四项的时候最后一项刚好在重叠线以上。
"""
from __future__ import annotations

import re
from pathlib import Path


def _css() -> str:
    raw = (Path(__file__).parent.parent / "static" / "design.css").read_text(
        encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", raw, flags=re.S)


def test_open_dropdown_is_lifted_above_later_cards():
    """展开的多选面板必须压在后面的卡片之上。

    ``.card`` 上有 ``backdrop-filter``，那会让每张卡都成为**层叠上下文**——面板
    的 ``z-index:999`` 因此被困在自己那张卡里，够不着卡外面；而后面的卡是更晚的
    兄弟、同样是层叠上下文，于是整个盖上来。卡是半透明的，所以选项**看得见、
    点不到**，命中测试返回的是下一张卡的 ``.section-header``。

    只在页面拉宽之后犯：``.form-row`` 是 auto-fit，窄屏时字段各占一行、面板伸不
    出本卡；拉宽后字段并排，平台那格贴着卡底，面板才探进下一张卡。所以它和「平台
    从四个变五个」无关——列表变长只是让第五项恰好落进了重叠区，把问题暴露出来。

    这条守两件事：backdrop-filter 还在（它是成因），以及提升规则还在。
    """
    no_comments = _css()

    assert "backdrop-filter" in no_comments, (
        "卡片不再用 backdrop-filter 的话，下面这条提升规则的理由就没了，"
        "该重新评估而不是留着"
    )
    m = re.search(r"\.card:has\(\.multi-select\.open\)\{([^}]*)\}", no_comments)
    assert m, "展开时提升卡片的规则没了，宽屏下最后几项会被下一张卡盖住"
    body = m.group(1)
    assert "position:relative" in body, "只给 z-index 不给 position，z-index 不生效"
    z = re.search(r"z-index:(\d+)", body)
    assert z and int(z.group(1)) > 0, body
