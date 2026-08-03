# Xior — 平台状态

Xior Student Housing 的抓取现状：端点长什么样、反爬是什么、代码怎么应对、
自动预订卡在哪。实现以 [`scrapers/xior.py`](../scrapers/xior.py) 为准，本文档
描述的是那份实现所依赖的外部事实。

---

## 1. 平台概况

| 项 | 值 |
|---|---|
| 官网 | `https://www.xiorstudenthousing.eu`（WordPress） |
| PMS | Yardi / RENTCafe（与 OurDomain 同一套） |
| 数据端点 | `POST /wp-admin/admin-ajax.php`，`action=yardi_room_availability` 在**表单体**里 |
| 数据格式 | JSON |
| 监控粒度 | 单元级（具体房号、精确面积/租金/押金） |
| 覆盖范围 | 代码内注册 NL 30 栋 / 14 城；平台全欧 100+ |
| 传输方式 | **浏览器**（`BrowserFetcher` + `XIOR_PROFILE`） |

---

## 2. 反爬现状

### 2.1 Cloudflare 托管挑战（决定了传输方式）

`admin-ajax.php` 在 Cloudflare 托管挑战后面。curl_cffi 的 TLS 指纹伪装**过不去**，
恒返回 403 + 挑战页——指纹池换遍也一样，这不是指纹问题。

因此 Xior 走浏览器传输层：`BrowserFetcher` 导航 `https://www.xiorstudenthousing.eu/netherlands/`
过挑战，再用 `page.evaluate` 里的 `fetch` 发**同源** POST。clearance 绑 TLS 指纹，
把 cookie 搬给 HTTP 客户端无效。

### 2.2 IP 级限流（决定了请求节奏）

Cloudflare 对这个端点按 **IP** 限流，阈值约 15–20 req/window，而且**跨轮累积**——
不是每轮清零。固定出口下每轮 12 个请求，实测第 2 轮的第一个请求就被 429 拒绝。

代码里两道机制配合：

| 机制 | 参数 | 作用 |
|---|---|---|
| 全局请求间隔 | `_MIN_REQUEST_INTERVAL = 5.0` | 进程级锁，所有楼共用。把瞬时速率压到 ~12 req/min |
| 浏览器轮换 | `_BROWSER_MAX_AGE = 900`（15 min）+ `rotating_proxy=True` | 重建浏览器 = 换出口 IP，把累积量摊开。≈3–4 轮 ≈40 请求/IP |

429 仍会发生（高峰期实测每小时几次），退避重试用 `scrapers/base.py` 的
`RATE_LIMIT_BACKOFF`；退完还失败就整栋楼标 incomplete，由 monitor 逐 source
隔离，不影响其它平台。

> 楼栋数增加时单轮耗时线性增长（每栋楼房型数 × 5s）。楼很多时应该分轮抓，
> 而不是把间隔调小。

**实测成本（2026-08-03，取自 `round_stats` 遥测）**：

| source | 每轮中位 | 每 target |
|---|---|---|
| **xior** | **55.5s** | **13.9s** |
| ourdomain | 4.1s | 4.1s |
| ourcampus | 2.2s | 2.2s |
| holland2stay | 1.0s | 1.0s |

Xior 一家就占掉一轮 62 秒里的 55 秒。按 13.9s/栋 外推，注册表里的 30 栋
≈ **417 秒/轮**，而 `CHECK_INTERVAL` 是 300 秒——更要命的是 H2S 排在其它
source **之后**执行，不分片等于每轮把真正出房源的那个 source 推迟 7 分钟。

所以监控全部 30 栋是靠 `SHARD_SIZES=xior:5` 分轮抓实现的：每轮 5 栋
（≈70 秒），6 轮覆盖一遍，游标持久化在 SQLite 里、重启后接着转。
实现见 `monitor._apply_task_sharding()`。

### 2.3 Turnstile 不校验服务端

Yardi modal 里集成了 Cloudflare Turnstile：

```javascript
window.turnstile.render($tsEl[0], {
    sitekey: ...,
    action: 'yardi_room_availability',
    callback: window.onYardiTsSuccess,
});
```

但**端点本身不验证 token**。三组请求返回完全一致：

| 请求 | 结果 |
|---|---|
| 不带 `cf-turnstile-response` | `{"success":true, "data":{...}}` |
| `cf-turnstile-response: ""` | 同上 |
| `cf-turnstile-response: "dummy"` | 同上 |

