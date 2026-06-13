# Changelog

## v1.9.0 (2026-06-13)

### Breaking — H2S 传输层迁移至 CloakBrowser
- **背景**：Holland2Stay 将 GraphQL API 从 `api.holland2stay.com/graphql` 迁移至 `www.holland2stay.com/api/graphql`，并对旧子域名启用 Cloudflare Turnstile 保护。curl_cffi TLS impersonation 已无法通过（Turnstile 需要真实浏览器执行 JS challenge）。
- **抓取（scraper）**：`scrapers/holland2stay.py` 重写主体，新增 `BrowserFetcher`（共享模块 `browser_fetcher.py`），通过 CloakBrowser（patched Chromium，58 C++ 源码级反指纹 patch）自动执行 Turnstile challenge，然后通过 `page.evaluate(fetch)` 调用同域 GraphQL API。旧的 `scraper.py`（570 行）精简为 re-export 向后兼容。
- **自动预订（booker）**：`booker.py` 同步迁移，所有 GraphQL mutation 通过 `BrowserFetcher` 发送。`PrewarmedSession.session` → `.fetcher`。下单流程与真实浏览器对齐：
  - `AddNewBooking` 参数精简：移除 `contract_id`、`option_selected`（浏览器未传），仅保留 `cart_id` + `sku` + `contract_startDate`
  - 新增 `GetCheckoutAgreements` 步骤（照浏览器抓包，`setPaymentMethod` 后 `placeOrder` 前），fail-open
  - 完整链路：`CreateEmptyCart → AddNewBooking → SetPaymentMethodOnCart → GetCheckoutAgreements → PlaceOrder → IdealCheckOut`
- **新 API 字段变化**：H2S GraphQL 响应从 `custom_attributesV2` 嵌套对象变为扁平字段（`city: 29`, `basic_rent: 1395`, `energy_label: "A"` 等直接 int/string），大部分枚举字段返回 attribute option ID，通过 aggregations 接口构建 ID→label 映射。
- **工具链**：新增依赖 `cloakbrowser>=0.3.0`，Docker 镜像新增 Chromium 系统依赖 + `cloakbrowser install`（~300MB）。
- **资源开销**：CloakBrowser 空闲 ~190MB RAM，scraper 和 booker 各自独立实例。

### 改进
- **代码清洁**：`scraper.py` 从 570 行缩减至 28 行 re-export。H2S 爬取主体正式入驻 `scrapers/holland2stay.py`（当初 P0 多源重构的遗留 TODO）。
- **macOS 支持**：本地开发 CloakBrowser 可工作（v145，26 patches），但官方推荐 Linux 生产环境（v146，58 patches），后者 patches 更全、CF 绕过更稳定。
- **浏览器跨轮复用**：scraper 浏览器不再每轮创建/关闭，改为懒创建 + 跨轮复用。CF Turnstile 挑战从 ~40 次/小时降至 ~2 次/小时（首次创建 + token 过期重建）。BlockedError 时自动关闭重建；超过 2 小时主动重建避免会话过期。
- **Docker 兼容**：`BrowserFetcher` 内置 `--disable-dev-shm-usage` `--disable-gpu`（解决 `/dev/shm` 64MB 限制）。`docker-compose.yml` 内存限额 512M→1G，新增 `shm_size: 2gb`。`requirements.lock` 补全 `cloakbrowser`/`playwright`/`greenlet`/`pyee`/`2captcha-python` 精确版本。
- **Prewarm 异常修正**：`PrewarmCache.create()` 的 CF 屏蔽检测从 `BookingBlockedError` 改为 `BlockedError`（与 BrowserFetcher 抛出的异常类型一致）。

### 已知限制
- booker.py 迁移已完成代码层，尚未用真实 H2S 账号跑完整下单流程验证。

## v1.8.4 (2026-06-12)

### 功能改进 (Features)
- **H2S 防封策略收紧**：H2S GraphQL 403 现在进入 source-level circuit breaker，只暂停 Holland2Stay，Xior / OurDomain 等其它 source 继续抓取；冷却到期后只用 1 个 H2S 城市做 canary，成功后下一轮恢复完整 H2S 扫描。H2S prewarm 也从「抓取成功后刷新所有自动预订用户」改为「本轮确实有 H2S 自动预订候选时才为对应用户预登录」；登录/预订 403 后 1 小时内暂停 H2S 登录链路。连续第 3 次 H2S 403 起视为长时间 block，给 admin-only 发送“需要检查服务器”的告警，6 小时内不重复。
- **Web 监控暂停可见化**：Dashboard 在 monitor 未运行时向所有登录用户显示“系统监控已暂停”横幅，说明新房源通知、状态变更和自动预订均暂停。System Info 增加 admin-only 启动 / 暂停 / 重启监控按钮，复用现有 `/api/monitor/*` 控制接口。

## v1.8.3 (2026-06-11)

### 自动预订 — Xior / OurDomain (Auto-Booking)
- **RENTCafe 自动预订引擎**：新增 `bookers/rentcafe.py`，实现 RENTCafe（`securerc.co.uk`）多步表单自动化，Xior 和 OurDomain 共用同一引擎。
  - `RentCafeSession`：封装 HTTP 会话 + `cafeportalkey` 管理 + 自动 reCAPTCHA 求解
  - `RentCafeBooker`：`AbstractBooker` 子类，按 `self.source` 取平台专属凭据（`xior_*` / `ourdomain_*`），只做登录不自动注册
  - `XiorBooker` / `OurDomainBooker`：按平台区分配置
  - `bookers/__init__.py`：BOOKER_REGISTRY 新增 `xior`、`ourdomain`
  - **状态**：引擎框架已完成，但 RENTCafe 多步表单余下步骤（Applicant Info 等）尚未侦察，Web 面板标记为"开发中，暂不可用"
- **reCAPTCHA 求解模块**：新增 `captcha/`（`solver.py`），基于 2Captcha API 自动求解 reCAPTCHA v2/v3 Enterprise。
  - RENTCafe 固定 sitekey：v2=`6LfAdx8T...` / v3=`6LfBeqEa...`
  - v3 → v2 回退策略：v3 求解器 token 得分恒为 0.10（Google 指纹识别），RENTCafe 自动降级到 v2 checkbox → 用 2Captcha 真人求解（准确率 99%）
  - 新增依赖：`2captcha-python>=2.0.0`
- **Xior RENTCafe 侦察更新（2026-06-04）**：`docs/XIOR.md` §11 根据最新实测全面修订。
  - 纠正旧结论：`register.aspx` 和 `guestlogin.aspx` **现在都有** reCAPTCHA v3 Enterprise
  - 新增完整 sitekey 记录、v3→v2 回退 JS 逻辑
  - 注册链接被 Xior JS 隐藏，但后端接口仍存活
  - 预订流程 reCAPTCHA 成本估算：$0.003-0.005/次
  - 确认 WordPress AJAX 数据可靠性：19/20 交叉验证准确率
- **平台独立账号配置**：`AutoBookConfig` 拆分为三套独立凭据——H2S（`email`/`password`）、Xior（`xior_email`/`xior_password`/`xior_first_name`/`xior_last_name`/`xior_phone`/`xior_birth_date`）、OurDomain（`ourdomain_*` 同）。Web 面板对应拆分为三个独立区段，Xior/OurDomain 加"开发中"标记。
- **移除自动取消旧订单**：`cancel_enabled` 功能不可用且不再计划实现，Web 面板入口已删除（数据模型保留字段兼容旧数据）
- **依赖更新**：`requirements.txt` 新增 `2captcha-python>=2.0.0`

### 功能改进 (Features)
- **代理故障直连降级**：抓取代理连续失败且错误内容可确认代理服务端异常（如 Webshare `502 Bad Gateway`、`X-Webshare-Error`、`internal_error_auth_circuit_breaker_open`、CONNECT 502）后才进入 10 分钟 cooldown；全部代理都进入 cooldown 时，不再继续硬试故障代理，而是临时降级为服务器原生 IP 直连。monitor 在该状态下把抓取频率限制为最多 10 分钟一次，代理 cooldown 到期后自动恢复优先使用代理。
- **H2S Cloudflare 403 降噪**：预登录 prewarm 从「抓取前并发」改为「抓取成功后再启动」，避免当前出口被 WAF 屏蔽时每轮额外打出多用户登录 GraphQL；prewarm 一旦遇到 Cloudflare 403，会清理缓存并在 15 分钟内停止主动预登录。连续 H2S 403 冷却改为指数退避（15 → 30 → 60 → 120 分钟，上限 2 小时），减少同一出口反复撞 WAF。

### Bug 修复 (Bug fixes)
- **Xior 可订假阳性**：WP `yardi_room_availability` feed 会把「实际订不了」的单元报成「Available to book」，新增两道可用性校验闸（均作用于 `scrapers/xior.py` 的 `_to_listing`，下调时降级为 `Occupied` 但留库，便于日后状态变更通知）：
  - **① 可用日期 60 天窗口**：`Notice Unrented` 的 `availableDate` 距今 > 60 天（现住户远期才搬走，实测见过 `2027-07-01`）→ 不报可订。日期缺失/不可解析时保守保留（不漏报）。
  - **② floorplans.aspx 权威校验**：用 RentCafe OLE 的 `floorplans.aspx`（curl_cffi 直取，无 CF challenge）求出真正可订（`(Available)`+`applyButton`）的户型集合，单元 `floorplanId` 不在集合内 → 降级（解决"点 apply 链接进去说没了"）。仅当有窗口内候选时才多查一次/栋，**fail-open**：抓不到就信 feed，绝不漏报真房源。
- **monitor 错误类型补全**：抓取后管线（入库/通知）异常过去裸冒泡到 main_loop 只打日志不告警，现统一归类为「数据/通知管线错误」给 admin 发带类型告警（30 min 节流）；抓取阶段未分类异常改为 admin-only + 节流（不再每轮广播给普通用户）。
- **代理失效通知去掉 emoji**。

## v1.8.2 (2026-06-03)

### Bug 修复 (Bug fixes)
- **Android 登录 401 错误消息丢失**：服务端 `/api/v1/auth/login` 登录失败时返回 HTTP 401 + JSON `{"ok":false,"error":{"code":"unauthorized","message":"用户名或密码错误"}}`，但 Retrofit 对非 2xx 响应直接抛 `HttpException` 而非解析为 `ApiResponse` 对象。旧代码未捕获 `HttpException`，错误落到泛型 `catch (e: Exception)` 分支，显示 "Network error" 而非服务端真实消息。修复：
  - `ApiClient.kt`：`ApiException` 新增 `fromHttpException()` 静态方法，从 `HttpException.errorBody` 解析 JSON 提取 error.message
  - `AuthViewModel.kt`：`login()` / `register()` / `restoreSession()` 三个方法新增 `catch (e: HttpException)` 分支（排在 `ApiException` 前），用 `ApiException.fromHttpException()` 提取真实错误消息展示给用户

### CI / 构建 (CI/CD)
- **Release APK 自动构建**：GitHub Actions tag 推送时原先只构建 AAB（仅限 Google Play 分发）。现同时运行 `:app:assembleRelease` 产出签名 release APK，与 AAB 一起上传到 GitHub Release。用户可从 Release 页直接下载 APK 安装，不再局限于 Google Play。
  - `build.yml`：`Build release AAB` step 添加 `./gradlew :app:assembleRelease`，新增 `Upload APK to Release` step 上传 `app-release.apk`

## v1.8.1 (2026-06-03)

### 功能改进 (Features)
- **用户优先级拖拽排序**：用户管理页原先只能通过每个卡片上的 ▲/▼ 按钮一次一格调整自动预订优先级，每次点击整页刷新。现改为 HTML5 拖拽排序——抓住卡片左侧 `⠿` 手柄拖到目标位置松手即完成，DOM 即时更新排名数字，后台 `POST /api/users/reorder` 一次请求批量持久化所有 `sort_order`。▲/▼ 按钮保留作为移动端备选方案。
  - `templates/users.html`：user card 加 `draggable="true"` + 拖拽手柄 + ~80 行 JS（dragstart/dragover/drop → DOM 重排 → fetch API 持久化）
  - `static/design.css`：`.drag-handle` grab 光标/hover 高亮，`.dragging` 半透明，`.drag-over-*` 蓝色落点指示线
  - `app/routes/users.py`：新增 `POST /api/users/reorder`，接收 `{order: [id, ...]}` 批量更新
  - `mstorage/_user_configs.py`：新增 `reorder_users_bulk()`，单事务完成全部 `sort_order` 重编号

