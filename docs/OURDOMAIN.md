# OurDomain 监控 — 设计文档

> 2026-05-22 实测验证。监控粒度从 FP 级升级为**单元级**——满足按价格/面积/楼层/朝向过滤的需求。

---

## 1. 平台概况

| 项 | 值 |
|---|---|
| 官网 | `https://www.thisisourdomain.nl` (Webflow) |
| PMS | RENTCafe by Yardi（SecureRC） |
| 监控粒度 | **单元级**（具体房间号 #6045，含面积/楼层/朝向） |
| 覆盖楼宇 | Amsterdam Diemen + South-East（同一 RENTCafe property ID 184283） |
| 物理单元数 | **8 个**（共享于 8 个 FP 类型之下） |
| 每个单元字段 | 单元号、面积（单值 m²）、月租（单值 €）、押金、楼层、朝向、可入住日期 |

---

## 2. 架构：两阶段抓取

### 阶段 1：floorplans.aspx — 发现 FP ID

```
GET https://thisisourdomain.securerc.co.uk/onlineleasing/
    ourdomain-amsterdam-diemen/floorplans.aspx
```

提取 8 个 FP ID（`subPointerId=N`），供阶段 2 使用。FP 级别的按钮 class/text **不可靠**——实测 `contactButton`("Get Notified") 的 FP 在单元级仍然全部 "Available"。

### 阶段 2：availableunits — 获取单元

```
GET https://thisisourdomain.securerc.co.uk/onlineleasing/
    rcLoadContent.ashx
    ?contentclass=availableunits
    &floorPlans=1107060          ← FP ID（可逗号分隔多个）
    &MoveInDate=2026-06-01       ← 目标入住月份（YYYY-MM-DD）
    &myolePropertyID=184283
```

返回每单元一行，`data-selenium-id` 锚点完整：

```
<tr id="unitrow_307195" data-selenium-id="urow1">
  <th data-selenium-id="Apt1"  id="307195">#6045</th>            ← 单元号 + 数字 ID
  <td data-selenium-id="SqFt1">22</td>                            ← m² 单值
  <td data-selenium-id="Rent1">€ 1.587</td>                      ← 月租单值
  <td data-selenium-id="Deposit1">€ 2.622</td>                   ← 押金
  <td data-selenium-id="Amenity1">                                ← 详情浮层
    <label>Ground Floor</label>
    <label>Courtyard View</label>   ← 部分单元有多个 label
  </td>
  <td data-selenium-id="AvailDate1">                              ← 状态
    <span class="text-success">Available</span>                   ← 可预订
    <span class="text-warning">Wait List</span>                   ← 等位
  </td>
  <td data-selenium-id="Action1">
    <input value="Book now" onclick="ApplyNowClick('307195','1107060','184283','6-6-2026',...)" />
  </td>
</tr>
```

**HTTP 层：** `curl_cffi chrome131` 即可，GET 请求无需 session cookie，与 H2S 完全相同的策略。注意 POST 会触发 Cloudflare 403——一律用 GET。

---

## 3. 当前单元清单（2026-05-22，MoveInDate=2026-06-01）

```
  单元号    ID       面积   月租       押金      楼层/朝向              状态
  ─────────────────────────────────────────────────────────────────────
  #6045   307195    22m²   €1.587    €2.622    Ground Floor            Available
  #6023   307174    22m²   €1.572    €2.598    Ground Floor            Available
  #6043   307193    22m²   €1.587    €2.834    Ground Floor            Available
  #6057   307202    22m²   €1.572    €2.834    Ground Floor            Available
  #6028   307179    23m²   €1.595    €2.635    Ground Floor, Courtyard Available
  #6222   307302    23m²   €1.563    €0        Floor 1-4, Courtyard    Available
  #6171   307277    28m²   €1.647    €2.721    Floor 1-4               Available
  #6134   307250    28m²   €1.662    €2.746    Floor 1-4, Courtyard    Available
```

**统计：**

