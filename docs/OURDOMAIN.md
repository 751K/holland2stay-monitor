# OurDomain — 平台状态

OurDomain 的抓取现状：端点长什么样、反爬是什么、代码怎么应对、自动预订卡在哪。
实现以 [`scrapers/ourdomain.py`](../scrapers/ourdomain.py) 为准，本文档描述的是
那份实现所依赖的外部事实。

---

## 1. 平台概况

| 项 | 值 |
|---|---|
| 官网 | `https://www.thisisourdomain.nl`（Webflow） |
| PMS | RENTCafe by Yardi（SecureRC） |
| 数据形态 | HTML table，两阶段 GET |
| 监控粒度 | 单元级（具体房间号 #6045，含面积/楼层/朝向） |
| 传输方式 | curl_cffi + **TLS 指纹轮换**（不走浏览器） |

**两栋楼是两个独立的 RENTCafe property，各自一个 host**：

| city_key | host | property_id |
|---|---|---|
| `diemen` | `thisisourdomain.securerc.co.uk` | `184283` |
| `south-east` | `southeast-thisisourdomain.securerc.co.uk` | `182801` |

每栋楼是一个独立的 `ScrapeTask`。楼盘规模很小，单栋常年只有个位数可订单元。

---

## 2. 反爬现状

OurDomain 是三个平台里唯一**不需要浏览器**的：SecureRC 只做 WAF 级 403，没有
托管挑战。但它有自己的麻烦——per-fingerprint + per-IP 跟踪。

### 2.1 403 的两个维度

| 维度 | 表现 | 解法 |
|---|---|---|
| TLS 指纹 | 同一指纹短时间重复打就进「挑战中」，返回 403 + `Just a moment...` | 换指纹 |
| 出口 IP | 同一 IP 被盯上时，**四个指纹轮完仍全是 403** | 换 IP |

第二条是关键：换指纹只在换 IP 的前提下才有效。所以 `scrape()` 的重试循环里
**每次尝试都重新取一次代理**，且用 `rotating=True`：

```python
proxy = get_proxy_url(self.source, rotating=True)
```

这与 H2S / Xior 相反——那两个需要出口 IP 稳定才能复用 CF clearance，OurDomain
没有 clearance 可复用，换 IP 才是它的恢复手段。这一点踩过坑：把它固定到专属
sticky IP 之后，同一个 IP 被盯上时四个指纹轮完全部 403，无法自愈。

### 2.2 指纹池与冷却

默认池 8 个，覆盖 4 个家族 × 桌面/移动（`_DEFAULT_IMPERSONATES`）。选它们的
理由是**扩大可用指纹空间**：同家族同平台的 JA3/JA4 和 h2 settings 差异很小，
混进 Safari / Firefox / 移动端，某个家族被烧时还有完全不同的回路可走。

排除：老版本（chrome99–110，已被标「可疑/过时浏览器」）、`tor145`（触发高强度
挑战）、带 `beta` 后缀的不稳定版本。

进程级状态机（`_FINGERPRINT_STATE`，重启清空）：

- 成功 → 记 `last_good_at`，并**清除** cooldown
- 403 → 记 `cooldown_until = now + 30min`
- 排序：`last_good`（未冷却的）→ `fresh` → `cooldown 中的兜底`

冷却中的指纹必须先从 `last_good` 桶里排除再排序——否则「成功过 + 正在冷却」的
指纹会同时落进两个桶，去重时保留首次出现的位置，反而被排到最前，冷却对最该
冷却的那个指纹完全失效。

单轮尝试次数由 `OURDOMAIN_WAF_RETRIES` 控制（默认 4，上限 8）；指纹顺序可用
`OURDOMAIN_IMPERSONATES` 覆盖。

### 2.3 同 session 内 403 重试

第一次 403 时 Cloudflare **同时下发了 `cf_clearance` cookie**。curl_cffi 不跑 JS
算不出最终 token，但 cookie 已经攒在 session 上——很多时候短等 2s 后第二次 GET
同 URL 就直接过了（CF 见到部分 cookie 会放宽到 light challenge）。