### 法律文件更新 (Legal)
- **Google Maps / FCM 披露**：隐私政策与使用条款按平台区分第三方服务——iOS 推送=APNs / Android 推送=FCM (Firebase)；iOS 地图=Apple Maps / Android+网页地图=Google Maps。新增 Google Maps Platform ToS 引用及 Google 隐私政策链接。`app/legal/privacy.txt` / `privacyzh.txt` / `terms.txt` / `termszh.txt` 四份文件同步更新至 2026-06-03。

### Bug 修复 (Bug fixes)
- **地图刷新按钮失效**：Google Maps 迁移（v1.7.6）将 `templates/map.html` 内所有 JS 包入 IIFE，但 geocode 按钮仍用 HTML `onclick="runGeocode()"` 属性绑定事件。HTML onclick 在全局作用域执行，`runGeocode` 被 IIFE 隔离后不可访问 → 按钮完全无响应。修复：IIFE 末尾加 `window.runGeocode = runGeocode` 暴露全局引用。
- **Geocode 完成后自动刷新地图**：geocode 进度轮询结束后直接调用 `loadMapData()`（延迟 800ms 等缓存写入落盘），不再要求用户额外点一次手动"刷新地图"按钮。恢复 v1.4.5 原有行为（Google Maps 迁移时误删）。

## v1.8.0 (2026-05-31)

### iOS 崩溃诊断修复 (Crash Diagnostics Fixes)
- **kind 解析 bug 修复**：`CrashDiagnosticsCollector.pendingDiagnostics()` 中从文件名解析 kind 时用 `split("-")` 取 `comps[1]`，但 ISO 时间戳含 `-` 分隔符（如 `2026-05-31T185748Z-other-UUID.json`），导致取到月份字符串（`"05"`）而非 kind。改为 `comps[comps.count - 2]` 从右往左取，兼容所有命名格式。
- **iOS 26 `appLaunchDiagnostics` 支持**：iOS 26 将崩溃/挂起数据封装在 `MXDiagnosticPayload.appLaunchDiagnostics` 中而非 `crashDiagnostics`，旧逻辑未识别导致 kind 归为 `"other"`。新增 `launch` 分类并加入服务端白名单。
- **`appLaunchDiagnostics` 体积大**：典型 MetricKit 诊断包 800–920 KB（远超旧 256 KB 限制），且 `json.dumps(indent=2)` 将嵌套堆栈树膨胀至 5–7 MB 磁盘占用。

### iOS 性能优化 (iOS Performance)
- **通知列表真正懒加载**：`NotificationsView` 原结构为 `LazyVStack → Section → VStack → ForEach`，VStack 包裹使懒加载失效，所有行一次性全建。改为每行作为 LazyVStack 直接子节点，各自绘制 `UnevenRoundedRectangle` 切片（首行圆上角、尾行圆下角、中间方角），0 间距堆叠成连续卡片。数百行通知时只渲染可视区。
- **typeFilter 切换瞬时化**：旧逻辑每次切类型筛选都重做 O(n) 日期解析 + 动画式整列重建。改为双桶策略——数据变化时单次 O(n) 扫描产 `allToday/allYesterday/allEarlier`（全量未过滤），切类型时只从缓存桶按 `n.kind`（O(1) 存储字段）筛，零日期解析、零动画。
- **NotificationItem 预计算**：`listingTitleHint`（正则去前缀）和 `parsedDate`（多格式日期解析）从 computed property 移到 decode 时一次性计算。之前每行渲染都重跑正则+日期解析（最贵的单行操作）× N 行 → 卡。
- **通知 API 7 天窗口**：`mstorage/_notifications.py` 新增 `within_days` SQL 参数，`app/services/notification_service.py` 默认取最近 7 天；`unread` 从窗口内已过滤集计算，与列表/badge 一致。
- **Live 绿点动画稳定性**：`LoginView` 的 live dot 呼吸从单布尔驱动两段 `.animation(value:)` 改为 `liveRipple`/`liveCore` 各自 `withAnimation(.repeatForever)` 显式驱动，修复转场事务把 repeatForever 捕获成一次性弹跳的 bug。

### iOS 多语言适配 (Localization)
- **简中 (zh-Hans)**：补全 21 条缺失翻译（崩溃诊断、通知设置、密码修改等界面文本）
- **繁中 (zh-Hant)**：补全 57 条缺失翻译

### 服务端 (Server)
- **崩溃端点 payload 限制**：`MAX_PAYLOAD_BYTES` 从 256 KB → 2 MB，实测覆盖 iOS 26 诊断体积
- **kind 白名单扩展**：`ALLOWED_KINDS` 加入 `launch`、`other`
- **磁盘优化**：`crash_reports/*.json` 去掉 `indent=2` pretty-print，嵌套诊断树节省 80%+ 磁盘
- **健康检查绕过代理 (NO_PROXY)**：容器配置抓取代理后 Python urllib 默认把所有请求（含 localhost 健康检查）都走代理 → 代理拒连 → 403 → 容器被标 unhealthy。`docker-compose.yml` 加 `NO_PROXY=localhost,127.0.0.1,::1` + 健康检查显式空 `ProxyHandler({})` 强制直连。

## v1.7.11 (2026-05-30)

### Bug 修复 (Bug fixes)
- **Dashboard 启动时间修复**：监控进程重启后 7 天运行时间百分比从 ~1% 重新攀升的 bug 已修复。旧方案存单个 `monitor_started_at` 时间戳，超过 7 天后下次重启会被覆盖为当前时间 → 掉到 1%，且不感知中途宕机。新方案改为**每小时存活采样**（`record_uptime_sample()`）：每个 UTC 小时记一条幂等样本到 SQLite meta 表，uptime% = 168h 里有样本的小时数 / 168。持久化跟 listings 同库、同 Docker volume，重启/重建不丢，宕机的小时自然没样本 → 真实反映可用率。
- **Android 下载链接 404**：修复登录页 Android App 下载链接在新版本发布后 404 的问题。根因是 GitHub Actions CI workflow（`build.yml`）没有 Android 构建 job，每次发新版 `.aab` 资产缺失。同时修复了 `versionCode`/`versionName` 硬编码和 `LoginScreen`/`SettingsScreen` 中版本字符串硬编码的问题。
- **非 macOS 服务器 iMessage 灰掉**：服务器端检测平台（`sys.platform`），非 macOS（Linux / Docker）上用户设置页的 iMessage 通知选项自动变灰（`opacity:0.45` + `pointer-events:none` + checkbox `disabled`），标注"不可用 — iMessage 需要 macOS 环境"。新增 3 个测试（`test_user_routes.py`）。

### 文档与工程 (Docs & Engineering)
- **文档全面更新**：`README.md` / `README_cn.md` 加 Android 下载链接；`ANDROID_PLAN.md` 补当前快照表 + 架构实际落地说明 + RC1-RC4 通过标记；`FUTURE_PLAN.md` 更新路线图 + 里程碑；`API.md` 补条件缓存（ETag/304）章节、`/legal` 端点、`/admin/monitor/restart`；`dataflow_en.mmd` / `dataflow_ch.mmd` 补 FCM 分流、uptime 采样、条件缓存、webhook；`guide.html` / `guide_cn.html` 补移动 App 下载入口、修复 GitHub 仓库名；`openapi.json` 补 `/legal` 端点；`iOS_README.md` 更新状态和性能优化项。
- **工程配置**：`.dockerignore` 加 `android/` + `.github/`；`.gitignore` 清理冗余 Xcode/Android IDE 条目，`*.p12` 归拢到 Android 段。

### Android (Android)
- **CI 自动构建 AAB**：`build.yml` 新增 `android` job，tag push 时自动构建 release `.aab` 并上传到 GitHub Release。
- **版本号动态化**：`versionName` 从 `APP_VERSION` 环境变量注入（CI 从 git tag 派生，如 `v1.7.11` → `1.7.11`）；`versionCode` 自动派生（如 `1.7.11` → `1711`）。
- **UI 版本字符串**：`LoginScreen` 的 `"UNOFFICIAL · v1.7.9"` 和 `SettingsScreen` 的 `"About FlatRadar 1.7.1"` 改为 `BuildConfig.VERSION_NAME`，跟随构建版本自动更新。
- **签名配置兼容 CI**：`build.gradle.kts` 签名密码支持 `ANDROID_STORE_PASSWORD` / `ANDROID_KEY_PASSWORD` / `ANDROID_KEY_ALIAS` 环境变量，兼容 GitHub Secrets 注入。

## v1.7.10 (2026-05-30)

### Bug 修复 (Bug fixes)
- **GitHub Actions Release 缺 DMG/ZIP**：修复 `build.yml` 中 upload-artifact 和 action-gh-release 步骤引用的文件名与构建脚本实际产出不一致的问题。构建脚本 (`build_dmg.sh` / `build.bat`) 产出的文件名为 `FlatRadar.dmg` / `FlatRadar.zip`，但 workflow 中写的是 `Holland2Stay Monitor.dmg` / `Holland2Stay Monitor.zip`，导致每个 release 都报 "No files were found" 并空发布。v1.7.10 统一为 `FlatRadar.dmg` / `FlatRadar.zip`。
- **Xior 未知状态测试断言过期**：`test_xior_scraper.py` 中 `test_to_listing_unknown_status_falls_back_to_available` 仍断言未知状态返回 `"Available to book"`，但 v1.7.9 已将默认值改为 `"Occupied"`（fail-closed）。测试名和断言同步更新为 `test_to_listing_unknown_status_falls_back_to_occupied`。

### 代码质量 (Code Quality)
- **SQLite 连接池化**：Web 路由中每个请求不再重复创建 SQLite 连接。`app/db.py` 的 `storage()` 在 Flask 请求上下文中将连接存入 `g._storage` 并复用，`teardown_appcontext` 自动关闭。非请求上下文（monitor / CLI / 测试）行为不变。消除每请求 ~3ms 的重复 `sqlite3.connect()` + `executescript()` 开销。
- **图表查询下推至 SQL**：`mstorage/_charts.py` 的 `_count_feature_values()` 和 `_bucketed_number_dist()` 从 Python 侧逐行 `json.loads()` 改为 SQLite `json_each()` 在数据库引擎内完成 JSON 解析 + 前缀过滤，Python 仅做分类。`json_valid(features)` 守卫防止非法 JSON 导致查询抛错。大数据库（1000+ 房源）下图表加载提升 50-80%。
- **N+1 batch UPDATE 修复**：`mark_status_change_notified_batch()` 从 for 循环逐条执行 UPDATE 改为单条 `WHERE listing_id IN (...)` 批量更新，与同文件 `mark_notified_batch()` 模式一致。
- **异常捕获范围收窄**：`scraper.py` `_to_listing()` 的 `except Exception` 改为 `except (TypeError, KeyError, ValueError, AttributeError)`，避免 `KeyboardInterrupt` / `SystemExit` / `MemoryError` 被意外吞掉。`notifier.py` `_send_with_retry()` 两处 `except Exception: pass` 改为 `except (OSError, asyncio.TimeoutError)` 并加 DEBUG 日志。
- **Cloudflare 403 检测统一**：`booker.py` 内联的 CF 检测改为复用 `scrapers/base.py` 的 `is_cloudflare_body()`，消除两份不同实现（booker 旧版只匹配大写 `<!DOCTYPE html>` 会漏检测小写 HTML）。同时确认 `batch_session()` 机制已在 `HollandStayScraper` 实现，P1 多源上线后 HTTP Session 跨城市复用不会退化。