| 维度 | 范围 |
|---|---|
| 面积 | 22 / 23 / 28 m²（3 档） |
| 月租 | €1,563 – €1,662（差距仅 €99） |
| 押金 | €0 – €2,834（#6222 免押金） |
| 楼层 | Ground Floor ×6、Floor 1-4 ×3 |
| 朝向 | 3 间带 Courtyard View |

### 关键发现

1. **同一个单元出现在所有 8 个 FP 下。** 例如 #6045 在 FP 1107060 (Short Stay) 和 FP 1106316 (1-5y Contract) 都出现。FP = 合同类型标签，单元 = 物理房间。
2. **Diemen 和 South-East 是同一个 RENTCafe property（184283）**，数据完全一致。只需抓一次。
3. **MoveInDate 对结果影响很小。** 实测 2026-05-23、06-01、07-01、09-01 返回相同 8 个单元。建监控时每个 FP 只需查最近 1–2 个月。
4. **FP 级按钮状态不可靠。** "Get Notified" 的 FP 在单元级仍全部 "Available"+"Book now"。监控必须以单元级状态为准。
5. **单元级租金是单值，不是范围。** 与 FP 级 `"€1,797–€1,900"` 不同，单元级 `€1.587` 是精确值——`parse_float` 直接可用，无需特殊处理。

---

## 4. 与 H2S 的架构差异

| 维度 | H2S | OurDomain |
|---|---|---|
| HTTP 客户端 | `curl_cffi` | `curl_cffi`（相同） |
| CF 绕过 | TLS 指纹模拟 | TLS 指纹模拟（相同） |
| 数据格式 | GraphQL JSON | HTML table（两阶段：FP 列表 + 单元列表） |
| 翻页 | 有 | 无（每页 ≤8 行） |
| 抓取请求数 | 1 req/city/page | 1 (floorplans) + 8 FP × 1 = **9 req/轮** |
| 状态来源 | `available_to_book[].label` | `<span class="text-success">Available</span>` |
| 数据粒度 | 单元级（具体房间号） | 单元级（具体房间号 #6045） |
| 房源生命周期 | 出现/消失 | 状态翻转（Available ↔ Wait List） |
| 价格格式 | 单值 `"707.000000"` | 单值 `"€ 1.587"`（单元级） |
| ID 格式 | 字母 slug | 纯数字 `307195` |
| 详情页 | `residences/{url_key}.html` | 按钮 onclick 直接跳到预订流程 |
| 额外字段 | — | 单元号、楼层、朝向、入住日期、免押金标记 |

---

## 5. 实现设计

### 5.1 新文件

```
scrapers/ourdomain.py    # OurDomainScraper(AbstractScraper)
```

零新依赖（`curl_cffi` + `re`）。

### 5.2 Scraper 流程

```python
class OurDomainScraper(AbstractScraper):
    source = "ourdomain"

    BASE = "https://thisisourdomain.securerc.co.uk/onlineleasing"
    PROPERTY_ID = "184283"

    # city_key → URL slug + display name
    BUILDINGS = {
        "diemen": {"slug": "ourdomain-amsterdam-diemen", "display": "Amsterdam Diemen"},
    }

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        session = req.Session(impersonate="chrome131")

        # ── 阶段 1：获取 FP ID 列表 ──
        fp_html = session.get(f"{self.BASE}/{slug}/floorplans.aspx")
        fp_ids = re.findall(r"subPointerId=(\d+)", fp_html.text)  # 8 个

        # ── 阶段 2：对每个 FP 获取单元 ──
        all_units: dict[str, dict] = {}  # unit_id → data
        for fp_id in fp_ids:
            url = (
                f"{self.BASE}/rcLoadContent.ashx"
                f"?contentclass=availableunits"
                f"&floorPlans={fp_id}"
                f"&MoveInDate={self._next_month()}"  # 下个月 1 号
                f"&myolePropertyID={self.PROPERTY_ID}"
            )
            unit_html = session.get(url)
            for urow in re.finditer(
                r"id='unitrow_(\d+)'.*?</tr>", unit_html.text, re.DOTALL
            ):
                self._merge_unit(all_units, urow, fp_id)

        # ── 阶段 3：去重 + 构建 Listing ──
        listings = []
        for unit_id, data in all_units.items():
            listings.append(Listing(
                id=f"od_{unit_id}",
                name=f"{data['apt']} - {data['detail']}, {data['sqft']}m²",
                status=data["status"],
                price_raw=data["rent"],
                available_from=data.get("avail_date"),
                features=[
                    f"Unit: {data['apt']}",
                    f"Area: {data['sqft']} m²",
                    f"Deposit: {data['dep']}",
                    f"Detail: {data['detail']}",
                    f"Building: {task.city_display}",
                ],
                url=f"{self.BASE}/{slug}/floorplans.aspx",
                city=task.city_display,
                source=self.source,
            ))
        return ScrapeResult(task, listings, complete=True)
```