所以拿到 CF 类 403 先在同 session 内重试**一次**，仍失败才抛 `BlockedError` 让
上层换指纹。稳态下一个指纹就能稳定服务，这一步省下大量换指纹开销。

### 2.4 只用 GET

POST 会触发 403。两个数据端点都是 GET。

---

## 3. 端点契约

### 3.1 阶段一：floorplans.aspx —— 发现 FP ID

```
GET {base}/{slug}/floorplans.aspx
```

提取 floorplan ID，两种格式二选一（不同楼用不同主题）：

| 格式 | 出现在 | 用于 |
|---|---|---|
| `subPointerId=NNNN` | photo gallery 的 onclick | Diemen |
| `myFloorPlanId=NNNN` | Get Notified / Contact Us 链接 | South-East |

顺带解析 `{fp_id: fp_name}`，只作为 Occupancy 推断的兜底。可用来源是每个 FP 的
anchor：

```
onclick="showDialog('Floor Plan Executive Studio | Furnished | Contract 1-5 years',
                    'photogallery', 'imagetype=floorplan&...&subPointerId=1106316&...');"
```

第一个参数是干净的 FP 名，同一 onclick 里的 `subPointerId` 是它的 ID，两者强耦合。

> 试过两条走不通的路：`#FFloorPlan` dropdown 的 checkbox label 是 JS hydrated 的，
> curl_cffi 看不到；anchor 的 `title` 属性在生产 server-side HTML 里全是 `"1"` /
> `"Max Rent"` 等占位文本，不含 FP 名。

**FP 级别的按钮状态不可靠**——实测 `contactButton`（"Get Notified"）的 FP 在单元级
仍然全部 "Available"。判定必须以单元级为准。

### 3.2 阶段二：availableunits —— 获取单元

```
GET {base}/rcLoadContent.ashx
    ?contentclass=availableunits
    &floorPlans={fp_id}
    &MoveInDate={YYYY-MM-DD}
    &myolePropertyID={property_id}
```

`MoveInDate` 取下个月 1 号。实测 5 月到 9 月的不同日期返回相同单元，所以只查一个
日期；如果未来发现结果随日期显著变化，再扩成两个日期并行。

每单元一行：

```html
<tr id="unitrow_307195" data-selenium-id="urow1">
  <th data-selenium-id="Apt1"  id="307195">#6045</th>
  <td data-selenium-id="SqFt1" data-label="Sq.M.">22</td>
  <td data-selenium-id="Rent1">€ 1.587</td>
  <td data-selenium-id="Deposit1">€ 2.622</td>
  <td data-selenium-id="Amenity1">
    <label>Ground Floor</label>
    <label>Courtyard View</label>
  </td>
  <td data-selenium-id="AvailDate1">
    <span class="text-success">Available</span>     <!-- 或 text-warning / Wait List -->
  </td>
  <td data-selenium-id="Action1">
    <input value="Book now" onclick="ApplyNowClick('307195','1107060','184283','6-6-2026',...)" />
  </td>
</tr>
```

**字段提取要双策略**：两栋楼用了不同的 RentCafe 主题，`data-selenium-id` 编号
不一致。先按 selenium-id 精确匹配，不中再按 `data-label`（用户可见列标题：
`Sq.M.` / `Apartment` / `Rent` / `Deposit` / `Amenities` / `Availability`）兜底——
后者跨主题更稳定。核心字段全空时会打一条 WARNING，方便回看哪些主题还需要加
label 兜底。

`available_from` 从 `ApplyNowClick(...)` 的 `DD-MM-YYYY` 参数提取。

### 3.3 单元会重复出现在多个 FP 下

同一个物理单元（如 #6045，ID `307195`）会出现在该楼**所有** FP 的查询结果里——
FP 是合同类型标签，单元才是物理房间。`_merge_unit()` 按 `unit_id` 去重，只收集
FP 标签不重复建 Listing。

推论：**`floorPlans` 过滤器不可信**，不能靠 FP→unit 映射判断单元类型。所以
Occupancy 用 `sqft` 反推（< 30 m² → One，30–60 → Two，≥ 60 → Family），FP 名
只在 sqft 缺失时兜底。

