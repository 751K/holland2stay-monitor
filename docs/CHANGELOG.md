# Changelog

## v1.3.1 (2026-05-13)

### Bug 修复

- **prewarm future 阻塞下单 (P2)**：`run_once()` 中取 prewarm 时若 future 未完成，不再 `await` 阻塞，改为跳过让 `try_book()` 走正常登录。同时给每个 future 加 `add_done_callback`，完成后自动写入缓存，解决慢 prewarm 的 session 泄漏问题
- **`_stash_pending_prewarms` 改为非阻塞 (P2)**：从 `await fut` 改为只收 `.done()` 的 future，未完成的跳过不阻塞通知
- **RetryQueue 空 key 残留 (P2)**：`discard()` / `remove_gone()` 在集合清空后 `del self._queue[user_id]`，避免持久化 `{"user":[]}` 脏数据
- **`load_retry_queue` 类型校验 (P2)**：顶层 JSON 非 dict 时 warning + 重置；子值只接受 list/set，其他类型跳过
- **`Optional` 导入缺失 (P2)**：`config.py`、`models.py`、`scraper.py`、`users.py` 补充 `from typing import Optional`

### 代码质量

- **monitor.py**：合并重复的 `from config import`、删除孤儿注释分隔线、`ab_candidates` 类型标注补全
- **test 模式**：`_safe_print` 包装 `UnicodeEncodeError`，Windows/管道环境不乱码
- **`mstorage/_base.py`**：`_migrate()` 加注释说明 executescript 隐式提交约束；清理未使用的 import
- **`web.py`**：移除未使用的 `import logging`
- **`users.py`**：移除未使用的 `from pathlib import Path`

### 测试

- RetryQueue 空 key 清理 ×2、`load_retry_queue` 顶层类型异常 ×2（`[]` / `"abc"`）

---

## v1.3.0 (2026-05-13)

### `monitor.py` 重构 — 提取 `mcore/` 包

`monitor.py` 1,235 行承担了间隔计算、预登录缓存、自动预订回退、重试队列等多种职责。
本次将纯逻辑和小型服务抽到 `mcore/` 包，`monitor.py` 降至 971 行（-21%）。

- **`mcore/interval.py`**（58 行）：`get_interval()` / `apply_jitter()`，智能轮询间隔计算（纯函数，无状态）
- **`mcore/prewarm.py`**（96 行）：`PrewarmCache` 类，预登录 session 缓存管理（get / set / is_valid / create / invalidate / clear）
- **`mcore/booking.py`**（171 行）：`book_with_fallback()` + `RetryQueue` 类（load / save / add / discard / remove_gone）
- **`mcore/__init__.py`**（15 行）：统一 re-export

原有 6 个预登录辅助函数（`_safe_create_prewarmed`、`_close_prewarmed_quietly`、`_is_cached_session_valid` 等）合并为 `PrewarmCache` 类方法；
`_book_with_fallback` + 全局重试队列字典合并为 `book_with_fallback` 函数 + `RetryQueue` 类。外部行为不变。

### `storage.py` 重构 — 拆分为 `mstorage/` 包

`storage.py` 1,177 行 / 42 个方法全部集中在一个 `Storage` 类中。
本次按领域拆为 6 个 Mixin，通过多重继承组合，对外接口完全不变（`storage.Storage` 继续可用）。

- **`mstorage/_base.py`**（114 行）：`StorageBase` — 连接 / schema 迁移 / meta 读写 / reset / close
- **`mstorage/_listings.py`**（258 行）：`ListingOps` — diff / mark_notified×4 / 面板查询×9 / filter helper
- **`mstorage/_charts.py`**（219 行）：`ChartOps` — 10 个统计图表 + 2 个共享 helper
- **`mstorage/_notifications.py`**（72 行）：`NotificationOps` — web_notifications CRUD×6
- **`mstorage/_map_calendar.py`**（96 行）：`MapCalendarOps` — 地图坐标缓存 + 日历查询
- **`mstorage/_retry.py`**（35 行）：`RetryQueueOps` — 竞败重试队列持久化
- **`mstorage/__init__.py`**（33 行）：Mixin 组合声明
- **`storage.py`**：1,177 → 17 行，纯 `from mstorage import Storage` re-export