所以过了 §2.1 的托管挑战之后，不需要再解 Turnstile。这两件事是分开的：
挑战是 Cloudflare 边缘做的，Turnstile 是站点自己加的装饰。

---

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

三个 ID 存在 `XiorScraper.BUILDINGS` 这个**硬编码注册表**里，不是每轮从页面提取。
`discover_buildings()` 可以重新生成它（从城市页 → 楼栋页 → `window.xior` +
Yardi modal 的 `data-room-id`），但不在抓取路径上。`semester_id` 每年轮换一次，
届时重跑一次发现即可。

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

**WordPress 层 `success=true` 不代表上游成功。** 向 Yardi 取可用性失败时，WP 照样
返回 `success=true` + `units=[]`，真实结果只在 `availability_response.errorCode`：

| code | 含义 |
|---|---|
| `200` | 正常返回（`units` 可能为空） |
| `204` | 无可用单元。官方前端走完整流程收到的也是它 |
| 其它 | 真故障 |

判据必须是 **2xx = 成功**，而且无法解析的 code 要**保守当作成功**——把正常的
零可用误标成 incomplete 会让 stale 收敛永不执行，代价远大于少记一次故障。
曾经用「非 204 即故障」，整晚 36 轮里返回 `200` 的那栋楼（4 个房型 × 36 轮
= 144 次）全被判成抓取失败，而真正的 429 只有 8 次。

---

## 4. 可用性判定

WP feed 的「可订」不等于「现在真能抢」，有两类假阳性，对应两道闸。两道闸都只
作用于映射成可订/可抽签的单元，降级时写成 `Occupied` 但仍留库——日后重新满足
条件会触发 `Occupied → 可订` 的状态变更通知。

### 4.1 状态映射

| `unitStatus` | 含义 | 映射 |
|---|---|---|
| `Notice Unrented` | 现住户已递交退租通知，人还没搬走 | `Available to book` |
| `Vacant Unrented Not Ready` | 已空置，房间还没收拾好 | `Available to book` |
| 其它 / `units` 为空 | 无房 | `Occupied`（fail-closed） |

两个 Yardi 状态的区别只在**为什么现在没人住**，对用户没有差别——都能立刻提交
申请。实测两类单元都带 `applyOnlineURL`，`availableDate` 分布完全重叠，且都要
过闸②，过不了的一律降级 `Occupied`。

> **Xior 没有抽签机制。** `Vacant Unrented Not Ready` 曾被映射成
> `Available in lottery`，那是错的——"lottery" 是 Holland2Stay 专有概念
> （H2S availability filter id=336 摇号池）。这个错标有两个实际后果：
> 面板给用户显示橙色 "Lottery" 徽标，等于告诉他们去参加一个不存在的摇号；
> 而且 stale 收敛对 lottery 用 **2 天**阈值而非 7 天，这些单元会以 3.5 倍
> 速度被推测成 `Occupied`。
>
> 「还不能入住」这层信息由 `available_from` 表达，闸① 已经把太远的滤掉了，
> 不需要再借一个语义不符的状态来编码。

### 4.2 两道闸

| 闸 | 信号源 | 规则 | 失败策略 |
|---|---|---|---|
| ① 可用日期窗口 | feed 的 `availableDate` | 距今 > 60 天（`_AVAILABLE_HORIZON_DAYS`）→ 降级 | 日期缺失/不可解析 → **不降级**（保守） |
| ② floorplans.aspx 权威校验 | RentCafe OLE `floorplans.aspx` | 单元 `floorplanId` 不在「真正可订」户型集合 → 降级 | 抓不到（网络/CF/非 200）→ **fail-open**，信 feed |

**闸 ① 存在的原因**：`Notice Unrented` 的 `availableDate` 可能在一年多以后（现住户
还没搬走）。实测见过 `2027-07-01` 的单元被报成「现在可订」——对「现在就要找房」
的用户是纯噪音。

**闸 ② 存在的原因**：feed 比 RENTCafe 实时库存更新慢，单元已被订走仍列在里面，
用户点 `applyOnlineURL` 进去发现「没了」。`floorplans.aspx` 是权威来源，每个户型
tile 二选一：

- `(Available)` + `<button class="applyButton" … floorPlans=<id>>` → 真能订
- `(Contact for Availability)` + `<button class="contactButton" data-function='contactUsLink'>` → 订不了

