# Xior 监控 — 设计文档

> 2026-05-22 侦察结论：Xior 使用 WordPress + Yardi (RENTCafe) 后端，房间数据通过 AJAX JSON 返回，**无反爬、无需浏览器**。三个平台里最容易抓。

---

## 1. 平台概况

| 项 | 值 |
|---|---|
| 官网 | `https://www.xiorstudenthousing.eu` (WordPress) |
| PMS | Yardi / RENTCafe（与 OurDomain 相同） |
| 真实数据端点 | `POST /wp-admin/admin-ajax.php?action=yardi_room_availability` |
| 监控粒度 | **单元级**（具体房号 M1.30.53，精确面积/租金/押金） |
| 覆盖范围 | NL 25 栋 + BE 32 栋 + DE/DK/ES/PT/PL/SE（总数 100+） |
| API 反爬 | **Turnstile 不验证服务端**（空 token 返回完整数据） |
| 数据格式 | **JSON**（不是 HTML 解析，不是 GraphQL） |

---

## 2. 技术验证

### 2.1 Cloudflare 现状

Xior **有 Cloudflare**（`server: cloudflare`，`cf-ray` 响应头），但主页和 AJAX 端点为 `cf-cache-status: BYPASS`——没有 JS Challenge，不会弹出 `Just a moment...` 拦截页。

**反爬措施：IP 级频率限制。** 实测结论：

| 测试 | 结果 |
|---|---|
| 10 连发 (无延迟) | 全部 200 ✅ |
| 50 连发 (无延迟) | 32/50 失败 (429) ⚠️ |
| 冷却 30s 后重试 | 仍 429 ⚠️ |
| `curl_cffi` + TLS 指纹池 | 同 IP 仍 429（CF 按 IP 限流） |

**结论：Cloudflare WAF 做 IP 级 429 限流，阈值约 15–20 req/窗口。** 正常监控（~125 req/轮，2s 间隔）在阈值内。但需要：

- `curl_cffi`（伪装浏览器 TLS——plain `requests` 库可能更早触发限流）
- 请求间 1–2s 延迟
- 429 退避重试（复用 `scrapers/base.py` 的 `RATE_LIMIT_BACKOFF`）
- 可选 `HTTPS_PROXY` 多出口轮换（IP 池分摊请求量）

不是"无反爬"，是"无 JS Challenge + 无 reCAPTCHA"。比 OurDomain 简单，比 H2S 相当。

### 2.2 Turnstile 不验证

Yardi modal JS 里集成了 Cloudflare Turnstile：

```javascript
window.turnstile.render($tsEl[0], {
    sitekey: ...,
    action: 'yardi_room_availability',
    callback: window.onYardiTsSuccess,
});
```

但服务端**不验证 token**。实测结果：

| 请求 | 结果 |
|---|---|
| 不带 `cf-turnstile-response` | `{"success":true, "data":{...}}` ✅ |
| `cf-turnstile-response: ""` | `{"success":true, "data":{...}}` ✅ |
| `cf-turnstile-response: "dummy"` | `{"success":true, "data":{...}}` ✅ |

三组返回完全一致。Turnstile 仅在前端挡普通用户——后端是开放的。

### 2.3 数据端点

```
POST https://www.xiorstudenthousing.eu/wp-admin/admin-ajax.php
Content-Type: application/x-www-form-urlencoded

action=yardi_room_availability
property_page_id=1126     ← 楼的 WP page ID（从 building 页 HTML 提取）
room_type_id=33944        ← 房型 ID（从 Yardi modal 的 <input> 提取）
semester_id=3281          ← 学期 ID（从 Yardi modal 的 hidden input 提取）
```

### 2.4 响应结构

```json
{
  "success": true,
  "data": {
    "units": [
      {
        "propertyId": 185845,
        "floorplanId": 1111471,
        "floorplanName": "Essential (Second - Fifth floor)",
        "apartmentId": 402419,           ← 单元 ID
        "apartmentName": "M1.30.53",     ← 房号
        "beds": 1,
        "baths": 0,
        "sqm": 19,                       ← 精确面积
        "minimumRent": 417,
        "maximumRent": 580,
        "deposit": 0,
        "availableDate": "01/07/2026",
        "unitStatus": "Notice Unrented", ← 状态
        "applyOnlineURL": "https://..."  ← 直接预订链接
      }
    ],
    "total": 2,
    "selected_room": {
      "type": "Comfy",
      "price_from": "601",
      "price_max": "",
      "sqm": "17-33",
      "beds": "1",
      "baths": "1"
    }
  }
}
```