### iOS 性能优化 (iOS Performance)
- **DateFormatter 静态化**：`NotificationItem.createdDate` 每次访问新建 `ISO8601DateFormatter` + 最多 4 个 `DateFormatter`（~100–200μs/次），列表滚动时每行每帧重复分配。全部改为 `static let` 共享实例（`isoFractional`、`isoPlain`、`fallbackParsers`、`shortDateFormatter`），`dayBucket` 的 `Calendar` 也改为 `static let`。消除通知列表最大的单点分配开销。
- **Listing.featureMap 键预归一化**：`featureValue(matching:)` 每次调用都对 `featureMap` 所有键现调 `normalizeFeatureKey`（folding + 多次 replacingOccurrences + lowercased，~4 次分配/键）。`ListingRow` 一行读 5–6 个派生属性、每个再遍历 10–20 个键 → 50 行可见时每帧 ~5,250 次字符串操作。改为 decode 时一次性预算 `normalizedFeatureMap`（`normalizeFeatureKey` 改为 `static`），后续查找只需归一化少量别名。
- **URLCache 条件 GET**：`APIClient` 从 `URLSession.shared` 改为专用 `URLSession`，配 2MB 内存 + 20MB 磁盘 `URLCache`。服务端 GET 200 响应带 `ETag` + `max-age=10` → 10s 新鲜窗口内切 tab 直接命中本地缓存、零网络；超窗后自动带 `If-None-Match` 复验，304 无 body 复用缓存。消除列表/地图/日历/图表的重复下载。
- **通知首屏非阻塞**：`NotificationsStore.fetch()` 中 `loadMoreUntilUnreadIsVisible()` 从同步 `await` 改为后台 `Task` 执行——首屏拿第一页就结束 loading 立刻渲染，不再干等 N 次串行往返。加 `maxBackfillPages=5` 上限防止未读极多时串行拉几十页拖垮网络/电量。`backfillTask` 可取消（登出 / 新一轮 fetch）。
- **地图聚类后台化**：`MapClustering.cluster()` 拆出纯值类型重载（`Double` 替代非 Sendable 的 `MKCoordinateRegion`），`MapView.recomputeClusters()` 改用 `Task.detached(priority: .userInitiated)` 把 2000 条 grid 分桶 + 排序移出主线程。加 `clusterTask` 取消机制防止快速缩放时旧结果覆盖新结果。

## v1.7.9 (2026-05-30)

### 新特性 (Features)
- **用户优先级排序**：Admin 用户管理页新增 rank 排序功能。每个用户卡片显示 `#1` `#2` `#3` 优先级徽标 + ▲/▼ 按钮，点击即可调整顺序。rank 越小自动预订优先级越高——当多个用户同时匹配同一房源时，rank 小的优先拿到（`sort_order` 字段此前已建但无可操作入口）。

### Bug 修复 (Bug fixes)
- **Dashboard 运行时间**：修复 Docker 容器重启后 7 天运行时间从 1% 重新计数的 bug。根因是 `/proc/uptime` 和 `/proc/<pid>/stat` 的 `starttime` 都相对于容器启动时间，重启后两者同时归零导致 `started_at ≈ now`。修复方案是将 monitor 启动时间持久化到 SQLite `meta` 表（`monitor_started_at`），Dashboard 优先读取 DB 值计算运行时间，跨容器重启保持不变；`/proc` 计算保留为回退路径（macOS 开发环境）。
- **抓取 GraphQL data=null 崩溃**：修复 H2S API 返回 `{"data": null}`（GraphQL 字段级 non-null 错误传播至根）时 `.get("products")` 抛 `AttributeError` 导致整轮抓取中断的 bug。改用 `(data.get("data") or {}).get(...)` 安全链式访问，同时第 1 页遇 null 时显式抛出可感知错误。

### 安全加固 (Security)
- **存储型 XSS**：修复 `user_form.html` 中 `renderHoods()` 动态渲染街区名时未转义 HTML 的问题，改用 `escapeHtml(h)`。
- **用户名枚举**：`/check-user` 端点加 IP 限速（30 次/分钟），防批量枚举已注册用户名。
- **Xior 未知状态 fail-open**：`_STATUS_MAP` 未知状态默认值从 `"Available to book"` 改为 `"Occupied"`（fail-closed），避免新状态被误判为可预订。
- **Android 签名密钥**：移除 `build.gradle.kts` 中硬编码的签名密码，改为从 `local.properties` / 环境变量读取。

## v1.7.8 (2026-05-28)

### 代码质量 (Code Quality)
- **后端**：device_service 平台路由改为显式 allowlist；`asyncio.run()` → `_run_async()` 兼容 async worker；FCM env var 加 `_safe_int/_safe_float` 防配置错误崩溃；config.py env key 正则加 `\b` 防前缀碰撞；Dashboard uptime 改用 `/proc/<pid>/stat` 计算进程真实启动时间（修复 Docker 重启后 uptime 不变的问题）。
- **Web 前端**：`escapeHtml` 补全单双引号转义；所有 fetch 静默 catch 加 `console.error`；multi-select 加 `aria-haspopup`/`role`/`aria-expanded` 无障碍属性。
- **iOS**：`resolveBaseURL()` 移除 force-unwrap 启动崩溃风险；NotificationsStore 静默 catch 加 DEBUG 日志。
- **Android**：`rememberPullToRefreshState()` 从 recompose 重建改为外部 `remember`；AuthViewModel 全局 catch 前置 `CancellationException` 重新抛出。

### Bug 修复 (Bug fixes)
- **Android FCM 推送端到端**：客户端 FCM 通道已拉通并真机验收通过。后端 `POST /api/v1/devices/test` 按 `platform` 分流——iOS 走 APNs，Android 走 FCM（data-only payload）。
- **Android 启动 ANR**：修复 App 冷启动时主线程阻塞导致 ANR 的问题。根因是 `SseClient.connect()` 的 `callbackFlow` 继承了 `viewModelScope.launch` 的 Main dispatcher，`readUtf8Line()` 在主线程上阻塞等待 SSE 数据；修复方案是将整个 SSE 读取循环包在 `withContext(Dispatchers.IO)` 中。
- **Android 地图定位**：修复 MapScreen 定位功能不可用的问题。移除自定义定位按钮，添加 `play-services-location` 依赖，改用 `FusedLocationProviderClient.getLastLocation()` 获取缓存位置；暗色模式下地图自动切换暗色样式。
- **Android 通知分类**：修复测试推送在通知列表中被归类为 BOOK 的问题，新增中文/emoji 关键词（🧪、测试推送、推送链路）匹配为 TEST 类型。
- **Android 崩溃诊断**：新增 `CrashReporter` 全局异常捕获，自动收集堆栈 + 设备信息，POST 到 `/api/v1/diagnostics/crash`（bearer_optional），同时写入本地文件兜底。后端诊断端点新增 `platform` / `os_version` 字段兼容 Android。
- **Android 登录页**：移除 Staff 管理员登录入口，保留 Tenant / Guest 两种模式。
- **Android 通知页**：新增进入页面时自动刷新列表，避免首次加载后 SSE 未连接时数据陈旧。
- **Android 日历性能**：修复 `CalendarScreen` 月历网格每次 recompose 重建 42 个 `LocalDate` 对象的问题，加 `remember(month)` 缓存；`DashboardScreen` 价格排序 `Regex` 从每次调用重建改为顶层单例。
- **iOS 通知筛选性能**：修复通知列表切换 type filter 时卡顿问题。`NotificationItem.kind` 从计算属性改为 decode 时预计算的存储属性（O(n) 字符串匹配 → O(1)），`currentFilterScope()` 改用 `rebucketDayGroups()` 中缓存的 kindCounts（消除额外 O(n) 扫描）。500 条通知下 filter 切换从 ~500ms 降至 ~20ms。
- **iOS 列表页性能**：修复 ListingsView 每次 body 重渲染都做 O(n log n) 排序 + O(n) `isNew()` 扫描（含每条 date 解析）的性能问题。改为 `@State` 缓存排序和 new/earlier 分桶结果，仅在 `store.listings` 或排序条件变化时重算。
- **iOS 状态色统一**：修复 Reserved 胶囊在不同页面显示颜色不一致的问题（详情页红色、通知页系统灰、列表页灰蓝）。统一所有页面 Book/Lottery/Reserved 状态色为 asset catalog 语义 token（`.statusBook`/`.statusLottery`/`.statusReserved`），涵盖 ListingDetailView、CalendarView、MapView、NotificationRow。
- **街区筛选保存失效**：修复用户编辑页面中，选择街区后点击保存实际未保存的问题。原因是街区下拉框动态加载时，`loadNeighborhoods()` 重新渲染 DOM 时使用了页面初始快照值 `selNbh`，覆盖了用户当前的勾选状态。

## v1.7.8 (2026-05-27)

### 体验优化 (UX)
- **登录页注册确认弹窗**：按钮改为「登录 / 注册」，点击后弹出确认卡片，分两步说明（首次登录自动创建账户 + 同意条款/隐私政策），用户确认后才提交表单。
- **邮箱即时验证**：用户配置邮箱时，输入合法格式的邮箱地址后即时出现「发送验证邮件」按钮，无需先保存表单再重发验证。
- **登录页提示文字换行优化**：推送功能说明与自动注册说明分行显示，阅读更清晰。

### Bug 修复 (Bug fixes)
- **多选下拉菜单底部溢出**：修复页面底部多选组件（如租客类型）下拉菜单超出视口无法点击的问题，现会自动向上翻转展开。

## v1.7.7 (2026-05-27)

### 代码维护 (Maintenance)
- 修复 `config.py` `_parse_xior_cities()` 死代码（残留 return 语句）
- 修复 `load_config()` 中 DB_PATH/TIMEZONE 热重载时不从 os.environ 重新读取的问题
- 修复 `MultiNotifier` 未调用 `super().__init__()` 导致 language 属性未初始化
- 修正 `scraper.py` 403 维护探测阈值注释与代码不一致
- Settings 页面 flash 消息硬编码中文改为走翻译系统
- 移除 Settings 页面冗余的 User Management / Client Management 提示条
- CSS 暗色模式颜色切换统一走变量：新增 `--grad-green/amber/red`、`--pill-telegram/email` 变量
- 暗色模式下文字渐变色提亮一档；Telegram/Email 渠道标签自动适配主题
- 修复暗色模式下表单输入框、filter 卡片边框的硬编码白色内阴影

### 界面优化 (UI)
- Web 端全面换用毛玻璃（Glassmorphism）设计风格
- 仪表盘、日历、统计、地图等核心页面统一玻璃质感

## v1.7.6 (2026-05-26)

### 新特性与界面重构 (Features & UI Revamp)
- **全新 B2C 风格登录页**：彻底重构登录页面，采用现代毛玻璃（Glassmorphism）视觉风格，摆脱后台管理系统的刻板印象。
- **日夜交替动态主题**：登录页新增日间/夜间模式切换功能，包含平滑的日出日落、月亮升起动画。
- **荷兰风情地平线动画**：登录页地平线新增两座纯 CSS 绘制的传统荷兰风车剪影，包含动态旋转的风帆与日夜光影适配。
- **客户端下载入口**：登录页新增 iOS 和 Android App 下载入口（安卓版提示“积极上架中”）。
- **注册与访客模式优化**：
  - “访客模式（只读）”文案精简为“访客模式”。
  - “注册用户账号”精简为“注册账户”。
  - 为副按钮（注册、访客）补齐了发光效果（Glow）与悬浮动画。
  - 为注册面板展开增加了纯 CSS 实现的丝滑手风琴下拉动效。
  - 加大登录页底部的 帮助、隐私、条款、赞助 等辅助链接的字号，“赞赏”统一更名为“赞助”。

### 体验优化与调整 (Enhancements)
- **数据统计面板**：默认显示的时间维度由“近 30 天”调整为更聚焦的“近 7 天”。
- **侧边栏结构优化**：
  - “App 会话管理”更名为“客户端管理”。
  - “系统信息”菜单项移至侧边栏最底部，优化功能层级。
- **房源筛选栏**：重构筛选表单的 CSS 布局（引入 Grid），确保多行表单元素的完美对齐。
- **仪表盘状态徽标**：对齐了 Recent Listings 与 Status Changes 模块的徽标样式。
- **赞助页面优化**：修复了收款码容器比例问题，自适应长方形收款码，消除多余的白边。