join key：feed 单元的 `floorplanId` == `floorplans.aspx` 的 `floorPlans=<id>`。

这一页用 curl_cffi 直取（它**不在**托管挑战后面，HTTP 200），且只在「本栋楼存在
窗口内候选可订单元」时才多请求一次——绝大多数轮次 0 候选 = 零额外请求。相关函数：
`_floorplans_url()` / `parse_bookable_floorplan_ids()` / `_fetch_bookable_floorplan_ids()` /
`XiorScraper._verify_bookable_floorplans()`。

---

## 5. Listing 映射

```python
Listing(
    id          = f"xr_{unit['apartmentId']}",
    name        = f"{building_display} {unit['apartmentName']}",
    status      = <见 §4>,
    price_raw   = f"€{minimumRent}–€{maximumRent}",   # 相等时只写一个
    available_from = _normalise_date(unit["availableDate"]),  # DD/MM/YYYY → YYYY-MM-DD
    features    = ["Unit: …", "Building: …", "Floorplan: …", "Area: … m²", "Deposit: €…"],
    url         = unit["applyOnlineURL"] or building_url,
    city        = building_display,
    source      = "xior",
)
```

`price_value` 取最低价（与 OurDomain 一致，`parse_float` 直接可用）。

通知里的链接直接是 `applyOnlineURL`——RENTCafe 预订页，含全部预填参数。

---

## 6. 与另外两个平台的差异

| | H2S | OurDomain | Xior |
|---|---|---|---|
| 数据格式 | GraphQL JSON | HTML table | AJAX JSON |
| 传输 | 浏览器 | curl_cffi + 指纹轮换 | 浏览器 |
| CF 强度 | 托管挑战 | WAF 403（换指纹可过） | 托管挑战 + IP 限流 |
| 出口 IP 策略 | 固定 sticky（复用 clearance） | **每次尝试换**（换 IP 才是它的解法） | 固定一段时间，随浏览器重建轮换 |
| 反机器人 | Turnstile | reCAPTCHA v3+v2 | Turnstile（不校验） |
| 每轮请求数 | N×城市（含翻页） | 1 + N 个 FP | N 个房型（~2–5/栋） |
| 预订链接 | 无 | 无 | `applyOnlineURL` |
| 自动预订 | 已实现 | 框架就绪，卡 reCAPTCHA | 框架就绪，卡 reCAPTCHA |

---

## 7. 风险

| 风险 | 现状 |
|---|---|
| Turnstile 改为强制校验 | 尚未发生。真发生则需接 Capsolver 之类的解题服务 |
| 限流阈值收紧 | 已在缓解范围内（5s 间隔 + IP 轮换）；进一步收紧需分轮抓取 |
| `semester_id` 变更 | 每年一次，重跑 `discover_buildings()` |
| 新楼 / 删楼 | `BUILDINGS` 手工维护，用户在 Web 面板勾选城市 |
| 端点改造 / 下线 | 可回退到 RENTCafe 直取（`floorplans.aspx`，与 OurDomain 同路径），但会丢失单元级精度 |

---

## 8. 自动预订可行性

框架在 [`bookers/rentcafe.py`](../bookers/rentcafe.py)，面板标记为「开发中」。
**阻塞点是 reCAPTCHA，不是流程未知。**

### 8.1 预订入口

每个 unit 的 `applyOnlineURL` 直达 RENTCafe，且 URL 含全部预填参数（`myOlePropertyId`
/ `floorPlans` / `UnitTypeId` / `ATId`），跳过选房步骤——这一点比 OurDomain 好。

### 8.2 九步流程

`oleapplication.aspx` 的侧边栏完整暴露：

```
1. Floorplan               ← URL 已预填
2. Rental Options          ← 点 "Start Application"
3. Applicant Info          ← 需登录/注册
4. Additional Applicants
5. Additional Rental Options
6. Applicant Charges
7. Lease Summary
8. Lease Creation          ← 最终签约
9. Review / Confirm
```

第 4 步之后未到达（登录后 session 重置，且侦察时无房可继续）。

**用过期单元的参数也能打开这一页**（实测：拿 2026-05-28 那条已 Occupied 的
`applyOnlineURL`，2026-08-03 仍返回 HTTP 200 完整表单）。这条对推进很关键——
第 1–2 步的侦察不必等到有房。RENTCafe 侧也**不需要浏览器**：`securerc.co.uk`
不在 §2.1 那道 Cloudflare 挑战后面，curl_cffi 直连即可，与 OurDomain 一致。