**当 `units` 为空数组时 → 该房型当前无房。**
**当 `units` 包含条目时 → 有可预订单元。**

---

## 3. 实时数据快照（2026-05-22）

### Maastricht Annadal（唯一有房的荷兰楼）

| 单元 ID | 房号 | 面积 | 月租 (min–max) | 押金 | 入住 | 状态 |
|---|---|---|---|---|---|---|
| 402419 | M1.30.53 | 19 m² | €417–€580 | €0 | 2026-07-01 | Notice Unrented |
| 402460 | M1.50.01 | 29 m² | €417–€580 | €0 | 2027-01-02 | Vacant Unrented Not Ready |

### 其他楼（无房示例 — Eindhoven Kronehoefstraat）

| 房型 ID | 类型 | 价格 | 面积 | 单元数 |
|---|---|---|---|---|
| 33944 | Comfy | €601+ | 17–33 m² | 0 |
| 33945 | Comfy (Balcony) | €661+ | 19 m² | 0 |
| 33946 | Comfy (Entresol) | €635+ | 21–32 m² | 0 |

---

## 4. 三阶段抓取流程

### 阶段 1：发现建筑

从城市页（如 `/netherlands/eindhoven/`）提取所有建筑 URL：

```
/netherlands/eindhoven/kronehoefstraat-student-accommodation/
```

从每个建筑页提取 `window.xior`：

```javascript
window.xior = {
  "wp_building_id": 1126,
  "building_code": "p0196467",
  "building_name": "Kronehoefstraat",
  "country": "NL",
  "city": "Eindhoven",
  "booking_engine": "yardi"
};
```

### 阶段 2：提取房型 ID

从建筑页的 Yardi modal 中提取 `property_page_id`、`semester_id`、房型 ID 列表：

```html
<input type="hidden" name="semester" value="3281">
<label class="modal-room-card">
  <input type="radio" name="room_type" value="comfy" data-room-id="33944">
  <span class="modal-room-card-title">Comfy</span>
</label>
```

房型去重：部分建筑同一 `data-room-id` 以 radio + hidden input 两种形式出现两次——`set()` 去重即可。

### 阶段 3：获取单元

```python
for room_id in room_type_ids:
    resp = session.post(ajax_url, data={
        "action": "yardi_room_availability",
        "property_page_id": str(property_page_id),
        "room_type_id": str(room_id),
        "semester_id": str(semester_id),
    })
    data = resp.json()
    for unit in data["data"]["units"]:
        # Map to Listing
```

---

## 5. Listing 映射

```python
Listing(
    id          = f"xr_{unit['apartmentId']}",       # "xr_402419"
    name        = f"{building_name} {unit['apartmentName']}",
    status      = "Available to book",                 # 有 unit 即 Available
    price_raw   = f"€{unit['minimumRent']}–€{unit['maximumRent']}",
    available_from = _normalize_date(unit["availableDate"]),  # "2026-07-01"
    features    = [
        f"Unit: {unit['apartmentName']}",
        f"Area: {unit['sqm']} m²",
        f"Beds: {unit['beds']}",
        f"Deposit: €{unit['deposit']}",
        f"Floorplan: {unit['floorplanName']}",
        f"Building: {building_name}",
    ],
    url         = unit["applyOnlineURL"] or building_url,
    city        = city_display,
    source      = "xior",
)
```

### 状态映射

| `unitStatus` | 含义 | 初步映射 |
|---|---|---|
| `Notice Unrented` | 租约通知期内、未出租（到期才空出） | `"Available to book"` |
| `Vacant Unrented Not Ready` | 尚未准备好（远期可预订） | `"Available in lottery"` |
| 其它 / `units` 为空 | 完全无房 | `"Occupied"`（fail-closed） |

