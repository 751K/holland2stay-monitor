# Changelog

## v1.2.3 (2026-05-12)

### 日志查看器界面升级

v1.2.1 引入了独立的 `errors.log`（WARNING+），但 Web 面板的日志查看页面只硬编码查看 `monitor.log`，缺少文件切换和基本浏览辅助。

v1.2.3 将日志查看器升级为完整的日志浏览界面：

- **文件切换 Tab**：Monitor Log / Errors Log 两个 tab，显示实时文件大小，一键切换 `?file=` 参数
- **行号**：左侧 48px gutter 显示行号（右对齐，灰色，不可选中），方便定位和引用
- **日志级别着色**：`[CRITICAL]` / `[ERROR]` 红色，`[WARNING]` 橙色，`[INFO]` 蓝色，`[DEBUG]` 灰色 — 一眼区分严重程度
- **关键词搜索**：顶部搜索框实时过滤日志行，匹配行数即时显示（如 "23 / 500 lines"）
- **自动滚动**：独立于暂停的 checkbox，靠近底部时自动追随新日志
- **保留功能**：3 秒轮询、暂停刷新、清空当前日志（带二次确认）均保留

### 新端点

- **`GET /api/logs/files`**：返回可用日志文件列表及各自大小（`{"files": [{"key": "monitor", "size": ..., "exists": true}, ...]}`），供前端动态渲染文件切换 tab

### 翻译

- 新增 5 个翻译 key：`logs_monitor`、`logs_errors`、`logs_search`、`logs_auto_scroll`
- 更新 `clear_logs` / `clear_logs_confirm` 语义更明确（含 "当前" 字样）

### 测试

- `test_log_routes.py` 新增 `TestApiLogsFiles` 类（3 个测试）：返回结构正确、匿名 401、guest 403

---

## v1.2.2 (2026-05-11)

### 403 / Cloudflare WAF 屏蔽处理

当 Holland2Stay API 返回 403（Cloudflare WAF 屏蔽）时，旧代码将其当普通失败处理，monitor 每 3–5 分钟刷一轮 error log，用户不知情，无法行动。

v1.2.2 将 403 提升为一等异常，与 429（可自动恢复）完全区分：

- **scraper 层**：`_post_gql` 检测 403 响应（含 Cloudflare 挑战页签名识别：`no-js ie6 oldie`、`challenge-platform` 等），立刻抛出 `BlockedError`，不进入重试循环（与 429 不同）
- **传播链**：`_scrape_city_pages` / `scrape_all` 将 `BlockedError` 透传，不被 `except Exception` 吞掉
- **monitor.run_once**：捕获 `BlockedError` → ERROR 日志（含城市数/用户数/代理状态）→ 通过用户通知渠道推送告警 → re-raise
- **monitor.main_loop**：捕获 `BlockedError` → 15 分钟冷却（vs 429 的 5 分钟），避免刷屏；恢复需换代理或重启进程
- **通知节流**：30 分钟内最多发 1 条屏蔽告警，避免持续屏蔽时重复推送
- **可操作建议**：错误消息包含三条恢复路径 — 换 HTTPS_PROXY 出口 IP / 重启 monitor 重建 session + TLS 指纹 / 暂停几小时让 Cloudflare 冷却

### 测试

- **`tests/test_scraper_403.py`**（15 个测试，4 个类）：
  - `TestPostGqlBlockedError`（5 个）：403 立即抛 BlockedError + Cloudflare 识别 + 不重试 + 429 回归保护 + 200 回归保护
  - `TestBlockedErrorPropagation`（2 个）：验证 BlockedError 不被中间层吞掉
  - `TestMonitorBlockedHandling`（4 个）：run_once re-raise + 用户通知 + 30 分钟节流 + 节流后恢复
  - `TestShouldNotifyBlock`（4 个）：节流函数单元测试（首次/二次/超时/间隔合理性）

### 文档