#### 第 3 步 Applicant Info（实测，2026-08-03）

**不需要登录就能到达。** 第 2 步点 Start Application 之后直接落到注册表单
（`formName2=mylistregister`，`IsRegister=-1`）；页面上有「Log in」链接指向
`guestlogin.aspx` 供已有账号走。侧边栏这栋楼是 **8 步**（Floorplan → Lease
Creation），不是文档原先记的 9 步。

**没有文件上传。** 实测 `input[type=file]` 数量为 0，正文也无 upload / proof /
bewijs 一类字样。这一条此前被列为「若存在则整个方向不成立」的头号风险，
到此排除。

可见字段：

```
txtName  txtName2  txtEmail  txtPassword        # 必填四项
drpCurrentCountry(默认 2=Netherlands)  txtPhone
ddlLanguage  HowDidYouHear  SubscribeToEmails
```

反自动化字段（比 reCAPTCHA 更需要注意）：

| 字段 | 观察到的值 | 推测作用 |
|---|---|---|
| `txtRenderTime` | `3-8-2026 15:12:16` | 页面渲染时刻。**提交过快很可能被判为机器人** |
| `txtCodeVal` | `MTczNjc2Njk3OA==-ha0nQzH…` | 又一个 `base64-签名` 对 |
| `txtvalue1` | 随机串 | 服务端下发，需回传 |
| `txtvalue2` | 空 textarea | 疑似蜜罐——**必须留空** |

隐藏字段把租约上下文带到下一步：`FloorPlanID` / `UnitTypeID` /
`txtExpectedMoveInDate` / `txtPreferredRent` / `hdnAcademicTermId` /
`hdnSchoolId` / `myOlePropertyId` / `cafeportalkey` 等。

第 4–8 步仍未到达（需要真实账号）。

#### ⚠️ 存在必填的证件上传（2026-08-03 用真实账号走到 Applicant Info 后确认）

`stepname=DocumentSummary` 页面上：

```
Additional Documents
ID/Passport Upload*        ← 星号 = 必填
```

这是**整个自动预订方向最关键的一条约束**。此前只在第 3 步注册表单上看到
`input[type=file]` 数为 0 就判断「没有文件上传」——那是错的，注册页确实没有，
但**登录后的申请流程里有**。

它到底挡不挡得住自动化，取决于一件尚未确认的事：**单元是在哪一步被锁住的**。
申请页原文写着

> Prices and specials are not guaranteed until you have paid the application fees.

暗示锁定发生在 **Application Charges（付费）**，而不是文件上传。若如此，自动化
仍可能先抢到锁定、证件事后补传；若不然，秒级抢房不成立。**下一步要确认的就是
这一点**，而不是继续做实现。

#### 完整流程（实测，9 步）

```
1 Floorplan            2 Rental Options       3 Applicant Info
4 Additional Applicants 5 Additional Rental Options
6 Application Charges  7 Lease Summary        8 Lease Creation
9 Move-in Charges
```

另有 `Documents` / `Alerts` / `Summary` 三个横向标签页（不在主流程里）。

**注册即登录，不强制 OTP**（§8.5 原第 3 项，到此有答案）。注册成功后直接跳
`stepname=Apartments&FromRegistration=1`。

#### 选房动作：`ContinueClick`

Apartments 步骤里每个可订单元一个「Reserve this room」按钮。名字唬人，实际是
**选中单元并跳到 ApplicantInfo**，不是当场锁房：

```javascript
ContinueClick('398336','1111515','185795','16-8-2026','',
  'oleapplication.aspx?myLeaseCafeType=2&stepname=ApplicantInfo&FromUnitSelection=1',
  '0','0','648','3281','1','16-8-2026','1-11-2026', …)
//            ↑ unitId    ↑ floorPlanId ↑ propertyId ↑ availableDate
```

**`unitId` 就是抓取侧 `xr_<id>` 里的那个 id**（例：`398336` ↔ `xr_398336`），
自动化不需要额外映射。

#### 第 3 步 Applicant Info 的字段

```
个人      Title  FirstName*  MiddleName*(+「我没有中间名」勾选)  LastName*
          Phone  Email*  MoveInDate*  MinimumLeaseTerm*  Gender*
地址      Country*  Address*  University*  PostCode-City*
筛查      DateOfBirth*  Nationality*  …
```