---

## 4. 状态映射

| `<span>` class / 文本 | 含义 | 映射 |
|---|---|---|
| `text-success` / `Available` | 可预订 | `Available to book` |
| `text-warning` / 含 `wait` | 等位中 | `Available in lottery` |
| 其它 / 行不存在 | 已租 | `Occupied` |

Wait List 仍算可预订（用户可能愿意等），与 `Listing.is_available` 的语义一致。

单元级租金是**单值**（`€ 1.587`），不是 FP 级的范围，`parse_float` 直接可用。

---

## 5. Listing 映射

```python
Listing(
    id             = f"od_{unit_id}",              # "od_307195"
    name           = "Diemen #6045",               # 楼栋短名 + 房号
    status         = <见 §4>,
    price_raw      = "€ 1.587",
    available_from = "2026-06-06",
    features       = [
        "Unit: #6045",
        "Building: Amsterdam Diemen",
        "Address: Dalsteindreef, 1112 XJ Diemen",          # 建筑级真实街道地址
        "Type: Studio",
        "Area: 22 m²",
        "Occupancy: One",                                  # 由 sqft 反推
        "Floor: 0",
        "Deposit: € 2.622",
        "Detail: Ground Floor, Courtyard View",
        "Floorplans: 1107060, 1106316",
    ],
    url            = f"{base}/{slug}/floorplans.aspx",
    city           = "Amsterdam Diemen",
    source         = "ourdomain",
)
```

`Address` 是**建筑级的真实街道地址**（写死在 `BUILDINGS` 里），供 geocode 用——
unit 名 "Diemen #6045" 是内部单元号，不可 geocode。同栋楼所有单元共享一个 pin，
符合物理事实。

`Occupancy` 用与 H2S 相同的词汇表（`One` / `Two (only couples)` /
`Family (parents with children)`），Web 端的 Occupancy 多选过滤器因此能跨 source
自然合并。楼层由 `parse_ourdomain_floor()` 从 Detail 提取（`Ground Floor` → 0，
`Floor 1-4` → 1）。

---

## 6. 风险

| 风险 | 现状 |
|---|---|
| SecureRC 上托管挑战 | 尚未发生。真发生则需和 H2S / Xior 一样改走浏览器传输层 |
| 403 频率上升 | 已在缓解范围内（每次尝试换 IP + 指纹池 + 同 session 重试）；实测每小时几次，指纹轮换后恢复 |
| `data-selenium-id` 变更 | 那是 Yardi 的测试锚点，删除代价大；且已有 `data-label` 兜底 |
| 新主题 / 新字段名 | 核心字段全空会打 WARNING，据此再加 label 兜底 |
| 楼盘规模小 | 固有——单栋常年个位数可订单元。开新楼时加一条 `BUILDINGS` 记录即可 |

---

## 7. 自动预订（2026-08-04 侦察重写）

实现在 [`bookers/rentcafe.py`](../bookers/rentcafe.py) 的 `OurDomainBooker`。
`monitor._AUTO_BOOK_SOURCES` 里**没有** `ourdomain`，这条链路对用户是关的。

### 7.1 结论：和 Xior 是同一套，只有入口不同

本文档此前记的「多步 ASP.NET 表单流未探明」已经过时。实测下来 OurDomain 和
Xior 跑的是**同一套 RENTCafe，契约逐字相同**：

| | Xior | OurDomain（两栋楼都测了） |
|---|---|---|
| 条款页验证码 | 标准 v3 `api.js` | 同 |
| v3 sitekey | `6LcjBc4U…bzl` | 同 |
| action | `start_application` | 同 |
| 回退标志字段 | `failed-captcha-3-rentable` | 同 |
| v2 sitekey | `6LfAdx8T…B1V0` | 同 |
| 条款表单 id / action | `termsandotheritems` → `/onlineleasing/rcformsave.ashx` | 同 |
| 登录页表单 | `Login` / `UserLogin` / `OtpOptions` / `VerifyOTP` | 同 |
| **密码登录有没有验证码** | 没有（验证码在 OTP 那条路上） | 同 |

