# Xior — 平台状态

本文记录 Xior Student Housing 的抓取现状：端点形态、反爬机制、可用性判定与代码的
应对方式。实现以 [`scrapers/xior.py`](../scrapers/xior.py) 为准，本文描述的是该实现
依赖的外部事实；平台行为可能变化，历史实测不代表当前线上状态。

自动预订部分见 [RENTCAFE.md](RENTCAFE.md)：Xior、OurDomain、OurCampus 共用同一套
RENTCafe 流程，本文只记录 Xior 特有的入口差异。

## 1. 平台概况

| 项 | 值 |
|---|---|
| 官网 | `https://www.xiorstudenthousing.eu`（WordPress） |
| PMS | Yardi / RENTCafe（与 OurDomain 同一套） |
| 数据端点 | `POST /wp-admin/admin-ajax.php`，`action=yardi_room_availability` 在表单体里 |
| 数据格式 | JSON |
| 监控粒度 | 单元级（具体房号、精确面积/租金/押金） |
| 覆盖范围 | 代码内注册 NL 30 栋 / 14 城；平台全欧 100+ |
| 传输方式 | 浏览器（`BrowserFetcher` + `XIOR_PROFILE`） |

## 2. 反爬现状

### 2.1 Cloudflare 托管挑战（决定了传输方式）

`admin-ajax.php` 位于 Cloudflare 托管挑战之后。`curl_cffi` 的 TLS 指纹伪装无法通过，
恒返回 403 与挑战页；轮换整个指纹池亦无改善，问题不在指纹层面。

因此 Xior 采用浏览器传输层：`BrowserFetcher` 导航至
`https://www.xiorstudenthousing.eu/netherlands/` 通过挑战，再在 `page.evaluate` 中用
同源 `fetch` 发起 POST。clearance 与 TLS 指纹绑定，把 cookie 转交 HTTP 客户端无效。

### 2.2 IP 级限流（决定了请求节奏）

Cloudflare 对该端点按 **IP** 限流，阈值约为每窗口 15–20 次请求，且**跨轮累积**。
固定出口下每轮 12 次请求时，实测第 2 轮首个请求即被 429 拒绝。

代码用两道机制应对：

| 机制 | 参数 | 作用 |
|---|---|---|
| 全局请求间隔 | `_MIN_REQUEST_INTERVAL = 5.0` | 进程级锁，全部楼栋共用，瞬时速率约 12 req/min |
| 浏览器轮换 | `_BROWSER_MAX_AGE = 900` 与 `rotating_proxy=True` | 重建浏览器即换出口 IP，约每 3–4 轮、每 IP 约 40 次请求 |

429 仍会发生，退避由 `scrapers/base.py` 的 `RATE_LIMIT_BACKOFF` 负责。退避后仍失败
时分三层处置：

1. 该栋楼标记为 incomplete；
2. **同批次剩余楼栋直接判定失败，不再发请求**——429 针对出口 IP，剩余楼栋必然同样
   被拒；逐个重试只会各自睡满一轮退避，实测每次爆发因此阻塞整轮约 6 分钟；
3. 触发 Xior 的 source 级熔断（10 分钟起，上限 1 小时），到期后以 1 栋楼作 canary
   试探。