### 缺陷修复 (Bug Fixes)
- **崩溃报告路径脱敏**：修复了崩溃报告中直接暴露本地服务器物理路径的问题，现统一脱敏展示为相对路径 (`/app/data/crash_reports`)。

## v1.7.5 (2026-05-25)

### 全量代码审查与安全加固

5 路并行扫描，26 个发现，修复 22 个：

**Android（6 修复）**
- **SSE 阻塞 Main 线程**：`SseClient.connect()` 内 `call.execute()` 包 `withContext(Dispatchers.IO)`
- **深链接断开**：`AppNavigation` 观察 `NavigationCoordinator.pendingListingId` → `navController.navigate("listing/$id")`
- **弱网误删 token**：`restoreSession()` 改为只对 `ApiException.isAuthError` 清 token，网络异常不清
- **Settings 状态擦除**：`saveServerUrl/saveColorScheme` 改用 `.copy(message=)` 而非新建 `SettingsUiState()`
- **LocationListener 泄漏**：`MapScreen` "My Location" 加 15s timeout，超时自动 `removeUpdates`
- **filter 不生效**：`PUT /api/v1/me/filter` 成功后加 `write_reload_request()`，让 monitor 热重载

**Python 后端（11 修复）**
- **SSE 绕过禁用用户**：加 `_user_token_still_allowed()` 检查
- **CSP 缺位**：`web.py` 加 `Content-Security-Policy` header
- **HSTS 缺失**：加 `Strict-Transport-Security: max-age=63072000`
- **booker None 崩溃**：`book_with_fallback` 返回 None 时 `continue`
- **Guest 无 CSRF**：`/guest` GET→POST + `@csrf_required`
- **房源 API 无限流**：guest 访问 100 req/min IP 限流，超限返 429
- **status change FCM gate 遗漏**：加 `get_fcm_client() is not None` 条件
- **测试推送 flash 条件错误**：`any("/" in m)` 替代复杂条件
- **CSS 无效值**：`active` → `var(--accent)`
- **测试推送无日志**：加 `logger.info(...)` 操作审计
- **stale docstring**：`legal_text.py` → `app.legal/`
- **Web 地图加载修复**：CSP 增加 Google Maps 所需的 `maps.googleapis.com` / `maps.gstatic.com` 许可，避免动态加载 Google Maps JS 被浏览器拦截后页面一直停在“加载中”；地图脚本增加 `onerror` 和初始化超时提示。
- **Web Google Map 性能优化**：保留 Google Maps 的同时接入 marker clustering；marker 改为 `requestAnimationFrame` 分批创建，InfoWindow 内容改为点击时懒创建，clusterer CDN 超时后自动降级为普通分批 marker，避免几百个点同步渲染卡住主线程。

**iOS（5 修复 + 31 单元测试）**
- **Biometric crash**：`SecAccessControlCreateWithFlags(...)!` → `guard let`
- **Dashboard 并发 mutation**：`fetchSummary()` 加 `guard !isLoading`
- **PushDelegate IUO**：`shared: PushDelegate!` → `PushDelegate?`
- **AdminStore 死代码**：清理未使用的 `original` 变量
- **LegalSheetView API fetch**：`.task {}` 拉取法律文本 + 本地 fallback
- **iOS 单元测试补齐**：新建 `FlatRadarTests` target（31 tests），覆盖 Listing/APIResponse/AuthModels/NotificationItem 模型编解码与状态逻辑。此前 13K 行代码零单元测试，现核心模型层已覆盖

### Android App — Map and Settings parity

- **Google Maps Compose 接入**：Android Map 页从城市分组列表升级为 GoogleMap marker 视图；接入 `maps-compose-utils` 官方 clustering，marker 按状态着色，初始 camera 根据房源 bounds 适配，点击 marker/cluster 显示底部房源卡片并可进入详情。
- **Android Map/Calendar 状态打磨**：Map 底部选中卡片补齐状态、价格、面积、入住日期和来源信息；Map/Calendar 错误态和空态增加 retry。
- **Android Map/Calendar DTO 对齐**：修复 `/map` 与 `/calendar` 返回 `data.listings`、`lat/lng`、`building` 轻量字段时的解析路径，移除开发期误加的 `items` fallback，避免进入 Map/Calendar 后出现 `Required value 'items' missing at $.data`。
- **Maps key 本地配置**：Gradle 从 `android/local.properties` 读取 `MAPS_API_KEY`，注入 Manifest 和 `BuildConfig`；未配置时 Map 页保留列表 fallback，避免开发/CI 白屏。
- **Settings 运行时配置**：新增 DataStore `PreferencesManager`，持久化 `server_url` 和 `color_scheme`；App 启动后自动应用 server URL，主题支持 System / Light / Dark。
- **Android Biometric sign-in**：user 登录/注册可选择保存本机生物识别登录；登录页通过系统 BiometricPrompt 解锁后复用正常登录 API，Settings 可移除本机保存凭据。
- **Android A1 错误展示**：新增 root `AppErrorBus` + snackbar，登录、注册、Dashboard、Listings、Listing Detail 的后端/网络错误统一进入全局提示。
- **Android 登录兼容存量账号**：Sign in 前端校验改为只要求密码非空，兼容后端已有 3 字符密码用户；注册和改密码仍保留新密码至少 4 字符。
- **Android 顶层导航修复**：从 Listing Detail 等二级页面点击 Dashboard/Browse/Alerts/Settings tab 时禁用详情栈 restore，避免 tab 看似无效、只能 Back 返回。
- **Android Browse 子模式入口**：phone 端 Browse tab 增加 List / Map / Calendar 二级 tabs，让 Map 和 Calendar 在 4-tab 布局下可见；tablet 端继续保留独立 Map/Calendar rail 项。
- **Android 品牌资源接入**：复用 `static/logo.png` 生成 Android launcher icon，并在登录页展示 FlatRadar logo，替换开发期默认图标体验。
- **Android Material 3 设计系统接入**：按 `FlatRadar Android M3.html` 设计规范落地第一批原生 Compose 改造，更新 M3 seed 色 `#0057CC`、light/dark color roles、Typography、Shape、状态色 token、80dp bottom navigation、Login、Dashboard hero 和 Alerts 列表/功能胶囊样式。
- **Android Dashboard Explore 统计修复**：`ChartEntry` 改为兼容后端 `source/status/range/hour/city/label/date` 动态字段，恢复 Explore 下平台、状态、价格、类型、能源、租客统计卡片展示，并按 iOS 逻辑合并 source/type/energy bucket。
- **Android Dashboard 统计交互修复**：Explore 统计卡片恢复点击展开能力，通过底部弹层展示完整分布明细和条形占比；Dashboard 根内容增加 status bar inset，避免标题与手机状态栏重合。
- **Android Browse 状态栏适配**：Browse 页 List / Map / Calendar 顶部切换栏增加 status bar inset，避免 edge-to-edge 模式下与系统状态栏重合。
- **Android Calendar 日期分组修复**：Calendar 不再复用会过滤 `2049/2050` 占位日期的通用 `ServerTime.dayKey()`，改为按 iOS Calendar 专用逻辑读取 `available_from` 前 10 位并校验日期，避免后端已有房源但选中日期列表为空。
- **Android M3 页面收口**：Listings 改为 M3 surface card 列表与 pill 搜索/筛选；Listing Detail 增加 M3 hero、tonal CTA 和 grouped detail sections；Settings 改为 profile card、tonal save button、40dp leading icon containers；Map/Calendar 统一 surfaceContainer、shape 和 FlatRadar 语义状态色。
- **Android Listing Detail 字段对齐**：详情页字段改为和 iOS 一样从后端 `feature_map` / `features` 派生 Type、Area、Building、Floor、Rooms、Energy、Finishing、Occupancy、Contract、Tenant，修复后端已有数据但 Android 显示 `—` 的问题。
- **Android Listing Detail parity**：详情页补齐 source/status/city 头部、价格/入住日期/面积/建筑 metric cards、Key Details、All Details、Monitoring 和官方平台链接；当前 API/model 无 listing 图片 URL，图片展示继续等待数据源。
- **Android 账号合规**：user Settings 增加 Change Password，调用 `/auth/password` 更新 app password，并显示其他 session 撤销结果。
- **Android 数据导出**：user Settings 增加 Export My Data，调用 `/me/export` 拆出 `data` JSON 后通过系统分享面板交付，不写入本地文件。
- **Android 法律入口**：Settings 增加 Terms of Use / Privacy Policy 页面，普通用户和 guest 可离线打开；admin 继续隐藏法律入口。
- **Android Calendar 月格**：Calendar 页从月份列表升级为月历网格，每日显示可入住房源数量，选中日期后展示当天房源并可进入详情，空态/错误态可重试。
- **Android Alerts inbox**：通知页按 TODAY / YESTERDAY / EARLIER 分组，增加类型色点、相对时间、Live 状态、单条 mark read、滑动已读、单条更多菜单和导航 unread 角标。
- **Android 计划文档复盘**：`docs/ANDROID_PLAN.md` 增加当前实现进度、A2/A5 状态和 FCM 阻塞说明。
- **Android Alerts 界面重设计**：重新设计 Alerts 界面，使用更具现代感的多色小药丸（如 New 绿点、Status 橘点）、未读角标叠层显示、横向过滤 Chip 及更现代扁平化分割线布局，提升列表的可读性与美观度。
- **Android Settings 界面优化**：去除了 `server_url` 服务器配置选项，防止普通用户误修改；并在“Push Notification Filter”设置栏下方动态显示当前应用中的活跃过滤条件摘要，点击可直接跳转到过滤配置页。
- **Android 登录界面打字性能优化**：将原本在重组时动态创建的 `BackMountainPoints` 与 `FrontMountainPoints` 坐标对列表抽离为顶层静态常量；重构 `MountainPath` 绘制函数，使用 `Modifier.drawWithCache` 将 `Path` 初始化移动到缓存区，避免打字重组触发 draw 帧时重新分配 Path 对象，实现零对象绘制和流畅打字；并使用 `remember(isDark)` 缓存顶部背景渐变。
- **Android 登出二级防误触**：在 Settings 界面点击 Log Out 时，加入 `showLogoutDialog` 状态并拉起二级确认弹窗（AlertDialog），防止用户误点导致会话非预期终止。
- **法律文本三端统一**：新增 `app/legal/*.txt` 作为 canonical source of truth（terms_en/zh + privacy_en/zh），`GET /api/v1/legal` 公开 API 端点（无需登录）。三端改为 API 优先 + 本地缓存 fallback：Android `LegalScreen` → `LegalViewModel` fetch，iOS `LegalSheetView` → `.task` async fetch，web `app/routes/legal.py` → `app.legal.get_legal()`。删除旧的 `legal_text.py`（web）、`LegalText.kt` 降级为 Android 离线 fallback、`LegalText.swift` 降级为 iOS 离线 fallback。免责条款同步更新为多平台中立声明（"not affiliated with any of the housing platforms it monitors"），去掉原先仅提 Holland2Stay 的单一措辞。
- **Android 中文字符翻译完整覆盖**：`values/strings.xml` + `values-zh/strings.xml` 各 ~170 条目完全对称，覆盖 Tab、仪表盘、登录注册、房源列表/详情/筛选、地图、日历、通知、设置、管理面板、使用条款和通用文案。
- **Android FCM 推送完整闭环**：
  - 客户端：`FcmService`（onNewToken + onMessageReceived）、`FcmTokenManager`（设备注册/注销 + 异常日志）、通知渠道（listings/general）、Android 13+ 运行时权限、通知点击 deep link 全部接入。
  - 后端：`notifier_channels/fcm.py`（OAuth2 服务账号认证 + FCM HTTP v1 API，send_one/send_many），`mcore/push.py` 按 `device_tokens.platform` 字段分流 iOS（APNs）/ Android（FCM）双发，所有 dispatch 函数双端覆盖。
  - 测试：Python FCM 35 tests（client 18 + dispatcher 17），Android 47 ViewModel tests。
