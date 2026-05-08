#!/bin/bash
# Docker entrypoint — 安全预检 + 首次启动初始化
set -e

# ── 首次启动：创建缺失的 .env ─────────────────────────────────────────
if [ ! -f /app/.env ]; then
    echo "[entrypoint] .env not found, creating from .env.example..."
    cp /app/.env.example /app/.env
    echo "[entrypoint] Please edit .env on the host and restart."
fi

mkdir -p /app/data /app/logs /app/logs/caddy

# ── 安全预检 ──────────────────────────────────────────────────────────
# 设置 H2S_SKIP_PREFLIGHT=1 可跳过（仅限本地/隔离网络部署）
if [ -z "${H2S_SKIP_PREFLIGHT:-}" ]; then

    PREFLIGHT_FAILED=0

    # 1. Caddyfile 占位域名检查
    #    docker-compose.yml 将 ./Caddyfile 额外挂载到 /app/Caddyfile.check（只读）
    if [ -f /app/Caddyfile.check ] && grep -q 'your\.domain\.com' /app/Caddyfile.check; then
        echo "" >&2
        echo "  ❌  FATAL: Caddyfile 仍使用占位域名 your.domain.com" >&2
        echo "      请将其替换为你的真实域名后重新运行 docker compose up。" >&2
        PREFLIGHT_FAILED=1
    fi

    # 2. WEB_PASSWORD 空值检查（从挂载的 .env 文件读取，非继承环境变量）
    #    空密码意味着面板可被任何人无需认证访问。
    _WEB_PWD=$(grep -E '^WEB_PASSWORD=' /app/.env 2>/dev/null | head -1 | cut -d'=' -f2- | sed "s/^['\"]//;s/['\"]$//")
    if [ -z "$_WEB_PWD" ]; then
        echo "" >&2
        echo "  ❌  FATAL: WEB_PASSWORD 未设置，Web 面板将无密码公开暴露。" >&2
        echo "      请在 .env 中设置 WEB_PASSWORD=<强密码> 后重新运行 docker compose up。" >&2
        PREFLIGHT_FAILED=1
    fi
    unset _WEB_PWD

    if [ "$PREFLIGHT_FAILED" = "1" ]; then
        echo "" >&2
        echo "  ℹ️  如确认在隔离/本地网络中运行，可通过以下方式跳过预检：" >&2
        echo "       H2S_SKIP_PREFLIGHT=1 docker compose up" >&2
        echo "  或在 docker-compose.yml 的 environment 节中设置 H2S_SKIP_PREFLIGHT=1" >&2
        echo "" >&2
        exit 1
    fi

fi

echo "[entrypoint] Preflight OK. Starting Holland2Stay Monitor..."
exec "$@"