### 5.3 去重逻辑

同一个物理单元（如 #6045, ID=307195）会出现在多个 FP 下。`_merge_unit()` 收集 FP 标签但不重复创建 Listing。

```python
def _merge_unit(self, all_units, urow_match, fp_id):
    unit_id = urow_match.group(1)
    if unit_id not in all_units:
        all_units[unit_id] = self._extract_unit(urow_match)
    all_units[unit_id]["fp_ids"].append(fp_id)
```

### 5.4 字段提取

```python
def _extract_unit(self, urow):
    html = urow.group(0)
    idx = re.search(r"data-selenium-id='urow(\d+)'", html).group(1)

    apt   = re.search(rf"Apt{idx}'[^>]*>([^<]+)<", html).group(1)
    sqft  = re.search(rf"SqFt{idx}'[^>]*>([^<]+)<", html)
    rent  = re.search(rf"Rent{idx}'[^>]*>([^<]+)<", html)
    dep   = re.search(rf"Deposit{idx}'[^>]*>([^<]+)<", html)

    # 状态：span class 决定
    avail_span = re.search(rf"AvailDate{idx}'[^>]*>.*?<span[^>]*class='([^']*)'[^>]*>([^<]*)<",
                           html, re.DOTALL)
    status = "Available to book" if "success" in (avail_span.group(1) or "") else "Occupied"

    # 详情：所有 <label> 文本拼接
    details = [m.group(1).strip() for m in re.finditer(r"<label[^>]*>([^<]+)<", html)
               if m.group(1).strip() not in ("Max-Rent", "Prices and special offers...")]
    # 例：["Ground Floor", "Courtyard View"]

    # 可入住日期：从 ApplyNowClick onclick 提取
    onclick_date = re.search(r"ApplyNowClick[^)]+'(\d+-\d+-\d+)'", html)

    return {
        "apt": apt.strip(),
        "sqft": sqft.group(1).strip() if sqft else "",
        "rent": rent.group(1).strip() if rent else "",
        "dep": dep.group(1).strip() if dep else "",
        "status": status,
        "detail": ", ".join(details),
        "avail_date": onclick_date.group(1) if onclick_date else "",
        "fp_ids": [],
    }
```

### 5.5 状态映射

单元级状态比 FP 级更精确：

| `<span>` class | 含义 | 映射 |
|---|---|---|
| `text-success` → "Available" | 可预订 | `"Available to book"` |
| `text-warning` → "Wait List" | 等位中 | `"Available in lottery"`（语义最接近） |
| 其他 / 行不存在 | 已租 | `"Occupied"` |

`Listing.is_available` 检查 `status.lower() in ("available to book", "available in lottery")`，Wait List 单元仍算可预订（用户可能愿意等），语义合理。

### 5.6 MoveInDate 策略

实测不同日期（5 月到 9 月）返回相同单元。保守策略：

```python
def _next_month(self) -> str:
    """返回下个月 1 号。例：2026-06-01"""
    from datetime import date
    today = date.today()
    year = today.year + (today.month // 12)
    month = (today.month % 12) + 1
    return f"{year}-{month:02d}-01"
```

如果未来发现单元随日期显著变化，可扩展为 `[下个月, 下下个月]` 两个日期×8 FP = 16 请求/轮。

### 5.7 Listing 字段映射

