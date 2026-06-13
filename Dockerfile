FROM python:3.11-slim-bookworm

WORKDIR /app

# supervisord + CloakBrowser / Chromium 系统依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor \
    # CloakBrowser / Chromium 运行时依赖
    libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
    libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2t64 libatspi2.0-0t64 \
    # Chromium headless 额外依赖
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxrender1 libxi6 \
    libxtst6 libglib2.0-0t64 libgl1-mesa-glx libgl1 \
    fonts-liberation libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖文件，利用 Docker layer cache
# requirements.lock 锁定精确版本，保证构建可重复性
# requirements.txt 保留 >= 约束，供本地开发 / 版本升级参考
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir -r requirements.txt
# 下载 CloakBrowser patched Chromium（~140MB 压缩包，解压后 ~300MB）
RUN python -m cloakbrowser install

# 复制应用代码
COPY *.py ./
COPY app/ app/
COPY bookers/ bookers/
COPY captcha/ captcha/
COPY mcore/ mcore/
COPY mstorage/ mstorage/
COPY notifier_channels/ notifier_channels/
COPY scrapers/ scrapers/
COPY .env.example ./
COPY templates/ templates/
COPY static/ static/
COPY docs/guide.html docs/guide_cn.html docs/

# 复制 entrypoint 并加执行权限
COPY docker/entrypoint.sh /entrypoint.sh

# 运行时目录
RUN mkdir -p data logs \
    && useradd -m appuser \
    && chown -R appuser:appuser /app \
    && chmod +x /entrypoint.sh

COPY docker/supervisord.conf /etc/supervisor/conf.d/app.conf

USER appuser

EXPOSE 8088

ENTRYPOINT ["/entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/app.conf"]
