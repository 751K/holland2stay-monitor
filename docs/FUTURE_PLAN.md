# Future Plan / 未来规划

本文档记录后续版本可以继续推进的方向。

---

## v2.0 规划（2026-09-07 重定，基线 `12ab06c`）

> 本节 09-07 早些时候写过一版，依据的审查做在 **v1.33–v1.35 那批修复之前**，
> 于是把二十余条已经修掉的缺陷当成了待办。这一版对着 HEAD 逐条复核后重写。
> 复核结论：原「方向二（交付语义）」与「方向三（安全边界）」的绝大部分已经
> 交付，v2.0 的实际内容收敛成 **CI 门禁 + 结构重构 + 一把零头**。

### 复核：原计划里已经做完的部分

09-01 至 09-02 的一批提交（`c0cd30f` `4926ffc` `2195920` `3f2868d` `4705207`
`5dc7954` `5bfa70b` `5a27e8f`）覆盖了原方向二、原方向三，以及原方向五的一半：

| 原条目 | 现状 |
|---|---|
| 重放窗口字符串比较，退化成「当天零点起」 | ✅ 改 `julianday`（`mstorage/_listings.py:524/543/563`） |
| FCM `device_dead` 比散文 `message` | ✅ 读 `details[].errorCode` / `error.status`（`notifier_channels/fcm.py:393-401`） |
| OAuth 失败丢整批 Android 推送 | ✅ `token()` 进 try + `gather(return_exceptions=True)`（`fcm.py:337/444`） |
| FCM 换 token 走抓取代理 | ✅ `direct_httpx_kwargs()`（`fcm.py:255`），守卫测试同时补上两个洞 |
| 热重载先关旧 notifier 再建新的 | ✅ 先构造成功再 close（`monitor.py:3425`） |
| `_submit_bookings` 引用不存在的 `storage` | ✅ 已带参数（`monitor.py:1622`） |
| 占房成功后订单号不交给用户 | ✅ `cart_id` / `order_number` 进结果（`booker.py:197-200`） |
| `cancel_pending_orders` 会取消用户手动订的房 | ✅ 只取消 `remember_our_reservation` 记过的 SKU，无记录时一笔不取消 |
| `contract_start_date` 漏哨兵过滤 | ✅ 复用 `is_sentinel_available_from`（`scrapers/holland2stay.py:567`） |
| `RentCafeSession` 构造在 try 外 | ✅ 已移进 try（`bookers/rentcafe.py:843`） |
| 反代之后 `remote_addr` 恒为代理 IP | ✅ `ProxyFix`（`web.py:147-149`） |
| `/system` 的 `os.environ.clear()` 权限提升窗口 | ✅ 改用 `dotenv_values()`（`app/routes/system.py:89`） |
| `/api/v1/devices/test` 写 `user_id=""` 的全站通知 | ✅ 已改为当前用户 |
| `/login` 对不存在用户名静默自动注册 | ✅ 已移除，注册统一走 `register_user`（`app/routes/sessions.py:145`） |
| `next` 未过滤 | ✅ `app/safety.py:safe_next_url`，四处调用点已接 |
| crypto 密钥静默自举 | ✅ 库里有密文时不再自动生成（fail-safe） |
| 每个 source 的 dispatch 无墙钟超时 | ✅ `_SOURCE_DISPATCH_TIMEOUT_SEC`（`monitor.py:161`） |
| `update_checker` 的 `git pull` 无 timeout | ✅ `timeout=15`（`update_checker.py:26`） |
| `run_once` 收尾步骤无隔离 | 🟡 `mark_stale` / 两处 prune 已各自 try；`count_all` / `send_heartbeat` / `prune_notifications` 仍裸露 |
| 影子 source 过滤排在重放之前 | 🟡 顺序未变（`monitor.py:2884` vs `2901`），但 `_drop_shadow_sources` 现在把事件标 `notified`，漏洞已从另一侧堵上，降为低危 |

### 为什么 v2.0 的定义是「CI 加结构」

上面这张表说明缺陷本身是能修的，问题在于**没有东西守着它们不复发**：CI 至今
只打桌面包、不跑测试，而 843 行的 `run_once` 与四个平行的 source 集合仍在按
同一种方式放大同一类错误。复核里新发现的一条正好是活样本——

`notifier.py:235` 的注释写着「支持幂等键的渠道要自己带上（见 ResendNotifier 的
Idempotency-Key）」，而 `ResendNotifier` 的 headers 里只有 `Authorization`。
这是**修完那批「注释里说有的保护、代码里没接线」之后新长出来的同一种脱节**，
而且没有任何测试或 lint 能发现它。

