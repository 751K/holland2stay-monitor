"""
reset_db.py — 清空数据库，用于测试
=====================================
用法：
    python reset_db.py            # 清空 listings / status_changes / meta，保留表结构
    python reset_db.py --all      # 直接删除整个 .db 文件后重建
    python reset_db.py --dry-run  # 只打印将要执行的操作，不实际修改

清空后重新启动 monitor.py，所有房源会被视为"新房源"重新触发通知和自动预订。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH  = Path("data/listings.db")
PID_FILE = Path("data/monitor.pid")


def _check_monitor_running() -> None:
    """如果 monitor 仍在运行，打印警告并退出，防止 reset 后又被旧进程重新写入。"""
    if not PID_FILE.exists():
        return
    pid = PID_FILE.read_text().strip()
    if not pid.isdigit():
        return
    import os
    try:
        os.kill(int(pid), 0)   # 发送空信号；进程存在则不抛异常
        print(f"❌ monitor.py 仍在运行（PID {pid}）")
        print("   请先停止监控（Ctrl+C），再执行 reset_db.py，")
        print("   否则旧进程会立刻把数据重新写回数据库。")
        sys.exit(1)
    except (ProcessLookupError, PermissionError):
        # 进程不存在 or 无权限发信号（Windows 上正常）
        pass


def _confirm(prompt: str) -> bool:
    ans = input(f"{prompt} [y/N] ").strip().lower()
    return ans == "y"


def reset_tables(db_path: Path, dry_run: bool = False) -> None:
    """清空三张表，保留 SQLite 文件和表结构。"""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # 统计当前行数，方便对比
    counts = {}
    for table in ("listings", "status_changes", "meta"):
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        counts[table] = cur.fetchone()[0]

    print("当前数据库内容：")
    for table, cnt in counts.items():
        print(f"  {table:<20s} {cnt} 条")

    if dry_run:
        print("\n[dry-run] 不会实际修改数据库。")
        conn.close()
        return

    if not _confirm("\n确认清空以上所有数据？"):
        print("已取消。")
        conn.close()
        return

    cur.executescript("""
        DELETE FROM listings;
        DELETE FROM status_changes;
        DELETE FROM meta;
    """)
    conn.commit()
    conn.close()

    print("✅ 数据库已清空，表结构保留。")
    print("   重新启动 monitor.py 后，所有房源将被视为新房源。")


def reset_file(db_path: Path, dry_run: bool = False) -> None:
    """直接删除 .db 文件，下次 Storage 初始化时自动重建。"""
    if not db_path.exists():
        print(f"文件不存在：{db_path}")
        return

    size_kb = db_path.stat().st_size // 1024
    print(f"将删除：{db_path}（{size_kb} KB）")

    if dry_run:
        print("[dry-run] 不会实际删除文件。")
        return

    if not _confirm("确认删除整个数据库文件？"):
        print("已取消。")
        return

    db_path.unlink()
    print(f"✅ 已删除 {db_path}，下次启动 monitor.py 会自动重建。")


def main() -> None:
    parser = argparse.ArgumentParser(description="清空 Holland2Stay 监控数据库（测试用）")
    parser.add_argument("--all",     action="store_true", help="删除整个 .db 文件而非仅清空表")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不实际修改")
    parser.add_argument("--db",      default=str(DB_PATH),  help=f"数据库路径（默认: {DB_PATH}）")
    args = parser.parse_args()

    db_path = Path(args.db)

    if not args.dry_run:
        _check_monitor_running()

    if not db_path.exists() and not args.all:
        print(f"数据库不存在：{db_path}")
        sys.exit(1)

    if args.all:
        reset_file(db_path, dry_run=args.dry_run)
    else:
        reset_tables(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
