# Plaza — 平台状态

> 抓取侧已接入（2026-09-02），见 `docs/SCRAPING_RECON.md` §5b 与
> `scrapers/plaza.py`。**本文只写自动预订**：这是 2026-09-10 的一次纯静态侦察，
> 全部结论来自匿名可取的接口与前端 bundle，**没有账号，一次写请求都没发过**。
> 每条结论后面标了是「实测」还是「读代码推断」——这两者在下单这件事上不能混。

平台是 Zig / Hexia，Plaza 只是跑在上面的一个门户（`clientId = "wzp"`）。所以下面
这套端点大概率对所有 Zig 门户通用，将来接别的荷兰门户可以复用。

---

## 1. 结论先写：能做，但卡点和预想的不是同一个

用户的判断是「Plaza 啥资料都不需要」。**下单那一刻确实如此**——这是本项目接过的
所有平台里最轻的一次提交：

```jsonc
POST {base}/v1/reactie
Authorization: Bearer <access_token>
{ "zoekendeId": ..., "toewijzingId": ..., "motivatie": "..." }   // motivatie 可空
```

没有表单、没有证件上传、没有 IBAN、没有支付方式。对比一下另外两条路为什么停着：

| | 卡在哪 | 我们能不能代做 |
|---|---|---|
| Xior / OurDomain | 走到 Save 之后要填 **IBAN** | ❌ 代填金融凭据是硬限制 |
| H2S | 要 `payment_method` + 购物车 checkout | ⚠️ 能做，但要用户先在站点绑好支付方式 |
| **Plaza** | **什么都不要，两个 id 就提交** | ✅ |

真正的卡点不是资料。**2026-09-10 已用真实账号把整条链路实测走通，登录也拿到了**（§5）——
只剩「创建」那一个写请求没发过（§7）。

> ⚠️ **本节第一版把 DTH 说成「点下去当场成交、等于替用户签一份租约」，这是错的。**
> 依据只有确认框里那句 *definitief boeken*，而没去看**应征成功之后站点说了什么**。
> 看了就会发现：应征只是进入选择程序的**第 2 步**，被选中才有下文，不被选中连
> 文件都不会看。详见 §4——那一节现在是照着站点自己的流程文案重写的。

---

## 2. 传输：无 Cloudflare，无 captcha

抓取侧已知无挑战（§5b）。预订侧本次侦察也没看到任何人机验证：

- `LoginPopup.entry` / `LoginForm` 两个 bundle 里 grep 不到
  `recaptcha` / `hcaptcha` / `turnstile` / `cf-`（实测，2026-09-10）
- 门户配置 `useSsoForLogin = False`（实测），走的是用户名密码表单，不是外部 SSO

这意味着 **Plaza 不需要 `BrowserFetcher`**。H2S 那套「预热浏览器 + 过 CF 挑战 +
借用 lane」的全部复杂度，在 Plaza 这里一条都用不上——纯 `requests` 即可。

⚠️ 但 SDK 里有 MFA 端点：`/v1/mfa/authenticators/{id}`（读代码）。Plaza 有没有对
普通账号强制 MFA **未验证**。开账号时要顺手确认——如果强制，password grant 这条路
直接断，得改成设备令牌那一套。

---

## 3. 传输：两条路都通，登录只有一条

> **2026-09-10 用真实账号实测重写。** 第一版根据 React bundle 推荐走 Hexia REST
> （`/v1/reactie` 那套），**在 Plaza 上那套根本不存在**。下面每一条都是登录后
> 实际调通的。

### 3.1 Hexia API 是活的——挂在一个 portal 代理后面

> ⚠️ **本节被推翻过两次，两次都是我判断失误，记在这里当反面教材。**
>
> **第一版**（读 bundle）：推荐走 `/v1/reactie`。
> **第二版**（登录后直接打 `https://plaza.newnewnew.space/v1/reactie` 得到 404）：
> 断言「这个部署没开 Hexia API」，把整条路划掉。
> **第三版（本节，实测）**：开了，只是**不在根路径上**。