所以 v2.0 不加平台、不加客户端功能。**v2.0 的定义是：让「测试全绿」这件事真的
能背书生产行为，并让这个状态被 CI 守住。**

### 方向一：CI 成为门禁 —— **其它方向的前提**

现状：`.github/workflows/` 只有 `build.yml`（macOS / Windows 打包），不跑
pytest、不跑 lint。本地 `pytest` 87 秒，215 个测试文件全绿。

`ruff check --select E9,F,B,PLE` 的实测（2026-09-07）：

```
含 tests   231 项   （其中 tests 里 64 处 F811 redefined-while-unused）
排除 tests  65 项   （F401 32、F541 11、B905 5、B007 4、F841 4、F821 3、其余 6）
```

F821 四处，三处在生产代码：`app/forms/user_form.py:121`（`ApplicantProfile`）、
`users.py:188`（同）、`config.py:1920`（`ScrapeTask`）——前两处是 `TYPE_CHECKING`
下的类型名漏了引号，第三处同理。第四处在 `tests/test_inbound_webhook.py:224`。

要做的：

1. `pytest` + `ruff check --select E9,F,B,PLE` 进 CI，push 与 PR 都跑，失败即红。
   先清零：`--fix` 能处理 141 项，剩下的手改。**tests 目录一并纳入**——那 64 处
   F811 多半是重复定义的 fixture 或用例，正是「测试看着全绿其实少跑了」的形状。
2. 补一类「调用链固定」测试：对每个已实现但可能没接线的判定，断言生产路径真的
   调到它。样板是 `tests/test_proxy_failover.py::TestClassifierIsActuallyCalled`。
   v2.0 至少覆盖：`is_proxy_error` 在浏览器型 source 与 H2S 分支上被调用、
   `_mark_h2s_login_blocked` 在三处 prewarm 失败点上被调用、`device_dead` 用真实
   FCM 响应体命中、`cleanup_expired_tokens` 有调用方、**Resend 请求带幂等键**。
3. 测试构造数据一律走生产写入路径（`_now_iso()`、`add_web_notification()`），
   不再手写 SQL 塞时间戳。这是一条 review 规则，写进 `tests/README` 或 conftest 注释。

完成标准：CI 在 `master` 上连续红过至少一次并被修好（证明门禁真的在拦）；
`ruff`（含 tests）零报告。

### 方向二：结构重构 —— **让同一类 bug 没有地方再长**

09-02 那批修复几乎全是「一处一处地改对」，结构一行没动。不动结构，方向一的门禁
只能拦住重复犯的错，拦不住换个地方长出来的同一种错。五项，按能挡住多少复核发现
排序。

**2.1 `run_once` 拆成显式管线**（方向四的前置）

`run_once` 现在 **844 行**（`monitor.py:2159-3002`），monitor.py 68 个顶层函数、
10 个模块级可变全局。已经修掉的三条高危——`_submit_bookings` 抽函数时漏掉
`storage`、影子过滤排在重放之前、收尾步骤没有隔离——全都是「顺序」和「作用域」
错误，在一条显式的 `scrape → diff → replay → filter → book → notify → persist`
阶段链里是一眼可见的属性，而在 844 行里要靠人读出来。**它们已经各修一次，但下
一条同类错误仍会长在同一片土壤上**。每个阶段一个函数，输入输出是 dataclass；
超时与异常隔离在阶段边界统一套，而不是每处手写。

**2.2 source 差异收进一个 `SourceSpec`**（方向四的前置）

平台差异现在靠四个集合加分支表达：`KNOWN_SOURCES`、`_BROWSER_SOURCES`
（`monitor.py:1230`）、`_PROXYLESS_CAPABLE_SOURCES`（`:302`）、
`_AUTO_BOOK_SOURCES`（`:1508`），外加 run_once 里 H2S 一整段与 `_dispatch_isolated`
平行的代码。H2S 不参与代理冷却与 pacing 就是这个结构的直接产物（已于 `4705207`
单点修好，结构没变）。改法是每个 source 声明自己的 executor 类型、可否无代理、
屏蔽处理钩子、canary 钩子、可否自动预订，调度层只剩一条路径。09-01 到 09-02 三
周内接了 Magis、Student Experience、Plaza 三个平台，每次都要往四个集合里各加一
次——第八个平台是这条的直接受益者。

**2.3 配置不再以 `os.environ` 作总线**（方向三的形态）