三层只作用于 Xior，其余平台同轮照常抓取。调度与熔断的通用机制见
[架构总览：平台与抓取](ARCHITECTURE.md#平台与抓取)。

**单轮耗时随楼栋数线性增长**，每栋约为「房型数 × 5 秒」，实测 14 秒量级，比其余
source 高一个数量级。30 栋楼需约 7 分钟才能扫完，而 `CHECK_INTERVAL` 只有 300 秒；
Holland2Stay 又排在其余 source **之后**执行，不分片等于每轮都推迟真正产出房源的
source。因此监控全部楼栋通过分轮抓取实现（默认 `SHARD_SIZES=xior:4`）：每轮抓若干
栋，游标持久化于 SQLite，重启后继续轮转。实现见 `monitor._apply_task_sharding()`。

分片不能单独解决问题：楼栋数减少后分片会自动失效（每轮即全量），单栋请求频率反而
升高。控制「同一 target 多久抓一次」的是 `SOURCE_MIN_INTERVALS`，二者须配合。

预订侧另有一套按「IP × 写接口」的限流，规则与抓取侧不同，见
[RENTCAFE.md 的写接口限流](RENTCAFE.md#73-预订侧的写接口限流)。

### 2.3 Turnstile 不校验服务端

Yardi modal 集成了 Cloudflare Turnstile（`action: 'yardi_room_availability'`），但
**端点本身不校验 token**。三组请求结果完全一致：

| 请求 | 结果 |
|---|---|
| 不携带 `cf-turnstile-response` | `{"success":true, "data":{...}}` |
| `cf-turnstile-response: ""` | 同上 |
| `cf-turnstile-response: "dummy"` | 同上 |

通过 §2.1 的托管挑战之后无需再求解 Turnstile。两者属于不同层面：挑战由 Cloudflare
边缘实施，Turnstile 是站点自行添加的前端组件。

## 3. 端点契约

### 3.1 请求

```
POST https://www.xiorstudenthousing.eu/wp-admin/admin-ajax.php
Content-Type: application/x-www-form-urlencoded

action=yardi_room_availability
property_page_id=1126     ← 楼的 WP page ID
room_type_id=33944        ← 房型 ID
semester_id=3281          ← 学期 ID
```

三个 ID 保存在 `XiorScraper.BUILDINGS` 这一**硬编码注册表**中，并非每轮从页面提取。
`discover_buildings()` 可重新生成（城市页 → 楼栋页 → `window.xior` 与 Yardi modal 的
`data-room-id`），但不在抓取路径上。`semester_id` 每年更替一次，届时重新执行一次
发现流程即可。

### 3.2 响应

```json
{
  "success": true,
  "data": {
    "units": [
      {
        "propertyId": 185845,
        "floorplanId": 1111471,
        "floorplanName": "Essential (Second - Fifth floor)",
        "apartmentId": 402419,
        "apartmentName": "M1.30.53",
        "beds": 1,
        "sqm": 19,
        "minimumRent": 417,
        "maximumRent": 580,
        "deposit": 0,
        "availableDate": "01/07/2026",
        "unitStatus": "Notice Unrented",
        "applyOnlineURL": "https://..."
      }
    ],
    "total": 2,
    "availability_response": { "errorCode": 200, "errorMessage": "" }
  }
}
```

### 3.3 `errorCode` 是 HTTP 风格状态码

**WordPress 层的 `success=true` 不代表上游调用成功。** 向 Yardi 请求可用性失败时，
WordPress 仍返回 `success=true` 与 `units=[]`，真实结果只在
`availability_response.errorCode` 中：

| code | 含义 |
|---|---|
| `200` | 正常返回（`units` 可能为空） |
| `204` | 无可用单元。官方前端走完整流程收到的也是此值 |
| 其它 | 真实故障 |

判据为 **2xx 即成功**，无法解析的 code 保守视为成功——把正常的零可用误标为
incomplete 会让 stale 收敛永不执行，代价远大于漏记一次故障。曾采用「非 204 即故障」
的判据，结果返回 `200` 的那栋楼整晚每轮、每个房型都被判定失败，误判量高出真实 429
一个数量级。

## 4. 可用性判定

WordPress feed 标记「可订」不等于当前能提交申请，存在两类假阳性，由两道校验拦截。
两道校验都只作用于映射为可订的单元；降级时写 `Occupied` 但仍保留在库中，日后重新
满足条件会触发状态变更通知。

### 4.1 状态映射

| `unitStatus` | 含义 | 映射 |
|---|---|---|
| `Notice Unrented` | 现住户已提交退租通知，尚未搬离 | `Available to book` |
| `Vacant Unrented Not Ready` | 已空置，房间尚未整理完毕 | `Available to book` |
| 其它情形，或 `units` 为空 | 无可用房源 | `Occupied`（fail-closed） |

两个状态的差异只在**当前无人居住的原因**，对用户没有区别：均可立即提交申请，实测
两类都带 `applyOnlineURL`，`availableDate` 分布完全重叠，且都要通过第二道校验。

**Xior 不存在抽签机制。** `Vacant Unrented Not Ready` 曾被映射为 `Available in
lottery`，这是错误映射："lottery" 是 Holland2Stay 的专有概念（availability filter
id=336 的摇号池）。该映射会让面板显示橙色 Lottery 徽标，等于提示用户参与一个不存在的
摇号。「尚不可入住」已由 `available_from` 表达，无需借用一个语义不符的状态来编码。

### 4.2 两道闸

| 闸 | 信号源 | 规则 | 失败策略 |
|---|---|---|---|
| ① 可用日期窗口 | feed 的 `availableDate` | 距今超过 60 天（`_AVAILABLE_HORIZON_DAYS`）即降级 | 日期缺失或无法解析时**不降级** |
| ② `floorplans.aspx` 权威校验 | RentCafe OLE `floorplans.aspx` | `floorplanId` 不在「确实可订」的户型集合内即降级 | 无法获取时 **fail-open**，以 feed 为准，但**不得推翻库内已有判定**（见 §4.3） |

**第一道闸**：`Notice Unrented` 的 `availableDate` 可能远在一年以后（现住户还未
搬离）。实测出现过 2027-07-01 入住的单元被报为「当前可订」，对急需租房的用户纯属
噪音。

**第二道闸**：feed 更新慢于 RENTCafe 实时库存，单元已被预订却仍留在 feed 里，用户点
`applyOnlineURL` 进去才发现没了。`floorplans.aspx` 是权威来源，每个户型 tile 三者
取其一：

- `<button class="applyButton" … floorPlans=<id>>` 且按钮文字为 `Available`：确实可订
- `<button class="contactButton" data-function='contactUsLink'>Contact Us`：不可订
- `<button class="applyButton" … floorPlans=<id>>` 但按钮文字为 `Rented Out`：不可订

**判据是按钮文字，不是类名。** RENTCafe 在户型租完后不改 `applyButton` 与
`data-selenium-id="ApplyNow"`，只改文字；tile 顶上的 `(Available)` 计数租完了也不
更新，同样不能用。未见过的按钮文字**放行并告警**——漏报真房源比误报贵。

关联键为 feed 单元的 `floorplanId` 等于 `floorPlans=<id>`。该页面只在「本栋楼存在
窗口内候选可订单元」时才额外请求。相关函数：`_floorplans_url()` /
`parse_bookable_floorplan_ids()` / `_fetch_bookable_floorplan_ids()` /
`XiorScraper._verify_bookable_floorplans()`。

**取数方式**：该页面曾在生产上尝试 10 次、全部失败（5 次 HTTP 403、5 次 challenge
页），长期处于 fail-open。现复用 OurDomain 的 SecureRC 打法（`scrapers/ourdomain.py`
的 `_get_text()` 与指纹冷却状态机）：浏览器风格 header、同 session 内 403 重试（首次
403 会顺手下发 `cf_clearance`，第二次往往软通过）、每次尝试换出口 IP + 换 TLS 指纹，
缺一不可。

出口 IP 用 `rotating=True`，与浏览器那条 sticky 线路无关：浏览器的 clearance 属于
Xior 主站 origin，在 `securerc.co.uk` 上不适用，没有 clearance 可复用，换 IP 才是
恢复手段（与 OurDomain 同理）。传输类异常也要换线路重试：实际遇到的
`curl: (56) CONNECT tunnel failed, response 502` 是出口故障，换指纹无用、换 IP 有用；
这类失败**不记指纹冷却**。收尾日志按「全部被挡」/「全部连不通」/「两者皆有」分开。

### 4.3 fail-open 只填空白，不推翻证据

校验拿不到时报出的「可订」带 `Listing.status_unverified = True`，存储层据此决定
是否放行（`mstorage/_listings.py::_should_hold_unverified`）：

| 库内现状 | 本轮（未校验）报可订 | 处置 |
|---|---|---|
| 已是 `Available to book` | 可订 | 放行，维持可订 |
| `Occupied` / `Reserved` | 可订 | **压住**：不写 `status_changes`、不发通知，仅更新价格等非状态字段 |
| 库内无此条（新房源） | 可订 | 放行并收录——无旧状态即无相反证据 |

界线在于**填补空白**与**推翻证据**是两回事：库内的 `Occupied` 是上一轮权威校验的
结果，拿一份已知滞后的 feed 去推翻它，等于让状态取决于「校验请求通没通」。该规则
来自 2026-08-25 的一次事故：`xr_373301` 当日因校验时通时断在库内翻转 5 次，向 38 名
用户发出 190 条通知；51 次校验中 15 次未打通（约 29%），房源本身全程未变。

代价：确有新单元上架而校验恰好不可用时，播报延后一至两轮（约 6–12 分钟）。

## 5. Listing 映射

```python
Listing(
    id          = f"xr_{unit['apartmentId']}",
    name        = f"{building_display} {unit['apartmentName']}",
    status      = <见 §4>,
    price_raw   = f"€{minimumRent}–€{maximumRent}",   # 相等时只写一个
    available_from = _normalise_date(unit["availableDate"]),  # DD/MM/YYYY → YYYY-MM-DD
    features    = ["Unit: …", "Building: …", "Floorplan: …", "Area: … m²", "Deposit: €…",
                   "Finishing: Furnished", "Tenant: student only"],   # 见 §5.1
    url         = unit["applyOnlineURL"] or building_url,
    city        = building_display,
    source      = "xior",
)
```

`price_value` 取最低价（与 OurDomain 一致）。通知中的链接直接采用
`applyOnlineURL`，其中已包含全部预填参数。

### 5.1 Tenant：全站学生盘

`Finishing` 与 `Tenant` 均非抓取所得，而是 `config.SOURCE_ASSUMED_FEATURES` 声明的
平台级事实。Xior 为纯学生住宿，全站不存在收入或雇佣条款，因此整个 source 取单一值
`student only`——与 OurDomain 不同，后者仅 Diemen 一栋楼内部就分学生与须有收入两档
（见 [OURDOMAIN.md](OURDOMAIN.md) §5.1）。

取值沿用 Holland2Stay `tenant_profile_restrictions` 的词汇表，Web 端 Tenant 多选过滤
器因此可跨 source 合并。`tenant` 已在 `_SOURCE_FILTER_DIMS` 中登记——只声明数据而不
登记能力表时该维度仍走 fail-open。存量房源由 `SOURCE_ASSUMED_FEATURES` 的 backfill
补齐，无需重新抓取。

## 6. 与另外两个平台的差异

| | H2S | OurDomain | Xior |
|---|---|---|---|
| 数据格式 | GraphQL JSON | HTML table | AJAX JSON |
| 传输方式 | 浏览器 | curl_cffi 加指纹轮换 | 浏览器 |
| Cloudflare 强度 | 托管挑战 | WAF 403（换指纹即可通过） | 托管挑战加 IP 限流 |
| 出口 IP 策略 | 一段时期内固定，随浏览器重建轮换 | 每次尝试均更换 | 一段时期内固定，随浏览器重建轮换 |
| 反机器人机制 | Turnstile | reCAPTCHA v3 与 v2 | Turnstile（不校验） |
| 每轮请求数 | 城市数 × 页数 | 1 + N 个 FP | N 个房型，每栋约 2–5 个 |
| 预订链接 | 无 | 无 | `applyOnlineURL` |
| 自动预订 | 已实现并启用 | 已实现到入口段，未开放 | 已实现到保存草稿，未开放 |

OurDomain 与 Xior 使用**同一套 RENTCafe，契约逐字相同**，共用
[`bookers/rentcafe.py`](../bookers/rentcafe.py)。平台差异只在「如何从一条 listing
到达 Applicant Info」：Xior 的单元信息预填在 `applyOnlineURL` 中，选房发生在登录
**之后**；OurDomain 需自行构造入口，选房发生在登录**之前**。详见
[RENTCAFE.md](RENTCAFE.md) 的平台入口差异。

## 7. 风险

| 风险 | 现状 |
|---|---|
| Turnstile 改为强制校验 | 尚未发生。一旦发生，需接入 Capsolver 一类的解题服务 |
| 限流阈值收紧 | 在可缓解范围内（5 秒间隔与 IP 轮换）；进一步收紧需依赖分轮抓取 |
| `semester_id` 变更 | 每年一次，重新执行 `discover_buildings()` 即可 |
| 楼栋增减 | `BUILDINGS` 人工维护，用户在 Web 面板勾选所需城市 |
| 端点改造或下线 | 可回退至 RENTCafe 直取（`floorplans.aspx`，与 OurDomain 路径相同），但失去单元级精度 |

## 8. 自动预订

预订流程、请求契约、验证码、边界与未验证事项统一记录在
[RENTCAFE.md](RENTCAFE.md)，此处不再重复。对 Xior 只强调两点：

- `applyOnlineURL` 是唯一入口，单元参数已预填，比 OurDomain 少一步；
- 当前链路止于保存草稿，且代传证件后的保存尚未验证，因此 `xior` 不在
  `monitor._AUTO_BOOK_SOURCES` 中，用户侧关闭。