真正的 base 是从登录那一刻的网络记录里看到的：

```
POST /portal/proxy/frontend/api/v1/oauth/token          → 200   ← 凭据在这里换 token
POST /portal/account/frontend/loginbyservice/format/json → 200   ← token 换 portal session
```

```
{base} = https://plaza.newnewnew.space/portal/proxy/frontend/api
```

**第二版错在哪**：我拿 404 当成了「功能不存在」。404 只说明*我请求的那个路径*不存在，
不说明*那个功能*不存在——差一个前缀就是这个结果。当时手上已经有 `loginbyservice`
这条线索（它的存在本身就意味着「前面还有一步拿 token」），但我没顺着往下找，
直接下了结论。

实测通的（2026-09-10，全部 GET / 只读）：

| 路径 | 结果 |
|---|---|
| `GET {base}/v1/reactie?zoekendeId=<id>&limit&page` | **200**，返回本人全部应征 |
| `POST {base}/v1/reactie/validate` | **405**「toegestane methoden: GET, DELETE」 |
| `POST {base}/v1/woning/{id}` | 404 路由不存在 |
| `POST {base}/v1/reactie-zonder-inschrijving` | 404 路由不存在 |

两个推论：

1. **`/v1/reactie/validate` 没开。** 那个 405 说的是 `/v1/reactie/{id}` 的允许方法
   ——代理把 `validate` 当成了 id。所以**上游 dry-run 仍然没有**，dry-run 还是得用
   §3.3 的 `kanReageren`。
2. **`DELETE /v1/reactie/{id}` 开着**（就是那句 405 列出来的）。撤回可以走这条，
   比旧端点干净。

`POST {base}/v1/reactie`（创建）**没测**——那是真正的写请求，需要用户先点头。见 §7。

### 3.1b `zoekendeId` 是 cookie，不是 persoon.id

`GET /v1/reactie` 不带 `zoekendeId` 会返回：

```json
{"type":"InvalidParameter","message":"Parameter \"zoekendeId\" of value \"NULL\" violated a constraint ..."}
```

它的值就是登录后种下的 **`zoekendeId` cookie**（10 位纯数字，非 HttpOnly，
`document.cookie` 直接可读）。

⚠️ **不是 `account.persons.seeker.mainApplicant.persoon.id`。** 拿 `persoon.id` 去调
返回 **403**——两个都是数字 id、都在账号数据里，但只有一个对。这是那种「传错了不报
参数错误、直接报权限错误」的坑，403 会把人引去查登录态而不是查参数。

### 3.2 Content-Type 决定成败，而且失败是静默的

⚠️ **这是本次踩得最深的一个坑，写 booker 的人一定会再踩一次。**

`getallobjects` 吃 JSON。**其余 portal 端点只吃 `application/x-www-form-urlencoded`。**
用 JSON 调 `getobject` / `getdynamicdata` / `getactievereacties`：

- HTTP **200**
- 响应体是合法 JSON
- 但里面**只有 `sAngularServiceData`，没有 `result` 键**
- 没有任何报错

也就是说「参数发错了」和「这个账号什么都反应不了」长得一模一样。我在这上面绕了
四五轮，先后怀疑过没登录、没付费、要传 objectId——**都不是，就是编码错了**。

**booker 里必须断言 `result` 键存在**，缺了就当错误上抛，不要 fail-open 成空列表。
空列表在这里的含义是「没房源」，而它真正的含义是「你请求发错了」。

### 3.3 拿下单参数：`getobject`

```http
POST /portal/object/frontend/getobject/format/json
Content-Type: application/x-www-form-urlencoded
Cookie: <会话 cookie，HttpOnly>

id=16613
```

返回 `result.reactionData`（2026-09-10 实测）：

```jsonc
{
  "action": "add",
  "label": "Reageer",                  // DTH 上是 "Boeken"
  "kanReageren": true,                 // ← 能不能应征，服务端说了算
  "redenMagNietReagerenCode": null,    // ← 不能的话，原因码在这
  "kanMotiveren": false,               // ← false = 不需要写动机
  "isPassend": true,
  "zoekprofielMatch": true,
  "loggedin": true,
  "url": "?add=11882&dwellingID=16613" // ← 下单参数，服务端给的
}
```