> ⚠️ 初步映射只是第一步。WP `yardi_room_availability` feed **会产生假阳性**，
> 实际可订状态还要过下面两道校验闸（`_to_listing` 内实现）。

### 可用性校验（2026-06-04 修订）

WP feed 的「可订」并不等于「现在真能抢」，实测两类假阳性：

1. **远期单元**：`Notice Unrented` 的 `availableDate` 可能在一年多以后（现住户
   还没搬走）。生产实测见过 `2027-07-01` 的单元被报成「现在可订」。
2. **滞后/已订走**：feed 比 RENTCafe 实时库存更新慢，单元已被订走仍列在 feed，
   用户点 `applyOnlineURL` 进去发现「没了」。

对应两道闸（仅作用于映射成「可订/可抽签」的单元；下调时降级为 `Occupied`，
仍留库 → 日后重新满足条件会触发 `Occupied→可订` 的状态变更通知）：

| 闸 | 信号源 | 规则 | 失败策略 |
|---|---|---|---|
| **① 可用日期窗口** | WP feed 单元级 `availableDate` | 距今 **> 60 天**（`_AVAILABLE_HORIZON_DAYS`）→ 降级 | 日期缺失/不可解析 → **不降级**（保守） |
| **② floorplans.aspx 权威校验** | RentCafe OLE `floorplans.aspx` | 单元 `floorplanId` 不在「真正可订」户型集合 → 降级 | 抓不到（网络/CF/非200）→ **fail-open** 信 feed |

**floorplans.aspx 权威信号**（curl_cffi 直取，HTTP 200，无 CF challenge）：
每个户型 tile 二选一——

- `(Available)` + `<button class="applyButton" … floorPlans=<id>>` → **真能订**
- `(Contact for Availability)` + `<button class="contactButton" data-function='contactUsLink'>` → **订不了**（点了只弹「联系我们」对话框，正是用户遇到的"点进去没了"）

join key：WP 单元的 `floorplanId` == floorplans.aspx 的 `floorPlans=<id>`。

**性能**：仅当某栋楼存在「窗口内候选可订单元」时，才额外 GET 一次该栋的
floorplans.aspx（URL 由 `applyOnlineURL` 推导）；绝大多数轮次 0 候选 → 零额外
请求。相关函数：`_floorplans_url()` / `parse_bookable_floorplan_ids()` /
`_fetch_bookable_floorplan_ids()` / `XiorScraper._verify_bookable_floorplans()`。

### 租金处理

`minimumRent`/`maximumRent` 范围。`price_value` 取最低价（与 OurDomain 一致，`parse_float` 直接可用）。

---

## 6. 与 H2S / OurDomain 对比

| | H2S | OurDomain | **Xior** |
|---|---|---|---|
| 数据格式 | GraphQL JSON | HTML table | **AJAX JSON** |
| HTTP 客户端 | curl_cffi Chrome | curl_cffi Safari | **curl_cffi**（IP 限流需指纹池） |
| CF 绕过 | TLS 指纹 + 代理 | TLS 指纹 (Safari) | **TLS 指纹（轻量，无 JS Challenge）** |
| 反机器人 | 无 | reCAPTCHA v3+v2 | Turnstile **(不验证)** |
| 单元级 | ✅ 单元 | ✅ 单元 | ✅ 单元 |
| 精确面积 | ✅ | ✅ | ✅ |
| 精确租金 | ✅ 单值 | ✅ 单值 | ✅ min/max |
| 预订链接 | ❌ | ❌ | ✅ applyOnlineURL |
| 翻页 | 有 | 无 | 无 |
| 每轮请求数 | N×城市 | 9 (1+8) | **N 个房型**（~4-6/栋） |
| 覆盖 | NL 26 城市 | Amsterdam 2 栋 | **欧洲 100+ 栋** |
| 实现难度 | 已实现 | ~8h | **~4h**（最简单） |

---

## 7. 实现设计

### 7.1 新文件

```
scrapers/xior.py    # XiorScraper(AbstractScraper)
```

依赖：`curl_cffi` + `re`（标准库）。同 H2S/OurDomain 共享 HTTP 策略。

