# Changelog

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
