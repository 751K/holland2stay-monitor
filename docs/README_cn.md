# FlatRadar 房源监控

[![Website](https://img.shields.io/badge/Website-flatradar.app-0057CC?style=flat-square)](https://flatradar.app) [![Guide](https://img.shields.io/badge/Guide-Chinese-10B981?style=flat-square)](https://flatradar.app/guide?lang=zh) [![Support](https://img.shields.io/badge/Support-help-64748B?style=flat-square)](https://flatradar.app/support) [![Sponsor](https://img.shields.io/badge/Sponsor-GitHub-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/751K) [![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue?style=flat-square)](../LICENSE) [![Release](https://img.shields.io/github/v/release/751K/holland2stay-monitor?style=flat-square)](https://github.com/751K/holland2stay-monitor/releases) [![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/) [![iOS](https://img.shields.io/badge/iOS-SwiftUI-000000?style=flat-square&logo=apple&logoColor=white)](https://github.com/751K/FlatRadar-iOS) [![Android](https://img.shields.io/badge/Android-Compose-3DDC84?style=flat-square&logo=android&logoColor=white)](https://github.com/751K/FlatRadar-Android)

> English version: [README.md](README.md)

FlatRadar 监控荷兰租房市场，提供托管服务 [flatradar.app](https://flatradar.app)，
也可以自行部署。它同时追踪 Holland2Stay、OurDomain、OurCampus、Xior、Magis、
Student Experience 与 Plaza 七个平台，一旦出现符合所设条件的房源，即通过所选渠道
发出通知，通知中附有直达链接。对 Holland2Stay 更进一步——先将预订推进至支付页面，
再连同付款链接一并发出，因此收到通知时仅余付款一步。
Plaza 则可在用户单独启用后自动提交应征。

**FlatRadar 的服务范围不包括自动付款，亦不保证预订成功。** 能否订到仍取决于平台上的实际竞争。

## 使用方式

| 方式 | 适合 | 从这里开始 |
|---|---|---|
| **托管服务** | 大多数人 | 打开 [flatradar.app](https://flatradar.app)。首次登录会自动创建账户；在面板里设置筛选条件和通知渠道即可，无需安装。 |
| **移动端 App** | 在手机上接收提醒 | [iOS](https://apps.apple.com/us/app/flarradar/id6769857080) · [Android APK](https://github.com/751K/FlatRadar-Android/releases/latest/download/app-release.apk) |
| **桌面安装包** | macOS / Windows | 从 [Releases](https://github.com/751K/holland2stay-monitor/releases) 下载 |
| **自行部署** | 自己掌控数据库与投递 | 见下方[快速开始](#快速开始)。需要 Docker 和约 2 GB 内存；视网络情况可能需要住宅代理。 |

托管服务与自行部署运行同一套代码。

使用指南：[flatradar.app/guide?lang=zh](https://flatradar.app/guide?lang=zh) ·
支持：[flatradar.app/support](https://flatradar.app/support) ·
[support@flatradar.app](mailto:support@flatradar.app)

> FlatRadar 是独立的第三方工具，与所监控的任何房源平台均无隶属、背书、赞助或
> 合作关系。请仅用于个人非商业用途，并遵守各平台的服务条款。做决定前，请务必
> 到官方平台核实房源详情、价格、资格与预订状态。

---

## 功能概览

| | |
|---|---|
| **监控范围** | Holland2Stay、OurDomain、OurCampus、Xior、Magis、Student Experience、Plaza 共七个平台；新房源集中上架的时段轮询更为频繁 |
| **通知渠道** | Web、Telegram、邮件、WhatsApp、iOS 推送、Android 推送、iMessage，可同时启用多个 |
| **筛选条件** | 租金上限、面积下限、楼层下限、户型、入住人数、城市、街区、平台、合同类型、租客要求等 |
| **浏览方式** | 列表、地图、日历、仪表盘、图表，界面支持中英文 |
| **多用户** | 支持。设访客、用户、管理员三种角色，各用户的过滤条件与凭据互相独立 |
| **自动预订与应征** | Holland2Stay 止于支付页面；Plaza 支持自动应征，默认关闭；见[自动预订](#自动预订) |

### 平台覆盖

| 平台 | 覆盖范围 | 抓取成熟度 | 预订 |
|---|---|---|---|
| Holland2Stay | 任意配置的荷兰城市 | 稳定，为房源的主要来源 | 支持自动预订 |
| OurDomain | Amsterdam Diemen / South-East | 稳定 | 仅通知（预订链路已实现，未开放）|
| Xior | 14 个城市共 30 栋楼，可按需选择 | 稳定 | 仅通知（预订链路已实现，未开放）|
| OurCampus | Amsterdam Diemen（1 栋） | 已用真实 markup 校准；出房极少 | 仅通知 |
| Magis | 5 城 17 栋（Eindhoven 占 9 栋） | 2026-09-01 接入，纯 HTTP 无反爬 | 仅通知 |
| Student Experience | Amsterdam（4 处）与 Leiden | 2026-09-02 接入；入住日期只在详情页，故每个单元多一次请求 | 仅通知 |
| Plaza | 13 个荷兰城市 | 2026-09-02 接入；付费账号（€27.50/年）是「能应征」的门槛，不是「能看见」的门槛 | 支持自动应征，默认关闭 |

平台还可以跑在**影子模式**（`SHADOW_SOURCES`）下：照常抓取入库，但不发任何通知。
新接入的抓取器靠这个用真实数据验证一段时间，期间没有人的提醒依赖于它。

第三方站点随时可能变更，覆盖范围亦随之变化。各平台的抓取实现见
[H2S.md](H2S.md)、[XIOR.md](XIOR.md)、[OURDOMAIN.md](OURDOMAIN.md)、
[PLAZA.md](PLAZA.md)、[SCRAPING_RECON.md](SCRAPING_RECON.md)。

### 客户端

| 入口 | 状态 |
|---|---|
| Web 面板 | 稳定，为自部署的主要入口 |
| [iOS App](https://apps.apple.com/us/app/flarradar/id6769857080) | 维护阶段，已上架 App Store，在当前范围内功能完整 |
| [Android App](https://github.com/751K/FlatRadar-Android/releases/latest/download/app-release.apk) | Beta，提供已签名的 `.apk`，直接安装即可。FCM 推送已验证。不上架 Play Store，直接下载即为其分发方式 |
| 桌面版 | macOS `.dmg` 与 Windows `.zip`，见 [Releases](https://github.com/751K/holland2stay-monitor/releases) |

---

## 环境要求

|  | 最低 | 说明 |
|---|---|---|
| 内存 | **2 GB** | 每个受 Cloudflare 保护的 source 会常驻一个 headless Chromium（各约 200–400 MB）。1 GB 仅够运行单个 source。 |
| 磁盘 | 约 1.5 GB | 仅 patched Chromium 解压后即约 700 MB |
| Docker | Engine 20.10+，含 Compose 插件 | 使用 `docker compose`，而非旧版 `docker-compose` 脚本 |
| Python | 3.11+ | 仅从源码运行时需要。Docker 镜像使用 3.11；CI 与桌面版构建使用 3.12 |
| 系统 | 推荐 Linux | macOS 的限制见下方[从源码运行](#从源码运行) |
| 网络 | 可能需要住宅或移动代理 | 部分平台会对机房出口 IP 返回 403。云端部署通过 `HTTPS_PROXY` 走代理，代理账号是额外成本；使用托管服务则不需要 |
| 域名 | 可选 | 仅 HTTPS 部署需要，下文的本地路径无需域名 |

---

## 快速开始

### 一、本地部署

不需要域名和证书。依次执行：

```bash
git clone https://github.com/751K/holland2stay-monitor.git
cd holland2stay-monitor
cp .env.example .env
mkdir -p data logs
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d h2s
```

首次构建需数分钟。完成后访问 `http://127.0.0.1:8088`，无需登录。默认只监控 Holland2Stay 的 Eindhoven，其余平台在面板中按需启用。

> ⚠️ 该模式下面板是明文 HTTP、且没有密码，只监听本机地址。**请勿用于公网服务器**。

### 二、云端部署

开始部署前需要自行配置相关网络环境。DNS、防火墙端口、TLS 证书、客户端 IP 的处理
方式，以及如何换用其它反向代理，见 [DEPLOYMENT.md](DEPLOYMENT.md)。

**第 1 步**，创建配置与数据目录：

```bash
cp .env.example .env
mkdir -p data logs logs/caddy
```

**第 2 步**，编辑 `.env`：

```env
WEB_PASSWORD=一串足够长的随机字符
HTTPS_PROXY=http://user:pass@proxy-host:port
PUBLIC_BASE_URL=https://your.domain.com
SESSION_COOKIE_SECURE=true
```

- `WEB_PASSWORD`——留空时容器拒绝启动。
- `HTTPS_PROXY`——服务器出口被限制时可配置住宅或移动代理。机房出口曾出现 403，
  是否可用以实际网络和平台响应为准。
- `PUBLIC_BASE_URL`——验证邮件的链接由它拼出；未设置时程序拒绝发送。
- `SESSION_COOKIE_SECURE`——`.env.example` 里的默认值是 `false`，HTTPS 部署需改为
  `true`。

`SUPPORT_EMAIL` 与 `TIMEZONE` 已预填好（`support@example.com`、
`Europe/Amsterdam`），其余键也都有可用默认值——只在默认值不适用时才需要改。

**第 3 步**，编辑 `Caddyfile`，把其中的 `your.domain.com` 换成实际域名。

**第 4 步**，启动：

```bash
docker compose up -d
```

证书由 Caddy 自动申请与续期。启动完成后访问域名，用第 2 步所设的 `WEB_PASSWORD`
登录；用户名在未自行设置 `WEB_USERNAME` 时为 `admin`。

> entrypoint 启动前只校验两项：`WEB_PASSWORD` 非空、`Caddyfile` 中不再含
> `your.domain.com`。任一不满足都会打印 `FATAL` 并终止容器。其余键一概不校验——
> 漏填 `HTTPS_PROXY` 或 `PUBLIC_BASE_URL` 容器照常启动，要到日志里才看得出来。

### 确认运行状态

两个进程都应是 `RUNNING`：

```bash
docker exec h2s supervisorctl -c /etc/supervisor/conf.d/app.conf status
```

跟踪首轮抓取：

```bash
docker compose logs -f h2s
```

首轮比之后慢，属正常。一轮正常结束时会看到 `本轮完整扫描: N/N 城市 (...)`，
随后是 `本轮结束: ... 新房源`。

登录后在面板中添加用户、通知渠道，并选择待监控的平台与城市。用户级与系统级配置
均在面板中管理，`.env` 此后只在更换凭据、域名或时区时才需要改动。

### 首次使用清单

1. 以管理员身份登录面板。
2. 打开「用户」页面，创建需要接收提醒的用户。
3. 为每个用户启用至少一个通知渠道，并发一条测试通知。
4. 打开「设置」页面，启用平台**以及**对应的城市或楼盘——两者都要勾选，只勾城市不会生效。
5. 观察 `/monitoring` 的第一轮完整扫描，确认预期的平台都有数据。
6. 日常调整都在面板里做。`.env` 只用于部署设置、密钥和代理；把运行参数写回 `.env`
   会覆盖面板中的值。

### 从源码运行

仅供开发使用。生产环境应采用 Docker：容器已固定 Chromium 版本与 supervisor 配置。

```bash
pip install -r requirements.txt
python -m cloakbrowser install   # patched Chromium，解压后约 700 MB
cp .env.example .env
python web.py                    # 仅启动面板
python monitor.py                # 抓取循环，需另开终端
```

访问 `http://127.0.0.1:8088`。注意 `web.py` 与 `monitor.py` 是两个**互相独立**的
进程，共享 SQLite 并通过进程控制请求重载：只运行 `web.py` 不会抓取新房源。

> **macOS**：免费版 CloakBrowser 的 macOS 构建落后于 Linux，且 headless 模式
> 可能崩溃，因此本地运行会自动退回可见窗口模式。需长期依赖的部署请使用
> Linux 或 Docker。

---

## 升级与备份

```bash
cd /path/to/holland2stay-monitor
docker exec h2s python -c "import sqlite3; sqlite3.connect('data/listings.db').execute('VACUUM INTO ?', ('data/listings.db.bak.$(date +%Y%m%dT%H%M%S)',))"
git pull
docker compose build h2s
docker compose up -d --force-recreate h2s
```

需要备份的有两处，且**必须一起备份**：

- `data/listings.db` —— 房源、用户、凭据、设备 token，以及系统级设置（`app_settings`）
- `.env` —— 各类密钥，尤其是 `DATA_ENCRYPTION_KEY`

库中的密码与平台凭据是用 `.env` 里的密钥加密的，只恢复其中一个会得到一批无法
解密的凭据。自 v1.16.0 起，监控范围与轮询节奏亦存于库中，只恢复 `.env` 将回落到
代码默认值。

容器运行期间**不要直接复制数据库文件**，会遗漏最近的写入。取一致快照用：

```bash
docker exec h2s python -c "import sqlite3; \
  sqlite3.connect('data/listings.db').execute('VACUUM INTO \"data/backup.db\"')"
```

---

## 配置存储

配置分三处存放：

| 内容 | 位置 | 在哪里改 |
|---|---|---|
| 用户级：通知渠道、过滤条件、自动预订 | SQLite `user_configs` | 面板「用户」页 |
| 系统级：source、城市、轮询间隔 | SQLite `app_settings` | 面板「设置」页 |
| 部署级：凭据、路径、时区、对外基址 | `.env` | 文本编辑器 |

监控范围与轮询节奏由数据库管理。`SOURCES`、`CITIES`、
`*_CITIES`、`AVAILABILITY_FILTERS`、`CHECK_INTERVAL`、`PEAK_*` 等运行参数存于
数据库，由面板管理。首次启动会自动将其从 `.env` 迁入，迁移前把整份文件备份为
`.env.bak.<时间戳>`，日志中记有搬入与跳过的明细。

部署参数、服务端密钥和高级调优参数可保留在 `.env`：

| 类别 | 说明 |
|---|---|
| 服务端凭据 | 密码、API key、加密密钥和代理 URL；用户的平台与通知渠道凭据另存于加密的用户配置。 |
| 部署参数 | `DB_PATH`、`TIMEZONE`、`PUBLIC_BASE_URL`、`SESSION_COOKIE_SECURE`、`SUPPORT_EMAIL`；读取数据库前须先取得 `DB_PATH`。 |
| 阈值开关 | 通知渠道、配额和其他高级设置，多数有默认值。 |

以下几项需优先了解：

| 键 | 默认值 | 作用 |
|---|---|---|
| `WEB_PASSWORD` | — | **必填**，留空时容器拒绝启动 |
| `HTTPS_PROXY` | — | 服务器出口被平台限制时使用的抓取代理 |
| `PUBLIC_BASE_URL` | — | 验证邮件需要有效的 HTTPS 地址；未配置时程序拒绝生成链接 |
| `MONITOR_HEARTBEAT_MAX_AGE` | `900` | monitor 静默超过该时长后 `/health` 报告 unhealthy |
| `HEALTH_*` / `WATCHDOG_*` | 见 `.env.example` | `/monitoring` 所依托的数据退化告警阈值 |
| `STALE_RESERVED_HOURS` / `STALE_OCCUPIED_HOURS` | `0.5` / `2` | 房源自 feed 中消失多久后推定为 Reserved，再经多久判定为 Occupied，见[房源状态](#房源状态) |

完整清单见 [.env.example](../.env.example)，其中对每个键均有说明；键名与类别登记于
`env_registry.py`。

### 环境变量优先于数据库

运行参数的取值顺序为 **启动时外部环境变量 > `.env` 残留运行参数 > `app_settings` > 代码默认值**。保留外部环境变量一层是为容器化
排障提供强制覆盖口子：

```bash
docker compose run -e CHECK_INTERVAL=30 h2s python monitor.py
```

面板会标出被外部环境变量或 `.env` 覆盖的键，monitor 启动时亦会对 `.env` 中残留的
此类键发出警告。**日常改配置请用面板**；在 `.env` 中重新写入这些键会盖过面板。
部署参数、密钥与调优参数不参与热重载；修改后需重启相关进程，修改数据库路径应重启
整个应用。

### 输入校验

键名拼错不再是静默的。monitor 启动时审计 `.env`，对未登记的键发出警告并给出最接近
的候选：

```
⚙️  .env: PEAK_STRAT 不是本项目认识的配置键，它不会有任何效果（是不是想写 PEAK_START？）
```

监控范围一类的值格式坏了同样会被指出，且区分两种后果：格式错误（分隔符、字段数、
数值类型）由面板直接拒绝保存；实体不认识（城市 ID 或平台名不在已知表内）仅告警并
照常保存——官方注册表会更新，写死拒绝会使一个新上线的城市变成保存失败。

### 启用一个新平台

在面板「设置」页勾选平台**并**勾选该平台的城市或楼盘，二者缺一不可：仅勾选城市不会
生效。`SHADOW_SOURCES` 中列出的 source 照常抓取入库但**不发送任何通知**，用于新平台
对用户开放前的静默验证；不在 `SOURCES` 中的条目会被忽略并记录警告——仅写入影子名单
而未启用的平台属于「未启用」，而非「影子」。

上线生产前建议执行一次预检：

```bash
python -m tools.doctor --no-network
```

该命令须在**宿主机的仓库目录**中执行，而非容器内——`tools/` 是有意不打入镜像的。
它为只读操作：不写入配置、不发送通知、不干预 monitor 进程。

其中 `Settings` 一节回答部署后最常见的疑问：迁移是否已执行（`app_settings` 中
有多少项）、`.env` 中是否残留会盖过面板的键、以及有无拼错的键名。

---

## 房源状态

平台 feed 可能不明确说明房源何时下架，因此系统还需要根据完整抓取中的缺失情况
推断状态。部分平台也会直接报告不可用或已占用状态；上游报告和系统推断在面板中
分别标识。

对于未覆盖预留状态的 feed，默认先推断 Reserved，再根据消失总时长推断 Occupied；
如果配置的 Holland2Stay 状态已包含 Reserved，则可跳过中间推断：

```mermaid
stateDiagram-v2
    direction LR

    state "可订 / 抽签 / Unknown" as A
    state "Reserved" as R
    state "Occupied" as O

    [*] --> A: feed 中首次出现
    A --> R: 消失 30 分钟
    R --> O: 消失 2 小时
    A --> O: feed 已覆盖预留且消失 2 小时
    R --> A: feed 中再次出现
    O --> A: feed 中再次出现
```

凡系统推断得出的状态，面板上均会在状态旁显示一枚**「推测」**徽标，API 中对应
`status_is_inferred` 字段，使平台上报与系统推断始终可以区分。实际运行中，所见的
`Occupied` 多数为推断结果。推测转换**不产生通知**。

默认 2 小时参考 Holland2Stay 的付款时限，但状态仍是推断，不能保证与平台实时状态
一致。中间态 `Reserved` 的作用是降低判错的代价——
`Reserved → 可订` 本就是常见迁移；若直接判定为 Occupied 而房源随后回归，则会向
一批用户推送失实的「重新上架」通知。两个阈值均可配置（`STALE_RESERVED_HOURS` /
`STALE_OCCUPIED_HOURS`），设计理由与相关教训见
[架构总览：房源状态与完整性](ARCHITECTURE.md#status-convergence)。

收敛**仅对本轮完整扫描成功**的 source×城市执行，抓取失败绝不会被读作「房源已
下架」。

---

## 自动预订

Holland2Stay：系统以所配置的账号登录，尝试符合条件的可直接预订房源，
**止于支付 URL，不会完成支付**。

如果预订结果不明确，系统会暂停该 Holland2Stay 账号的自动预订并发送核实提示，
避免重复下单。请登录 Holland2Stay 检查订单或预留、按需付款或取消，然后在用户
设置中选择“已核实，恢复自动预订”。后台预订任务仍在运行时无法解除保护；若任务
卡住，请先停止监控。该保护跨监控重启保留，同一账号的多个用户共享保护。

Plaza：配置用户名和密码并单独开启 Plaza 自动应征后，系统可对符合条件的房源
直接提交应征。该开关默认关闭；应征提交后即生效，并不保证获得房源。详见
[Plaza 说明](PLAZA.md#61-已开到-source-级用户侧默认关)。
如果某套房源的应征结果不明确，请登录 Plaza 查看“我的应征”，然后在用户设置中
选择“已核实，恢复该房源应征”。核实前仅暂停同一账号对这套房源的自动应征，
其他房源仍可正常应征；后台任务仍在运行时不能解除保护。

Xior、OurDomain、OurCampus 使用 RENTCafe，共用一份实现
（[`bookers/rentcafe.py`](../bookers/rentcafe.py)）。验证码和部分申请流程已实现，
但各平台端到端预订尚未验证，功能仍处于**关闭**状态。

| 平台 | 已完成的部分 | 尚待验证的部分 |
|---|---|---|
| Xior | 真实账号已推进至申请表并保存草稿（2026-08-03） | 系统代传证件后能否正常保存尚未确认。草稿**不锁定房源**；下一页需填写 IBAN/SWIFT。 |
| OurDomain | 入口段已对真实站点走通（2026-08-04）：floorplans → 可用单元 → 条款页 POST | 登录之后的环节需要真实账号验证。找不到申请表时，代码会重选一次单元；仍不匹配则中止。 |
| OurCampus | `OurCampusBooker` 已注册、面板上可填凭据，但**用户侧关闭**（不在 `_AUTO_BOOK_SOURCES`）。真实账号实测：选中单元、条款页、Start Application（v3 一次过）、登录均已跑通 | 登录之后落到 Applicant Info 这一段没通。另外「开始申请能占住单元」已被证伪：站点写明提交只进抽签池、不锁房 |
| Magis | 无 | 预订流程尚未侦察 |

> 线上演示环境对普通用户关闭了自动预订。如有需要请邮件联系，或[自行部署](#快速开始)。

---

## 排障

排障应首先打开 **`/monitoring`**（admin）：其中包含分 source 的健康卡片、最近
30 轮明细（格式为 `房源数 (完整/任务)`）以及当前活跃的告警。下表中的多数问题
它可直接回答，无需进入容器。

`/health` 与 `/monitoring` 回答的是两个不同的问题。前者判断**「循环是否仍在
运行」**（依据心跳新鲜度），并决定容器的健康状态；后者判断**「数据是否仍然
正确」**——当解析器被上游改版破坏时，`/health` 仍为正常。数据退化仅向 admin
告警，**不会**将容器标记为 unhealthy：重启无法修复解析器与上游结构不匹配的
问题，只会中断正在进行的抓取。

| 现象 | 原因与处理 |
|---|---|
| 完全没有通知，面板显示陈旧数据 | monitor 进程已停止。先用 `supervisorctl status` 确认，再执行 `supervisorctl start monitor`。心跳超过 `MONITOR_HEARTBEAT_MAX_AGE` 后 `/health` 将返回 503。 |
| 某平台在 `/monitoring` 上显示为 `down` / `warn` | 卡片上已注明触发了哪条规则。参见[健康状态与排障](ARCHITECTURE.md#health-and-troubleshooting)。 |
| 需查询某时间段的日志 | `/logs` 支持关键字、级别与 `since`–`until` 过滤，且在服务端执行，不限于当前屏幕内的内容。 |
| 日志反复出现 `H2S source 熔断` | Cloudflare 正在拦截该出口 IP。熔断仅暂停该 source，稍后会以单个城市试探恢复。若持续出现，请配置 `HTTPS_PROXY`。 |
| 日志反复出现 `CF 挑战 ... 未解开` | 同上，通常属 IP 信誉问题，而非本地配置问题。 |
| 某个平台持续返回 0 条 | 可能确无房源。先查看该轮是否被标记为完整扫描，参考[房源状态与完整性](ARCHITECTURE.md#status-convergence)。 |
| 已出租的房源仍显示为「可订」 | 该城市未被完整扫描，因而未进入收敛范围。查看 `/monitoring` 轮次表格中的 `(完整/任务)`。 |
| 一批房源同时转为 Occupied | 若均带**「推测」**徽标则属正常：这是老化收敛的补偿执行（阈值刚调整过，或停机后首轮恢复），该过程**不发送通知**。不带徽标者才是平台实际上报。 |
| 日志反复出现 `抓到 0 个单元，第 N/3 轮` | 属正常行为。OurDomain / OurCampus 的空结果需连续 3 轮确认，以防单轮抖动收敛整栋楼。 |
| 容器被 OOM 终止 | 调高 `mem_limit`。每个受 Cloudflare 保护的 source 会常驻一个浏览器。 |
| 容器无法启动，日志中出现 `FATAL` | entrypoint 的安全预检未通过：或 `Caddyfile` 仍为 `your.domain.com`，或 `WEB_PASSWORD` 为空。两者均属于拒绝暴露无鉴权面板的保护措施。 |
| 已设置 `WEB_PASSWORD` 但无法登录 | 用户名为 `admin`，除非另行设置 `WEB_USERNAME`。 |
| `supervisorctl` 报 "no such file" | 其 socket 不在默认路径，命令需附加 `-c /etc/supervisor/conf.d/app.conf`。 |

容器的 `healthy` 状态已同时覆盖 web 与 monitor，但 unhealthy **不会**触发自动
重启。若需告警，须将容器健康状态接入自有的监控系统。

---

## 文档

| 文档 | 用途 |
|---|---|
| [使用指南](https://flatradar.app/guide?lang=zh) | 面板设置与日常使用 |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Docker 拓扑、Caddy 与 TLS、端口、日志、换用其它反向代理 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 系统架构、关键故障边界与排障入口 |
| [API.md](API.md) | 面向移动端与外部集成的后端契约 |
| [CHANGELOG.md](CHANGELOG.md) | 版本历史 |
| [H2S.md](H2S.md) · [H2S_BOOKING_OPS.md](H2S_BOOKING_OPS.md) · [RENTCAFE.md](RENTCAFE.md) · [XIOR.md](XIOR.md) · [OURDOMAIN.md](OURDOMAIN.md) · [PLAZA.md](PLAZA.md) · [SCRAPING_RECON.md](SCRAPING_RECON.md) | 各平台抓取侦察；`RENTCAFE.md` 写 Xior / OurDomain / OurCampus 共用的预订流程，`PLAZA.md` 只写预订侧，抓取侧连同 Magis / Student Experience 都在 `SCRAPING_RECON.md` 里 |
| [iOS_README.md](https://github.com/751K/FlatRadar-iOS/blob/master/docs/iOS_README.md) | iOS 客户端 — 已迁至 [FlatRadar-iOS](https://github.com/751K/FlatRadar-iOS) |
| [ANDROID_PLAN.md](https://github.com/751K/FlatRadar-Android/blob/master/docs/ANDROID_PLAN.md) | Android 客户端 — 已迁至 [FlatRadar-Android](https://github.com/751K/FlatRadar-Android) |
| [dataflow_ch.mmd](dataflow_ch.mmd) · [dataflow_en.mmd](dataflow_en.mmd) | 完整抓取与通知流程的 Mermaid 图 |

---

## 支持开发

FlatRadar 由个人独立开发与维护，服务器、推送基础设施及 App Store 相关费用均为
自行承担。

- 如觉得本项目有用，欢迎 Star。
- 可通过 [GitHub Sponsors](https://github.com/sponsors/751K) 或
  [flatradar.app/donate](https://flatradar.app/donate) 赞助。
- 问题反馈：[flatradar.app/support](https://flatradar.app/support)。

## 许可证

[PolyForm Noncommercial License 1.0.0](../LICENSE) —— 个人及其它非商业用途免费；
本许可证不授予商业使用权。
