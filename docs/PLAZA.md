# Plaza — 平台状态

> 抓取侧已接入（2026-09-02），见 `docs/SCRAPING_RECON.md` §5b 与
> `scrapers/plaza.py`。**本文只写自动预订。**
>
> 2026-09-10 分两轮：先是纯静态侦察（读匿名接口与前端 bundle），随后用**真实账号**
> 登录把整条链路走了一遍。后一轮推翻了前一轮的若干结论，推翻的经过原样留着——
> 每条都标了是「实测」还是「读代码推断」，这两者在下单这件事上不能混。
>
> **一次写请求都没发过**（`react` / `verwijderreactie` 全程没调用）。

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

### 3.3a `kanReageren` 也不是「能不能应征」

⚠️ **本文档踩过的最深的一个坑，而且是端到端真跑才撞出来的。**

它回答的是「能不能操作」。**已经应征过的房源，它仍然是 `true`**——因为确实可以
操作，只是那个操作是撤回（2026-09-10 实测，两条已应征的房源）：

```jsonc
{ "action": "remove", "kanReageren": true, "label": "Delete comment",
  "url": "?remove=999001&dwellingID=16626" }
```

只看 `kanReageren` 的实现会：预检通过 → 把 `url` 原样回传 → **POST 一个 remove**
→ 静默撤销用户已有的应征 → 回查发现没有 → 报「已提交但回查不到」。

**毁掉用户要的东西、报告还不说实话。** 而在撞上它之前，`bookers/plaza.py` 的
34 条单元测试全绿——因为测试替身里 `action` 永远是 `"add"`。测试再多也测不出一个
从没在替身里出现过的服务端状态；这个洞只有真账号真跑才会露出来。

判据是 **`action`**：

| `action` | 含义 | booker |
|---|---|---|
| `add` | 还没应征，可以应征 | 正常提交 |
| `remove` | **已经应征过**，这个动作是撤回 | 当成功返回，**不发写请求** |
| 其它 | 没侦察过 | 拒绝 |

实现里设了**两道闸**：`action` 必须是 `add`，且解析出的参数里必须有 `add`、不能有
`remove`。两个信号独立、都来自服务端，同时要求才不可能出现「以为在应征、实际在撤回」。

> 顺带纠正：`WINKEL-REACTIE-DUBBEL` **不是**「已应征」在预检里的信号——那个码只出现
> 在**提交的响应**里。稳态预检给的是 `action="remove"`。第一版把两者搞混了。

### 3.3b `kanReageren` 不是会话检查

2026-09-10 会话掉线后偶然撞见的，**很容易写错且错了不报错**：

```
loggedin:    false      ← 没登录
kanReageren: true       ← 仍然是 true
```

两个字段答的是不同问题：

| 字段 | 答的是 |
|---|---|
| `kanReageren` | **这条广告**本身开不开放应征——与你是谁无关 |
| `loggedin` | **这个会话**认不认你 |

匿名访客点「Reageer」得到的是登录弹窗，不是提交。所以只查 `kanReageren` 的实现会
带着一个无效会话一路走到 `POST react`。

`bookers/plaza.py` 里那道 `loggedin` 检查因此是**载荷性的**，不是消歧用的锦上添花。
本文档与该文件的第一版都把它写成「两者都为 false，靠 kanReageren 区分不了」——
那是推的，而且推反了。有一条测试专门钉住它不能被删。

### 3.4 下单与撤回

> **2026-09-10 抓包实测重写。** 用户自己在浏览器里点了一次 Reply，把真实请求抓了
> 下来。抓之前这一节写的是「参数就是 `reactionData.url` 里那两个」——**不完整**，
> 照着写的实现发出去不会成功。

```http
POST /portal/object/frontend/react/format/json
Accept: application/json, text/plain, */*
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
X-Requested-With: XMLHttpRequest

__id__=Portal_Form_SubmitOnly&__hash__=672e…&add=11884&dwellingID=16626
```

body 是**两半拼起来**的：

| 来源 | 参数 |
|---|---|
| `reactionData.url`（该房源的） | `add` / `dwellingID`（DTH 还多一个 `redirect=1`） |
| **`GET /portal/core/frontend/getformsubmitonlyconfiguration/format/json`** | `__id__` = `form.id`，`__hash__` = `form.elements.__hash__.initialData` |

`__hash__` 是防重放令牌。**没有 CSRF 头、没有 referer 要求**——防护全在这个 body 参数上。
`formService` 里另有一段「提交的响应若带 `formHash`，用它替换下次的 `__hash__`」
（读 bundle），也就是它会轮换；每次提交前现取即可绕开，实测同一会话内连取两次值相同。