**`add` 就是 `toewijzingId`**（与 `getactievereacties` 里的 `toewijzingId` 对得上，
实测 11953/11954 两条现有应征），`dwellingID` 就是 `listing.id`。DTH 的 url 多一个
`&redirect=1`。

> `kanReageren` + `redenMagNietReagerenCode` **就是我们要的 dry-run**——服务端已经
> 把「这个账号此刻能不能应征这一条」算好了。虽然不是 `/v1/reactie/validate` 那种
> 官方 validate，但语义等价，而且不用发写请求。`dry_run=True` 就停在这一步。

### 3.4 下单与撤回

```http
POST /portal/object/frontend/react/format/json           # 下单
Content-Type: application/x-www-form-urlencoded
add=11882&dwellingID=16613

POST /portal/registration/frontend/getactievereacties/format/json   # 事后核对
POST /portal/registration/frontend/verwijderreactie/format/json     # 撤回
```

**`react` 本次一次都没调用**——见 §7。参数形状来自前端把 `reactionData.url` 的
query string 解析后原样回传（读代码 + `url` 字段实测存在），booker **原样回传即可，
不要自己拼**：哪天上游多塞一个参数，原样回传照常能用，手拼的会漏。

`getactievereacties` 返回的每条带 `kanVerwijderdWorden`（实测为 `true`）、`positie`、
`aantalKandidatenVoorMij`——**位次和前面排了多少人是能读到的**，值得进通知。

### 3.5 会话

登录后的会话 cookie 是 **HttpOnly**（`document.cookie` 里看不到），另有一个可见的
`zoekendeId` cookie。对 `requests.Session` 没有影响——`cookies` 自动带。

**旧 portal 端点不吃 `zoekendeId`**：`getobject` / `react` 都不带这个参数，服务端
从会话认人。**但 Hexia 那条路必须显式传**（§3.1b）——同一个账号，两套路径对身份的
要求不一样，别互相套用。

---

## 4. 应征 ≠ 拿到房子

这一节是本文档改动最大的地方。第一版的结论错了，错在只读了确认框、没读回执。

### 4.1 应征之后站点自己怎么说

`reactionaddedwoning_content`——**这是应征一条 woning 成功后弹给用户看的原话**
（取自 `POST /portal/core/frontend/gettranslations/format/json`，2026-09-10 实测）：

> 「Bedankt voor je reactie! Je bevindt je nu in **stap 2** van het proces: het
> aanleveren van je persoonlijke gegevens en documenten... Let op: we controleren je
> documenten pas **als we je een woning toewijzen**. Word je niet geselecteerd, dan
> bekijken we je documenten dus niet. Na het voltooien van je inschrijving neem je
> automatisch deel aan de **selectieprocedure: first come, first serve**. **Alleen
> wanneer je geselecteerd bent** voor een woning ontvang je bericht van ons。」

也就是说：**应征是流程的第 2 步，不是最后一步。** 之后还有筛选、才有 toewijzing、
才有 aanbieding。「不一定给你」是站点自己写的默认结局——不被选中的人**连通知都
不会收到**。

`reactionadded_content` 说的是同一件事的另一半：

> 「Jouw reactie is toegevoegd. Zijn jouw gegevens bijgewerkt en klopt jouw inkomen?
> Met onjuiste gegevens kom je **mogelijk niet in aanmerking** voor een woning。」

DTH 的排序规则本身也是这么写的（`VolgordeBepalingDescriptionDTH`）：

> 「De eerste die reageert **en voldoet aan de voorwaarden** die genoemd worden in de
> advertentie, krijgt de woning **aangeboden**」

三个词都在往同一个方向指：**voldoet aan de voorwaarden**（还要满足条件）、
**aangeboden**（是「被提供」不是「归你」）、**geselecteerd**（要被选中）。

