#!/bin/bash
# Docker entrypoint — 首次启动时自动创建缺失的 .env 和目录
set -e

if [ ! -f /app/.env ]; then
    echo "[entrypoint] .env not found, creating from .env.example..."
    cp /app/.env.example /app/.env
    echo "[entrypoint] Please edit .env on the host and restart."
fi

mkdir -p /app/data /app/logs
echo "[entrypoint] Starting Holland2Stay Monitor..."

exec "$@"