### 7.2 Scraper 结构

```python
class XiorScraper(AbstractScraper):
    source = "xior"
    AJAX_URL = "https://www.xiorstudenthousing.eu/wp-admin/admin-ajax.php"

    BUILDINGS = {
        "eindhoven-kronehoefstraat": {
            "url": "https://.../kronehoefstraat-student-accommodation/",
            "display": "Eindhoven Kronehoefstraat",
            "property_page_id": 1126,
            "semester_id": 3281,
            "room_type_ids": [33944, 33945, 33946],
        },
        # ... more buildings
    }

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        bldg = self.BUILDINGS[task.city_key]
        all_units: dict[str, dict] = {}

        for room_id in bldg["room_type_ids"]:
            data = _post_ajax(bldg["property_page_id"], room_id, bldg["semester_id"])
            for unit in data.get("units", []):
                all_units[unit["apartmentId"]] = unit

        listings = [_to_listing(u, bldg) for u in all_units.values()]
        return ScrapeResult(task, listings, complete=True)
```

### 7.3 建筑发现（自动化 vs 手动）

**方案 A：手动维护 BUILDINGS 字典（推荐起步）**

荷兰 25 栋楼的 `property_page_id`、`semester_id`、`room_type_ids` 首次手动提取，存入 `BUILDINGS`。后续变更少（楼不会每天变）。

**方案 B：自动化发现**

从城市页爬取建筑 URL → 访问建筑页 → 提取 `window.xior` + Yardi modal → 解析 `property_page_id` 等字段。实现简单但每轮多 25+ HTTP 请求。

建议先用方案 A 跑荷兰核心城市，稳定后加方案 B。

### 7.4 ID 前缀

```python
listing.id = f"xr_{apartment_id}"  # "xr_402419"
```

### 7.5 请求量

```
每栋楼：N 个房型 × 1 个 POST = ~4–6 req/building
25 栋 NL：~100–150 req/轮
```

即使 25 栋楼全抓，每轮 150 个 POST，无 CF/反爬限制，5 分钟内轻松完成。

---

## 8. 通知模板

```
🏠 Xior Maastricht Annadal
M1.30.53 (Essential 2nd–5th floor) 可预订
€417–€580/月 | 19 m² | Dep: €0 | 入住: 2026-07-01
```

每单元含 `applyOnlineURL` 直达预订链接——通知里的链接直接是 RENTCafe 预订页。

---

## 9. 风险与限制

| 风险 | 可能性 | 缓解 |
|---|---|---|
| Turnstile 未来强制验证 | 中 | 可升级为 Turnstile 解决服务（与 capsolver 同供应商），或回退到直接 RENTCafe URL |
| semester_id 变更 | 低（学期每年轮换） | 楼 page HTML 自带当前值，自动提取即可 |
| 新楼 / 删楼 | 中 | Web 面板加城市勾选，用户控制 |
| admin-ajax.php 被加固 | 低 | 可回退到 RENTCafe 直接 URL（floorplans.aspx，同 OurDomain） |

---

## 10. 工程量

| 任务 | 时间 |
|---|---|
| `XiorScraper` 实现 | 2 小时 |
| 荷兰 25 栋楼数据录入 | 1 小时 |
| 单元测试 | 0.5 小时 |
| `--test` 模式验证 | 0.5 小时 |
| 注册 + 接线 | 0.5 小时 |
| **合计** | **4–5 小时** |

三个平台里最快的一次集成——纯 JSON、CF 仅做 IP 限流（无 JS Challenge）、无翻页。

---

## 11. 自动预订（Auto-Book）可行性分析

> **2026-06-04 更新**：重新实测 `register.aspx`、`guestlogin.aspx`、`flexregistrationlandingpage.aspx`，发现 RENTCafe **全线已上 reCAPTCHA Enterprise (v3 + v2 fallback)**，2026-05-22 的"注册无 reCAPTCHA"结论已过时。本章已根据最新实测结果全面修订。

### 11.1 预订入口

Xior 的 AJAX 响应中每个 unit 自带 `applyOnlineURL`，直达 RENTCafe 预订页：