现在的机制是 `settings_store` 每轮往 `os.environ` 注水，各模块随时
`os.environ.get`。import 时求值的常量热重载不到、三套布尔解析
（`config.py:134`、`config.py:2397`、`notifier.py:833`、`app/auth.py:150` 四处
`lower() != "false"` 都不 `strip()`，`APNS_ENABLED=" true"` 静默关推送）、
`target_config` 的严格解析器至今没有调用方（`grep target_config config.py` 零命中），
全是这个总线的后果。目标形态：进程持有一个不可变的 `Settings` 快照，热重载就是
换掉这个对象，启动之后没有任何代码再读 `os.environ`。

**2.4 Notifier 分发走事件，不走拍平的文本**

`MultiNotifier._send`（`notifier.py:220`）把所有高层方法压成一段字符串再分发，
于是 `EmailNotifier.send_heartbeat` 的豁免是死的、`WebNotifier` 把聚合房源显示成
error 类型。改成 `send_event(event)`，格式化在各渠道内做，渠道能力用标志声明。
顺带把 `notifier.py:1044` 与 `app/routes/users.py:541` 两份渠道构造合成一个工厂。

**2.5 模块级可变状态收成一个 `RuntimeState`**（随各方向顺手做）

`mcore/push.py:179` 的 `_dedup`、app 层六张限流字典、`_DETAIL_CACHE`、
`mcore/health.py:33-88` 的阈值、monitor 的一批 `PersistedBackoff` 都是 import 时
建好的单例。无界增长、热重载失效、测试要靠 `reset()` 清场，是同一个原因。不需要
依赖注入框架，一个显式传递的对象就够。

**明确不动的：** 单机 SQLite + web / monitor 双进程这个模型没有问题。不拆库、
不上队列、不把 scraper 改成全异步。那些是运维成本，不是正确性问题，而这个项目是
一个人在运营。

完成标准：`run_once` 不超过 150 行且不含嵌套函数；`grep -n 'holland2stay' monitor.py`
只剩 `SourceSpec` 注册处；生产代码里 `os.environ.get` 只出现在 `load_config()` 与
`settings_store`；`MultiNotifier` 没有 `_send(text)`；`tests/` 里不再有
`monitor._h2s_login_blocked_until = 0.0` 这类直接重置模块状态的写法。

### 方向三：配置系统收口 —— **一份实现，热重载覆盖全部**

现状是三个平行世界：`config.py`（2413 行，混着代理池、TLS 指纹池、申请人档案、
过滤能力表）、`target_config.py`（344 行严格解析器，**没有调用方**）、
`settings_store.py`（241 行，按 `env_registry.RUNTIME_KEYS` 热重载）。

具体的洞：`target_config.TARGET_KEYS` 有六个平台键，`_PARSERS` 只覆盖其中三个
——`MAGIS_CITIES` / `STUDENTEXPERIENCE_CITIES` / `PLAZA_CITIES` 落在
`STRUCTURED_KEYS` 之外，面板上写进去的值**完全不过校验**。这三个键正是最近三周
新加的，也就是说：加平台时忘了同步这张表，没有任何东西会提醒。

要做的：

1. `load_config()` 改调 `target_config.parse_*`；fatal 的拒绝启动，非 fatal 的 WARNING。
2. `_PARSERS` 覆盖 `TARGET_KEYS` 的每一个键，用参数化测试守住（加平台时自动红）。
3. `_bool` / `_env_int` 各一份实现并 `strip()`，全仓库改调；`tools/doctor.py:63`
   那份是对的，提上来。
4. 热重载能刷到的常量改成函数式读取（与 `enabled_sources()` 一致）；做不到的
   在面板上标「需重启」。
5. `config.py` 拆分：代理池 / 指纹池 → `net.py` 或 `mcore/proxy.py`；
   `ApplicantProfile` → `bookers/`；过滤能力表 → `models.py`。目标是 `config.py`
   只剩 `Config` 与 `load_config()`。

1、2、3 不依赖新结构，先做；4、5 就是 2.3 本身。

完成标准：`grep -rn 'lower() != "false"'` 零命中；`STRUCTURED_KEYS == set(TARGET_KEYS) | {…}`
有测试守；`tests/test_config.py` 从 happy-path 用例变成坏配置矩阵。

### 方向四：残余的正确性缺陷

复核后仍然成立的只剩下面这些。除超时两条外都不依赖重构，可以随时做。

