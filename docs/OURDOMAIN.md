# OurDomain — 平台状态

本文记录 OurDomain 的抓取现状：端点形态、反爬机制、可用性判定与代码的应对方式。
实现以 [`scrapers/ourdomain.py`](../scrapers/ourdomain.py) 为准，本文描述的是该实现
依赖的外部事实；平台行为可能变化，历史实测不代表当前线上状态。

自动预订部分见 [RENTCAFE.md](RENTCAFE.md)：OurDomain 与 Xior、OurCampus 共用同一套
RENTCafe 流程，本文只记录 OurDomain 特有的入口差异与验证进度。

## 1. 平台概况

| 项 | 值 |
|---|---|
| 官网 | `https://www.thisisourdomain.nl`（Webflow） |
| PMS | RENTCafe by Yardi（SecureRC） |
| 数据形态 | HTML table，两阶段 GET |
| 监控粒度 | 单元级（具体房间号 #6045，含面积/楼层/朝向） |
| 传输方式 | curl_cffi + TLS 指纹轮换（不走浏览器） |

**两栋楼是两个独立的 RENTCafe property，各自一个 host**：

| city_key | host | property_id |
|---|---|---|
| `diemen` | `thisisourdomain.securerc.co.uk` | `184283` |
| `south-east` | `southeast-thisisourdomain.securerc.co.uk` | `182801` |

每栋楼对应一个独立的 `ScrapeTask`。楼盘规模较小，单栋的可订单元长期维持在个位数。

## 2. 反爬现状

OurDomain 使用 HTTP 客户端而非浏览器：SecureRC 前面是 Cloudflare，但不需要执行 JS
解挑战——普通 curl 会拿到 `403 cf-mitigated: challenge`，换一个像样的 TLS 指纹即返回
200。它另有一套机制：按 TLS 指纹与出口 IP 双重跟踪。

### 2.1 403 的两个维度

| 维度 | 表现 | 应对方式 |
|---|---|---|
| TLS 指纹 | 同一指纹短时间内重复请求即进入「挑战中」，返回 403 与 `Just a moment...` | 更换指纹 |
| 出口 IP | 同一 IP 被标记后，**轮换全部指纹仍返回 403** | 更换 IP |

第二条是关键：换指纹只有在同时更换 IP 时才有效。因此 `scrape()` 的重试循环**每次
尝试都会重新获取代理**，并启用 `rotating=True`：

```python
proxy = get_proxy_url(self.source, rotating=True)
```

这与 Holland2Stay、Xior 相反：后两者需要出口 IP 稳定才能复用 Cloudflare clearance，
OurDomain 没有可复用的 clearance，换 IP 才是恢复手段。曾把出口固定到专属 sticky IP，
结果该 IP 被标记后轮换全部指纹仍 403，无法自行恢复。

### 2.2 指纹池与冷却

默认池（`_DEFAULT_IMPERSONATES`）**按实测挑，不按版本新旧挑**。2026-09-15 从生产
容器经轮换代理、用 curl_cffi 0.16.3 测得：

| 结果 | 指纹 |
|---|---|
| 全过 | `chrome150`、`safari2601`、`safari184`、`safari184_ios`、`safari18_0`、`safari180`、`safari180_ios`、`safari17_2_ios`、`safari172_ios`、`safari170` |
| 偶发 403 | `firefox147`、`firefox144`、`firefox135`、`safari260_ios`、`safari18_0_ios` |
| 全部 403 | `chrome146`、`chrome145`、`chrome142`、`chrome136`、`chrome133a`、`edge101`、`chrome131_android`、`safari260` |
| 页面过、POST 403 | `chrome131`、`chrome124` |

**桌面 Chrome 只有 `chrome150` 能过，146 及以前全部 403。** 推测 Cloudflare 把明显
落后于当前稳定版的 Chrome 当可疑；若成立，Chrome 指纹几个月就会过期，Safari 老版本
却一直能用。因此池子以 Safari 打底，`chrome150` 只占一个位置。旧池把 `chrome136` 排
第一，生产日志里每次冷启动都是 `chrome136` 403 → `chrome131` 403 → `safari18_0` 才
过。现池前 4 个（`OURDOMAIN_WAF_RETRIES` 默认只取 4 个）均为全过。

`chrome150` / `firefox147` 从 curl_cffi 0.16 起才有，`requirements.lock` 锁定 0.16.3
（连带 `cffi` 升到 2.x）。macOS 27 上 0.15 及以后的 wheel 导入会报
`symbol not found '_CFArrayAppendValue'`，需在导入前以 `RTLD_GLOBAL` 预加载
CoreFoundation；Linux 容器不受影响。