### 测试补充

- **`test_mcore_interval.py`**（12 tests）：`get_interval` 6 场景 + `apply_jitter` 6 边界
- **`test_mcore_booking.py`**（21 tests）：`area_key` / `RetryQueue` / `book_with_fallback` 全覆盖
- **`test_mcore_prewarm.py`**（17 tests）：`PrewarmCache` CRUD / is_valid / invalidate / clear / create
- **`test_mstorage_notifications.py`**（12 tests）：通知 CRUD / 分页 / 已读 / 清理
- **`test_mstorage_listings.py`**（16 tests）：面板查询 / filter helper / counts
- **`test_mstorage_map_calendar.py`**（10 tests）：日历 / 地图 / geocode 缓存 / reset_all

### 测试清理

- 移除 `test_monitor_cooldown.py` 中与 `test_mcore_interval.py` 重复的 `TestApplyJitter`（3）、`TestGetInterval`（3）
- 移除 `test_prewarm_cache.py` 中与 `test_mcore_prewarm.py` 重复的 `TestIsCachedSessionValid`（5）

---

## v1.2.10 (2026-05-13)

### 移动端 Web 体验全面升级

对全部 8 个页面进行了移动端适配，覆盖布局、触摸、安全区、iOS Safari 兼容性。

- **房源列表 (P0)**：≤768px 自动切换为卡片视图，每张卡片纵向展示名称、状态、租金、面积、户型、城市、可租日期，替代 10 列横滑表格
- **Dashboard (P0)**：最近房源表格同步改为卡片视图
- **全局触摸目标 (P0)**：`@media (pointer: coarse)` 下所有交互元素（侧边栏导航、按钮、表单、多选、toggle）最小高度 ≥44px（WCAG 推荐），`@media (hover: none)` 移除 hover 闪烁
- **日历 (P1)**：新增月视图/列表视图切换按钮，列表视图按月筛选、按日期分组展示房源；月视图 grid 改用 `minmax(0, 1fr)` 防止窄屏溢出
- **统计页 (P1)**：4 列图表网格从脆弱的 inline style 选择器改为 `.grid-4` CSS 类，响应式 4→2→1 列
- **安全区适配 (P2)**：nav-toggle、toast、登录页按钮、通知面板均使用 `env(safe-area-inset-*)` 避开刘海/底部指示条
- **iOS Safari (P2)**：地图页和日志页 `100vh` → `100dvh`，避免地址栏展开/收起导致高度跳动
- **Dashboard 刷新 (P2)**：`<meta http-equiv="refresh">` 替换为 Page Visibility API 驱动的 JS 定时刷新，标签页隐藏时暂停
- **页面标题 (P2)**：移动端 `.page-header` 加 `padding-left:48px`，不再被 hamburger 按钮遮挡
- **Toast (P2)**：移动端 `max-width:calc(100vw - 32px)`，`min-width:0`，窄屏不再溢出
- **System 页 (P2)**：配置表和环境表包裹 `overflow-x:auto`，长路径用 `.cell-break` 自动换行

### Bug 修复

- **通知面板 `calc()` 语法错误**：`calc(100vw-32px)` 缺少空格，浏览器视为无效值。修复为 `calc(100vw - 32px)`
- **CSS 级联 — 房源卡片被隐藏**：`.listing-cards{display:none}` 位于 mobile media query 之后，覆盖了 `display:flex`。移至 media query 之前
- **地图 geocode 错误面板**：inline `position:absolute` 优先级高于移动端 CSS `position:relative`，错误面板覆盖页面头部。提取为 `.geocode-errors` 类
- **日历列表视图空白**：JS `style.display = ''` 无法覆盖 CSS `.cal-list{display:none}`，改为 `'block'`
- **日历列表视图翻月不生效**：`renderListView()` 未按 `currentMonth` 过滤，始终显示全部日期。增加月份过滤
- **多选筛选器空值显示空白**：JS `update()` 将 `textEl.textContent` 清空为 `''`，覆盖了模板的"不限"/"All"占位文本。改为捕获并恢复初始 placeholder
- **多选占位文案**：`multi_select_placeholder` 从"点击选择..."改为"不限"/"All"，明确未筛选 = 全部