连页面 JS 的函数名（`callReCaptchaV2Rentable` / `getCaptchaTokenRentable`）都
一样。所以 [`docs/XIOR.md`](XIOR.md) §8 里那一整套侦察结论——会话层、登录的四个
坑、验证码契约、申请表字段由页面驱动、证件上传接口、成败判据、锁定发生在付款
那一步——**对 OurDomain 同样成立**，不再重复。

`captcha/rentcafe_pages.py` 里 `oleapplication` 和 `termsandotheritems` 分列两行
而不是做别名：那张表的用途是记录「哪一页实测是什么样」，合并会把「两页碰巧
一致」写成「本来就是一页」，Yardi 哪天只改其中一边就看不出来了。

### 7.2 唯一的差异：入口

**Xior** 的 `applyOnlineURL` 直达 `oleapplication.aspx`，单元预填在 URL 里，
选房发生在**登录之后**。

**OurDomain** 的条款步骤是独立页，选房发生在**登录之前**，而且入口要自己打出来：

```
① GET  {base}/{slug}/floorplans.aspx                    建会话 + 轮换指纹
② GET  {base}/rcLoadContent.ashx?contentclass=availableunits&…
                                                        拿「Book now」的参数
③ POST {base}/{slug}/termsandotheritems.aspx
        isViaForm=1&UnitID=…&FloorPlanID=…&myOlePropertyId=…&MoveInDate=…&src=
                                                        单元上下文进服务端会话
```

`ApplyNowClick` 的第 5 个参数就是 ③ 的目标页（`termsandotheritems.aspx`），
和 Xior `ContinueClick` 的第 6 参数同构——**位置不同，写错不会报错**，只会带着
一组错位的参数去提交。所以两个平台各有一个解析函数，不做「带开关的通用解析」。

③ 之前 vs 之后（实测）：

```
裸 GET termsandotheritems.aspx      POST 之后
  UnitId        = '0'                 UnitId        = '211053'
  FloorplanId   = '0'                 FloorplanId   = '1113962'
  QuotedRent    = '0'                 QuotedRent    = '1138.00'
                                      QuotedRentEncr= 'MTEzOC4wMA%3d%3d-RquIlWlxLKs%3d'
```

**只发那五个参数，不回传 QuotedRent。** 报价由服务端自己算并签名，塞旧价格
轻则被拒、重则用一个过期价格建申请。

单元参数**每次现查、不持久化**。存下来的 `FloorPlanID` / `MoveInDate` 会过期，
而且存量参数回答不了「这个单元现在还在不在」——现查那张表等于顺手做了一次
竞争检测，行没了就是 `race_lost`，上层去试备选房源。这和 Xior 那边 `find_unit`
做的是同一件事，只是数据源不同。

### 7.3 还差什么

| 项 | 状态 |
|---|---|
| ①②③ 入口段 | **已实测跑通**（2026-08-04，真实单元，条款表单 18 个字段全部落位） |
| ④ Start Application | 未跑（要花验证码钱、会在服务端建 prospect） |
| ⑤ 登录后单元上下文还在不在 | **未验证**——OurDomain 这条路上没有选房页，掉出流程就没有 Xior 那样的重选入口。代码里的处理是：核对是不是 Applicant Info → 不是就重 POST 一次 Book now → 还不是就明确报错中止 |
| 申请表字段 | 未见过。`rentcafe_applicant.TEXT_FIELDS` 里 `STU_*` 三项是 Xior 自定义的，OurDomain 多半没有——缺了会抛 `FormShapeChangedError`（**这是对的**，宁可中止也不提交缺项申请） |
| 账号是一套还是一栋楼一套 | **未验证**。两栋楼是两个 securerc 主机，cookie 不跨主机，按 Xior 的经验很可能要分开。当前用面板已有的单对 `ourdomain_email`/`ourdomain_password`，验出问题再照 `xior_accounts` 拆 |

边界和 Xior 一样停在 **Save（存草稿）**：再往前是 `ApplicationCharges`，要填
IBAN / SWIFT，代填金融凭据是硬限制。`draft_saved` **不代表抢到房**。