```
https://brouwersweg-xiorstudenthousing.securerc.co.uk/onlineleasing/
  nlmannas-brouwersweg-100-maastricht-203/oleapplication.aspx
  ?stepname=RentalOptions
  &myOlePropertyId=185845
  &floorPlans=1111471
  &UnitTypeId=34430
  &ATId=3281
  ...
```

**优于 OurDomain**：URL 含所有预填参数，跳过选房步骤，直达条款页。

### 11.2 预订流程（P2 手动侦察，2026-05-22 + 2026-06-04 更新）

```
Step 1 — Xior 建筑页 → Yardi modal（WordPress 路径）
  点 "Check Availability" → Cloudflare Turnstile 验证（WordPress 层）
  → 选房型 → 选单元 → 跳转 RENTCafe

Step 2 — oleapplication.aspx（Rental Options）
  侧边栏完整暴露 9 步流程：
    1. Floorplan              ← 已选
    2. Rental Options         ← 当前步骤，点 "Start Application"
    3. Applicant Info         ← 个人信息，需登录/注册
    4. Additional Applicants
    5. Additional Rental Options
    6. Applicant Charges
    7. Lease Summary
    8. Lease Creation         ← 最终签约（此处有 reCAPTCHA）
    9. (Review/Confirm)
  前 2 步已完成（房型+租金选项），第 3 步开始需登录/注册。

Step 3 — Registration / Login
  注册 (register.aspx):
    - 表单字段: FirstName, LastName, Email, Password, BirthDate, Phone
    - ⚠️ reCAPTCHA v3 Enterprise（sitekey: 6LfBeqEa...，action: GuestRegistration）
    - 如果 v3 分数不够 → 回退到 v2 checkbox（sitekey: 6LfAdx8T...）
    - 表单提交到: POST /onlineleasing/rcformsave.ashx
    - reCAPTCHA badge 隐藏（invisible 模式）

  登录 (guestlogin.aspx):
    - ⚠️ 同样有 reCAPTCHA v3 Enterprise（action: UserLogin）
    - 相同的 v3+v2 回退机制，相同的 sitekey
    - 表单包含 OTP 二次验证字段（SMS/邮件验证码？）
    - 之前认为 "Email → Continue" 流程——实际是 Email + Password + reCAPTCHA v3

  备用入口 (flexregistrationlandingpage.aspx):
    - ✅ 无 reCAPTCHA
    - 仅 3 个字段: formName, myOlePropertyId, cafeportalkey
    - 功能: 选择租约类型（Student Lease / Traditional Lease）
    - 可能作为绕过注册 reCAPTCHA 的入口（尚待验证）

  注意: RENTCafe IP 级 attempt-limit，连续失败锁 30 分钟

Step 4-9 — 未到达（登录后 session 重置，且当前无房可继续）
  推测条款提交 (termsandotheritems.aspx) 同样有 reCAPTCHA，与 OurDomain 一致
```

### 11.3 关键发现（2026-06-04 修订）

| | OurDomain | Xior |
|---|---|---|
| 步骤数 | 未知（未探明） | **9 步（侧边栏暴露）** |
| RENTCafe 表单 | termsandotheritems → rcformsave.ashx | 相同 |
| 条款页 reCAPTCHA | v3+v2，硬校验 | v3+v2，硬校验 |
| 注册页 reCAPTCHA | ✅ 有 | **⚠️ 也有（2026-06 新增）** |
| 登录页 reCAPTCHA | ✅ 有 | **⚠️ 也有（v3 Enterprise）** |
| 登录方式 | Username + Password | **Email + Password + OTP 可选** |
| 注册页隐藏 | 未知 | **JS 隐藏了注册链接**（`$('a#ClickHereToRegisterLink').hide()`） |
| 预填参数 | 无 | ✅ oleapplication URL 含全部参数 |
| 反机器人 | reCAPTCHA | reCAPTCHA（全线 v3 Enterprise）+ **IP 级 attempt limit** |

### 11.4 reCAPTCHA 详情（2026-06-04 实测提取）

**RENTCafe 在 Xior 上使用 Google reCAPTCHA Enterprise（`enterprise.js`）**，每页两级回退：