尝试列表**不再**把 `config.get_impersonate()` 从全局池随机抽的一个插到最前：全局池
服务于不挑指纹的平台，里面有实测 403 的桌面 Chrome。

这是某一天的快照，WAF 策略会变。重测时用临时脚本、在独立进程里跑——冷却状态是进程
级的，在生产进程里测会污染它。`tests/test_impersonate_pools.py` 钉住三件事：池里每个
名字都被锁定版本的 curl_cffi 支持；SecureRC 池里没有实测 403 的指纹；`requirements.txt`
的下限与锁定版本一致。

进程级状态机（`_FINGERPRINT_STATE`，重启后清空）的规则：

- 请求成功：记录 `last_good_at`，并**清除**其 cooldown；
- 返回 403：记录 `cooldown_until = now + 30min`；
- 排序优先级：`last_good`（未处于冷却）→ `fresh` → 处于冷却中者作为最后备选。

冷却中的指纹必须先排除再排序——否则「曾成功且正在冷却」的指纹会同时落入两个分组，
去重时保留首次出现的位置，反而被排到最前，使冷却失效。

单轮尝试次数由 `OURDOMAIN_WAF_RETRIES` 控制（默认 4，上限 8）；指纹顺序可通过
`OURDOMAIN_IMPERSONATES` 覆盖。

### 2.3 同一 session 内的 403 重试

首次返回 403 时，Cloudflare 同时下发了 `cf_clearance` cookie。curl_cffi 不执行 JS、
算不出最终 token，但 cookie 已保存在 session 中——多数情况下等待 2 秒后对同一 URL
再 GET 一次即可通过（Cloudflare 检测到部分 cookie 后会放宽为 light challenge）。

因此遇到 Cloudflare 类 403 先在同一 session 内重试**一次**，仍失败才抛
`BlockedError` 交由上层更换指纹。稳态下单个指纹即可稳定服务，这一步显著减少换指纹
的开销。

### 2.4 仅使用 GET

POST 请求会触发 403，两个数据端点均采用 GET。

## 3. 端点契约

### 3.1 阶段一：floorplans.aspx —— 发现 FP ID

```
GET {base}/{slug}/floorplans.aspx
```

用于提取 floorplan ID。两栋楼采用不同的 RentCafe 主题，ID 格式二者取其一：

| 格式 | 出现位置 | 适用楼栋 |
|---|---|---|
| `subPointerId=NNNN` | photo gallery 的 onclick | Diemen |
| `myFloorPlanId=NNNN` | Get Notified / Contact Us 链接 | South-East |

同时解析 `{fp_id: fp_name}` 映射，仅在推断 Occupancy 时作为备选依据。可用数据来自
每个 FP 的 anchor：

```
onclick="showDialog('Floor Plan Executive Studio | Furnished | Contract 1-5 years',
                    'photogallery', 'imagetype=floorplan&...&subPointerId=1106316&...');"
```

第一个参数是完整 FP 名称，同一 onclick 中的 `subPointerId` 即其 ID，二者强耦合。

另有两条路径经验证不可行：`#FFloorPlan` dropdown 的 checkbox label 由 JS 动态渲染，
curl_cffi 取不到；anchor 的 `title` 在生产环境的服务端 HTML 中均为 `"1"`、
`"Max Rent"` 之类的占位文本。

**FP 层级的按钮状态不可靠**：实测标记为 `contactButton`（"Get Notified"）的 FP，其
单元层级仍全部显示 "Available"。判定必须以单元层级为准。

### 3.2 阶段二：availableunits —— 获取单元

```
GET {base}/rcLoadContent.ashx
    ?contentclass=availableunits
    &floorPlans={fp_id}
    &MoveInDate={YYYY-MM-DD}
    &myolePropertyID={property_id}
```

`MoveInDate` 取下月 1 日。实测 5 月至 9 月的不同日期返回的单元完全相同，因此只查
单一日期；若日后结果随日期明显变化，再扩展为并行查询两个日期。

每个单元对应一行：

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

**字段提取采用双重策略**：两栋楼的 `data-selenium-id` 编号不一致。先按 selenium-id
精确匹配，未命中时按 `data-label`（用户可见列标题：`Sq.M.` / `Apartment` / `Rent` /
`Deposit` / `Amenities` / `Availability`）匹配——后者跨主题更稳定。核心字段全部为空
时记录 WARNING，便于确认哪些主题仍需补充 label 匹配。

`available_from` 自 `ApplyNowClick(...)` 的 `DD-MM-YYYY` 参数提取，**不取「Date
Available」那一格**。两者经常不一致（OurCampus 08-27：格子 `31-8-2026`、回调
`10-9-2026`），经确认回调里的才是实际入住日期。