### 细节

- 日历列表视图支持城市筛选联动，切换筛选后保持当前视图
- 登录页语言切换按钮从 inline `style="right:62px"` 改为 `.login-lang-btn` 类，统一 safe-area 适配
- 翻译新增 `cal_month_view` / `cal_list_view` 两个 key

---

## v1.2.9 (2026-05-13)

### 移除 v1→v2 迁移逻辑

v1.2.0 起用户配置从 `.env` 迁移至 `data/users.json`，此后的 8 个版本一直携带从 `.env` 自动创建默认用户的迁移代码。该逻辑已无调用场景，本次彻底移除：

- **`users.py`**：删除 `migrate_from_env()` 函数（~95 行）
- **`monitor.py`**：移除 `migrate_from_env` 导入和调用，更新 `users.json` 不存在/为空时的提示文案
- **`.env.example`**：删除底部 13 行旧版迁移注释
- **`docs/README.md` / `docs/README_cn.md`**：移除"自动迁移"描述，改为"在 Web 面板手动添加用户"
- **`translations.py`**：更新 `users_empty_hint`，移除迁移提示
- **注释修正**：`monitor.py:1178` 从"避免迁移逻辑覆盖现有数据"改为"避免忽略或覆盖现有数据"
- **测试**：删除 `TestMigrateFromEnv` 类（2 个测试）

### 功能增强

- **跨平台进程终止**：`_terminate()` 替代裸 `os.kill()`，Windows 通过 `ctypes.windll.kernel32.TerminateProcess` 实现，POSIX 保持 SIGTERM
- **asyncio 兼容 Gunicorn worker**：`_run_async()` 检测已有 event loop（gevent/asyncio worker），在新线程中跑独立 loop，避免 `asyncio.run()` 抛错
- **`ListingFilter.is_empty()` 自动化**：用 `dataclasses.fields()` 迭代替代手动枚举所有字段，新增过滤字段无需同步修改此处
- **`get_impersonate()` 权重修复**：排除上次选择时同步移除对应权重，避免池/权重列表错位

### 性能

- **SQL 批量更新**：`mark_many_notified()` 从逐条 `UPDATE` 改为单条 `WHERE id IN (...)` 批量更新

### 细节

- **Web 日志静化**：屏蔽 Werkzeug HTTP 访问日志（`GET /static/...` 等），仅保留 WARNING+
- **翻译整理**：`map_geocode_btn` / `map_geocode_hint` / `map_loading` 从 Calendar 区移到 Map 区；删除重复 `settings_heartbeat` key
- **设置页补充提示**：weekdays-only 复选框下方增加说明文字
- **测试**：新增 `test_invalid_numeric_not_written`（非法/空值不写入 .env）；conftest 补充 `web.log` fixture

---

## v1.2.8 (2026-05-13)

### 功能增强

- **心跳改为按时间间隔**：从固定 12 轮发送一次改为按分钟配置（`HEARTBEAT_INTERVAL_MINUTES`，默认 60 min），设为 0 禁用心跳。首轮不再立即发心跳，需等待完整间隔。设置页可在智能轮询区直接修改。
- **新增下午高峰窗口**：智能轮询从单一窗口（8:30–10:00）扩展为双窗口（早 8:30–10:00 + 下午 13:30–15:00），`PEAK_START_2` / `PEAK_END_2` 可在设置页配置，Web 面板可直接修改。
- **设置/用户变更写入日志**：全局配置保存和用户创建/更新/删除时，将完整配置快照记录到 `data/web.log`，日志查看器可追溯操作历史。
- **Web 进程独立日志**：新增 `data/web.log`（Flask 应用日志），与 monitor 的 `monitor.log` 分离；日志查看器新增 Web 日志 Tab（中/英标签），`updateTabSize` 复用 `LOG_LABELS` 映射。

### Bug 修复