⚠️ **响应顶层没有 `result`**：是 `success` / `reactionData` / `reactionId` /
`messages` / `numberOfReactions`。把 `result` 写死成必须存在的话，一次**成功**的应征
会被抛成传输错误——比失败更糟，房子应征上了而用户收到「失败」，于是既不去确认也不撤回。

响应里的 `reactionData` 是**提交之后**的状态（`kanReageren` 变 false、原因码
`WINKEL-REACTIE-DUBBEL`），不要拿它当提交前的判断。

```http
POST /portal/registration/frontend/getactievereacties/format/json   # 事后核对
POST /portal/registration/frontend/verwijderreactie/format/json     # 撤回
```

### 已知的 `redenMagNietReagerenCode`

| 码 | 含义 | booker 怎么处理 |
|---|---|---|
| `WINKEL-REACTIE-NIETMEERGEPUBLICEERD` | 广告不再发布 | `race_lost`，换下一个候选 |
| `WINKEL-REACTIE-DUBBEL` | 这个账号已应征过 | **当成功**——终态已成立，报失败会让上层不停重试 |
| 其它 | 没侦察过 | fail-safe：不发写请求，把原码带出来 |

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

第一步的 body **没有采集**——里面是明文密码，抓包时只记了 URL 和方法。

字段名改用**形状探针**验（2026-09-10 实测，全程只用假账号，没碰真实凭据）：

| 发什么 | 回什么 | 说明 |
|---|---|---|
| `{client_id:"wzp", grant_type:"password", username, password}` | `invalid_grant`「The user credentials were incorrect」 | 形状被完全接受，一路走到校验凭据 |
| 漏掉 `grant_type` | `unsupported_grant_type`「Check that all required parameters have been provided」 | 形状不对时报的是另一种错 |
| `grant_type: "bogus_grant"` | 同上 | |

两种错误可区分，所以第一行不是「没报错就当对了」。响应取 `access_token`——字段名
来自 bundle 里消费**这同一个端点**的 refresh 分支（它解构
`{access_token, refresh_token}`）。

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

## 6. 落地状态：端到端已验证（2026-09-10）

`bookers/plaza.py` 的 `PlazaBooker` 已注册进 `BOOKER_REGISTRY`，并用**真实账号**
跑通了全部路径：

| 路径 | 结果 |
|---|---|
| 登录（`oauth/token` → `loginbyservice`） | ✅ 真实凭据跑通 |
| `action="remove"`（已应征过） | ✅ 提前返回 success，**不发写请求** |
| `action="add"` + `dry_run` | ✅ 预检通过、信封取到、停在提交前 |
| `action="add"` + 真提交 | ✅ 应征成立 |

### 6.1 已开到 source 级，用户侧默认关

`plaza` 在 `monitor._AUTO_BOOK_SOURCES` 里（2026-09-10 端到端验证之后进的）。
但进这个元组**不等于对谁都开**——还有第二道闸：

```
_AUTO_BOOK_SOURCES 含 plaza        ← source 级，代码里
  ∧ auto_book.plaza_enabled        ← 用户级，面板里的开关，默认关
  ∧ plaza_username / plaza_password ← 凭据
```

**为什么 Plaza 比另外三个平台多一道开关**：那三个都还有一步在用户手里——H2S 下单
后要付款、Xior / OurDomain 停在存草稿——所以「填了凭据」约等于「授权到那一步」。
Plaza 的应征**一次 POST 就落地**，中间没有任何人工关卡。让填凭据顺带等于授权自动
应征，跨度太大。

凭据那一半也不能省：没凭据就不产生候选，否则每条新房源都会跑一次注定失败的登录，
而失败会消耗上游的尝试额度。

#### `maxAantalReacties = "0"` = 不限（2026-09-10 查清）

门户配置里这个值是字符串 `"0"`，一度当成待查项。判据在 bundle 里
（`zig.portal.reacties` 的 `reactiePlaatsenMogelijk`）：

```js
return portalConfig !== undefined
  && ( !(0 < portalConfig.maxAantalReacties && !neemtDeelAanHaastrij)
       || totaalAantalReacties < portalConfig.maxAantalReacties )
```

`0 < maxAantalReacties` 是**启用上限的前提**。值为 `0` 时前提不成立，`!(false)`
恒真——不限。（JS 里 `0 < "0"` 会把字符串转成数字，即 `0 < 0` 为假。）

