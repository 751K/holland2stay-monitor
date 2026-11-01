"""从 docs/CHANGELOG.md 提取某个版本的正文，供 GitHub Release 使用。

用法::

    python packaging/release_notes.py v2.0.0 dist/release_notes.md

找不到对应版本时写一个最小的回退正文，并以 0 退出——发布流程不该因为
CHANGELOG 漏写一个版本就失败。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "docs" / "CHANGELOG.md"


def extract(changelog: str, tag: str) -> str:
    version = tag[1:] if tag.startswith("v") else tag
    lines = changelog.splitlines()

    start = None
    heading = re.compile(rf"^## v?{re.escape(version)}(?:\s|$)")
    for i, line in enumerate(lines):
        if heading.match(line):
            start = i
            break
    if start is None:
        return f"Release {tag}\n"

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^## v?\d", lines[j]):
            end = j
            break

    # 去掉版本标题：Release 标题已经是版本号，正文直接从摘要开始。
    # 链接定义先剔除，最后统一按实际引用补一次，避免重复。
    body_lines = [
        line for line in lines[start + 1:end]
        if not re.match(r"^\[[0-9a-f]{7}\]:", line)
    ]
    body = "\n".join(body_lines).strip()

    # Markdown 引用式链接的定义可能写在条目末尾；只补上正文实际引用到的，
    # 并按首次出现的顺序去重（同一个 commit 可能在旧条目里也定义过）。
    used = set(re.findall(r"\[([0-9a-f]{7})\]", body))
    seen: set[str] = set()
    refs: list[str] = []
    if used:
        for line in lines:
            match = re.match(r"^\[([0-9a-f]{7})\]:", line)
            if match and match.group(1) in used and match.group(1) not in seen:
                seen.add(match.group(1))
                refs.append(line)
        if refs:
            body += "\n\n" + "\n".join(refs)
    return body + "\n"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    tag, output = sys.argv[1], sys.argv[2]
    text = extract(CHANGELOG.read_text(encoding="utf-8"), tag)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
