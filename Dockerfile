FROM python:3.11-slim-bookworm

WORKDIR /app

# supervisord 负责同时跑 monitor.py 和 web.py
RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor \
    && rm -rf /var/lib/apt/lists/*

# 先复制 requirements 利用 Docker layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY *.py ./
COPY templates/ templates/

# 运行时目录（data/ 和 logs/ 会通过 volume 挂载覆盖，这里只是保证目录存在）
RUN mkdir -p data logs \
    && useradd -m appuser \
    && chown -R appuser:appuser /app

USER appuser

COPY supervisord.conf /etc/supervisor/conf.d/app.conf

EXPOSE 8088

# -n = nodaemon（前台运行，Docker 需要）
CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/app.conf"]