- **Android Listing 模型对齐 iOS**：删除 `Listing` 中 9 个与 `featureMap` 重复的硬编码字段（areaText/energyLabel/buildingText/finishing/floor/rooms/occupancy/contractType/tenantRequirement），`display*` 计算属性统一从 `featureMap` 派生。`MapCalendarListingDto.toListing()` 将 DTO flat 字段 `putIfAbsent` 合并进 `featureMap`，后端改 key 名时两端同步自适应。
- **架构审查（5 Critical + 10 Warning）**：确认 SQLite WAL 模式多进程安全、users.json 仅作一次性迁移输入运行期只读 SQLite；修复 FCM 私钥日志泄漏风险（不再 dump traceback）；`FcmTokenManager` 不再静默吞异常；`push.py` 移除 `storage.conn` 直接访问、补齐 FCM 路径日志。


## v1.7.1 (2026-05-23)

### 平台维护态检测与安静降级

Holland2Stay 计划维护期间整站（含 GraphQL API）返回 403，旧路径将所有 403 一律当作 Cloudflare WAF 屏蔽处理——发用户告警、走 15 min 冷却、打 ERROR 日志。维护态下用户什么都做不了，凌晨告警是噪音。

v1.7.1 引入 **UpstreamMaintenanceError**，在 403 连续出现时主动探测主站，命中维护页则走"安静等待"路径：

- **`scrapers/base.py` — 维护检测基础设施**：新增 `UpstreamMaintenanceError` 异常类（与 `BlockedError` 语义区分——前者自己会恢复、后者需人工介入）；`is_maintenance_body()` 通过 5 组英文短语识别维护占位页；`probe_h2s_maintenance()` GET 主站探测，异常安全（网络错误吞掉返回 False）。
- **`scraper.py` — 连续 403 触发探测**：进程级 `_consecutive_403_count` 跨轮累计，达阈值 3 时 GET 主站；命中维护页 → 抛 `UpstreamMaintenanceError` 并清零 streak；未命中 → 维持原 `BlockedError` 路径。成功响应自动清零 streak。
- **`scrapers/__init__.py` — dispatcher 维护优先**：所有任务失败时 `UpstreamMaintenanceError` 优先于 `BlockedError` 上抛，确保 monitor 选择正确冷却策略。
- **`monitor.py` — 维护态两段处理**：
  - `run_once`：捕获后写 `upstream_maintenance_seen_at` / `upstream_maintenance_last_at` meta 键驱动 dashboard banner；**不给普通用户发告警**（避免凌晨维护吵醒人）；给 admin web 通知面板发一条（1 小时节流）。抓取成功时自动清空维护态 meta。
  - `main_loop`：15 min 冷却（与 BlockedError 同长度但语义不同——INFO 日志、不重置 adaptive_peak、不计入 network_fail_streak）。
- **Web dashboard 维护 banner**：新增 `.maintenance-banner` CSS（温和警告色，区别于 error alert）；`base.html` 顶部渲染维护标题 + "Since X time ago"；`_inject_upstream_maintenance` context processor 注入状态；`monitor_service.py` 新增 `get_upstream_maintenance()`。
- **翻译**：3 个新 key（`upstream_maintenance_title` / `_hint` / `_since`），中英双语。

### OurDomain TLS 指纹智能轮换

SecureRC（OurDomain 用的 RentCafe + Cloudflare）对 TLS 指纹做 per-fingerprint 跟踪——同一指纹短时间内重复使用会被标记进入"挑战中"状态返 403。旧实现每次 `scrape()` 固定从 chrome131 开始依次重试，等于反复把"被烧"的指纹往枪口上送，chrome131 / chrome124 看起来"特别容易被封"只是因为它俩总是排最前面。

v1.7.1 引入进程级指纹状态记忆 + 同 session 内 403 软重试：

- **指纹状态追踪**（`_FINGERPRINT_STATE`）：成功通过的指纹记录 `last_good_at`，下次 `scrape()` 优先用它；403 失败的标记 30 min cooldown，期内排到队尾。排序逻辑：上次成功 → 未冷却 → 冷却中兜底。进程重启清空（等于"忘掉旧烧"从配置顺序重探），指纹热度本身就是分钟级现象，无需持久化。
- **同 session 内 403 软重试**（`_get_text`）：Cloudflare JS challenge 返回 403 的同时也会下发 `cf_clearance` cookie，`curl_cffi` 不跑 JS 算不出 challenge token，但 cookie 已攒到 session 上——第二次 GET 同 URL 往往直接通过。拿到 Cloudflare 403 后先等 2s 再同 session 重试 1 次，仍失败才抛 `BlockedError` 让上层切指纹。大幅减少"换指纹"开销，稳态下一个指纹即可稳定服务。
- **`_impersonate_attempts()` 智能排序**：从固定顺序改为按状态分桶 → 合并（last_good → fresh → cooldown），受 `OURDOMAIN_WAF_RETRIES` 限制长度。

### 测试

- **`tests/test_scraper_maintenance.py`**（6 个类，17 个测试）：`is_maintenance_body` 单元测试、`probe_h2s_maintenance` 单元测试、`_post_gql` 403 streak → 维护探测全链路、dispatcher 维护优先上抛、monitor 维护态 admin 通知 + 节流 + meta 写入。

### iOS App — 性能优化

- **Dashboard chart 请求分批**：`fetchMiniCharts()` 从 7 并发改为 3 批串行（3→2→2），峰值并发从 7 降到 3，首页 sparkline + source/status mini card 最先返回。避免慢网络下 TCP 队头阻塞同时打到后端。
- **`Listing.isNew` / `ageText` 减少 `Date()` syscall**：新增 `isNew(asOf:)` / `ageText(asOf:)` 重载，调用方可外部快照 `now` 复用。`ListingsView` 分桶循环从每条 `Date()` 改为循环前快照一次（100 条 = 100→1 次 syscall）；`ListingRow.titleLine` 中 `isNew` 和 `ageText` 共用同一个 `now`（每行 2→1 次 syscall）。

### Web 前端 — 性能优化

- **SSE bfcache 支持**：admin 页面在 `pagehide` 时关闭 SSE EventSource 连接，`pageshow` 时若从 bfcache 恢复则重连。配合 `Cache-Control: no-cache`（而非 `no-store`），浏览器可将当前页放入 back-forward cache，返回键瞬间复原不再空白卡死。
- **状态胶囊 filter 归并**：新增 `status_capsule` filter，一次 `.lower()` 同时返回 label + CSS 类名。模板每行从 `status_short` + `status_badge` 两次 filter 调用（各做一次 `.lower()`）改为单次调用。`listings.html` / `index.html` 的表格和移动卡片均已简化。
- **LCP 优化**：`design.css` preload 加 `fetchpriority="high"`；sidebar logo preload；CSS 版本号升至 v16。
- **SQLite 索引补全**：新增 `listings(city)`、`listings(first_seen)`、`listings(status)`、`listings(last_seen)`、`status_changes(changed_at)`、`status_changes(listing_id)` 6 个索引。dashboard 首页城市筛选 / 状态计数 / 排序、status_changes JOIN 查询不再走全表扫描。
- **维护态查询缓存**：`_inject_upstream_maintenance` context processor 加 5s TTL 缓存。之前每个页面渲染都读 SQLite meta 表，现在最多每 5 秒读一次。
- **Dashboard 60s 自动刷新**（已知问题，待修）：当前用 `window.location.reload()` 整页硬刷新，浪费带宽和服务器资源。建议改为 AJAX 局部刷新。

---

## v1.7.0 (2026-05-22)

### 后端 — 多源抓取架构（P0→P1）

- **`scrapers/` 包**：新增 `AbstractScraper` ABC + `ScrapeTask`/`ScrapeResult` 协议。每个第三方平台实现 `scrape(task) → ScrapeResult`，`dispatch_scrape_tasks()` 按 `source` 路由、隔离故障、合并产出。
- **`scrapers/base.py`**：共享异常 `RateLimitError`/`BlockedError`/`ScrapeNetworkError` 从 `scraper.py` 迁入，所有 scraper 统一异常协议。
- **`scrapers/holland2stay.py`**：`HollandStayScraper` 封装现有 GraphQL 抓取逻辑，行为零变更。
- **`monitor.py` 全量切换到 `dispatch_scrape_tasks()`**：旧 `scraper.scrape_all()` 路径已移除。多源抓取结果合并后统一走 diff → notify → book 管线。
- **`Listing.source` 字段**：标识房源平台来源（`"holland2stay"` / `"ourdomain"`），UI/通知模板可据此显示 source badge。

### 后端 — OurDomain / RENTCafe 集成

- **`scrapers/ourdomain.py`**：`OurDomainScraper` — RENTCafe 两阶段抓取（`floorplans.aspx` → `availableunits`），单元级数据提取（房间号 #6045、面积单值 m²、月租单值 €、押金、楼层、朝向），`unit_id` 跨 FP 去重，`parse_ourdomain_floor()` 楼层解析（Ground → 0）。
- **HTTP 策略**：`curl_cffi` + `safari17_0` impersonation 通过 RENTCafe Cloudflare（Chrome 指纹在此路径被拦，Safari 可过 GET + POST）。
- **自动预订侦察**：RENTCafe 多步 ASP.NET 表单 POST → `rcformsave.ashx`；受 reCAPTCHA v3+v2 保护。第三方解决服务（capsolver/2captcha）可行但未实现——后续步骤待手动侦察。详见 [OURDOMAIN.md](OURDOMAIN.md) §10。
- **`OurDomainScraper` 已注册到 `SCRAPER_REGISTRY`**，`scrape_tasks_v2()` 展开 `OURDOMAIN_CITIES`。
- **Diemen & South-East 共用一个 RENTCafe property (184283)**，8 个物理单元，每个单元可签多种合同类型。

### 后端 — 预订管线重构

- **`mcore/booking.py`**：`book_with_fallback()` 抽取到独立模块，按面积降序尝试备选房源；`RetryQueue` 持久化竞败候选，跨轮重试。
- **`mcore/interval.py`**：自适应间隔 + 抖动逻辑独立模块。
- **`mcore/prewarm.py`**：`PrewarmCache` 进程级 session 缓存，TTL 刷新。
- **`mcore/push.py`**：APNs 推送调度独立模块，含去重节流。

### iOS — APNs 双语推送

- **`_T` 中英翻译表**：9 条通知模板（新房源标题/正文、状态变更、预订成功、聚合轮次、异常告警），`_t(text, lang)` 查表。
- **按设备语言分组发送**：`_send_to_user()` 取设备 `language` 字段，分组后每语言组构建独立 payload；同一用户中英设备各收各的语言。
- **推送去 emoji**：标题/正文移除所有 emoji，仅保留 `[H2S]`/`[OD]` source tag 前缀。

### iOS — 设备语言上报

- **`PushStore.currentLanguage`**：读取 `Locale.current.language.languageCode`，iOS 16+ 兼容。
- **`DeviceRegisterRequest.language`**：`POST /api/v1/devices/register` 新增 `language` 字段。
- **`device_tokens.language`**：DB 新增列（默认 `'en'`），幂等 migration 兼容老库。

### DB 迁移

- **`device_tokens.language`**：`TEXT NOT NULL DEFAULT 'en'`，幂等 `_add_column_if_missing`。
- **`user_configs.language`**：`TEXT NOT NULL DEFAULT 'en'`，幂等 `_add_column_if_missing`。用户推送语言偏好。
- **`mstorage/_listings.py`**：新增 `count_by_status()` 方法，仪表盘用。

### 通知多语言

- **`UserConfig.language`**：新增字段（`"en"` / `"zh"`），控制 iMessage/Telegram/Email/WhatsApp 推送语言。
- **`notifier.py`**：`_NOTIF_LABELS` 18 条中英翻译表 + `_tl(text, lang)`。`BaseNotifier.__init__` 接收 `language`，`_format_*` 所有标签走 `_tl()` 动态切换。
- **通知文案去中文硬编码**：`WebNotifier` 和所有 `_format_*` 中的硬编码中文（`/月`、`入住`、`新房源上架` 等）改为英文 + `_tl()`，全渠道统一。
- **APNs 推送**：此前已独立支持双语（按设备语言），不受此变更影响。

### 后端 — Xior 集成