### 4.2 应征之后还有一整条状态机

React bundle 里的 reactie 状态枚举（读代码），以及每个状态对应的文案：

```
IN_BUFFER → INTERESSEPEILING → KANDIDAATVOORSTEL → IN_TOEWIJZING
          → AANGEBODEN → GEACCEPTEERD → HUURCONTRACT_GETEKEND
                       ↘ GEWEIGERD    ↘ GEANNULEERD
```

`VoortgangPositie-details-vervolgDth-In toewijzing` 与 `-Aangeboden` 两个状态的文案
都是「Wacht rustig af. We nemen contact met je op als je aan de beurt bent.」——
**安心等着，轮到你我们会联系你**。`-Geaccepteerd` 是「Wij controleren de gegevens
van deze kandidaat.」——**我们正在核对这位候选人的资料**。

租约签订是 `HUURCONTRACT_GETEKEND`，是这条链的最后一环，离应征隔着好几步，而且
中间明确存在 `GEWEIGERD`（被拒）和候选人自己 `zich teruggetrokken`（退出，见
`T&T-Geweigerd`）两个出口。

### 4.3 那 DTH 的确认框到底在说什么

那句话确实存在，也确实会弹——触发条件就是 `model.advertentieSluitenNaEersteReactie`
为真（读代码，`submitForm` 里的分支），所以今天这 29 条都会弹：

> 「Wil je deze kamer **definitief boeken**? Dat betekent dat je deze kamer accepteert
> en **geen andere aanbieding meer krijgt**。」

正确的读法是：它描述的是**你这一侧作出的承诺**——「我预先接受这一套，并且在这轮
里不再接收其它 offer」。它描述的**不是**平台那一侧的结果。两句话放在一起才完整：

| | 说的是 | 内容 |
|---|---|---|
| 确认框（应征前） | 你的承诺 | 接受这套；这轮不再拿别的 offer |
| 回执（应征后） | 平台的流程 | 第 2 步；先到先得；被选中才通知你 |

所以 DTH 的真实代价是**机会成本**（这一轮里排他），不是「合同已成立」。这跟
H2S「下单后不付款就作废」其实是同一量级的东西，甚至更轻——H2S 那边订单是真的占住
了房子。

### 4.4 判据还是那个布尔

⚠️ **用 `advertentieSluitenNaEersteReactie`，不要用 `modelCategorie.code == "dth"`。**
DTH 那 29 条的 `modelCategorie` 整个是
`{"icon": null, "code": null, "toonOpWebsite": false, "id": null}`（2026-09-10 实测）
——按 code 判会把它们全部落进 else。`scrapers/plaza.py` 用的就是这个布尔，别改。

分布（原始响应 61 条，剔掉 3 条停车位与 4 条德国房源后剩 54 条荷兰住宅）：

| 模型 | 条数 | 判据 | 差别 |
|---|---|---|---|
| **DTH** | 29 | `advertentieSluitenNaEersteReactie == true` | 应征前多一个确认框；这轮排他 |
| `reactiedatum` | 25 | `modelCategorie.code == "reactiedatum"` | 无确认框；有 `closingDate` 兜底 |

两者**都**要走 §4.1 那条选择程序，**都**可能不被选中。区别只在确认框和排他性，
不在「成不成交」。

---

## 5. 登录（2026-09-10 实测拿到）

两步，都在 `plaza.newnewnew.space` 上：

```http
POST /portal/proxy/frontend/api/v1/oauth/token        # 凭据 → token
POST /portal/account/frontend/loginbyservice/format/json   # token → portal session（空 body）
```

第一步的 body **本次没有采集**——里面是用户的明文密码，抓包时只记了 URL 和方法。
字段名可以从 bundle 推：表单字段是 `username` / `password`（读 `LoginForm`），
OAuth 侧是 `client_id: "wzp"` + `grant_type: "password"`（读 `useQueryParams` 的
refresh 分支，它对称地用 `grant_type: "refresh_token"`）。