```python
Listing(
    id              = f"od_{unit_id}",        # "od_307195"
    name            = "#6045 - Ground Floor, Courtyard View, 23m²",
    status          = "Available to book",
    price_raw       = "€ 1.595",              # 单值，parse_float → 1595.0
    available_from  = "2026-06-06",            # 从 ApplyNowClick 提取
    features        = [
        "Unit: #6045",
        "Area: 23 m²",
        "Deposit: € 2.635",
        "Detail: Ground Floor, Courtyard View",
        "Building: Amsterdam Diemen",
    ],
    url             = "https://thisisourdomain.securerc.co.uk/onlineleasing/"
                      "ourdomain-amsterdam-diemen/floorplans.aspx",
    city            = "Amsterdam Diemen",
    source          = "ourdomain",
)
```

### 5.8 过滤维度

与现有 `ListingFilter` 的匹配关系：

| 过滤条件 | OurDomain 数据源 | 备注 |
|---|---|---|
| `max_rent` | `€ 1.587` → `parse_float` → `1595.0` | 单值，直接可用 |
| `min_area` | `23` (m²) | `parse_float` 直接可用 |
| `min_floor` | `"Ground Floor"` / `"Floor 1-4"` | 需新增 `parse_ourdomain_floor()` |
| `allowed_cities` | `"Amsterdam Diemen"` | 与 H2S 共用 city 白名单 |
| `allowed_types` | `"Studio"` (来自 FP 元数据) | 如需户型过滤，在 feature_map 加 Beds 字段 |
| 朝向/视野 | `"Courtyard View"` 在 Detail 中 | 没有现有过滤匹配 → 可作为 `allowed_*` 扩展 |
| 押金 | `"€ 0"` / `"€ 2.622"` | 没有现有过滤匹配 → 新增 `max_deposit` 字段 |

**楼层映射函数：**

```python
def parse_ourdomain_floor(detail: str) -> int:
    """从 Detail 字符串提取最低楼层号。Ground Floor → 0。"""
    if "ground" in detail.lower():
        return 0
    m = re.search(r"Floor\s*(\d+)", detail)
    if m:
        return int(m.group(1))  # "Floor 1-4" → 1
    return None
```

### 5.9 ID 前缀化

| 源 | ID 格式 | 示例 |
|---|---|---|
| H2S | `{url_key}`（原样，不改） | `kastanjelaan-1-108` |
| OurDomain | `od_{unit_id}` | `od_307195` |

### 5.10 单 building 简化

Diemen 和 South-East 共用同一个 RENTCafe property（184283），数据完全一致。Config 只配一个 city：

```python
# config.py
ScrapeTask(source="ourdomain", city_key="diemen", city_display="Amsterdam Diemen")
```

如果未来两栋楼在 RENTCafe 拆分为不同 property，再添加第二个 ScrapeTask。

---

## 6. 通知模板

```
🏠 OurDomain Amsterdam Diemen
#6045 - Ground Floor, 22m² is now Available to book
€1,587/month | Dep: €2,622 | Move-in: 2026-06-06
```

与 H2S 通知格式一致（`status_change` / `new_listing` 走相同 pipeline）。首次抓取时 8 个单元全部触发 `new_listing`，后续仅状态变化触发 `status_change`。

---

## 7. 请求量估算

```
每轮：1 (floorplans.aspx) + 8 (FP × availableunits) = 9 GET
每次 GET：TCP 复用（同一 session），~200ms/req
每轮总耗时：< 2 秒
```

在 H2S 的 5 分钟间隔下，9 个额外请求完全不可见。即使扩展为 2 个 MoveInDate（16 请求/轮），仍在 3 秒以内。

---

## 8. 风险与限制

| 风险 | 可能性 | 缓解 |
|---|---|---|
| RENTCafe 改版 | 低 | `data-selenium-id` 是 Yardi 测试锚点，删除需改大量回归测试 |
| CF 升级 | 低 | 与 H2S 共享 curl_cffi 策略 |
| MoveInDate 未来差异化 | 中 | 当前均返回相同单元；如变化，多日期并行查询即可 |
| 单元共享于多个 FP | 已处理 | 通过 unit_id 去重 |
| 仅 8 个单元 | 固有 | OurDomain Diemen 是小型楼盘。如 Greystar 开新楼，新 property ID 加入即可 |