| 问题 | 位置 | 修法 |
|---|---|---|
| Resend 重试无幂等键，而注释已声称有 | `notifier.py:235` / `ResendNotifier._send` | 带 `Idempotency-Key`（按 listing id + 事件类型派生）；否则把那句注释改掉 |
| `mark_status_change_notified` 按 `listing_id` 而非主键 | `mstorage/_listings.py:590` | 按 `status_changes.id` 标记；`_batch` 同改 |
| 预订 future `await` 无超时 | `monitor.py:1999` | `asyncio.wait_for`；`booking_deadline` 只管「要不要试下一套」，管不住这里 |
| geocode 同步跑在事件循环里 | `monitor.py:3367` | 进 executor。Photon 不可达时最坏 150 秒 |
| `count_all` / `send_heartbeat` / `prune_notifications` 无隔离 | `monitor.py:3332-3341` | 各自 try，与相邻两处 prune 对齐 |
| `MAX_CONTENT_LENGTH` 未设 | `web.py` | 加上；Caddy 侧同时配 `request_body max_size` |
| bcrypt 72 字节上限未拦 | `users.py:767 _bcrypt_hash` | 超长直接 4xx，而不是让 `hashpw` 抛 `ValueError` 变 500 |
| SSE 的 `?token=` 是长期 token | `app/routes/api_v1/notifications.py:111` | 换成短期 ticket（长期 token 会落进代理日志与浏览器历史） |
| `page.evaluate` / `page.content()` 无墙钟超时 | `browser_fetcher.py:1273/1102` | 渲染器卡死时 `_SOURCE_DISPATCH_TIMEOUT_SEC` 能兜住整轮，但单页仍会白等到上限 |
| 公告群发没有逐用户投递记录 | `app/services/announcement_service.py` | 2026-08-28 那次 FCM 全挂，事后无法精确补发。落一张投递结果表 |
| 城市选择器提供未监控的城市 | `app/routes/users.py:277` | `known_city_names()` 收敛到实际启用的城市 |

### 附带清理（不单独立项，随上面各方向顺手做）

- 死代码：`bookers/rentcafe.py:307 submit_step`（实现的正是文档警告过的坑）与
  `:634 _needs_recaptcha`、`scrapers/xior.py:476` 末尾不可达的 `raise`。
- 注释与实现脱节：`mstorage/_base.py:832` 「全部 9 张表」实际 14 张；
  `app/routes/api_v1/diagnostics.py:15` docstring 写 256 KB，常量是 2 MB。
  （原列表里 `booker.py:687` 的 `addNewBooking` 与 `app/routes/notifications.py:88`
  的「自建连接」两条复核不成立，已删。）
- `_dedup`（`mcore/push.py:179`）、六张限流字典、`_DETAIL_CACHE`、
  `app_tokens` / `device_tokens` 四处无界增长，各加上限或清理入口。
- 裸 SQL 散落在六处路由 / 服务层，收回 `mstorage`；`feedback` 表的建表语句从
  请求路径挪进迁移。

### 明确不在 v2.0 内

- **新平台。** DUWO / SSH / Pararius 的调研（下方 §2）保留，但在方向一落地之前，
  每加一个 scraper 都是在往一个 CI 不跑测试的仓库里加一份新的「测试全绿但生产
  路径不同」。09-01 至 09-02 连接三个平台之后 `_PARSERS` 漏了三个键，就是证据。
- **RENTCafe 预订的端到端验证。** 它卡的是真实账号，不是代码。账号到位随时可做，
  但不作为 v2.0 的发布条件；`_AUTO_BOOK_SOURCES` 保持仅含 `holland2stay`。
- **可观测性第二批**（遥测进 `/api/v1`、耗时趋势、按城市维度）。留给 v2.1。
- **客户端功能。** 两个客户端已迁出，本仓库只提供 API。

### 发布判据

v2.0 打 tag 的条件，缺一不可：

1. CI 跑 pytest + ruff（含 tests 目录），`master` 全绿。
2. 上面四个方向各自的「完成标准」全部满足。
3. `ARCHITECTURE.md` §5、§6、§7 与实现一致；§6.1 的 at-least-once 承诺补上
   「重试有幂等键」这一句并与实现一致。
4. 重构前后 `tests/` 的用例数不减少。
5. 方向四那张表零残留；本节以外新发现的中危项允许残留，但每一条在 §9 已知限制
   里有记录。

### 顺序与依赖

```
方向一（CI 门禁）
  ├─► 方向四（残余缺陷，前八条）────────────────┐
  ├─► 方向三（配置收口 1/2/3）─────────────────┤
  └─► 方向二（结构重构）                        │
        ├─ 2.3 ─► 方向三（4/5）────────────────┼─► v2.0
        ├─ 2.4 ─► 幂等键与通知事件化 ───────────┤
        └─ 2.1 + 2.2 ─► 方向四（超时两条）──────┘
```