**这一步 booker 第一次跑的时候要用真实账号验一次**，因为字段名是推的不是抓的。
验的方式：用户自己在本地跑，凭据不经过任何第三方——与 `gh secret set` 同一模式。

拿到 token 之后有两条路，**登录之后就分叉了**：

| | 旧 portal 端点 | Hexia（经代理） |
|---|---|---|
| 认身份 | 会话 cookie | 会话 cookie + 显式 `zoekendeId` |
| 编码 | form-urlencoded | JSON |
| 预检 | `getobject` → `kanReageren` ✅ | `validate` 没开 ❌ |
| 下单 | `react`（参数原样回传） | `POST /v1/reactie`（**未验证**） |
| 撤回 | `verwijderreactie` | `DELETE /v1/reactie/{id}` ✅ |
| 查自己的应征 | `getactievereacties` ✅ | `GET /v1/reactie?zoekendeId=` ✅ |

**建议**：预检和下单走旧端点（`kanReageren` 是唯一确认可用的 dry-run，而且下单参数
是服务端给的、原样回传最稳），撤回和核对走 Hexia（更干净、字段更全）。

完整链路：

```
oauth/token → loginbyservice
  → getobject(id) 读 reactionData.kanReageren
      ├ false：按 redenMagNietReagerenCode 归类返回，不发写请求
      └ true ：把 reactionData.url 的参数原样 POST 给 react
  → GET /v1/reactie?zoekendeId=… 核对确实进去了
```

---

## 5b. 顺手挖出来一个抓取侧的真 bug（**已修**，2026-09-10）

侦察下单的时候撞上的，**与下单无关，但影响现在的生产**。修在 `scrapers/plaza.py`，
判据与「不能用哪三个」写在该模块的 `_EXTRA_AANBOD_NOTE`，测试在
`tests/test_plaza_scraper.py::TestExtraAanbod`。

对 12 条房源逐条调 `getobject` 读 `kanReageren`（2026-09-10 实测）：

| | 条数 | `kanReageren` | `redenMagNietReagerenCode` |
|---|---|---|---|
| `reactiedatum` 抽样 | 8/8 | `true` | `null` |
| **DTH 抽样** | **4/4** | **`false`** | **`WINKEL-REACTIE-NIETMEERGEPUBLICEERD`** |

「NIETMEERGEPUBLICEERD」= 不再发布。这 29 条 DTH 全是 Utrecht Limapad 的
**UU Reserved Accommodation**（Utrecht University 国际硕士生保留房源，详情页原话：
「beschikbaar om te boeken voor studenten die zijn uitgenodigd」——只对被邀请的人开放），
详情页上明写着：

> Je kunt niet meer reageren op deze advertentie. De sluitingstermijn van deze
> advertentie is inmiddels verstreken.

**而 `scrapers/plaza.py:354` 给每一条都写死 `status="Available to book"`。**

后果：全站 54 条荷兰住宅里 **29 条（54%）根本应征不了，却在按「可预订」推送**。
而且 `Available to book` 正是本项目触发自动预订候选的那个状态
（`monitor._collect_booking_candidates` 里的 `STATUS_AVAILABLE`）——Plaza 一旦开了
自动预订，这 29 条会持续产生必然失败的下单尝试。

### 为什么 `closingDate` 兜不住

原本可能指望用 `closingDate` 过滤过期的。**不行**——实测 54 条的 `closingDate`
**全部等于 `publicationDate` + 整一年**：

```
15676  pub 2026-07-01 → closing 2027-07-01
16613  pub 2026-09-02 → closing 2027-09-02
17097  pub 2026-09-02 → closing 2027-09-02
```

它是个形式字段，不是截止时间。拿它判「还在不在架」永远为真。

### 正确的判据：`isExtraAanbod`

> 本节第一版写的是「匿名接口里没有任何字段能区分这两类，只能登录后查」。
> **错的**——把 29 条与 25 条做逐字段对比就找到了。当时没做这个对比就下了结论。