- README.md / README_cn.md 新增 [flatradar.app](https://flatradar.app) 在线演示链接

---

## v1.2.1 (2026-05-11)

### 测试套件（Pytest）

v1.2.1 引入 10 个 pytest 测试模块，覆盖核心逻辑和安全边界：

- **纯函数测试**（6 个）：
  - `test_models_filter.py` — ListingFilter pass/reject 边界
  - `test_crypto.py` — 加密/解密往返 + 密钥轮换
  - `test_safety.py` — safe_next_url 开放重定向防护
  - `test_storage_diff.py` — SQLite diff 检测（新增/变更/过期）
  - `test_applescript_escape.py` — AppleScript 转义覆盖所有特殊字符组合
  - `test_prewarm_cache.py` — Prewarm 缓存生命周期
- **HTTP 集成测试**（3 个）：
  - `test_auth_routes.py` — 登录/登出/访客/session 角色保护
  - `test_user_routes.py` — 用户 CRUD RBAC 鉴权
  - `test_log_routes.py` — 日志 API 文件白名单 / 清空 / 路径穿越防护
- **表单测试**（1 个）：`test_user_form.py`
- **共享 fixture**：`temp_db`（隔离 SQLite）、`client` / `admin_client` / `guest_client`（预注入 session）、`fresh_crypto`（隔离密钥状态）、`isolated_data_dir`（tmp_path 重定向）

零外部网络依赖，可通过 `python -m pytest tests/ -v` 在任何环境运行。

### 预登录缓存 v2 — Phase B 跨轮复用

v1.2.0 的预登录（Phase A）在每轮 scrape 前并行建立 session，节省了 ~450ms，但**每轮都重新登录**，多轮无候选场景下浪费 `generateCustomerToken` 调用。

Phase B 将预登录 session 缓存到进程级 `dict`，跨轮复用：

- **命中**：直接同步取用，零网络 IO
- **Token TTL 剩余 < 5 分钟**：在 executor 中后台刷新（与 scrape 并行，不额外等待）
- **email 变更 / 用户被禁用 / 热重载**：自动失效并关闭旧 session
- **booking 后保留缓存**；仅 `unknown_error` 失效（session 疑似损坏）
- **Race 防护**：refresh margin（300 s）远大于一次 booking 耗时（~10 s），保证 try_book 内部不会触发 session 过期路径

每轮无候选时也保留缓存供下轮复用，每天从 288 次登录（5 min 间隔）降至 ~4 次（4 小时 token + margin 刷新）。

### 错误日志（errors.log）

`monitor.log` 长跑下 INFO 噪音（轮询节奏、正常 diff 等）淹没真正的告警。v1.2.1 新增独立的错误日志：

- **`data/errors.log`**：仅记录 WARNING / ERROR / CRITICAL，专用于事后排查
- **详细 formatter**：`%(name)s.%(funcName)s:%(lineno)d`，一眼定位问题源
- **更大保留**：`backupCount=5`（vs monitor.log 的 3），错误稀疏但时间窗口更长
- **全局接管**：所有模块（scraper、booker、monitor、notifier）的 `logger.warning` / `logger.error` 均自动写入

### 日志上下文增强

所有关键路径的日志消息加入更多上下文信息，方便定位问题：

- **scraper**：429 退避显示累计等待时间；网络异常含 traceback；非 429 HTTP 错误含响应片段；城市抓取失败含 city_id / proxy 状态
- **booker**：`addNewBooking` / `placeOrder` 错误含 sku / contract_id / start_date / cart_id
- **monitor**：限流告警含城市数/用户数/代理状态；抓取失败含城市名列表
- **预订失败**：含 listing_id / sku / email / dry_run / prewarmed / 各阶段耗时

### 修复

- **`hmac.compare_digest` TypeError**：含非 ASCII 字符（中文/emoji）的 CSRF token 或登录用户名/密码会使 `hmac.compare_digest()` 抛出 `TypeError`，导致 POST 路由返回 500。`app/csrf.py` 和 `app/routes/sessions.py` 改用 `.encode("utf-8")` 后的 bytes 进行比较，任意 Unicode 安全比较且时序常数保留
- **Dashboard 城市列表截断**：`get_all_listings(limit=2000)` 可能漏掉只在早期记录中出现的老城市；改用 `get_distinct_cities()`（`SELECT DISTINCT city`），无 LIMIT 截断风险
- **AppleScript 注入防护**：`_build_applescript` 的 recipient 参数此前未转义，admin→admin 注入或多用户配置场景存在横向攻击面；抽取 `_escape_applescript_literal()`，recipient 和 message 统一转义
- **日志查看器路径穿越**：`/api/logs` / `/api/logs/clear` 新增文件白名单（`monitor` / `errors`），拒绝任一 `file` 参数值，防止 `file=../../etc/passwd` 类路径穿越

### 新增功能

- **日志查看器支持切换文件**：`/api/logs?file=monitor|errors` 可在 Web 面板查看不同日志

---

## v1.2.0 (2026-05-11)

### 重构：web.py 模块化拆分

web.py 长期积累至 1,200 行，涵盖路由、鉴权、表单、i18n、进程控制等所有 Web 面板逻辑，维护和理解成本高。v1.2.0 将其拆分为 18 个内聚模块，每个模块 15–240 行，职责单一。

**架构设计：**

- `web.py`（154 行）精简为 Flask app 引导层：实例化 → 安全头 → CSRF → Jinja 过滤器 → context processor → 路由注册
- `app/` 共享模块（7 个）：`auth.py`、`csrf.py`、`db.py`、`env_writer.py`、`i18n.py`、`jinja_filters.py`、`process_ctrl.py`、`safety.py`
- `app/routes/` 路由模块（10 个）：`dashboard.py`、`calendar_routes.py`、`map_routes.py`、`notifications.py`、`control.py`、`sessions.py`、`settings.py`、`stats.py`、`system.py`、`users.py`
- `app/forms/` 表单模块（1 个）：`user_form.py`

**关键设计决策：**

- **保留扁平 endpoint**：放弃 Flask Blueprint（会强制 `url_for("bp.index")` 前缀），改用 `app.add_url_rule()` 直接挂载路由，模板和前端 17 处 `url_for()` + 所有 fetch URL 零改动
- **`register(app)` 模式**：每个路由模块导出 `register(app)` 函数，`web.py` 依次调用，新增模块只需在 `__init__.py` 中 import 并在引导层加一行 register 调用
- **PyInstaller 兼容**：`h2s_monitor.spec` 使用 `collect_submodules("app")` 自动收集所有子模块为 hiddenimports，未来新增模块无需手动维护清单
- **Docker 构建**：`Dockerfile` 新增 `COPY app/ app/`，将整个 app 包复制进镜像

### 技术细节

- **TLS fingerprint 动态函数**：`get_impersonate()` 替代静态 `CURL_IMPERSONATE` 常量，在运行时根据目标域名返回 Chrome 指纹版本，便于后续扩展多目标
- **路由不按 Blueprint 组织的原因**见 `app/routes/__init__.py` 文档注释

---

## v1.1.9 (2026-05-08)

### 修复

- **DB_PATH / TIMEZONE 配置不生效**：v1.1.8 将 `DB_PATH` / `TIMEZONE` 提升为 `config.py` 模块级常量时，定义位置在 `load_dotenv()` 之前，导致 `.env` 中自定义值被忽略（始终使用默认值）；修复方式为移至 `load_dotenv()` 和 `resolve_project_path()` 之后
- **Caddyfile 无效指令**：`roll_keep_days` 不是 Caddy 合法指令，正确的日志保留时长指令为 `roll_keep_for`（带单位时间值）；改为 `roll_keep_for 720h`（等价 30 天）

---

## v1.1.8 (2026-05-08)

### 安全修复

- **DOM XSS — 日历页**：`templates/calendar.html` 中 `l.url` / `l.name` / `l.price_raw` / `l.city` 直接拼入 `innerHTML`；改为 `createElement` + `textContent`，`href` 加 `https?://` 协议白名单
- **DOM XSS — 地图页**：`templates/map.html` Leaflet popup 通过字符串拼接构造 HTML 传给 `bindPopup()`；改为 DOM 节点传入，`href` 同样加协议校验；鼠标悬停状态栏从正则反解析 HTML 改为读 `marker._listingName`
- **Docker 启动预检**：`entrypoint.sh` 新增两项安全检查，任一失败则 `exit 1` 阻止容器启动：
  - `WEB_PASSWORD` 未设置（读 `.env` 文件，非继承环境变量，防假通过）
  - `Caddyfile` 仍含占位域名 `your.domain.com`
  - 隔离/本地环境可通过 `H2S_SKIP_PREFLIGHT=1` 跳过；`docker-compose.yml` 已预置注释示例

### 修复

- **Healthcheck 语义**：`/health` 此前在 monitor 停止时返回 503，导致管理员主动停止监控也让容器变 `unhealthy`；改为始终 200，monitor 运行状态仅通过响应体 `"monitor"` 字段透出
- **自动预订快速通道**：新上线 Available to book 房源此前进 `ab_pending`，等通知全部发完才提交预订（1–3 s 延迟）；现与状态变更房源统一，立即 `run_in_executor`；同步移除已无用的预登录（prewarm）机制

### 生产环境

- **Gunicorn 替代 Flask 内置服务器**：`supervisord.conf` 改用 `gunicorn --workers=1 --threads=8 --timeout=0`；`requirements.txt` 新增 `gunicorn>=22.0.0`
  - `--workers=1`：SQLite 单进程，避免多进程写锁冲突
  - `--threads=8`：支持多路 SSE 长连接并发
  - `--timeout=0`：禁用 worker 超时，防止 SSE 连接被 30 s 默认超时强杀
- **Caddy 访问日志**：`Caddyfile` 从 `/dev/null` 改为 `/var/log/caddy/access.log`，10 MiB 自动轮转，保留 7 份 / 30 天；`docker-compose.yml` 新增 `./logs/caddy:/var/log/caddy` 卷挂载
- **依赖版本锁定**：新增 `requirements.lock`，以 `==` 精确版本覆盖全部直接 + 传递依赖；`Dockerfile` 改用 lock 文件安装，构建可重复

### 代码质量

- **单一数据源**：`DB_PATH` / `TIMEZONE` 提升为 `config.py` 模块级常量，`load_config()` 直接引用；`web.py` 删除重复读取，改为从 `config` 导入，`resolve_project_path` 不再在 `web.py` 中重复调用
- **Storage 封装**：`web.py` 两处裸 `sqlite3` 连接（`_get_filter_options` / `api_neighborhoods`）替换为 `Storage.get_feature_values(category, cities)`，绕过抽象层的问题消除
- **死代码清理**：`templates/users.html` 中 `lf.max_area` 引用（`ListingFilter` 无此字段）、`translations.py` 中 `user_form_max_area` 翻译键一并删除
- **`.env.example` 精简**：删除已迁移至 Web UI 的 40+ 行通知渠道 / 过滤 / 自动预订配置项，保留系统级配置；底部补充 v1→v2 迁移说明，消除新用户困惑

---

## v1.1.7 (2026-05-08)

### 修复

- **设置页保存报 500**：`dotenv.set_key()` 内部使用 `os.replace()`（原子 rename），在 Docker bind-mount 的 `.env` 文件上触发 `OSError [Errno 16] Device or resource busy`；改用自实现的 `_write_env_key()`（读取 → 内存修改 → 原地写回）彻底规避

### 变更

- **访客权限进一步收紧**：
  - 铃铛通知按钮与通知面板对访客隐藏（`{% if is_admin %}`）
  - `/api/notifications`、`/api/notifications/read`、`/api/events` 改为 `@admin_api_required`，访客无法轮询通知或订阅 SSE
  - 地图页「解析地址」按钮对访客隐藏，防止触发 geocode 写入
  - 前端通过 `window._isAdmin` 变量跳过通知初始化，避免产生无意义的 403 请求

---

## v1.1.6 (2026-05-08)

### New

- **访客模式（Guest Mode）** — 登录页新增"访客模式"按钮，无需密码以只读身份进入面板；可查看仪表盘、房源、日历、地图、统计；用户管理、设置、系统信息、日志查看仍需 admin 登录
- **RBAC 角色鉴权** — `session["role"]` 区分 admin / guest；新增 `admin_required` / `admin_api_required` 装饰器，17 条路由按角色保护
- `WEB_GUEST_MODE` 环境变量：默认 `true`，设为 `false` 关闭访客入口
- **Caddy 反代 + HTTPS** — 新增 `Caddyfile`，`docker-compose.yml` 集成 Caddy 服务，自动签发 Let's Encrypt 证书；h2s 容器改为内部 `expose`，仅 Caddy 暴露 80/443

### Fixed

- 访客可见监控开关 / 关闭按钮 → Dashboard 相关控件对 guest 隐藏
- 通知面板中自动预订付款 URL（idealCheckOut 直链）对访客可见 → API 层对 `booking` 类通知的 `url` 字段过滤，guest 无法获取付款链接
- `/guest` 路由可将已登录 admin 静默降级为 guest → 增加角色保护，admin session 访问 `/guest` 直接跳首页

### Changed

- `.env.example` 新增 `WEB_GUEST_MODE`、`SESSION_COOKIE_SECURE`、`SESSION_LIFETIME_HOURS` 配置项
- `NOTIFICATION_CHANNELS` 默认值由 `imessage` 改为 `telegram`（VPS 环境更通用）
- `docker-compose.yml` 重构：Caddy 前置反代，仅 80/443 对外暴露

---

## v1.1.5 (2026-05-08)

### New

- **房源列表筛选拆分** — 状态、城市（下拉）、名称（文本）、最高租金、最小面积独立筛选
- **Dashboard 城市过滤** — 仪表盘按城市过滤 KPI 和列表

### Fixed

- 自动预订跳过通知可用性检查 → 加 `notifications_enabled` + `has_channels` 三道防线
- 快速预订并非立即执行 → 状态变更候选直接提交线程池
- 地理编码 31 条失败（地址含 neighborhood 干扰 Photon）
- 自动/手动地理编码并发冲突 → 统一 `_geocode_status` 管理
- 通知 URL XSS（`renderNotifications` 改为 DOM `addEventListener`）
- 加密密钥线程不安全（`_get_cipher` double-checked locking）
- Session 默认 31 天 → 24 小时
- Dockerfile 缺少 `.env.example` 导致首次部署崩溃
- `admin` 硬编码默认用户名
- `location =` JS 导航失效、房源时间中英切换等 UI 修复

### Changed

- 标签命名规范化：`allowed_offer` → `allowed_contract` / `allowed_promo` → `allowed_offer`
- 地理编码 worker 重复代码抽取为 `_run_geocode_worker`
- `status_changes` 表加索引、城市列表提取为 `get_distinct_cities()`
- 安全头注入（`X-Frame-Options` 等）、supervisord 日志分离
- 清理 `max_area` 残留引用、移除荷兰境外遮罩功能

---

## v1.1.0 (2026-05-07)

### New

- **用户过滤条件** — 租金、面积、楼层、户型、入住类型、城市、片区、合同类型、租客要求、标签/促销，通知和自动预订独立配置
- **多选下拉组件** — 替换文本输入为下拉多选，Checkbox 方式选择，选中的标签显示在输入框内
- **中英双语标签** — 过滤选项根据界面语言自动切换显示名称
- **片区按城市动态加载** — 选择城市后片区列表自动过滤
- **短租/长租识别** — 从 GraphQL 提取 Contract / Tenant / Offer 标签，房源列表可区分
- **Photon 地理编码** — 替换 Nominatim，速度快 4 倍，地图页新增手动解析按钮

### Fixed

- 监控重启后配置丢失（子进程继承旧环境变量）
- 通知角标多标签页同步（从服务端查询真实未读数）
- 地图解析失败
- SMTP 端口 587 被校验拒绝

### Changed

- 移除"最大面积"过滤条件
- 合同类型和标签字段命名规范化（`allowed_contract` / `allowed_offer`）
- 通知发送失败重试机制
- 翻页加 `MAX_PAGES=50` 安全上限
- 地理编码加线程锁防并发

---

## v1.0.1 (2026-05-06)

- 修复打包问题
- GitHub Actions CI/CD — 推送 tag 自动构建双平台产物并挂到 Release

---

## v1.0.0 (2026-05-06)

- 首次正式发布
- 26 城市监控、多通知渠道（iMessage / Telegram / Email / WhatsApp）
- Web 管理面板（仪表盘、房源、用户、设置、地图、日历、统计）
- 自动预订（加入购物车 → 下单 → 支付链接）
- 智能轮询、限流防护、热重载
- 支持打包发布，MacOs和Windows双平台兼容
