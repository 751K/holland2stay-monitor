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
| `Notice Unrented` | 租约通知期内、未出租 | `Available to book` |
| `Vacant Unrented Not Ready` | 尚未准备好 | `Available in lottery` |
| 其它 / `units` 为空 | 无房 | `Occupied`（fail-closed） |

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

### 8.3 reCAPTCHA

RENTCafe **全线**使用 Google reCAPTCHA Enterprise，每页两级回退：

| 属性 | 值 |
|---|---|
| v3 sitekey | `6LfBeqEaAAAAALsbENKGUsE98xFoA3ZpqkbzogBI` |
| v2 sitekey（回退） | `6LfAdx8TAAAAAOiesnT8CNKNtb1C6doK-RKnB1V0` |
| JS | `https://www.google.com/recaptcha/enterprise.js?render=<sitekey>` |
| 各页 action | `GuestRegistration` / `UserLogin` / … |
| 隐藏字段 | `g-recaptcha-response-v3`、`failed-captcha-3`、`recaptchaEnterpriseFormId` |

统一执行逻辑：

```
表单验证 → grecaptcha.enterprise.execute(sitekey, {action})
  成功 → token 填入 #g-recaptcha-response-v3
  失败 → failed-captcha-3 == 'false' → 渲染 v2 checkbox → token 填入 #g-recaptcha-response
```

覆盖范围：注册（`register.aspx`）、登录（`guestlogin.aspx`）、条款提交
（`termsandotheritems.aspx`）都有。唯一没有 reCAPTCHA 的入口是
`flexregistrationlandingpage.aspx`（仅 3 个字段，功能是选租约类型），
能否作为绕过入口尚未验证。

另外：Xior 用 JS 隐藏了 RENTCafe 上的注册链接（`$('a#ClickHereToRegisterLink').hide()`
等），意图是让用户走 WordPress 侧注册，但后端接口仍然存活，可以直接 POST。

### 8.4 成本估算

| 步骤 | reCAPTCHA | 求解方式 | 耗时 | 成本 |
|---|---|---|---|---|
| 登录 | v3 Enterprise | Capsolver `ReCaptchaV3TaskProxyLess` | 10–20s | ~$0.001 |
| 注册（如需） | v3 Enterprise | 同上 | 10–20s | ~$0.001 |
| 条款提交 | v3 + v2 | v3 先试，失败回退 v2 | 15–30s | ~$0.002 |
| **合计** | | | **30–60s** | **~$0.003–0.005** |

RENTCafe 还有 IP 级 attempt limit，连续失败锁 30 分钟——自动化重试要非常克制。

### 8.5 未确认

- `flexregistrationlandingpage.aspx` 选完租约类型后，是否直接跳到无 reCAPTCHA 的表单
- v3 token 能否跨步骤复用（同一 sitekey）；可以的话只需求解 1 次
- 服务端 v3 score 阈值多高——阈值低则几乎不触发 v2 回退
- 登录的 OTP 二次验证是否强制（`guestlogin.aspx` 的 `OtpOption` / `otpVerification`）
- 第 4 步 Applicant Info 的字段清单，以及中途是否有文件上传或人工审核