- **`scrapers/xior.py`**：`XiorScraper` — WordPress AJAX JSON 抓取（`admin-ajax.php?action=yardi_room_availability`），单元级数据（房号 M1.30.53、精确面积 m²、月租 €、押金、入住日期、直达预订链接），`apartmentId` 去重。429 退避重用 `RATE_LIMIT_BACKOFF`。
- **建筑字典**：荷兰 30 栋楼（15 城市），含 `property_page_id`、`semester_id`、`room_type_ids`，自动发现 + 手动维护。
- **`discover_buildings()`**：城市页 → 建筑页 → 提取 Yardi modal 元数据，可一键刷新全量楼数据。
- **HTTP 策略**：`curl_cffi` + 1.5s 间隔防 CF 限流。Turnstile 不验证服务端（空 token 返回完整数据）。
- **Config**：`KNOWN_XIOR_CITIES`（30 栋），`XiorCityFilter`，`scrape_tasks_v2()` 集成，`.env` 默认 Eindhoven 两栋楼。

### iOS — Alerts 界面重设计

- **`NotificationsView` V3**：与 Dashboard / Browse 视觉语言对齐——`insetGrouped` 白色大圆角容器 + hairline 分割，不再逐行独立卡片。顶部双药丸 toolbar（type filter + Mark all read）。Live pill 绿点 + halo 呼吸动画。删除 emoji 和 32×32 icon tile → 8pt 小色点。

### Bug 修复

- **`stop_monitor()` 残留 PID 文件**：`terminate_process()` 杀进程后未清理 `monitor.pid`，导致 `monitor_pid()` 返回僵尸 PID→仪表盘误显示"监控运行中"。修复：`stop_monitor()` 增加 `PID_FILE.unlink(missing_ok=True)`。
- **仪表盘 toggleMonitor 状态竞争**：`visibilitychange` 事件在切回标签页时强制 `location.reload()`，与 `toggleMonitor` 成功的本地 DOM 更新竞争——本地刚改为"已停止"，切页回来 reload 又把后端状态（进程尚未完全退出）覆盖回"运行中"。修复：`toggleMonitor` 成功分支改为 `location.reload()`，去掉脆弱的 16 行手动 DOM 操作。
- **`scrapers/holland2stay.py` 缺汇总日志**：日志只显示内部 `scraper: [Eindhoven] 共抓取 12 条`，缺少 `scrapers.holland2stay:` 前缀的 source 级汇总，与 OurDomain/Xior 日志格式不一致。修复：`HollandStayScraper.scrape()` 返回前加 `logger.info("[%s] Holland2Stay 共抓取 %d 条房源", ...)`。
- **`scraper.py` / `ourdomain.py` 重复常量**：`_RATE_LIMIT_BACKOFF` 和 `_is_cloudflare_body` 在两处重定义。修复：移至 `scrapers/base.py` 并导入复用。

### Web — Xior 适配

- **`app/jinja_filters.py`**：`source_label` / `source_short` 加 Xior（`"Xior"` / `"XR"`）。
- **`templates/settings.html`**：平台勾选加 `XR · Xior`，新增 Xior 楼盘复选框（30 栋）。
- **`app/routes/settings.py`**：读写 `XIOR_CITIES` env，`allowed_sources` 加 `"xior"`。
- **`app/services/listing_service.py`**：`_xior_display_name()` 处理 Xior 房源名显示。
- **`translations.py`**：`settings_xior_cities`、`settings_xior_hint` 中英标签。

### 文档

- **`docs/XIOR.md`**：完整设计文档（10 节）— 平台概况、技术验证、数据快照、三阶段抓取流程、Listing 映射、平台对比、实现设计、通知模板、风险、工程量。
- **`docs/OURDOMAIN.md`**：完整设计文档（11 节）— 含自动预订可行性分析（§10）和 reCAPTCHA 绕过方案。
- **`docs/SCRAPING_RECON.md`**：Xior 加入速览矩阵（第 1 位）；§5 Xior 独立侦察报告；原有 §4 OurDomain 更新；§7 推荐路径重排（Xior 排第一）。
- **`docs/README.md` / `docs/README_cn.md`**：项目描述 H2S 单平台 → 多平台（H2S + OurDomain + Xior）；数据流图、模块职责表、技术决策表全面更新。
- **`docs/CHANGELOG.md`**：v1.7.0 条目。

### 测试

- **`tests/test_ourdomain_scraper.py`**：27 个测试（FP ID、单元解析、楼层映射、occupancy 推断、抓取流程、403 异常、TLS 指纹重试）。
- **`tests/test_push_dispatcher.py`**：推送测试适配新 `payload_fn` 接口，17/17 通过。
- **`tests/test_scraper_dispatch.py`**：多源 dispatch 部分成功/全量失败场景，2/2 通过。

---

## v1.6.1 (2026-05-21)

### iOS — Settings 重构

- **Settings 按角色精修**：admin 登录后的 Settings 隐藏「Legal」入口（admin 自己维护条款 / 隐私政策，再放一遍是噪音）。
- **User 推送开关**：user 端 Push Notifications 板块去掉 Permission / Device ID 诊断行，改为一个 `Enable Notifications` 开关；OFF 时删除当前设备后端绑定，ON 时申请权限 + 重新注册。系统 Notification 权限为 `denied` 时开关禁用 + 引导去 iOS Settings。admin 端保留诊断信息和 Test Push / Re-register 按钮。
- **`PushStore.setEnabled(_:)`**：与 `logout` 区分——只清后端 device 绑定、不清缓存的 APNs token，用户再次开启时可直接复用。
- **Buy me a coffee 视觉**：section header 用 SF Symbol `cup.and.saucer.fill` 替代 ☕ emoji（HIG 推荐 UI chrome 用 SF Symbol，VoiceOver 朗读语义化"cup and saucer"），文字在前 / 图标在后。

### iOS — Live 心跳点动画与 LoginView 修复

- **LoginView "live" 绿点呼吸动画修复**：原本 ripple 外层圈被 badge `.clipShape(RoundedRectangle(cornerRadius: 12))` 在左上角剪掉一块，动画看起来朝右下"鼓出去"而不是原地呼吸。改用 `.background(_, in: RoundedRectangle(...))`，与 DashboardView.liveBadge 写法对齐——圆角只作用于背景层、content 不参与裁剪。
- **核心点加柔光晕**：7×7 核心点叠 `.shadow(color: iconColor.opacity(0.4), radius: 5)`，1.0 → 1.12 微缩放在小尺寸下也清晰可感。
- **reduceMotion 同步停起**：补 `.onChange(of: shouldAnimate)`，会话中途切换"减弱动态效果"时正确停 / 起循环（与 DashboardView 对齐）。

### iOS — Accessibility（覆盖 6 / 9 ASC Nutrition Label 条目）

**VoiceOver + Voice Control**
- icon-only 按钮全部补 `.accessibilityLabel`：AdminMonitorView 刷新箭头、ListingsView 搜索框 ✕、CalendarView 月份 ◀/▶、MapView Safari 图标、BrowseView mode menu、过滤 chip ✕。
- 关键自定义视图 `.accessibilityElement(children: .ignore)` + 自定义 label：
  - DashboardView `liveBadge` → "Live, updated 8 minutes ago"
  - ListingsView `heartbeatRow` → "127 listings, updated 8m"
  - `ListingRow` → "New listing, Apartment 305, €1,067, Available to Book, Eindhoven, 28 m², from 5 Jan"
  - `NotificationRow` → 整卡 event + title + body + 时间合并朗读 + tappable 行加 hint "Double tap to open listing details"
  - CalendarView 月份标题加 `.isHeader` trait
- 装饰性 glyph（chip 内 xmark、Menu chevron.down、Menu icon 等）`.accessibilityHidden(true)`。
- AdminUsersView 用户启用 Toggle 补 `.accessibilityLabel("Enable \(name)")` + `.accessibilityValue("On"/"Off")`，VO 不再读到无名开关。

**Reduced Motion**
- OnboardingView 接 `@Environment(\.accessibilityReduceMotion)`：Back / Next 按钮 + TabView page 切换的 `.spring` 在开启时降级为瞬时切换。
- 与 DashboardView / LoginView 现有 reduceMotion 处理形成完整覆盖。

**Sufficient Contrast**
- LoginView 4 个自定义 RGB 灰阶（`domainColor` / `footerTextColor` / `descriptionColor` / `subtitleColor`）接 `@Environment(\.colorSchemeContrast)`，Increase Contrast 时全部拉到 WCAG AA 4.5:1 以上（`domainColor` 从 1.5:1 提到 ~4.6:1）。
- NotificationRow 已读卡的 `.tertiary` body / 时间在 Increase Contrast 时上抬到 `.secondary`（避开 ~3.4:1 低对比）。
- NotificationRow mono caps 事件标签（`statusLottery` 橙色在白底仅 ~3.4:1）在 Increase Contrast 时切到 `.primary`，类别色信号由左侧 icon 方块 + cardTint 承担、不丢语义。
- ListingRow `detailColumn` 10pt mono caps 列标题在 Increase Contrast 时 `.tertiary` → `.secondary`。
- ListingRow 状态徽章在 Increase Contrast 时 tint 0.13 → 0.20 + 加 1pt 同色 stroke（同时强化「Differentiate Without Color Alone」——形状轮廓不再纯靠颜色差）。

**Differentiate Without Color Alone**
- 上述 status badge stroke、NotificationRow icon 块 + 文字标签 + cardTint 三冗余、Calendar 数字计数（不只蓝色）、live dot 配 "Live"/"Offline" 文字——所有色彩信号都有等价的形状 / 文字冗余。

**Dynamic Type 下限保护**
- NotificationRow mono caps 事件标签 10.5pt → 11pt（达 iOS HIG 正文最小字号），tracking 0.5 → 0.4 保持紧凑视觉密度。

### Publish 建议

- 已可在 ASC Nutrition Label 勾选：**VoiceOver / Voice Control / Reduced Motion / Sufficient Contrast / Differentiate Without Color Alone / Dark Interface**（6 条）。
- 仍不建议勾选 **Larger Text**（代码内大量 `.font(.system(size: N))` 固定字号未做 Dynamic Type scaling）；**Captions / Audio Descriptions** App 无视频音频内容，自动不适用。

### Web 端 — 侧边栏与主题切换打磨

- **修复 sidebar 顶端紫色横线泄漏**：新增的 `.skip-link`（无障碍"跳到主内容"按钮）原本用 `transform: translateY(-120%)` 隐藏到视口外，但元素实高 ≈ 36px、配上 `top: 8px` 后底边落在 y ≈ 0.8px，导致 accent 色泄漏 1–2px 在 FlatRadar 图标上方显出一条横线。改用 `top: -100px` + `:focus` 时拉回 `top: 8px` + 0.15s `top` 过渡，无障碍跳转功能完整保留。
- **修复切日夜主题时"横线不跟着动"**：`html.theme-transitioning` 规则原本只覆盖 `background-color` / `color` / `box-shadow`，所有带 border 的横线元素（`<hr>` / table 分割线 / card 外框 / `sidebar-label` 下划线 / `.breadcrumb` 等）切换时颜色瞬间跳变，跟旁边卡面慢慢渐变形成不协调。补 `border-color` / `outline-color` / `fill` / `stroke`（inline SVG 一并覆盖）。
- **修复 KPI 大数字"晚一点才变色"**：原 `color .25s` 跟 `background-color .35s` 错位 100ms，导致 `.kpi-num` / `.lc-rent` / 表格数字在 250ms 就跑完、背景还在转，中间帧看起来像数字晚到。所有过渡属性统一 `.3s ease`，JS 端 `setTimeout` 400ms 移除 class 仍留 100ms 缓冲。
- **主题按系统时间自动判断**：未显式 toggle 过的用户首次访问时，根据本地时钟判定 `19:00–06:59` 自动走 dark。优先级 `localStorage` > 系统时间 > `prefers-color-scheme`，已显式 toggle 的用户选择仍然 stick。`base.html` + `login.html` 两个内联 `<head>` 脚本同步更新。
- **静态资源缓存版本**：`design.css?v=6` → `v=9`，强制浏览器拉新样式。