| 属性 | 值 |
|------|-----|
| reCAPTCHA v3 sitekey | `6LfBeqEaAAAAALsbENKGUsE98xFoA3ZpqkbzogBI` |
| reCAPTCHA v2 sitekey (fallback) | `6LfAdx8TAAAAAOiesnT8CNKNtb1C6doK-RKnB1V0` |
| v3 JS 加载 | `https://www.google.com/recaptcha/enterprise.js?render=<sitekey>&hl=en` |
| 各页 action | `GuestRegistration` / `UserLogin` / 等等 |
| 表单隐藏字段 | `g-recaptcha-response-v3` (v3 token) |
|  | `failed-captcha-3` ("true"/"false"，v3 失败则渲染 v2) |
|  | `recaptchaEnterpriseFormId` |
| v2 容器 | `<div id="recaptcha-container">`，动态渲染 |

**执行流程（所有 RENTCafe 页面统一逻辑）：**

```
表单验证 ($('#form').valid())
  → grecaptcha.enterprise.execute(sitekey, {action})
    → 成功: token 填入 #g-recaptcha-response-v3
    → 失败: $('#failed-captcha-3').val() == 'false'
      → grecaptcha.enterprise.render('recaptcha-container', {sitekey: v2key})
      → 用户点击 v2 checkbox
      → token 填入 #g-recaptcha-response
```

**注意：注册链接被 Xior JS 隐藏。** 在 `oleapplication.aspx` 页面中，以下元素被 jQuery 动态隐藏：
- `$('a#ClickHereToRegisterLink').hide()`
- `$('a[href*="flexregistrationlandingpage.aspx"]').hide()`
- `$('a[href*="register.aspx"]').hide()`

这意味着 Xior 意图阻止用户在 RENTCafe 上自注册（可能统一走 WordPress 侧的注册流程），但后端接口仍然存活。

### 11.5 可行性评估（修订）

**整体结论：技术上可行，但比之前预计的多 2-3 次 reCAPTCHA 求解。**

定量的成本预估：

| 步骤 | 页面 | reCAPTCHA | 求解方式 | 耗时 | 成本 |
|------|------|-----------|----------|------|------|
| 登录 | guestlogin.aspx | v3 Enterprise | Capsolver `ReCaptchaV3TaskProxyLess` | 10-20s | ~$0.001 |
| 注册（如需） | register.aspx | v3 Enterprise | 同上 | 10-20s | ~$0.001 |
| 条款提交 | termsandotheritems.aspx | v3+v2 | v3 先试，失败则 v2 | 15-30s | ~$0.002 |
| **总计** | | | | **30-60s** | **~$0.003-0.005** |

**优势（不变）：**
- Step 1-2 结构已明确（选房 → 条款），第三步是登录/注册
- Step 4-9 的步骤名已知——不再需要猜测流程
- oleapplication URL 含全部预填参数
- reCAPTCHA sitekey 已提取，Capsolver 可直接对接

**挑战（新增）：**
- 注册和登录现在都有 reCAPTCHA v3，不再是无障碍自动化
- 注册链接被 JS 隐藏，但后端接口仍存活——直接 POST `register.aspx` 即可
- reCAPTCHA v3 是打分制，如果分数不够会触发 v2 回退（多了 ~15s + $0.001）

**待探索：**
- `flexregistrationlandingpage.aspx`（无 reCAPTCHA）能否作为绕过入口——选择租约类型后是否直接跳转到无 reCAPTCHA 的表单？
- v3 token 是否可以跨步骤复用（同一个 sitekey）？如果可以，只需 1 次求解
- 服务端对 v3 score 的阈值设得多高？如果阈值低，v3 token 几乎不会触发 v2 回退

**仍需确认：**
- 登录时的 OTP 二次验证是否强制（`guestlogin.aspx` 中的 `OtpOption` / `otpVerification` 字段）
- Step 4 Applicant Info 有哪些字段
- 中间是否有文件上传或人工审核

**下一步：** 用 Capsolver 跑一遍完整注册→登录→预订流程，验证 token 是否被 RENTCafe 服务端接受。
