"""模板里不该有写死的中文界面文案。

2026-08-04 走查发现的实例：

- ``app_accounts.html`` 里「推送设备」整个 tab（9 个表头 + 2 个状态徽标 +
  空态 + 两处 title/confirm）全是中文字面量，英文界面照样显示中文；
- ``listings.html`` 的「共 N 条」把「共」写死了，英文页面显示成 "共 49"；
- ``users.py`` 把 ``title="新增用户"`` 直接传进模板，英文界面标签页也是中文。

这类东西不报错，只有真的把界面切成英文一页页看才会发现——所以拿测试盯住。

白名单只放**不需要翻译**的内容：donate 页是面向中文用户的独立页面；
``{% if lang == 'en' %}`` 分支里的中文是正常的双语分支。
"""
from __future__ import annotations

import pathlib
import re

CJK = re.compile(r"[一-鿿]")

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"

#: 整份文件豁免的模板。donate.html 是只面向中文用户的赞助页。
EXEMPT_FILES = {"donate.html"}

#: 注释、以及模板自己做双语分支的地方，不算硬编码。
_BLANKED = [
    re.compile(r"\{#.*?#\}", re.S),      # Jinja 注释
    re.compile(r"<!--.*?-->", re.S),     # HTML 注释
    re.compile(r"<script\b.*?</script>", re.S | re.I),  # 见下方说明
    re.compile(r"<style\b.*?</style>", re.S | re.I),
]

#: 行内双语三元式：``{{ 'X' if lang == 'en' else '中文' }}`` / ``zh ? '中文' : 'X'``。
#: 这种写法本来就要两种语言各写一遍，中文出现在里面是正常的。
_BILINGUAL = re.compile(r"\blang\s*==|\bzh\s*\?|\?\s*['\"][^'\"]*[一-鿿]")

_JINJA_COMMENT_LINE = re.compile(r"^\s*(#|//|/\*|\*)")


def _strip_ignorable(text: str) -> list[tuple[int, str]]:
    """抹掉注释和 <script>/<style> 后按行返回，行号从 1 开始。

    用等长空白替换而不是删除，保证行号不漂。

    <script> 里的中文另说：那里有一批只有中文的动态提示（"发送中..."之类），
    是既有的 i18n 欠账，不在这次走查的范围里。这个测试只盯**静态 HTML 文本
    和属性**——本次修掉的 app_accounts 表头、listings 的「共」都属于这一类，
    也是最容易被漏看的一类（页面一打开就在那儿）。
    """
    for pat in _BLANKED:
        text = pat.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return list(enumerate(text.splitlines(), 1))


def _lang_branch_lines(text: str) -> set[int]:
    """``{% if lang == 'en' %} ... {% endif %}`` 之间的行号。

    这种结构本来就要两种语言各写一遍，里面的中文是正常的。
    """
    lines = text.splitlines()
    inside, depth, out = False, 0, set()
    for i, line in enumerate(lines, 1):
        if re.search(r"\{%-?\s*if\s+lang\s*==", line):
            inside, depth = True, 1
            out.add(i)
            continue
        if inside:
            depth += len(re.findall(r"\{%-?\s*if\b", line))
            depth -= len(re.findall(r"\{%-?\s*endif\b", line))
            out.add(i)
            if depth <= 0:
                inside = False
    return out


def _offending_lines(path: pathlib.Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    skip = _lang_branch_lines(text)
    hits = []
    for lineno, line in _strip_ignorable(text):
        if lineno in skip or not CJK.search(line):
            continue
        if _JINJA_COMMENT_LINE.match(line) or _BILINGUAL.search(line):
            continue
        hits.append(f"{path.name}:{lineno}: {line.strip()[:90]}")
    return hits


def test_no_hardcoded_chinese_in_templates():
    offenders: list[str] = []
    for path in sorted(TEMPLATES.glob("*.html")):
        if path.name in EXEMPT_FILES:
            continue
        offenders.extend(_offending_lines(path))
    assert not offenders, (
        "模板里有写死的中文界面文案，英文界面会照样显示中文。"
        "请改用 {{ _('key') }} 并在 translations.py 补 key：\n  "
        + "\n  ".join(offenders)
    )


def test_detector_actually_catches_something():
    """自测：探测器别因为正则写错而永远返回空。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "x.html"
        p.write_text("<th>设备</th>\n{# 中文注释不算 #}\n", encoding="utf-8")
        hits = _offending_lines(p)
        assert len(hits) == 1 and "设备" in hits[0]