---

---

## 10. 自动预订（Auto-Book）可行性分析

> 2026-05-22 实测：RENTCafe 预订流程受 **reCAPTCHA v3 + v2** 保护，纯 HTTP（curl_cffi）无法突破。结论：**不可行，除非引入 Playwright。**

### 10.1 预订流程

```
选择单元 → termsandotheritems.aspx → rcformsave.ashx → 个人信息 → 审核 → 支付
```

**Step 1 — 单元选择（✅ HTTP 可行）**

`ApplyNowClick` 函数提交 ASP.NET 表单至 `termsandotheritems.aspx`：

```
POST /onlineleasing/.../termsandotheritems.aspx
Content-Type: application/x-www-form-urlencoded

isViaForm=1
UnitID=307195
FloorPlanID=1107060
myOlePropertyId=184283
MoveInDate=6-6-2026
src=
```

实测：`curl_cffi` + `safari17_0` impersonation → HTTP 200。Chrome 指纹在此路径被拦截，Safari 指纹可过。

**Step 2 — 条款页 + 表单提交（❌ reCAPTCHA 阻断）**

`termsandotheritems.aspx` 页面含 25 个隐藏字段 + reCAPTCHA v3 埋点。表单 POST 至 `rcformsave.ashx` 时：

```json
// 服务端响应
{"type": "error", "text": "Please verify that you are not a robot."}
```

同时触发 reCAPTCHA v2 显式挑战：`callReCaptchaV2Rentable()`，sitekey = `6LfAdx8TAAAAAOiesnT8CNKNtb1C6doK-RKnB1V0`。

v3 在后台打分，分数不够时降级为 v2 视觉验证。无有效 token 时表单**硬拒绝**，不走业务逻辑。

**Step 3+ — 个人信息 / 审核 / 支付**

未到达（被 Step 2 阻断）。根据页面 CSS 引用（`#applicantloginmkt`、`form#Login`）推测后续需要：
- 登录 RENTCafe 账号或创建新账号
- 填写个人信息（姓名、出生日期、联系方式、收入/工作信息）
- 审核确认
- 支付押金/首月租金

### 10.2 reCAPTCHA 绕过方案

**方案 A：reCAPTCHA 解决服务（推荐，无需 Playwright）**

第三方服务通过 HTTP API 返回有效 token：