### 3.3 单元会重复出现在多个 FP 下

同一个物理单元（例如 #6045，ID `307195`）会出现在该楼**全部** FP 的查询结果中——FP
是合同类型标签，单元才是物理房间。`_merge_unit()` 按 `unit_id` 去重，只收集 FP 标签，
不重复创建 Listing。

由此可推出：**`floorPlans` 过滤器不可信**，不能依据 FP 至 unit 的映射判断单元类型。
因此 Occupancy 改由 `sqft` 反推（小于 30 m² 为 One，30–60 为 Two，不小于 60 为
Family），FP 名称仅在 `sqft` 缺失时作为备选。

## 4. 状态映射

判据按顺序取第一条命中：

| 条件 | 含义 | 映射结果 |
|---|---|---|
| 状态格 `text-warning` 或文本含 `wait` | 等位中 | `Available in lottery` |
| 行内有未禁用的下单按钮（`UnitSelect` / `ApplyNowClick`），**按钮文字**含 `lottery` / `loting` / `loterij` | 抽签，点下去是进池子 | `Available in lottery` |
| 状态格 `text-success` / 文本 `Available`，或行内有未禁用的下单按钮 | 可预订 | `Available to book` |
| 其它情形 | 订不到 | `Occupied` |

按钮的**存在**决定能不能点，按钮的**文字**决定点了是什么。只看其一都会判错：
`muted` 日期格 + `Book Now` 是「自该日起可订」（v1.26.0 前被判成 Occupied）；
`muted` 日期格 + `Join Lottery` 是抽签（2026-09-15 前被判成可订）。

实测过的按钮文字：OurDomain South-East `Book now`、OurCampus `Book Now` /
`Join Lottery`。其余文字按可订放行并打 WARNING——漏报比误报贵，但新文字不能再默默
过去（`Join Lottery` 当初就是这么过去的）。

Wait List 仍计为可预订（部分用户愿意等待），与 `Listing.is_available` 的语义一致。

单元级租金为**单一数值**（如 `€ 1.587`），不是 FP 级的区间，可直接交由
`parse_float` 处理。

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
        "Tenant: student and employed",                    # 见 §5.1
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

`Address` 是**建筑级的真实街道地址**（硬编码于 `BUILDINGS`），供 geocode 使用——unit
名称 "Diemen #6045" 是内部编号，无法地理编码。同栋楼的全部单元共享同一坐标，与物理
事实相符。

`Occupancy` 采用与 Holland2Stay 相同的词汇表（`One` / `Two (only couples)` /
`Family (parents with children)`），Web 端 Occupancy 多选过滤器可跨 source 合并。楼层
由 `parse_ourdomain_floor()` 自 Detail 字段提取（`Ground Floor` 记为 0，`Floor 1-4`
记为 1）。

### 5.1 Tenant：谁能租

平台将房源区分为「学生可租」与「须有收入」两类，而 **RentCafe 租赁门户不承载该
信息**——unit 行只有面积、租金、押金、楼层。判据只存在于官网 Criteria 页：

| 楼栋 | Criteria 页原文要点 | `Tenant` 取值 |
|---|---|---|
| OurDomain Diemen | Superior Studio：1-person max.，Students (with Guarantors) & Young Professionals；Executive / Superior Plus：2-person max.，Young Professionals only | 按面积切，见下 |
| OurDomain South East | 全篇无学生条款；须 fixed-hour 雇佣合同（满 3 个月）或 KvK 注册满一年的 ZZP，个人月毛收入 ≥ 3× base rent（双人合计 4×） | `employed only` |
| OurCampus Diemen | 须 MBO / HBO / 大学在校证明（限荷兰院校），PhD 与博后明确不符合；无收入要求。页脚标注「Young professionals book at: OurDomain」 | `student only` |

Diemen 需在单元级区分户型，而 §3.3 已说明 FP → unit 映射不可用；生产数据亦印证其
噪声：#7343（34.43 m²）挂有 Superior Studio 的 FP ID，#6387（33.78 m²）反而没有。
可用判据是面积——该楼各户型尺寸区间互不重叠：

```
Superior Studio       22 – 30 m²   Regulated     学生可租（须担保人）
Executive Studio      33 – 39 m²   Free Market   Young Professionals only
Superior Plus Studio  33 – 49 m²   Free Market   Young Professionals only
1-Bedroom Apt / Loft  45 – 50 m²   Free Market   Young Professionals only
```