与实测一致：账号上挂着 5 条在跑的应征时，新房源的 `kanReageren` 仍为 true。

⚠️ 两点保留：这是**前端**的闸，只决定 UI 给不给按钮，服务端有没有自己的上限没测过；
而且这个值是**门户配置**，Plaza 哪天改成非 0 我们不会收到通知。真出现上限，表现会是
`kanReageren` 变 false 带一个我们没见过的原因码——booker 对未知码 fail-safe，
不会硬闯，日志里看得到。

#### DTH 一律不自动应征（2026-09-10 决定）

`bookers/plaza.py` 里有一道硬闸：`is_dth(obj)` 为真直接返回 `unsupported`，
**不发写请求，dry_run 也不放行**。

**理由是一个查不到的事实。** DTH 应征前那句确认——「geen andere aanbieding meer
krijgt」——的**范围站点没有说明**：是只管这一轮，还是会连带影响用户手上其它在跑的
应征。3595 条 `gettranslations` 全搜过，除确认框本身没有第二处提到它。而这一条恰好
决定代价有多大。

事实未知时两边不对称：

| | 不自动应征 | 照常应征 |
|---|---|---|
| 代价 | 少自动化一类**当前一条都够不着**的房源（29 条全是额外供给，已判 `Not available`）；用户自己点一下即可 | 可能自动放弃用户手上**全部**其它 offer，中间无人工介入 |

**刻意没做成用户开关。** 开关的前提是用户能做出知情选择，而这里谁都不知道那句话的
范围——给个开关等于把我们的无知包装成用户的同意。等真出现一条在架的 DTH、能观察到
实际行为了，再回来改。

两个实现细节：

- **判据是 `model.advertentieSluitenNaEersteReactie`**，不是 `label`（本地化的：
  荷兰语 "Boeken"、英文 "Reply"），也不是 `modelCategorie.code`（DTH 那批是 null）。
- **闸放在预检之前。** 挪到之后同样不会提交，但一条不可应征的 DTH 会走成
  `race_lost`——那个 phase 会被 `monitor` 放进重试队列，于是一条我们永远不打算按的
  房源会被无限重试。这个差别「有没有提交」类的断言抓不到，有一条测试专门钉位置。

### 6.2 实现里的四条硬规矩

1. **`action` 必须是 `add`。** `kanReageren` 不是「能不能应征」，已应征的房源它
   仍为 true 而动作是 `remove`（§3.3a）。两道闸：`action` 和参数各验一次。
2. **`kanReageren` 为 false 时绝不发写请求。** 未知原因码一律当作不能应征。
3. **下单参数原样回传 + 提交信封**（§3.4）。两半缺一不可。
4. **不信 `react` 的响应判成功**，一律回查 `getactievereacties`。

另外 `_portal_post` 的 `expect` 要能关掉（react 的响应没有 `result`），`loggedin`
要单独检查（未登录时 `kanReageren` 仍为 true）。

### 6.3 凭据

`AutoBookConfig.plaza_username` / `plaza_password` / `plaza_enabled`，面板里有独立
一块，含那道开关。**用户名不是邮箱**，所以字段名与另外三个平台不同是有意的。
密码走既有的加解密路径；开关是布尔，不加密。

Plaza **不参与** `_BACKFILLED_CRED_PAIRS` 那套 H2S 凭据回退——那是历史包袱，而且
把邮箱抄过来必然登录失败，失败还会消耗上游的尝试额度。

---

## 7. 仍然没查清的

- **`redenMagNietReagerenCode` 没有完整清单。** 见过两个
  （`WINKEL-REACTIE-NIETMEERGEPUBLICEERD` / `WINKEL-REACTIE-DUBBEL`）。booker 对
  未知码 fail-safe（当作不能应征），但那意味着遇到新码会白白放弃一次机会——日志
  里能看到原码，攒到了就补进 `_REASON_PHASES`。
- **会话有效期未知。** 侦察当天遇到过一次掉线。booker 每次调用都重新登录，所以
  不影响正确性，但如果站点对登录频率有限制，那会成为问题——没测过。
- **写接口限流未知。** 读接口连打几十次没被拦，写接口是另一回事。
- **只用一个账号（regulier，非学生）测过。** 学生类型的 `inschrijving` 看到的
  `action` / `kanReageren` 会不会不同，没验证。
- **自动下单的 ToS 暴露面没单独看过。** 抓取侧看过 disclaimer
  （`SCRAPING_RECON.md` §5b），自动**下单**是另一件事。
- **`verwijderreactie`（撤回）从没调用过。** booker 目前也不需要它。