`University*` 是 Xior 特有的必填项；`Screening Information` 区块意味着有背景
审查环节。这一步本身仍然全是文本/下拉，**没有** file input。

#### `MoveInDateEncr` 不是障碍（实测推翻了先前判断）

先前把它列为「最可能卡死自动化」的机制。实测**不成立**：

- 页面上改 `sMoveInDate`（3-8-2026 → 15-9-2026），`MoveInDateEncr` **不变**，
  仍是签好名的原值——客户端根本不重算签名。
- 同一日期在**不同楼栋**下签名完全相同（Zernikestraat 与 Vaals 都是
  `My04LTIwMjY=-z8g85jGXmr8=`），说明签名只与明文有关，与房源/会话无关。
- 页面明写这个日期是「**开始申请的日期**」，合同日期由单元自身的可用日期决定。

格式是 `base64(明文)-签名`（`NDk5LjAw-…` → `499.00`）。结论：**自动化原样回传
服务端下发的值即可，不需要伪造签名**。

#### 第 2 步的字段契约（实测）

表单 `termsandotheritems` → POST `/onlineleasing/rcformsave.ashx`。

服务端下发、**必须原样回填**的：

```
formName2=termsandotheritems   formName=<base64>      cafeportalkey=<key>-<sig>
FloorplanId  FloorplanName     UnitTypeId  SchoolId   AcademicTermId/Name
RentalLevel  strRentalLevel    myOlePropertyId        myLeaseCafeType
IsRCOLE      leasingtype       PropLeadSource_<propId>
```

带签名、**不能伪造**的：

```
MoveInDateEncr = <base64(日期)>-<签名>     例 My04LTIwMjY=-z8g85jGXmr8=  →  3-8-2026
QuotedRentEncr = <base64(金额)>-<签名>
```

> 这两个是入住日和租金的加签副本。想改入住日就不能只改明文字段——签名对不上。
> 换日期要走服务端重新下发，具体接口未确认。**这是目前最可能卡住自动化的机制**，
> 优先级高于 reCAPTCHA。

需要用户输入的：`sMoveInDate`（Expected Move-In Date，客户端强制校验非空 +
`IsValidDate()`；Xior 把它的界面文案改成了 "Start application date"）。

附加租赁项：`hRentableitemstype` 由 JS 拼成 `<itemTypeId>^<qty>` 逗号串，
不选就是空串。

### 8.3 reCAPTCHA

> 这一节 2026-08-03 按真实页面重写过。原来写的是「RENTCafe **全线**使用
> reCAPTCHA Enterprise，一个 v3 sitekey 通吃」——**不成立**。条款页用的是
> 标准 v3 和另一个 sitekey。按原描述去解，token 服务端不认。

三个页面实测各不相同：

| 页面 | v3 类型 | v3 sitekey | action | 回退标志字段 |
|---|---|---|---|---|
| `oleapplication.aspx`（第 2 步条款） | **标准 v3**（`api.js`） | `6LcjBc4UAAAAABfXlERv_hq_KE3IWDAqbiWkbPzl` | `start_application` | `failed-captcha-3-rentable` |
| `guestlogin.aspx` | Enterprise（`enterprise.js`） | `6LfBeqEaAAAAALsbENKGUsE98xFoA3ZpqkbzogBI` | `UserLogin` | `failed-captcha-3` |
| `register.aspx` | Enterprise | 同上 | `GuestRegistration` | `failed-captcha-3` |
| `flexregistrationlandingpage.aspx` | **无** | — | — | — |

共同点只有两条：v2 回退 sitekey 都是
`6LfAdx8TAAAAAOiesnT8CNKNtb1C6doK-RKnB1V0`，token 都填进
`g-recaptcha-response-v3`。

这张表已经编码进 [`captcha/rentcafe_pages.py`](../captcha/rentcafe_pages.py)，
新增页面往那里补，不要在调用点写死。

条款页的执行链（实测抄录）：

```javascript
submitTermsForm()
  └─ failed-captcha-3-rentable === 'false'
       ├─ 是 → getCaptchaTokenRentable()
       │        grecaptcha.execute('6LcjBc4U…', {action:'start_application'})
       │          → token 填入 #g-recaptcha-response-v3 → 点 #divbtnStart 提交
       └─ 否 → 直接点 #divbtnStart（此时页面上已有解好的 v2）
```