把两组的所有字段拉平做差集，「组内恒定且两组取值不相交」的候选里，绝大多数是
这批房源恰好同质造成的巧合（`street='Limapad'`、`city='Utrecht'`、
`totalRent=867.75`、`publicationDate` 全同——它们本来就是同一批 UU 房源）。

**只有一个不是巧合：**

```
isExtraAanbod    29 条不可应征 = true      25 条可应征 = false
```

它不是巧合，因为**账号那边有一个对应的字段**：

```
account.persons.seeker.registration.hasAccessToExtraAanbod = false   ← 本次测试账号
```

`isExtraAanbod`（这条房源属于额外供给）配 `hasAccessToExtraAanbod`（这个账号有没有
额外供给的权限）——一对咬合的字段，语义自洽。UU Reserved Accommodation 正是这种
「只对被邀请者开放」的额外供给。

判据就写成：

```python
# 额外供给 = 只对被上游邀请的账号开放（如 UU Reserved Accommodation）。
# 与账号侧 registration.hasAccessToExtraAanbod 咬合；普通用户拿不到。
if obj.get("isExtraAanbod"):
    status = "Not available"      # 具体取值按 storage 的状态词表
```

⚠️ **两处不确定，别写成已知：**

1. 服务端给的原因码是 `WINKEL-REACTIE-NIETMEERGEPUBLICEERD`（不再发布），**不是**
   「你没权限」。所以 `isExtraAanbod` 与 `kanReageren=false` 之间到底是因果还是
   共现，这一个账号看不出来。可能这批既是额外供给、又恰好已下架。
2. 另有两个字段同样完美分组：`modelCategorie.toonOpWebsite=false` 和
   `modelCategorie.code=null`。前者字面意思是「不在网站上展示」，也很像判据。

**建议仍用 `isExtraAanbod`**，因为只有它在账号侧有咬合字段；但把
`toonOpWebsite=false` 也记一笔，将来若出现「isExtraAanbod=false 却仍不可应征」的
反例，先查它。

### 不要用这两个

- **`closingDate`**：恒等于 `publicationDate + 一年`，形式字段，判不出在架与否。
- **DTH 布尔**：这 29 条恰好都是 DTH，但 DTH 是**分配模型**，不是可用性。拿它当
  可用性判据，等于断言「所有 DTH 都应征不了」——将来出现一条真正在架的 DTH（那正是
  最该推的一类，先到先得当场定），会被静默吞掉。这与 `scrapers/plaza.py` 一开始把
  分配模型写进 features 而不是 status 的理由是同一条。

### 部署要按顺序来

这批房源会从 `Available to book` 变成 `Not available`，`diff()` 因此产出一串状态变化，
订阅相关城市的用户会收到一串「下架」通知——**内容不假，但说的是 5 月的房源，而且下架
这件事根本没发生**：变的是我们的判据，不是上游的状态。把判据变更播成平台事件，等于用
真实事件的通道发假事件。

`tools/converge_plaza_extra_aanbod.py` 负责静默收敛（默认 dry-run，`--apply` 才写）：

```
supervisorctl -c /etc/supervisor/conf.d/app.conf stop monitor
<部署新代码>
docker compose exec -T h2s python3 - --apply < tools/converge_plaza_extra_aanbod.py
supervisorctl -c /etc/supervisor/conf.d/app.conf start monitor
```

⚠️ **先停 monitor 再部署。** 在旧代码还在跑的时候改库，monitor 下一轮会拿旧判据把
它们改回 `Available to book`，反而多产出一次「重新上架」的假事件——比不做还糟。

脚本做两件事，缺一不可：**就地改 `listings.status`**（让 `diff()` 压根不产出事件）
＋ **把已存在的未通知变化标成已通知**（兜底 monitor 抢先跑过一轮的情况）。只做后者
不够——`status_changes` 被标记不影响 `listings.status`，下一轮会再产一次；这两半各有
一条测试钉着。

脚本**不自己判断哪些是额外供给**，而是跑一次 `PlazaScraper` 按它给出的 status 收敛
——判据必须是同一段代码，不是同一段描述（同 `tools/backfill_push_optin.py`）。