方向一先行，它是所有重构的安全网：没有 CI 跑测试，拆 `run_once` 就是盲改。
方向二内部 2.1 与 2.2 一起做（都在 run_once 里），2.3、2.4 各自独立，2.5 随手。
每个方向按 v1.x 的节奏各出一个小版本，v2.0 是它们的合集，不是一次大爆炸发布。

---

## 历史路线图（2026-06-13）

### 已完成：H2S 传输层迁移至 CloakBrowser
- H2S 将 API 迁至 `www.holland2stay.com/api/graphql` + Cloudflare Turnstile，旧 curl_cffi 路径封锁
  （该端点此后于 2026-08-11 再次迁移，当前值与迁移史见 [H2S.md](H2S.md) §2）
- **scraper**：`scrapers/holland2stay.py` 重写，`browser_fetcher.py` 共享模块，CloakBrowser 绕过 Turnstile + 浏览器内调用 GraphQL
- **booker**：同步迁移，所有 GraphQL mutation 走 BrowserFetcher
- **新 API**：扁平字段替代 `custom_attributesV2`；attribute ID→label 通过 aggregations 接口映射
- 为 Pararius / Funda 等 CF 保护的平台提供了通用基建

### 第一期：Android Play Store 上架 —— **已放弃（2026-08-03）**

Android 客户端 A0–A5 已完成，FCM 推送端到端拉通并通过真机验收。**不再上架 Play
Store**，分发方式确定为自 Release 页直接下载签名 APK；原计划中的 Google Play
Billing 内购、Data Safety、封闭测试与商店截图一并取消。