v2 回退由**服务端**决定：v3 分数不够时响应会触发
`callReCaptchaV2Rentable()`，它把 `failed-captcha-3-rentable` 置 `true` 并
渲染 checkbox。所以正常路径每次提交只需要 **1 个 v3 token**。

**`flexregistrationlandingpage.aspx` 不是绕过入口。** 它确实没有任何
reCAPTCHA（实测 0 个 sitekey、无 recaptcha 脚本），但它只是个「选租约类型」
的落地页，两个出口（Market / Student）都指回带 Enterprise 验证码的
`register.aspx`，只是换了个入口而已。§8.5 原来的第一个问号到此有答案了，
答案是否定的。

另外：Xior 用 JS 隐藏了 RENTCafe 上的注册入口——实测隐藏的是
`a#ClickHereToRegisterLink`、`a[href*="flexregistrationlandingpage.aspx"]`、
`a[href*="register.aspx"]` 三处，意图是让用户走 WordPress 侧注册。
后端接口仍然存活，可以直接 GET/POST。

### 8.4 成本估算

| 步骤 | reCAPTCHA | 求解方式 | 耗时 | 成本 |
|---|---|---|---|---|
| 登录 | v3 Enterprise | Capsolver `ReCaptchaV3TaskProxyLess` | 10–20s | ~$0.001 |
| 注册（如需） | v3 Enterprise | 同上 | 10–20s | ~$0.001 |
| 条款提交 | v3 + v2 | v3 先试，失败回退 v2 | 15–30s | ~$0.002 |
| **合计** | | | **30–60s** | **~$0.003–0.005** |

RENTCafe 还有 IP 级 attempt limit，连续失败锁 30 分钟——自动化重试要非常克制。

### 8.5 已确认 / 未确认

2026-08-03 侦察后的状态。

**已确认（不必再查）**

- ~~`flexregistrationlandingpage.aspx` 是否是无 reCAPTCHA 的旁路~~ → **不是**。
  它自身确实无验证码，但两个出口都回到带 Enterprise 验证码的 `register.aspx`。
- ~~v3 token 能否跨步骤复用（同一 sitekey）~~ → **不能**，因为压根不是同一个
  sitekey：条款页标准 v3 `6LcjBc4U…`，登录/注册 Enterprise `6LfBeqEa…`，
  action 也三者互异。每页必须单独解。
- 第 1–2 步可以用过期单元的参数侦察，且不需要浏览器（见 §8.2）。

- ~~`MoveInDateEncr` / `QuotedRentEncr` 的签名怎么来~~ → **不需要伪造**，原样回传
  服务端下发的值即可。详见上面「`MoveInDateEncr` 不是障碍」。
- ~~注册/登录是否强制 OTP~~ → **不强制**。注册成功即登录，直接进 Apartments 步骤。
- ~~第 3 步是否有文件上传~~ → 第 3 步本身没有，但**流程里有**：
  `DocumentSummary` 上的 `ID/Passport Upload*` 是必填。见上面的警告小节。
- 完整 9 步流程、`ContinueClick` 选房动作、Applicant Info 字段清单，均已实测记录。

**未确认（按优先级）**

1. **单元究竟在哪一步被锁住？** 这是决定整个方向成不成立的唯一问题。
   若锁定发生在 Application Charges（付费）之前/之时，而证件可以事后补传，
   自动抢房仍然成立；若必须先传证件才能锁定，秒级抢房不成立，该转向
   「预填好参数的一键跳转」。
2. **application fee 是多少、怎么付。** 页面写「until you have paid the
   application fees」，但金额和支付方式未见。
3. 反自动化字段的服务端校验强度：`txtRenderTime`（渲染时刻，疑似用于「提交
   过快」判定）、`txtvalue2`（空 textarea，疑似蜜罐）。这两个比 reCAPTCHA
   更容易在实现时踩到。
4. **连续登录失败锁定的作用域是账号还是 IP。** 2026-08-03 实测确实会锁
   （用户连续输错密码后无法登录）。文档 §2.2 记的是 IP 级——**若真是 IP 级，
   服务器上跑的 booker 会被同一机制打到，一个用户输错密码可能连累同出口 IP
   的其他人**。这条对部署形态影响很大，需要单独验证。
5. 服务端 v3 score 阈值多高——阈值低则几乎不触发 v2 回退，成本按 §8.4 的乐观值走。