2026-08-08 生产库存实测 24 个单元，面积落在 22.56 / 27.86 / 28.39 / 29.27 / 30.20 与
33.78 / 34.43 两簇，中间为空。阈值取 **32 m²**（`_TENANT_STUDENT_MAX_SQM`），两侧各
留约 1.8 m² 余量。

该阈值仅对 Diemen 成立。South East 的户型尺寸严重重叠（20.8–21.4 / 20.8–26.2 /
26.5–33.5 / 26.5–32.7），但整栋不设学生档，无需切分。

面积缺失时不写该字段。把「未知」记为「须有收入」会让符合条件的学生看不到房源，记为
「学生可租」则让人白填一轮申请——两种误判都劣于字段缺失。

取值沿用 Holland2Stay `tenant_profile_restrictions` 的词汇表（`student only` /
`employed only` / `student and employed`），Web 端 Tenant 多选过滤器可跨 source 合并。
规则以 `tenant_policy` 记于每条 `BUILDINGS` 记录，新增楼栋时必须一并声明（由测试守卫）。

## 6. 风险

| 风险 | 现状 |
|---|---|
| SecureRC 启用托管挑战 | 尚未发生。一旦发生，需与 Holland2Stay、Xior 一样改走浏览器传输层 |
| 403 频率上升 | 在可缓解范围内（每次换 IP、指纹池轮换、同 session 重试）；实测每小时数次，轮换后即恢复 |
| `data-selenium-id` 变更 | 该属性是 Yardi 的测试锚点，移除成本较高；且已有 `data-label` 备选 |
| 出现新主题或新字段名 | 核心字段全部为空时记录 WARNING，据此补充 label 匹配 |
| 楼盘规模较小 | 固有限制，单栋可订单元长期维持在个位数。新增楼栋补一条 `BUILDINGS` 记录即可 |
| Tenant 判据依赖官网 Criteria 页 | 该页手工维护，改版不会有任何报错。§5.1 的面积阈值同理：新增落在 30–33 m² 之间的户型会使其失效。测试断言阈值相对实测尺寸的位置，尺寸变动即失败 |

## 7. 自动预订

实现位于 [`bookers/rentcafe.py`](../bookers/rentcafe.py) 的 `OurDomainBooker`，与
Xior、OurCampus 共用 [RENTCAFE.md](RENTCAFE.md) 描述的整套流程。`monitor._AUTO_BOOK_SOURCES`
中**不含 `ourdomain`**，该链路对用户关闭。

### 7.1 与 Xior 的差异：入口

Xior 的 `applyOnlineURL` 直接带单元参数、选房在登录之后；OurDomain 的条款步骤是独立
页面，入口需自行构造，选房在登录之前：

1. `GET {slug}/floorplans.aspx` 建立会话；
2. `GET rcLoadContent.ashx?contentclass=availableunits` 取 `ApplyNowClick` 实参；
3. `POST {slug}/termsandotheritems.aspx`，只提交 `isViaForm=1`、`UnitID`、
   `FloorPlanID`、`myOlePropertyId`、`MoveInDate`、`src`，把单元上下文写进服务端
   会话。

细节与「不要回传 `QuotedRent`」的理由见 [RENTCAFE.md 的平台入口差异](RENTCAFE.md#32-ourdomain先选房再写会话)。

### 7.2 验证状态

| 项目 | 状态 |
|---|---|
| ① 建立会话、② 取单元、③ 写会话 | 已实测走通（2026-08-04，使用真实单元，条款表单 18 个字段全部正确填入） |
| ④ Start Application | 未执行（会消耗验证码费用，并在服务端创建 prospect 记录） |
| 登录后单元上下文是否保留 | **未验证**。OurDomain 的流程不含选房页，一旦脱离流程没有 Xior 那样的重选入口。代码处理：核对落地页是否为 Applicant Info，否则重新提交一次 Book now，仍不匹配则明确报错中止 |
| 申请表字段 | 尚未观察到。`rentcafe_applicant.TEXT_FIELDS` 中的 `STU_*` 三项是 Xior 自定义字段，OurDomain 很可能没有——缺失时抛 `FormShapeChangedError`（**正确行为**，宁可中止也不提交字段不全的申请） |
| 账号是全局一套还是每栋楼一套 | **未验证**。两栋楼分属两个 securerc host，cookie 不跨主机，参照 Xior 经验很可能需分别配置。当前使用面板上单一的 `ourdomain_email` / `ourdomain_password` |

边界与 Xior 相同，止于 Save：再往前是 `ApplicationCharges`，需填 IBAN / SWIFT，代填
金融凭据是硬性限制。`draft_saved` **不代表预订成功**。
