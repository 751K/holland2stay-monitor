# Changelog

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