| 服务 | 价格 | 延迟 |
|---|---|---|
| [capsolver.com](https://capsolver.com) | ~$1/1000 v3 | 1–3s |
| [2captcha.com](https://2captcha.com) | ~$3/1000 v3 | 5–15s |
| [anti-captcha.com](https://anti-captcha.com) | ~$1/1000 v3 | 2–5s |

流程：
```python
# 1. 从页面提取 sitekey
v3_sitekey = "6LcjBc4UAAAAABfXlERv_hq_KE3IWDAqbiWkbPzl"

# 2. 调解决服务获取 token
token = captcha_solver.get_recaptcha_v3_token(
    sitekey=v3_sitekey,
    url="https://thisisourdomain.securerc.co.uk/onlineleasing/.../termsandotheritems.aspx",
    action="start_application",
)

# 3. 填入表单并提交
post_data["g-recaptcha-response-v3"] = token
resp = session.post(rcformsave_url, data=post_data)
```

token 有效期约 2 分钟，足够一次表单提交。每次预订约需 2–4 次 token（条款→个人信息→审核→确认），成本 $0.01–0.04/次。

**方案 B：Playwright 浏览器（也可行，但更重）**

真实浏览器执行 reCAPTCHA JS 自然获得 token。优势是不依赖第三方服务，劣势是 ~300MB 依赖 + 资源占用高。

**结论：reCAPTCHA 不是硬障碍。** 真正的复杂度在多步表单的状态管理。

### 10.3 账号体系

| 步骤 | reCAPTCHA | 关键字段 |
|---|---|---|
| 注册 | ✅ v2（sitekey 同上） | FirstName, LastName, EmailAddress, Password, SecurityAnswer, UnitCode |
| 登录 | ❌ 无 | Username, Password, SecurityCode |
| 条款提交 | ✅ v3 + v2 降级 | 22 个 hidden fields + sMoveInDate + term(3-12月) |
| 后续步骤 | 未到达（被 captcha 阻断），推测含个人信息/审核/支付 |

**登录端点：**
```
POST /residentservices/ResidentCafeHandler.ashx
Username={email}&Password={pw}&SecurityCode=&myFormName=...&PortalNameInput=...
```

登录无 captcha——这是好消息。可以在 scraper 启动时先登录，维持 session。后续预订表单用方案 A 的 token 逐个提交。

### 10.4 真正的工程难点

即使有了 captcha 解决方案，以下问题仍在：

| 难点 | 说明 |
|---|---|
| **多步表单状态** | 每一步 POST 到 `rcformsave.ashx`（可能是不同 `contentclass`），依赖 `cafeportalkey` 等加密 token 维持 session 连续性 |
| **表单字段未知** | 已确认条款页（22 hidden + 2 visible）。条款之后的页面（个人信息等）尚未到达——字段结构靠猜测 |
| **个人信息需求** | RENTCafe 可能需要姓名、出生日期、联系方式、收入/工作信息、紧急联系人等。用户需提前在 Web 面板录入 |
| **脆弱性** | RENTCafe 页面结构变更（表单字段、步骤顺序、JS 验证逻辑）会导致预订断裂 |
| **测试困难** | 每次测试都是一次真实预订尝试（可能真的创建订单），没有 sandbox |

### 10.5 对比总结

| | H2S | OurDomain (RENTCafe) |
|---|---|---|
| HTTP 层 | GraphQL mutation + Bearer token | ASP.NET form POST + session cookie |
| 反机器人 | 无（仅 rate limit 429） | reCAPTCHA v3 + v2 → **可用解决服务绕过** |
| 解决成本 | — | $0.01–0.04/次预订 |
| 状态管理 | 无状态 | 有状态（cafeportalkey 加密 token） |
| 账号 | H2S 账号 | RENTCafe 账号（注册含 captcha → 一劳永逸） |
| 表单步骤 | 1 步 | 4+ 步 |
| 个人信息 | 已预设 | 每步填写 |
| 实现难度 | ~200 行 HTTP | **~800 行 HTTP + captcha 解决服务集成** |

### 10.6 结论

**不需要 Playwright。** reCAPTCHA 可通过第三方解决服务（capsolver/2captcha）绕过，成本可忽略。

真正的难点是**多步 ASP.NET 表单流**——目前只探明了第一步（条款页），后续步骤未到达。每条未知路径都是工程风险。建议分阶段：

| 阶段 | 内容 | 时间 |
|---|---|---|
| **P1 当前** | 监控 + 通知（已完成） | — |
| **P2 侦察** | 用真实 RENTCafe 账号手动走一遍完整预订流程，记录每步的 URL、字段、验证逻辑 | 2–3 小时 |
| **P3 实现** | 基于侦察结果实现自动预订：登录预热 + captcha 解决服务 + 表单自动填充 | 1–1.5 周 |

P2 是最关键的前置——不看到完整流程，无法准确估算 P3 工作量。如果流程中有文件上传（收入证明等）或人工审核环节，自动预订就直接不可行。

---

## 11. 工程量

| 任务 | 时间 |
|---|---|
| `OurDomainScraper`（两阶段 + 去重 + 字段提取） | 3–4 小时 |
| `parse_ourdomain_floor()` + filter 适配 | 1 小时 |
| 单元测试（HTML fixture） | 1 小时 |
| `--test` 模式验证 | 0.5 小时 |
| ID 前缀化迁移 | 1 小时 |
| monitor 接线 + `dispatch_scrape_tasks` | 1 小时 |
| 通知模板 | 0.5 小时 |
| **合计** | **8–10 小时** |

相比初版 FP 级方案（6–8 小时），多了去重逻辑和楼层解析，但换来了真正可过滤的单元数据。