- **设置页空值导致启动失败 (P2)**：清空数值设置框（`HEARTBEAT_INTERVAL_MINUTES`、`PEAK_INTERVAL` 等）后保存会写入空字符串，导致 `load_config()` 中 `int("")` / `float("")` 抛错，热重载失败，重启无法启动。修复：数值键空值不覆盖旧值；`config.py` 所有 `int()`/`float()` 改用 `or "default"` 兜底。
- **设置页非法数字值导致启动失败**：非空非法值（如 `PEAK_INTERVAL=abc`）同样会写入 `.env` 导致 `int("abc")` 抛错。修复：数值键写入前校验 format，非法值跳过并记录日志。
- **地图 geocode 错误详情 DOM XSS**：`s.errors[].address/reason` 拼入 HTML 后 `innerHTML` 渲染，地址来自外部抓取数据。修复：改用 `createElement` + `textContent`。
- **geocode 旧错误未清空**：新任务启动和"所有地址已缓存"返回时未重置 `errors=[]`，导致旧失败详情残留显示。修复：两处路径均清空。
- **WARNING 级别日志未落地**：`web.py` 给 root logger 加了 INFO handler 但未 `setLevel(INFO)`，`logger.info()` 被默认 WARNING 级别过滤。修复：加 `logging.getLogger().setLevel(logging.INFO)`。

### 测试

- 486 测试全部通过。修复 `test_booker_flow.py::test_attrs` 浮点精度 flaky 测试（`pytest.approx`）。

---

## v1.2.7 (2026-05-13)

### 修复

- **Den Bosch 地理编码解析到德国**：Photon 将 "Den Bosch"（口语别称）匹配到德国同名小镇而非荷兰的 's-Hertogenbosch。修复：`get_map_listings` 地址拼接追加 `"Netherlands"` 国家限定；新增 `_CITY_FORMAL` 别称映射 `"Den Bosch" → "'s-Hertogenbosch"`。后续有其他口语别称只需在映射表中加一条。

---

## v1.2.6 (2026-05-13)

### 统计页 10 图表 + 筛选增强

**新增 6 个统计图表（4→10）**
- 户型分布、能耗标签分布（环形图，标准颜色映射）
- 面积分布（<20 / 20-30 / 30-50 / 50-80 / >80 m²）、楼层分布（Ground / 1-2 / 3-5 / 6+）
- 租客要求分布（student only / employed only 等）、合同类型分布（Indefinite / 6 months max 等）
- 租金分布区间细化：€1000 以上拆为 4 档

**能耗等级改为「最低可接受等级」**
- 从多选白名单改为单选下拉（A+++ → F）
- 选择 "B" = 匹配 B 及以上的所有等级（A+++/A++/A+/A/B）
- 严格白名单校验：`_ENERGY_LABELS` 精确匹配，非法值（"banana"/"Z"）→ WARNING + 忽略
- `_energy_rank()` 从启发式解析改为白名单索引法，消除误匹配
- 表单 POST 加 `_sanitize_energy()` 防护，防恶意提交非法等级

**用户过滤新增 2 项**
- 通知过滤 + 自动预订过滤：新增「装修类型」（Upholstered / Shell）和「能耗等级」（最低可接受）
- Dashboard / Listings 显示楼盘名
- 房源列表新增城市/租客/能耗/装修筛选（城市和租客为多选）

### Bug 修复（2 个）

- **`or 99` 陷阱**：`_energy_rank("A+++")` 返回 0，`0 or 99 == 99` 导致排序错误
- **非法能耗值触发 500**：`?energy=Z` 使 `_energy_rank` 返回 None，`min_rank <= actual_rank` TypeError

### 安全加固（1 个）

- **存储 JSON 解析加固**：`_safe_features()` 统一 try/except，坏数据 WARNING 后返回 `[]`

### 重构（1 个）

- **前端 multi-select 标签刷新**：提取 `window.refreshMultiSelect()`，copyNotifFilters 不再内联重复逻辑

### 测试（183 个新测试，14 个模块）

**从 303 → 486（+60%）**