---

## v1.6.0 (2026-05-20)

### 后端 — 抓取完整性与 stale 状态收敛

- **完整扫描信号**：`scraper.scrape_all()` 现在返回每个城市的完整性状态；monitor 每轮记录 `本轮完整扫描: x/y 城市`，便于区分真实无房源与抓取不完整。
- **只对完整城市执行 stale 收敛**：7 天未见房源推测为 `Occupied` 的逻辑只在对应城市本轮完整抓取成功后运行，避免代理/网络故障时误判状态。
- **Lottery 独立 stale 窗口**：`Available in lottery` 使用更短的 2 天未见阈值；`Available to book` / `Unknown` 仍使用 7 天阈值，更贴近 lottery 房源短周期行为。
- **列表展示 last seen**：Web 房源列表新增 `Last seen`，避免把 `First seen` 误当成 stale 判断依据，排查状态收敛更直接。

### Web — 注册、账号与安全

- **登录页引流与注册入口**：登录页增加 App Store 下载链接，并支持 Web 端账号注册；注册前弹出使用条款与隐私政策确认。
- **侧边栏法律入口**：登录后的侧边栏底部新增完整「隐私条款」与「使用条款」入口。
- **Admin 设备管理入口**：admin 侧边栏新增 App 设备管理入口，不再只能从 Settings 深层进入。
- **邮箱验证加固**：验证链接强制依赖 `PUBLIC_BASE_URL`，缺失时 fail-closed；避免 Host header poisoning 生成攻击者域名链接。
- **用户邮件配置收紧**：普通 user 仅可使用 shared 邮件模式并修改收件邮箱；custom SMTP 限定 admin 配置，降低 SSRF / 出站滥用风险。
- **前端 XSS 防护补强**：用户名称、测试通知结果、渠道错误等用户可控内容改为安全渲染，避免 inline handler / `innerHTML` 注入。

### 通知

- **Telegram 品牌化 HTML 消息**：Telegram 发送使用 `parse_mode=HTML`、`disable_web_page_preview=true`，统一 FlatRadar 标题、加粗字段，并转义动态内容。
- **Email HTML 模板统一**：邮箱验证、测试通知与新房源邮件使用 FlatRadar 品牌模板，不再显示旧 H2S 命名。
- **配置提示完善**：Web 用户表单补充 Telegram BotFather / `getUpdates` 配置说明；iMessage 标明仅本地 macOS 部署可用。

### 统计与可观测性

- **统计范围联动修复**：Stats 页 7 / 30 / 90 days 切换现在同时影响 KPI 卡片、趋势图和分布图；公开 chart API 也按 `days` 过滤。
- **更清晰的网络失败链路**：第 1 页网络失败会向上抛出并参与连续失败计数/cooldown，不再伪装成“成功抓取 0 条”。

### iOS / App Store

- **版本更新**：项目版本推进到 `v1.6.0`；iOS App Store build `161`，面向 App Store Connect 完成截图、隐私、年龄分级、加密与内购资料准备。
- **StoreKit 打赏**：新增 consumable “Buy me a coffee” 内购档位，作为自愿支持入口。
- **移动端交互打磨**：Browse/List/Map/Calendar 在 iPhone / iPad 横竖屏下继续优化布局、搜索入口、地图按钮位置与深色模式表现。

### 测试

- 补充完整扫描、stale 收敛、lottery stale 窗口、统计范围联动、Telegram HTML 格式、前端安全渲染等回归测试。

---

## v1.5.0 (2026-05-16)

### 后端 — 账号注册与存储一致性

- **用户配置完全迁入 SQLite `user_configs`**：`users.json` 不再作为运行时数据源；首次启动按 `users_storage_migrated_v1` meta flag 一次性导入，并永久保留 `.bak` 备份。
- **移除 SQLite `app_users` 镜像表**：App 登录字段并入 `user_configs`，避免 `users.json`/`app_users` 双源不一致。
- **`POST /api/v1/auth/register`**：用户自助注册端点，bcrypt 密码哈希，注册即登录自动签发 token；同 IP 每小时限 3 次 + 复用登录爆破防护；并发注册冲突检测，失败自动回滚。
- **`DELETE /api/v1/me`**：用户注销账号端点，撤销所有 token + 删除 SQLite 用户配置。
- **`PUT /api/v1/me/filter`**：user 自助修改过滤条件，白名单校验 + 边界值检查。
- **`GET /api/v1/filter/options`**：返回所有过滤维度候选值（cities/types/contract/energy...），bearer_optional。
- **Listings 多维过滤**：`GET /api/v1/listings` 新增 `cities`、`types`、`contract`、`energy` 参数，Python 端过滤。
- **`update_users()` SQLite 事务化**：统一 read-modify-write 入口，使用 `BEGIN IMMEDIATE` 避免并发写丢失。
- **安全增强**：TTL 上限 365→90 天；用户名长度上限 64 字符；`err_conflict`（409）处理重名；`check_register_rate` 注册专用限流。

### 后端 — 发布前健康检查

- **`python -m tools.doctor`**：发布/部署前一键检查，支持 `--no-network` / `--smtp-login`，敏感信息脱敏。

### 后端 — 测试

- 并发注册测试、SQLite 用户迁移测试、网络失败传播测试、doctor smoke test。

### iOS — 登录页 V5 设计

- **Hero 山脉动画**：蓝色渐变背景 + 双层山脉剪影（`MountainPath` Shape）+ 呼吸 Logo（scaleEffect 循环动画）。
- **展开式角色卡片**：Tenant / Guest / Staff 三张卡片，点击展开内联登录表单，.spring 动画 + rotationEffect chevron。
- **注册流程**：Tenant 卡片底部 "Register" → 注册 sheet → username + password → POST /auth/register → 自动登录。
- **自适应深色模式**：20+ 颜色属性按 `colorScheme` 切换（hero 深海军蓝 / 浅蓝；卡片/文字/边框全适配）。
- **实时统计 badge**：从 `/stats/public/summary` 获取 live count / time ago / new today。
- **法律文档**：首次启动强制 Terms 弹窗（`.interactiveDismissDisabled`）；Settings + Login 内嵌完整使用条款和隐私政策。
- **版本号动态读取**：`Bundle.main.infoDictionary["CFBundleShortVersionString"]`。

### iOS — Dashboard V1 重设计

- **问候语 + 用户胶囊**：时段感知（Good morning/afternoon/evening）+ 角色自适应（user=蓝色/Menu，admin=红色/Menu，guest=灰色/Menu）。
- **Live badge**：绿点 + "Live · 199 listings · updated 2m ago" 统合胶囊；网络异常时变橙色 "Offline"。
- **统合统计卡片**：单张卡片含 TOTAL LISTINGS 大数字 + Sparkline 折线图（`Sparkline` Shape 从 daily_new 数据绘制）+ ↑N this week + 3 个 mini stat（New 24h / New 7d / Changes）。
- **Your matches**：user 专属区域，从 `/listings` 获取 3 条预览 mini 卡片（价格+城市），点击跳转详情。
- **Explore 2×2 网格**：By status（分段条绿/橙/灰 + 具体数字）/ By price（9 根柱状图 + 范围标签）/ By type（3 行横向进度条）/ By energy（A-F 竖条从绿到红）。
- **点击展开 ChartDetailView**：4 个 mini 卡片均可点击打开完整图表详情 sheet。

### iOS — Listings 增强

- **多维筛选 sheet**：城市多选、状态单选、户型多选、合同单选、能源单选（FilterOptions API 动态加载候选项）。
- **后端多维过滤参数**：`cities`/`types`/`contract`/`energy` 查询参数，Python 端过滤。
- **NEW 徽章颜色修正**：`Color(red: 52/255, green: 199/255, blue: 89/255)` #34C759 success 绿。
- **Listing 详情页免责**："Always verify listing details on the official Holland2Stay website before making decisions."

### iOS — 通知 V2 卡片式设计

- **卡片式 inbox**：TODAY / YESTERDAY / EARLIER 三区分组，SF Mono 标题，section header 显示条数。
- **右上角 "Read all"**：绿色勾药丸按钮（`.buttonBorderShape(.capsule)`）。
- **灰底白卡**：`.systemGroupedBackground` + `.plain` list row。
- **行列紧凑型重设计**：内联 `NEW · 38m` 徽章 + monospacedDigit 价格 + ●Book/●Lottery/●Reserved 状态胶囊。

### iOS — 设计系统

- **主色 #0A84FF**（替换原 `#1683FF`）：LoginView brandBlue 统一为 `Color(red: 10/255, green: 132/255, blue: 255/255)`。
- **语义色**：#34C759 success / #FF9500 warning / #FF3B30 error。
- **Tabular-nums**：Dashboard KPI 大数字、mini stat、matchedTotal、listing price 全部加 `.monospacedDigit()`。
- **Energy 条多色方案**：深绿 A+++/A++ → 成功绿 A+ → 浅绿 A → 黄 B → 橙 C → 红 D 及以下。
- **移除装饰色**：.purple/.pink/.indigo/.teal/.mint 全部替换为主色或语义色。

### iOS — 更多功能

- **Settings 重排**：Push Filter → Appearance → Push Notifications → Account → Admin → Legal → Coffee → About。
- **Server 入口隐藏** + `buildBaseURL`/`endEditing` 死代码清理。
- **账户管理**：Log Out + Delete Account（二次确认弹窗，DELETE /me）。
- **通知过滤器编辑器**：10 维度多选表单，FilterOptions 动态加载候选项。
- **深色模式优化**：Dashboard 灰底白卡 + Calendar 灰底。
- **用户胶囊增强**：admin/guest/user 全部显示；guest 加 Menu 可登出。
- **错误提示升级**：登录失败从统一"Session Expired"改为分类显示（"Login Failed" / "Access Denied" / "Too Many Requests" / "Connection Failed"），alert 消息显示后端实际错误原因。
- **Live indicator 精简**：删掉绿点圆圈，Live 绿色文字右上角。

### iOS — App Store 准备

- **PrivacyInfo.xcprivacy**：Required Reasons API（UserDefaults CA92.1）+ 数据收集声明（User ID / Email / Device ID / Search Hints / Crash Data / Diagnostic Data）。
- **App 图标**：新增 AppIcon-Dark.png / AppIcon-Tinted.png / AppIcon.png。
- **StoreKit 2 捐赠**："Buy me a coffee ☕" IAP，3 档 consumable（Espresso €0.99 / Latte €2.99 / Flat White €5.99），`CoffeeStore` 管理产品加载和购买。
- **Release 日志安全**：41 处 `print()` 全部包 `#if DEBUG`，Release 不泄漏 token/URL。

### iOS — 代码质量

- **消除死代码**：`hasToken()`、`buildBaseURL(from:)`、`endEditing()`。
- **消除重复**：`relativeTime` 提取到 `ServerTime.relativeTime(_:)`。
- **Force unwrap 安全**：`defaultServerHost` 常量下的 URL force unwrap 无风险。

### iOS — 多语言

- **174 条本地化**：en / zh-Hans 全覆盖（登录、Dashboard、Listings、Settings、错误、法律、管理面板）。

### 文档

- **README.md / README_cn.md**：Project Status 表新增 iOS 21 行条目 + 独立 iOS App 章节（架构/功能/端点表）。
- **iOS_README.md**：完整重写（功能矩阵/文件结构/端点/安全/版本历史 v1.5.0）。
- **CHANGELOG.md**：v1.4.1–v1.4.5 合并为 v1.5.0，涵盖所有改动。

---

## v1.4.1 (2026-05-15)

### iOS — 错误展示打磨

- **APIError 分类化 UI**：`errorDescription`（短标题）/ `failureReason`（详情）/ `recoverySuggestion`（操作建议）三层结构；每类错误配独立 SF Symbol 图标（401→lock.shield / 403→hand.raised.slash / 网络→wifi.slash 等）
- **全局 401/403 自动登出**：`APIClient` 检测到 auth 错误时 post `authFailedNotification`，`AuthStore` 监听并自动 `logout()`，任何页面任何请求触发都会清除会话
- **所有视图 Try Again 按钮**：`ContentUnavailableView` 错误状态加"Try Again"操作按钮，401 显示"请重新登录"、网络错误显示"检查网络"等分类提示
- **刷新失败弹窗**：数据已有但刷新失败时弹出 alert（title 按错误类型区分），不再静默吞错
- `LoginView` alert 标题随错误类型变化（网络错误→"Connection Failed"，401→"Session Expired"），不再固定"Login Failed"
- `DashboardView` 重构为 `summaryContent` / `errorView` / `roleBadge` 三个子组件，避免 type-checker 超时