---

## 6. 建议的落地形态

按本项目既有惯例（`bookers/__init__.py` 那张支持矩阵）：先注册 booker、但**不**加进
`monitor._AUTO_BOOK_SOURCES`，等端到端走通再开——Xior 和 OurDomain 现在就是这个
状态，理由写在 `monitor.py:1505`：

> 「放开等于拿用户的真实账号去提交半懂不懂的表单」

Plaza 到时要过的也是这一关，**不是**「DTH 太危险」那一关（§4 已经推翻了）。
两类房源走同一条路：都自动应征，都用 `/v1/reactie/validate` 先校验。

### 6.1 反而值得做全自动

把 §4.1 的流程文案再读一遍，会发现它几乎是在描述本项目存在的理由：

- 「**first come, first serve**」——先到先得，速度就是全部
- 「we controleren je documenten pas **als we je een woning toewijzen**」——
  资料只在分配时才查，所以应征那一刻真的什么都不用带
- 「**Alleen wanneer je geselecteerd bent** ontvang je bericht」——
  没被选中连通知都没有，人工盯盘的反馈回路等于零

一个只要两个 id、没有 captcha、没有 Cloudflare、上游还自带 validate 和 delete 的
先到先得平台——这是目前所有已接平台里**最适合自动化的一个**。

### 6.2 真正要防的风险换了一个

不是「替用户签了租约」，是这两条：

1. **资料不齐却抢到了位置。** 站点反复在说 *Zijn jouw gegevens bijgewerkt en klopt
   jouw inkomen?*。资料不全 → 被选中 → 核验不过 → `GEWEIGERD`，位置白占。
   这是**用户侧的前置条件**，booker 造不成也修不了，但**应该在开自动预订前检查一次
   并拦住**（`inschrijving` 完整性可以在登录后查）。
2. **DTH 的排他性。** 「geen andere aanbieding meer krijgt」是真的：这一轮里应征了
   DTH 就不再接别的 offer。所以**并发应征多条 DTH 没有意义**，且 filter 要收紧
   ——auto_book 的 `listing_filter` 在 Plaza 上比在 H2S 上更重要。

### 6.3 配置

`AutoBookConfig` 加一对 Plaza 独立凭据（`plaza_username` / `plaza_password`，与
H2S / Xior / OurDomain 互不通用）。**不需要** `plaza_allow_dth` 那个开关——第一版
提议它是基于错误的风险判断；用户要区分两类模型，用现成的 `listing_filter` 就够了，
多一个语义可疑的开关只会让人以为 DTH 是另一种东西。

`dry_run` 直接映射到 `POST /v1/reactie/validate`——本项目第一次有真正的 dry-run。

---

## 7. 本次侦察没做的事

- **一次写请求都没发。** `react` / `verwijderreactie` 全程没调用过——账号上那两条
  在跑的应征是用户自己点的，不是这次侦察产生的。所以 `react` 的**响应**形状仍未知
  （`reactionId` / `kanVerwijderdWorden` / `positie` 这些字段名来自读 bundle）。
- **登录端点未知**（§5）。
- **MFA 是否可开未知**——bundle 里有 `/v1/mfa/*`，但 §3.1 说了 `/v1/*` 在这台主机上
  是 404，所以大概率不适用。没验证。
- **写接口限流未知。** 读接口连打十几次没被拦，但写接口是另一回事。
- **`redenMagNietReagerenCode` 只见到一个值**（`WINKEL-REACTIE-NIETMEERGEPUBLICEERD`）。
  还有哪些码、分别什么含义，没有清单——booker 里对未知码要 fail-safe（当作不能应征），
  不要当作能应征。
- **只用了一个账号（regulier，非学生）测。** 学生类型的 `inschrijving` 看到的
  `kanReageren` 会不会不同，没验证。
- **自动下单的 ToS 暴露面没看。** 抓取侧看过 disclaimer（`SCRAPING_RECON.md` §5b），
  自动**下单**是另一件事，开之前要单独看一遍。
