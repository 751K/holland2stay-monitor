"""
Git update checker — 启动时检测远程仓库是否有新提交，询问用户是否更新。
"""
import subprocess
import sys


def check_for_updates() -> None:
    """
    检查远程是否有新提交，有则显示变更并询问是否 git pull。

    静默跳过条件（不阻塞启动）：
    - 非 git 仓库
    - 无远程 tracking branch
    - 非交互式终端（Docker / systemd / 管道）
    - git fetch 超时或网络不可达
    - 用户回答 n / no
    """
    # 非 tty 静默跳过（Docker、管道、systemd 等）
    if not sys.stdin.isatty():
        return

    try:
        subprocess.run(
            ["git", "fetch", "origin"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return

    # 检查落后于 upstream 的提交数
    count_result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD..@{upstream}"],
        capture_output=True, text=True,
    )
    if count_result.returncode != 0:
        return  # 无 upstream 或不在分支上

    try:
        behind = int(count_result.stdout.strip())
    except ValueError:
        return

    if behind <= 0:
        return  # 已是最新

    # 显示落后提交
    print(f"\n{'='*60}")
    print(f"GitHub 上有 {behind} 个新提交：")
    print(f"{'='*60}")
    log_result = subprocess.run(
        ["git", "log", "--oneline", f"HEAD..@{{upstream}}"],
        capture_output=True, text=True,
    )
    if log_result.returncode == 0 and log_result.stdout:
        for line in log_result.stdout.strip().splitlines():
            print(f"  {line}")
    print()

    # 询问用户
    try:
        ans = input("是否更新到最新版本？[Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("跳过更新")
        return

    if ans and ans not in ("y", "yes"):
        print("跳过更新（可稍后手动 git pull）\n")
        return

    # 执行拉取
    print("正在更新...")
    result = subprocess.run(
        ["git", "pull"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("✅ 已更新到最新版本\n")
    else:
        print(f"⚠️ 更新失败：{result.stderr.strip()}\n")