- `test_energy_filter.py`（42）：`_energy_rank` 白名单、ListingFilter passes/fail-closed、旧 list 兼容、`/listings?energy=` API
- `test_monitor_cooldown.py`（12）：`_apply_jitter` 边界、`_get_interval` 峰/谷/周末、`_should_notify_block` 节流
- `test_control_routes.py`（11）：start/stop/reload/shutdown 权限、CSRF、PID None、kill 异常
- `test_settings_routes.py`（6）：POST 写 .env、CSRF、智能轮询参数
- `test_notif_routes.py`（17）：分页、limit clamp、mark read、SSE 权限
- `test_storage_charts.py`（9）：能耗排序、面积/楼层 bucket、坏 JSON 跳过、坐标缓存
- `test_listings_filter.py`（10）：状态/城市/搜索/feature 查询、坏 JSON
- `test_users_edge.py`（10）：文件损坏/空/迁移、save/load round-trip
- `test_notifier_channel.py`（26）：MultiNotifier fanout/retry、email 规范化、WebNotifier
- `test_booker_flow.py`（9）：非 Available 拒绝、dry_run、过期/有效 prewarmed
- `test_map_guest.py`（7）：guest GET 不启动 geocode、POST 被拒、CSRF
- `test_frontend_helpers.py`（9）：`_mask_email`、Jinja2 自动转义、模板语法、AppleScript
- `test_i18n.py`（6）：翻译 key 完整性、tr fallback、localize_options
- `test_tools_smoke.py`（4）：tools/launcher import
- `test_user_form.py`（+5）：`TestEnergySanitization`

---

## v1.2.5 (2026-05-12)

### Web 面板增强

**房源列表筛选升级**
- 城市、租客要求改为多选下拉组件（和用户过滤页一致），合同类型保留单选
- 后端：单城市走 SQL 过滤（快），多城市走 Python 内存过滤

**Dashboard / Listings 显示楼盘名**
- Dashboard 新增「楼盘」列，紧挨房源名称，同字体权重
- Listings 页房源名称后显示 `· 楼盘名`

**统计页新图表**
- 「房源上线时间分布」：24 小时柱状图，按荷兰本地时间统计 `first_seen` 小时分布，一眼看出 H2S 几点集中放房
- 「租金分布」区间细化：€1000 以上拆为 €1000-1200 / €1200-1400 / €1400-1600 / >€1600 四档（原全挤在 >€1000 一栏）

**桌面端自适应布局**
- 768–2560px 区间内容宽度跟随视口缩放
- 2560px+ 锁定 2000px 内容区防止超宽屏松散

**用户表单：一键复制通知过滤到自动预订**
- 自动预订过滤条件旁新增「从通知过滤复制」按钮
- 数值字段（租金/面积/楼层）和多选字段（户型/城市/片区/合同/租客/促销）一键同步

### 翻译
- 新增 `col_building`、`filter_contract`、`filter_tenant`、`stats_hourly_dist`、`user_form_copy_filter` 等翻译 key

---

## v1.2.4 (2026-05-12)

### Bug 修复（3 个）

**预登录 session 过期导致自动预订静默失败**
`try_book()` 传入过期 prewarmed session 时，session 来源走 else 分支（新建），但登录判断仍用 `if prewarmed is None`（False，因为 prewarmed 非空但已过期），token 未赋值直接进入 `_do_book()` 触发 `NameError`。
- 引入 `using_prewarmed` 布尔变量，session 来源和登录决策统一使用
- 过期 prewarmed → `using_prewarmed=False` → 正常创建 session + 调用 `login()` + `own_session=True`

**抓取第 1 页网络失败静默返回 0 条**
`_scrape_city_pages` 中网络异常走 `except Exception: break`，返回空列表。`scrape_all` 将其当"该城市无房源"处理，monitor 更新 `last_scrape_at` 并继续正常轮询——坏代理/断网时监控空转刷 error log 不知情。
- 新增 `ScrapeNetworkError` 异常类（区别于 429/403）
- `_scrape_city_pages`：第 1 页网络失败抛 `ScrapeNetworkError`（后续页仍 break 保留已有数据）
- `scrape_all`：全部城市均失败才上抛；个别失败记日志继续
- `run_once`：捕获后不更新 `last_scrape_at`、不发用户通知，直接 re-raise
- `main_loop`：连续 3 次后触发 5 分钟冷却，成功后自动清零