客户端已迁出本仓库，构建与进度复盘见
[FlatRadar-Android](https://github.com/751K/FlatRadar-Android)
（`docs/ANDROID_PLAN.md`），CI 也在该仓库。本仓库的 `build.yml` 只构建桌面端。

### 第二期：iOS 性能优化

已完成：DateFormatter 静态化、featureMap 键预归一化、URLCache 条件 GET、通知首屏
非阻塞、地图聚类后台化（v1.7.10）。其中 URLCache 条件 GET 依赖后端的 ETag / 304
中间件，那部分在本仓库（见下方 [§1 后端改动](#后端改动全部已完成)）。

剩余的客户端侧优化项已随客户端迁出，见
[FlatRadar-iOS](https://github.com/751K/FlatRadar-iOS)。

### 第三期：Xior 自动预订研究 —— **研究部分已完成（2026-08-04）**
- 三个攻坚项均已落地：登录流程（两段式，四处陷阱逐一实测）、多步表单自动填写
  （字段由页面驱动，15 个字段名经实测校正）、reCAPTCHA（对接 2Captcha，
  `captcha/rentcafe_pages.py` 逐页记录所用版本为 v2 或 v3）
- 实现位于 `bookers/rentcafe.py`，OurDomain / OurCampus 共用同一份
- 分析原文见 [XIOR.md](XIOR.md) §8（此处早先所写的 §11 系笔误，XIOR.md 无该节）
- 余下的三项工作均不属于编码，且不在 v2.0 发布条件内（见上方
  [明确不在 v2.0 内](#明确不在-v20-内)）：
  1. **Xior 的最后一步尚未确认。** 系统代为上传证件后申请表能否正常保存，仅差一次
     真实尝试。Xior 的草稿**不锁定房源**，比 Holland2Stay 提前一步终止，下一页即需
     填写 IBAN/SWIFT，因此即使走通，价值也远低于 Holland2Stay 一线。
  2. **OurDomain 欠缺一个真实账号。** 登录之后的环节全部未验证，且该流程不含选房
     页，一旦脱离流程便没有重选入口。需要含完整申请人资料、背景调查同意及已上传
     证件的 RENTCafe 账号。
  3. **账号粒度尚不明确。** 两栋 OurDomain 楼分属两个 securerc 主机，cookie 不跨
     主机，很可能一栋楼一套账号；目前用的是面板上单一的 `ourdomain_email` /
     `ourdomain_password`，待验证暴露问题后再照 `xior_accounts` 拆分。
- OurCampus 不在该线之内：其预订流程从未侦察，出房极少，不存在可预订的标的。

---

## 1. Android 客户端 —— 已迁出

客户端代码、技术栈、架构对齐、阶段拆分与风险评估已随仓库迁至
[FlatRadar-Android](https://github.com/751K/FlatRadar-Android)，规划原文见该仓库的
`docs/ANDROID_PLAN.md`。

本节只保留**属于本仓库的那部分**——为支持 Android 所做的后端改动。

### 后端改动（全部已完成）
- FCM 推送通道：`notifier_channels/fcm.py` ✅（HTTP v1 API + OAuth2 service account），与 `apns.py` 对称
- 推送平台分流：`mcore/push.py` ✅ 所有 dispatch 函数双发 APNs + FCM
- 设备注册：`mstorage/_devices.py` ✅ `platform` 字段区分 `ios` / `android`
- `/api/v1/devices/register` ✅ 白名单 `ios` / `android`，按 platform 分流
- `/api/v1/devices/test` ✅ 按 platform 分流（iOS → APNs，Android → FCM data-only payload）
- 服务端已部署：`FCM_ENABLED=true` + service account JSON `/secrets/` ✅
- 条件缓存中间件：`app/routes/api_v1/__init__.py` ✅ ETag + Cache-Control + 304 对所有 GET 200 JSON 响应
- 每小时存活采样：`mstorage/_base.py` ✅ `record_uptime_sample()` / `uptime_percent_7d()` 替代旧的 `monitor_started_at`

---

## 2. 更多租房平台支持

> 状态更新（2026-05-25）：本章节是早期多平台规划记录。当前主线已经完成多源抓取架构，并接入 Holland2Stay、OurDomain 和 Xior；后续平台扩展仍可参考下方调研和架构原则。

### 目标

FlatRadar 已由 Holland2Stay 单源演进为多平台监控。面向荷兰国际学生与年轻职场人群的
主要租房平台仍有十余家，继续扩展平台覆盖可进一步接近**一站式房源雷达**的目标，
从而持续提升对用户的价值。

### 平台调研（按优先级）

| # | 平台 | 域名 | 定位 | 抓取难度 |
|---|---|---|---|---|
| 1 | **OurDomain** | `ourdomain.nl` | ✅ 已接入。Amsterdam Diemen Zuid / Rotterdam，RENTCafe 后端 | ✅ 已完成 |
| 2 | **DUWO** | `duwo.nl` / `room.nl` | 荷兰最大学生住房供应商（Amsterdam / Delft / Leiden / Den Haag / Wageningen / Hoofddorp），ROOM.nl 是 DUWO 联合多家组织的统一平台 | 中（账号绑定，部分房源需注册） |
| 3 | **SSH Student Housing** | `sshxl.nl` | 全国性大型学生住房（Utrecht / Amsterdam / Eindhoven / Maastricht / Groningen / Rotterdam / Zwolle / Tilburg / Den Haag） | 中（账号绑定，short-stay 渠道独立） |
| 4 | **Pararius** | `pararius.nl` | 综合租房 marketplace，国际学生使用率最高的非学生专属站，english-first | 高（大量房源 + 中介模式，可能要应对 anti-bot） |
| 5 | **Kamernet** | `kamernet.nl` | 单间合租 marketplace，学生 / 年轻人占比高，paid model（房客付费看联系方式） | 高（付费墙 + 中介关系，scrape 要谨慎合规） |
| 6 | **HousingAnywhere** | `housinganywhere.com` | 国际学生 marketplace，覆盖欧洲；荷兰段量大 | 中（有公开 API 但条款限制） |
| 7 | **De Key** | `dekey.nl` | Amsterdam 城市住房协会，年轻人 / 学生定向（Stadgenoot Light） | 中（部分房源走 WoningNet） |
| 8 | **Lieven de Key — Studentenwoningweb** | `studentenwoningweb.nl` | DUWO + Lieven de Key + Stadgenoot 等 Amsterdam 学生住房联合平台 | 中（账号 + 排队等待制） |
| 9 | **Funda Huur** | `funda.nl/huur/` | 综合租房（量大但中介房源占比高） | 高（强 anti-bot，可能要等他们开放 API） |
| 10 | **Camelot Europe** | `camelot-europe.com` | 长 / 短租 + 看护型住宅（anti-squat），Amsterdam / Rotterdam 有量 | 中 |

---

### 架构现状（已完成）

多源抓取架构已实现。`scrapers/` 包包含 `base.py`（`AbstractScraper`、`ScrapeTask`、
`ScrapeResult`）、`holland2stay.py`、`ourdomain.py`、`ourcampus.py` 与 `xior.py`。
核心设计如下：

- `Listing.source` 配合前缀化 ID（`h2s_` / `od_` / `oc_` / `xr_`），保证全局唯一
- 数据库已完成迁移：新增 `source` 列、前缀化 backfill 与索引
- `monitor.py` 按 source 隔离故障
- 通知模板已加入 source badge（iMessage / Email / Telegram / APNs / FCM）
- iOS 端 `SourceBadge` view 已上线，Web 端的 Source 列与筛选亦已上线

> 其中「每 source 独立 stale 阈值」一项已于 v1.13.0 撤销：四个平台的终态信号一致，
> 现统一为一套两段式收敛，见 [ARCHITECTURE.md §5.13](ARCHITECTURE.md#513-从-feed-里消失是唯一的下架信号)。

#### Filter 跨 source 归一化参考

| 字段 | H2S | OurDomain | DUWO | 归一化策略 |
|---|---|---|---|---|
| 城市 | `city: Eindhoven` | `location.city: Eindhoven` | `properties.city: Eindhoven` | `lower().strip()` 后比对 |
| 状态 | `Available to book / Available in lottery / Rented` | `Available / Reserved` | `Available / Sold` | 抽 `StatusKind` enum：`book` / `lottery` / `reserved` / `other`；每个 scraper 自己映射 |
| 房型 | `Studio / 1-room / 2-room` | `Studio / Apartment / Loft` | `Single / Shared / Studio` | 抽 `TypeKind` enum + 保留 raw；UI 端宽松匹配 |
| 能效 | `A+ / A / B / ...` | （可能没这字段） | （多数 不暴露） | optional，UI 端 missing 时不显示 |
| 价格 | `basic_rent: 707.000` | `price: 1200` | `kale_huur: 450` | 统一 `priceValue: float`（已是 Listing 字段） |

### 阶段拆分（更新）

| 阶段 | 内容 | 预计 |
|---|---|---|
| **P0** | 架构重构（`scrapers/` 包 + `Listing.source` + DB 迁移 + monitor.py 改造） | ✅ 已完成 |
| **P1** | **OurDomain** + **Xior** —— 实现 scraper，验证多源 pipeline；UI 加 source badge | ✅ 已完成 |
| **P1.5** | **RENTCafe 自动预订** —— 多步表单自动填写与 reCAPTCHA 求解（详见 [XIOR.md](XIOR.md) §8） | ⚠️ 代码已完成，待端到端验证 |
| **P2** | **DUWO / ROOM.nl** + **SSH Student Housing** —— 覆盖 Amsterdam / Delft / Leiden / Utrecht 高校城市；需处理登录态 cookie | 3 周 |
| **P3** | **HousingAnywhere**（公开 API 优先）+ **Studentenwoningweb** | 2 周 |
| **P4** | **Pararius** / **Kamernet** —— 难度高，量大；Pararius 可能需 Playwright | 3 周 |
| **P5** | 跨平台 stats / dashboard 扩展（饼图 / 平台对比 / 平台独立 stale 阈值 / Web admin 系统页 source 健康看板） | 1 周 |

---

### 风险与合规

#### 法律 / 合规

- **`robots.txt` + ToS 逐家审查**：每个平台抓取前明确读条款，记录在 `docs/scraping_compliance.md`。HousingAnywhere 等明确有公开 API 的优先用 API
- **个人信息合规（AVG / GDPR）**：只抓房源本身字段，**绝对不**抓上传者 / 中介个人电话邮箱姓名；如果某些平台房源描述里夹带这些，scraper 层做正则脱敏后入库
- **不绕过付费墙**：Kamernet 等付费看联系方式的平台，只抓 free tier 公开列表，不模拟登录拿付费数据
- **明确"非官方第三方"声明**：每个 source badge 旁加 tooltip "FlatRadar is not affiliated with {Platform}"；登录页 / 关于页同步说明
- **数据保留期**：保留下架房源用于历史统计 OK；但若某平台 ToS 要求删除则在 `mark_stale` 时整条 listing 删掉而非仅标记 Occupied

#### 技术风险

- **反爬升级**：Pararius 与 Funda 采用 Cloudflare 加行为检测，`curl_cffi` 的 chrome110 impersonate 可能不足以应对。备用方案为 headless Playwright（运行时成本提升 10–50 倍），仅在投入产出比高的平台上采用
- **登录态平台**（DUWO / SSH / Studentenwoningweb）：账号密码存 `.env`，cookie 定期刷新；账号被锁就 fall-back 到游客可见的子集 + 推送 admin 告警
- **每平台轮询节奏分开**：高频平台（H2S）保 5min；低频学生平台（DUWO / SSH）放宽到 30min。每个 source 自己的 `INTERVAL` env 变量，monitor 循环里独立调度
- **后端流量放大**：从 1 source 到 10 source，出口流量 × N。监控 Docker / VPS 带宽配额；nginx 加 limit_req 兜底
- **数据质量参差**：不同平台字段完整度差异大，UI 层做 graceful degradation——缺 energy label 就不显示那一行，而不是显示 "—"

#### 运维风险

- **各平台的 schema 变更概率较高**：第三方网站每次改版都可能导致 scraper 失效。建议：
  - 每个 scraper 在 CI 跑 daily smoke test（拉 1 个城市，断言至少 1 条结果）
  - smoke test 连续 3 天失败时自动告警（推送 admin APNs + 邮件）
  - `mstorage/_meta` 记录每个 source 最后成功时间 + 最近一次错误，Web admin 系统页可视化 "source health"
- **故障隔离**：单个 source 发生故障不得影响其余 source。`run_once()` 以 try/except 隔离每个 source 的 scrape 阶段（见前文 `monitor.py` 的改造示例）
- **回滚预案**：DB 迁移用 idempotent ALTER + meta flag；如果 source 列的引入暴露了未预期的查询性能问题，可临时把 `SOURCES=holland2stay` 退化到单 source 行为

---

## 3. iOS 客户端 —— 已迁出

性能优化专项（v1.7.10）已完成，见 `docs/CHANGELOG.md`。剩余的低优项（Dynamic Type
完整支持、Swift Charts 无障碍、iPad Stage Manager 多窗口）属于客户端侧，已随仓库迁至
[FlatRadar-iOS](https://github.com/751K/FlatRadar-iOS)。

---

## 4. 后端 — 低优 / 持续改进

### Phase 5（admin 写操作）剩余项

`PUT /me/filter` ✅ v1.5.0；`DELETE /me` ✅ v1.5.0；`POST /auth/register` ✅ v1.5.0；`POST /auth/password` ✅ v1.6.0；`POST /diagnostics/crash` ✅ v1.6.0。

待补充的项目：

- `POST /api/v1/admin/users`：admin 端的用户 CRUD API（目前仅有 Web 后台，尚未
  暴露为 API）
- `POST /api/v1/admin/monitor/{start,stop,reload,restart}` ✅ 已全部暴露为 API（v1.7.x）

### 多平台之后的统计与图表扩展

- Dashboard 增加「按平台占比」饼图
- Stats 页增加「各平台房源更新速度」对比
- ~~每个 source 独立的 stale 阈值~~ —— **该方向已放弃**。四个平台的终态信号一致，
  分设阈值描述的是一个并不存在的差异，v1.13.0 已统一为一套两段式收敛，见
  [ARCHITECTURE.md §5.13](ARCHITECTURE.md#513-从-feed-里消失是唯一的下架信号)。

---

## 已完成里程碑

| 里程碑 | 版本 |
|---|---|
| 移动端 Web 体验适配 | v1.2.10 |
| monitor / storage 重构 | v1.3.0 |
| Phase 1 — 鉴权 + API 框架 | v1.3.2 |
| iOS 客户端 v1 MVP | v1.3.2 |
| Phase 2 — 只读数据端点 | v1.3.3 |
| Phase 3 — APNs 子系统 | v1.3.3 |
| Phase 4 — iOS 客户端 Phase 2 适配 | v1.3.3 |
| APNs 设备注册 + Deep link + SSE | v1.4.0 |
| Map / Calendar iOS UI | v1.4.0 |
| 错误展示打磨 / 多语言 / 深色模式 | v1.4.1 |
| iPad / Mac 适配（NavigationSplitView） | v1.4.x |
| 用户配置 SQLite 化 + 自助注册 + 改密 | v1.5.0 / v1.6.0 |
| Crash diagnostics 上报 + Web admin 查看 | v1.6.0 |
| StoreKit "Buy me a coffee" 内购 | v1.6.0 |
| **App Store 上架** | **v1.6.0** |
| ASC Accessibility Nutrition Label 覆盖 6 / 9 | v1.6.1 |
| 全平台性能优化 / 代码质量加固 | v1.7.8 |
| 用户优先级排序 + 安全加固 | v1.7.9 |
| Android MVP 完成（A0–A5）+ CI 自动构建 | v1.7.9 |
| Android FCM 端到端拉通 | v1.7.8 |
| iOS 性能专项（DateFormatter / featureMap / URLCache / SSE / 地图聚类） | v1.7.10 |
| Dashboard 运行时间修复（每小时存活采样） | v1.7.11 |
| Android 版本号动态化 + CI AAB 自动发布 | v1.7.11 |
| 后端条件缓存中间件（ETag / 304） | v1.7.10 |
| SQLite 连接池化 + 图表查询下推 | v1.7.10 |