### iOS — 多语言 en + zh-Hans

- 新建 `Localizable.xcstrings`（154 条），覆盖所有视图、Store、APIError、权限状态、测试推送消息
- SwiftUI `Text("...")` 自动查询 catalog，`String(localized:)` 用于非 View 代码路径（`APIError`、`LoginMode.label`、`BrowseMode.label`、`MapView.shortStatus`、`SettingsView.pushPermissionLabel`、test push 消息）
- 跟随系统语言自动切换，无需手动选择

### iOS — 深色模式 + Settings 切换

- Settings 新增 "Appearance" section，Picker 三选一：System / Light / Dark
- `@AppStorage("color_scheme")` 持久化偏好，`FlatRadarApp` 读取并 `.preferredColorScheme()` 应用到根视图
- 修复深色下两处对比度：`ChartDetailView` 交替行 `Color.gray.opacity(0.08)` → `Color.primary.opacity(0.04)`；`CalendarView` 非选中日背景 `0.08` → `0.12`
- App 全程使用 SwiftUI 语义色，无硬编码 hex，无 `preferredColorScheme` 覆盖

### iOS — iPad 适配 + 键盘快捷键

- **响应式 TabView**：iPhone compact 保持 4 tab（Dashboard / Browse / Notifications / Settings）；iPad regular 展开 6 tab（Dashboard / Listings / Map / Calendar / Notifications / Settings），无二级嵌套
- **液态玻璃底栏**：`.toolbarBackground(.ultraThinMaterial)` 毛玻璃效果
- **键盘快捷键**：iPad ⌘1-⌘6 切 tab，iPhone ⌘1-⌘4
- **响应式网格**：Dashboard `gridColumns` compact=2、regular=3
- `MainTabView` 拆为 `compactTabView` + `wideTabView` 两个子布局，通过 `@Environment(\.horizontalSizeClass)` 自动切换
- `AppTab` 新增 `.listings` / `.map` / `.calendar` 三个 case（iPad only）
- `openListing(id:)` deep link 统一使用 `.listings`，iPhone 侧 `onChange` 自动重定向到 `.browse` + `.list` mode

### iOS — APNs 推送优化

- **一次性本地客户端**：`notifier_channels/apns.py` 从复用全局 `httpx.AsyncClient` 改为每次推送创建独立 client 并 `async with` 关闭，避免事件循环竞争导致的连接泄漏和不稳定
- **Settings 测试推送**：`PushStore` + `SettingsView` 新增 "Send Test Push" 按钮，调用 `POST /api/v1/devices/test`，结果弹窗显示成功/失败设备数及失败原因，验证 APNs 端到端链路
- **设备管理增强**：Settings 显示当前设备注册 ID、权限状态、Registration failed 错误详情；支持 Re-register Device 手动重注

### iOS — 服务端 / 构建

- 新增 `POST /api/v1/devices/test` 测试推送端点（`app/routes/api_v1/devices.py`），绕过 `push.dispatch` 的 user_id/throttle 限制，直接向当前 session 所有活跃设备推送
- `FUTURE_PLAN.md` 同步更新：错误展示/多语言/深色模式/iPad 适配/APNs 标记完成

---

## v1.4.0 (2026-05-15)

### iOS 后端 — 只读数据端点（Phase 2）

新增 9 个 `/api/v1/*` 端点，user 角色按 `listing_filter` 数据隔离，admin 全量：

- `GET /listings` / `GET /listings/<id>` — 分页列表 + 单条详情（`app/routes/api_v1/listings.py`）
- `GET /notifications` / `POST /notifications/read` / `GET /notifications/stream` — 通知分页 + 标记已读 + SSE 推送（`app/routes/api_v1/notifications.py`）
- `GET /map` / `GET /calendar` — 地图坐标 + 日历数据（`app/routes/api_v1/map.py` / `calendar.py`）
- `GET /me/summary` / `GET /me/filter` — 当前用户统计 + 过滤条件（`app/routes/api_v1/me.py`）

共享模块：`_helpers.py`（`row_to_listing` / `apply_user_filter` / `serialize_listing`）；`mstorage/_notifications.py`（`NotificationOps`，支持 `user_id` 过滤）。

SSE 鉴权支持 `?token=` query 参数（兼容浏览器 `EventSource` 不支持自定义 header）。

### iOS 后端 — APNs 子系统（Phase 3）

- **推送调度** `mcore/push.py`：`dispatch` / `dispatch_status_change` / `dispatch_aggregate` / `dispatch_error`，节流去重（同 user+listing+kind 5min / 每分钟 ≤10 条 / ≥3 聚合为 round），`APNS_ENABLED!=true` 时全 no-op
- **APNs HTTP/2 客户端** `notifier_channels/apns.py`：JWT ES256 `.p8` 签名 + httpx 异步发送，`ApnsConfig.from_env()` 惰性启用，403 `InvalidProviderToken` 自动重签
- **设备持久化** `mstorage/_devices.py`：`DeviceOps` — `register_device` / `get_active_devices_for_user`（JOIN `app_tokens` 过滤 revoked） / `disable_device`（APNs 410/400 软停） / `delete_device`
- **设备端点** `app/routes/api_v1/devices.py`：`POST /register` / `GET /` / `DELETE /<id>`，设备隔离按 `app_token_id`

### iOS 客户端 — Phase 2 适配（Phase 4）

14 个文件新增/修改，Listings / Notifications 从 "Coming Soon" 占位切换到真实数据：

**模型层** — `Listing` 新增 `priceValue`/`featureMap`/`firstSeen`/`lastSeen`；新增 `ListingsResponse`/`NotificationsResponse`/`MeSummary`/`Device*` 分页和设备模型；`NotificationItem` 新增 `markedRead()`

**网络层** — `APIClient` 新增 6 个 API 方法（listings/notifications/me/devices）；新增 `buildURL()` 修复 `appendingPathComponent` 把 `?` 编码成 `%3F` 的 bug

**Store 层** — `ListingsStore`（分页/搜索/loadMore/refresh）、`NotificationsStore`（分页/标记已读/全部已读，optimistic update）、`DashboardStore`（`fetchMeSummary`）、`PushStore`（设备注册/解绑）

**View 层**：
- `ListingsView` — searchable + 无限滚动 + 状态胶囊标签（绿=可订/橙=抽签）+ loading/empty/error
- `ListingDetailView` — **新建**，全字段 + `feature_map` 网格 + H2S 链接
- `NotificationsView` — 左滑标记已读 + 全部已读 + 类型图标（SF Symbol + 颜色）+ 无限滚动
- `NotificationRow` — 新增 type→图标/颜色映射（new_listing=绿 house / status_change=橙 arrow.swap / booking=蓝 cart）
- `DashboardView` — user 角色显示匹配/可订卡片；退出加确认框并锚定按钮
- `SettingsView` — 已注册设备列表；退出确认框锚定按钮
- `MainTabView` — Notifications tab 未读 badge

### 服务端增强

- **H2S 凭据验证**：user 登录优先 `app_password_hash`，未设置或失败时回退到 H2S GraphQL `generateCustomerToken` 验证，用户可直接用 H2S 邮箱+密码登录（`app/routes/api_v1/auth.py`）
- **bcrypt 容错**：`_dummy_bcrypt_verify` / `_bcrypt_hash` / `verify_app_password` 三处 `import bcrypt` 失败时优雅降级，不再 500（`auth.py` / `users.py`）
- **Web 表单容错**：`user_form.py` 捕获 bcrypt 未安装异常 → `ValueError`；`users.py` 路由捕获并 flash 提示

### Bug 修复（6 个）

- `URL.appendingPathComponent` 把 `?` 编码成 `%3F` → `buildURL()` 拆分 path + query
- refresh control 非空闲替换 → `isLoading` 条件加 `&& items.isEmpty`
- logout API double-wrapping decode → 返回类型改为 `RevokePayload`
- 不存在的用户名登录 500 → `_dummy_bcrypt_verify` 处理 ImportError
- Web 界面设置 App 密码崩溃 → `_bcrypt_hash` + 路由层 ValueError 捕获
- iPad/Mac 退出确认框位置错误 → `confirmationDialog` 锚定按钮

### 构建 / 部署

- **Dockerfile**：新增 `COPY notifier_channels/`（APNs 模块之前漏拷）
- **`.dockerignore`**：新增 `ios/` / `.claude/` / `tests/` / `*.p8` / `*.p12`
- **`.gitignore`**：新增 `**/xcuserdata/` / `**/.build/` / `*.p8` / `*.p12` / `DerivedData/`
- **`requirements.txt`**：已包含 APNs 依赖（`pyjwt[crypto]` / `httpx` / `h2` / `cryptography`）

### 文档

- 新增 `docs/iOS_README.md` — iOS 客户端 & API 后端架构文档（中文）
- 更新 `docs/FUTURE_PLAN.md` — Phase 2/3/4 标记完成，补全 bug 修复记录和待办清单

---

## v1.3.2 (2026-05-15)

### Booker 403 屏蔽精准处理

v1.2.2 为 scraper 引入了 `BlockedError`，但 booker 的 403 一直被 `except Exception` 当作 `unknown_error` 吞噬，导致三个连锁问题：
1. 日志看不出是 Cloudflare 拦截，用户不知道该换代理
2. `book_with_fallback` 继续尝试备选房源（每个都 403，浪费时间 + 加重风控）
3. `run_once` 给每个候选发一条 booking_failed 通知（刷屏）

v1.3.2 将 403 屏蔽提升为 booker 的一等异常，与 scraper 同级处理：

- **`booker.py`**：新增 `BookingBlockedError` 异常类 + `_check_blocked()` 检测函数（与 scraper 共享同一 Cloudflare 特征签名）。`_gql()` 和 `add_to_cart()` 两处 HTTP 调用后立即检测 403 并抛专用异常，不落入 `except Exception` 通用路径。
- **`try_book()`**：捕获 `BookingBlockedError` → `BookingResult(phase="blocked")`，与 `race_lost` / `unknown_error` 路径独立
- **`mcore/booking.py`**：`book_with_fallback()` 遇 `phase="blocked"` 立即停止重试（IP/指纹级问题，换房无意义）
- **`monitor.py`**：`run_once()` 聚合所有 blocked 候选，全轮发一条节流通知（30 min，与 scraper 共享 `_should_notify_block`）；失效 prewarm 缓存；保留 retry_queue 状态（非房源级问题，不丢弃重试队列）

### 代码质量

- **`config.py`**：`_energy_rank()` → `energy_rank()`，`_ENERGY_LABELS` → `ENERGY_LABELS`（公开 API，去掉下划线前缀）。所有调用方（`app/routes/dashboard.py`、`users.py`、`user_form.py`、`tests/`）同步更新。
- **`mstorage/_base.py`**：新增 `conn` property 替代 `_conn` 直接访问，6 处测试 + `system.py` 统一使用公开访问器；新增 `_migrated_paths` 进程级缓存，同一 db_path 只跑一次 schema migration（原每个请求 ~3ms）

### Bug 修复

- **Dockerfile / PyInstaller 遗漏模块 (v1.3.0 回归)**：v1.3.0 新增 `mcore/` 和 `mstorage/` 两个包，但 `Dockerfile` 缺少对应 `COPY` 指令导致容器内 import 失败；`h2s_monitor.spec` 缺少 `collect_submodules` 导致打包后同样的模块缺失。本次补全两处构建配置。

### 测试

- 新增 `test_booker_blocked.py`：验证 403 → `BookingBlockedError` → `phase="blocked"` 完整链路（`_check_blocked`、`try_book`、`book_with_fallback`、`run_once` 聚合通知 + prewarm 失效）
---

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