**`ensure_secret_key()` 首次运行不持久化**
条件 `if ENV_PATH.exists() or not ENV_PATH.parent.exists()`——当项目目录存在但 `.env` 缺失时（本地首次运行），两个条件都不满足，跳过写入，返回临时 key。重启后所有 session 失效。
- 去掉了前置条件，无条件尝试 `mkdir -p` + `write_env_key()`
- 写入失败才降级为进程内临时 key
- 同时写入 `os.environ` 确保进程内读取一致

### 安全加固（3 个）

**地图 API 自动 geocode 绕过访客只读限制**
`GET /api/map` 是 `api_login_required`（访客可访问），但在首次查询时自动启动后台 geocode 线程（外部 Photon 请求 + 数据库写入），访客模式只读承诺被破坏。
- 删除 `api_map()` 中的 auto-geocode 逻辑块
- 端点改为纯只读：只返回已缓存坐标，`uncached` 计数透出供前端提示
- admin 手动触发 geocode 仍通过 `POST /api/map/geocode`（`admin_api_required` + CSRF）

**日志脱敏：email 在错误日志中明文**
`booker.py` 预订失败的 WARNING/ERROR 日志含完整 email（个人身份信息）。
- 新增 `_mask_email()`，输出 `tes***@domain.com`
- 两处日志（debug + error 上下文）脱敏

**日志脱敏：代理 URL 含认证凭证**
`scraper.py` 的 DEBUG 日志完整打印 `http://user:pass@host:port`。
- 新增 `_mask_proxy_url()`，密码段替换为 `***`
- 一处 DEBUG 日志脱敏

### 重构

**统一代理读取**
`os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")` 在 scraper、booker、monitor 中共 5 处重复，且未覆盖 `ALL_PROXY`。Docker 文档建议设置 `ALL_PROXY` 但代码不支持。
- `config.py` 新增 `get_proxy_url()`，优先级 `HTTPS_PROXY > HTTP_PROXY > ALL_PROXY`
- 5 处内联读取全部替换；`scraper.py` / `booker.py` 移除闲置 `import os`
- `docker-compose.yml` 新增代理环境变量注释模板

### 新测试（68 个，3 个模块）

- **`tests/test_scraper_parse.py`**（16 个，3 个类）
  - `TestToListingNormal`（4）：完整字段、features、lottery、contract_start_date
  - `TestToListingMissingFields`（6）：price/status/url_key/available_from 缺失降级
  - `TestToListingEdgeCases`（6）：null 属性、空 selected_options、损坏数据、精度、日期截断
- **`tests/test_notifier_format.py`**（14 个，4 个类）
  - `TestFormatNew`、`TestFormatStatusChange`、`TestFormatBookingSuccess`、`TestFormatBookingFailed`
- **`tests/test_booker_helpers.py`**（22 个，3 个类）
  - `TestIsBookedByOther`（6）、`TestIsReservedByUser`（8）、`TestToH2sDate`（8）

附带修复：`_to_listing` 的 except 块对非 dict item 调用 `.get()` 的崩溃

### 文档

- 新增 `docs/dataflow_ch.mmd` / `docs/dataflow_en.mmd`：中英文 Mermaid 系统数据流图

---

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

### 文件整理

根目录从 35 个文件精简至 22 个，按用途分入子目录：

- **`docs/`**：README.md、README_cn.md、CHANGELOG.md（GitHub 自动识别 `docs/README.md` 作为仓库首页）
- **`docker/`**：supervisord.conf、entrypoint.sh（仅 Dockerfile 引用的两个辅助文件）
- **`packaging/`**：h2s_monitor.spec、build_dmg.sh、build.bat、asset/（构建打包相关）
- **`tools/`**：geocode_all.py、reset_db.py（一次性工具脚本）
- **修复**：移动后同步更新所有路径引用 — `h2s_monitor.spec` 的 `_base` 指向项目根、build 脚本分离 `SCRIPT_DIR`/`ROOT_DIR`、Dockerfile 两行 COPY 路径、`.github/workflows/build.yml` Windows 拼写纠正（`packing` → `packaging`）、`.gitignore`/`.dockerignore` 去重和对齐
- **LICENSE** 保留在根目录（GitHub 许可证检测要求）

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
