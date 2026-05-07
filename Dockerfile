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
COPY static/ static/

# 复制 entrypoint 并加执行权限
COPY entrypoint.sh /entrypoint.sh

# 运行时目录
RUN mkdir -p data logs \
    && useradd -m appuser \
    && chown -R appuser:appuser /app \
    && chmod +x /entrypoint.sh

COPY supervisord.conf /etc/supervisor/conf.d/app.conf

USER appuser

EXPOSE 8088

ENTRYPOINT ["/entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/app.conf"]
