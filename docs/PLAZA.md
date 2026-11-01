# Plaza — 平台状态

> 抓取侧见 [平台侦察与历史评估](SCRAPING_RECON.md#14-plaza) 与 `scrapers/plaza.py`；
> 本文记录自动应征。2026-09-10 使用真实账号验证了登录、预检和 `react` 真提交；撤回
> 接口未实测。平台接口和规则可能变化，历史观察不代表当前线上状态。

平台是 Zig / Hexia，Plaza 只是跑在上面的一个门户（`clientId = "wzp"`）。下面这套
端点大概率对所有 Zig 门户通用，将来接别的荷兰门户可以复用。

## 1. 自动应征的边界

`PlazaBooker` 通过 portal 登录，读取房源应征状态和表单配置，再向
`/portal/object/frontend/react/format/json` 提交表单。2026-09-10 的真实账号测试确认
应征成立；这表示进入平台的选择流程，**不保证获得房源**。系统不代付、签约或上传证件。
自动应征需配置账号并由用户单独启用；DTH 房源不自动应征。

提交结果不明确时，系统暂停同一账号对该房源的自动应征，用户核实后才能解除保护。

抓取侧有一条相关判据：额外供给房源（`isExtraAanbod=true`，如 UU Reserved
Accommodation）只对受邀账号开放，按不可应征处理，见
[SCRAPING_RECON.md](SCRAPING_RECON.md#14-plaza) 与 `scrapers/plaza.py` 的
`_EXTRA_AANBOD_NOTE`。

## 2. 传输：无 Cloudflare，无 captcha

抓取侧侦察未观察到挑战；预订侧当次侦察也未发现人机验证：

- `LoginPopup.entry` / `LoginForm` 两个 bundle 里 grep 不到 `recaptcha` / `hcaptcha` /
  `turnstile` / `cf-`（2026-09-10 实测）；
- 门户配置 `useSsoForLogin = False`，走用户名密码表单，不是外部 SSO。

因此 **Plaza 不需要 `BrowserFetcher`**，纯 `requests` 即可。

⚠️ SDK 里有 MFA 端点 `/v1/mfa/authenticators/{id}`（读代码）。Plaza 是否对普通账号
强制 MFA **未验证**；若强制，password grant 这条路直接断，得改用设备令牌。

## 3. 端点契约

### 3.1 Hexia API 是活的——挂在一个 portal 代理后面

真正的 base 从登录时的网络记录里取得：

```
POST /portal/proxy/frontend/api/v1/oauth/token           → 200   ← 凭据换 token
POST /portal/account/frontend/loginbyservice/format/json → 200   ← token 换 portal session
```

```
{base} = https://plaza.newnewnew.space/portal/proxy/frontend/api
```

直接打根路径 `/v1/reactie` 返回 404——404 只说明**那个路径**不存在，不说明功能不存在，
差一个前缀就是这个结果。

实测通的（2026-09-10，GET / 只读）：

| 路径 | 结果 |
|---|---|
| `GET {base}/v1/reactie?zoekendeId=<id>&limit&page` | **200**，返回本人全部应征 |
| `POST {base}/v1/reactie/validate` | **405**「toegestane methoden: GET, DELETE」 |
| `POST {base}/v1/woning/{id}` | 404 路由不存在 |

两个推论：

1. **`/v1/reactie/validate` 没开。** 405 说的是 `/v1/reactie/{id}` 的允许方法——代理把
   `validate` 当成了 id。上游 dry-run 仍然没有，dry-run 还得用 §3.3 的 `kanReageren`。
2. **`DELETE /v1/reactie/{id}` 开着**（就是那句 405 列出来的）。

`POST {base}/v1/reactie`（创建）**没测**——真正的写请求，需要用户先点头。

### 3.1b `zoekendeId` 是 cookie，不是 persoon.id

`GET /v1/reactie` 不带 `zoekendeId` 返回 `InvalidParameter`。它的值是登录后种下的
**`zoekendeId` cookie**（10 位纯数字，非 HttpOnly，`document.cookie` 可读）。

⚠️ **不是 `account.persons.seeker.mainApplicant.persoon.id`。** 拿 `persoon.id` 去调
返回 **403**——两个都是数字 id，只有一个对。这类错误报的是权限错误而不是参数错误，会
把人引去查登录态。

### 3.2 Content-Type 决定成败，而且失败是静默的

`getallobjects` 吃 JSON，**其余 portal 端点只吃 `application/x-www-form-urlencoded`**。
用 JSON 调 `getobject` / `getdynamicdata` / `getactievereacties` 时：HTTP 200、响应是
合法 JSON，但**只有 `sAngularServiceData`、没有 `result` 键**，没有任何报错。

也就是说「参数发错了」和「这个账号什么都反应不了」长得一模一样。读取要求 `result`
的 portal 端点时，booker 会检查该键；提交 `react` 的响应形状不同，不要求 `result`。
缺少预期字段时应报错，不能把请求格式错误当成空结果。

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
  "kanReageren": true,                 // ← 能不能操作，服务端说了算
  "redenMagNietReagerenCode": null,    // ← 不能的话，原因码在这
  "kanMotiveren": false,               // ← false = 不需要写动机
  "isPassend": true,
  "zoekprofielMatch": true,
  "loggedin": true,
  "url": "?add=11882&dwellingID=16613" // ← 下单参数，服务端给的
}
```

**`add` 就是 `toewijzingId`**（与 `getactievereacties` 里的 `toewijzingId` 对得上），
`dwellingID` 就是 `listing.id`。DTH 的 url 多一个 `&redirect=1`。

`kanReageren` + `redenMagNietReagerenCode` 是现有唯一的 dry-run：服务端已算好「这个账号
此刻能不能操作这一条」，不必发写请求。`dry_run=True` 停在这一步。

### 3.3a `kanReageren` 不是「能不能应征」

它回答的是「能不能操作」。**已经应征过的房源，它仍然是 `true`**——因为确实可以操作，
只是那个操作是撤回（2026-09-10 实测）：

```jsonc
{ "action": "remove", "kanReageren": true, "label": "Delete comment",
  "url": "?remove=999001&dwellingID=16626" }
```

只看 `kanReageren` 的实现会：预检通过 → 把 `url` 原样回传 → **POST 一个 remove** →
静默撤销用户已有的应征 → 回查发现没有 → 报「已提交但回查不到」。这属于毁掉用户已有的
应征，还报了一个不实的结论。

判据是 **`action`**：

| `action` | 含义 | booker |
|---|---|---|
| `add` | 还没应征，可以应征 | 正常提交 |
| `remove` | **已经应征过**，这个动作是撤回 | 当成功返回，**不发写请求** |
| 其它 | 没侦察过 | 拒绝 |

实现里有两道闸：`action` 必须是 `add`，且解析出的参数里必须有 `add`、不能有 `remove`。
两个信号独立、都来自服务端，同时要求才不会出现「以为在应征、实际在撤回」。

`WINKEL-REACTIE-DUBBEL` **不是**预检信号——它只出现在**提交的响应**里。稳态预检给的
是 `action="remove"`。

### 3.3b `kanReageren` 不是会话检查

会话掉线时会出现 `loggedin: false` 而 `kanReageren: true`：

| 字段 | 答的是 |
|---|---|
| `kanReageren` | **这条广告**本身开不开放应征——与你是谁无关 |
| `loggedin` | **这个会话**认不认你 |

只查 `kanReageren` 的实现会带着一个无效会话一路走到 `POST react`。`bookers/plaza.py`
里的 `loggedin` 检查是载荷性的，有测试专门钉住它不能被删。

### 3.4 下单与撤回

```http
POST /portal/object/frontend/react/format/json
Accept: application/json, text/plain, */*
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
X-Requested-With: XMLHttpRequest

__id__=Portal_Form_SubmitOnly&__hash__=672e…&add=11884&dwellingID=16626
```

body 是两半拼起来的：

| 来源 | 参数 |
|---|---|
| `reactionData.url`（该房源的） | `add` / `dwellingID`（DTH 还多一个 `redirect=1`） |
| `GET /portal/core/frontend/getformsubmitonlyconfiguration/format/json` | `__id__` = `form.id`，`__hash__` = `form.elements.__hash__.initialData` |

`__hash__` 是防重放令牌。没有 CSRF 头、没有 referer 要求，防护全在这个 body 参数上。
`formService` 在响应带 `formHash` 时会替换下次的 `__hash__`，即它会轮换；每次提交前
现取即可绕开。

⚠️ **响应顶层没有 `result`**：是 `success` / `reactionData` / `reactionId` /
`messages` / `numberOfReactions`。把 `result` 写死成必须存在，一次**成功**的应征会被
抛成传输错误——房子应征上了，用户却收到「失败」。

响应里的 `reactionData` 是**提交之后**的状态（`kanReageren` 变 false、原因码
`WINKEL-REACTIE-DUBBEL`），不要拿它当提交前的判断。

```http
POST /portal/registration/frontend/getactievereacties/format/json   # 事后核对
POST /portal/registration/frontend/verwijderreactie/format/json     # 撤回
```

已知的 `redenMagNietReagerenCode`：

| 码 | 含义 | booker 怎么处理 |
|---|---|---|
| `WINKEL-REACTIE-NIETMEERGEPUBLICEERD` | 广告不再发布 | `race_lost`，换下一个候选 |
| `WINKEL-REACTIE-DUBBEL` | 这个账号已应征过 | **当成功**——终态已成立，报失败会让上层不停重试 |
| 其它 | 没侦察过 | fail-safe：不发写请求，把原码带出来 |

`getactievereacties` 每条带 `kanVerwijderdWorden`、`positie`、
`aantalKandidatenVoorMij`——位次和前面排了多少人能读到，值得进通知。

### 3.5 会话

登录后的会话 cookie 是 **HttpOnly**，另有一个可见的 `zoekendeId` cookie。对
`requests.Session` 没有影响，`cookies` 自动带。

**旧 portal 端点不吃 `zoekendeId`**：`getobject` / `react` 都不带这个参数，服务端从
会话认人。**Hexia 那条路必须显式传**（§3.1b）——同一个账号，两套路径对身份的要求不同。

## 4. 应征 ≠ 拿到房子

### 4.1 应征之后站点自己怎么说

应征成功后弹给用户的原文（取自 `gettranslations`，2026-09-10 实测）说得很清楚：应征
只是流程的**第 2 步**，之后还有资料与筛选，只有**被选中**才会收到通知：

> 「Bedankt voor je reactie! Je bevindt je nu in **stap 2** van het proces... we
> controleren je documenten pas **als we je een woning toewijzen**. ... Alleen
> **wanneer je geselecteerd bent** voor een woning ontvang je bericht van ons.」

DTH 的排序规则也写着「先应征**且满足条件**的人，房源会被**提供**给他」（`aangeboden`
是「被提供」不是「归你」）。不被选中的人连通知都不会收到。

### 4.2 应征之后还有一整条状态机

```
IN_BUFFER → INTERESSEPEILING → KANDIDAATVOORSTEL → IN_TOEWIJZING
          → AANGEBODEN → GEACCEPTEERD → HUURCONTRACT_GETEKEND
                       ↘ GEWEIGERD    ↘ GEANNULEERD
```

`IN_TOEWIJZING` / `AANGEBODEN` 的文案是「Wacht rustig af. We nemen contact met je op
als je aan de beurt bent.」；`GEACCEPTEERD` 是「Wij controleren de gegevens van deze
kandidaat.」。中间明确存在 `GEWEIGERD`（被拒）与候选人自己退出两个出口。

### 4.3 DTH 的确认框在说什么

`advertentieSluitenNaEersteReactie` 为真时会弹：

> 「Wil je deze kamer **definitief boeken**? ... je accepteert deze kamer en **geen
> andere aanbieding meer krijgt**。」

它描述的是**你这一侧作出的承诺**——「我预先接受这一套，这一轮不再接收其它 offer」——
不是平台那一侧的结果：

| | 说的是 | 内容 |
|---|---|---|
| 确认框（应征前） | 你的承诺 | 接受这套；这轮不再拿别的 offer |
| 回执（应征后） | 平台的流程 | 第 2 步；先到先得；被选中才通知你 |

所以 DTH 的真实代价是**机会成本**（这一轮里排他），不是「合同已成立」。

### 4.4 判据与分布

**用 `advertentieSluitenNaEersteReactie`，不要用 `modelCategorie.code == "dth"`。**
DTH 的 `modelCategorie` 整个是 `{"code": null, ...}`，按 code 判会把它们全部漏掉。
`scrapers/plaza.py` 用的就是这个布尔，别改。

分布（2026-09-10，54 条荷兰住宅）：

| 模型 | 条数 | 判据 | 差别 |
|---|---|---|---|
| **DTH** | 29 | `advertentieSluitenNaEersteReactie == true` | 应征前多一个确认框；这轮排他 |
| `reactiedatum` | 25 | `modelCategorie.code == "reactiedatum"` | 无确认框；有 `closingDate` 兜底 |

两者**都**要走 §4.1 的选择程序，都可能不被选中。区别只在确认框和排他性。

## 5. 登录（2026-09-10 实测）

两步，都在 `plaza.newnewnew.space` 上：

```http
POST /portal/proxy/frontend/api/v1/oauth/token              # 凭据 → token
POST /portal/account/frontend/loginbyservice/format/json    # token → portal session（空 body）
```

第一步的 body 没有采集（含明文密码）。字段名用**形状探针**验过（只用假账号）：

| 发什么 | 回什么 | 说明 |
|---|---|---|
| `{client_id:"wzp", grant_type:"password", username, password}` | `invalid_grant`「credentials were incorrect」 | 形状被完全接受，走到校验凭据 |
| 漏掉 `grant_type` | `unsupported_grant_type` | 形状不对时报另一种错 |
| `grant_type: "bogus_grant"` | 同上 | |

两种错误可区分，所以第一行不是「没报错就当对了」。响应取 `access_token`。

登录之后有两条路：

| | 旧 portal 端点 | Hexia（经代理） |
|---|---|---|
| 认身份 | 会话 cookie | 会话 cookie + 显式 `zoekendeId` |
| 编码 | form-urlencoded | JSON |
| 预检 | `getobject` → `kanReageren` ✅ | `validate` 没开 ❌ |
| 下单 | `react`（参数原样回传） | `POST /v1/reactie`（未验证） |
| 撤回 | `verwijderreactie` | `DELETE /v1/reactie/{id}` ✅ |
| 查自己的应征 | `getactievereacties` ✅ | `GET /v1/reactie?zoekendeId=` ✅ |

**建议**：预检和下单走旧端点（`kanReageren` 是唯一可用的 dry-run，下单参数是服务端给
的，原样回传最稳）；撤回和核对走 Hexia。

完整链路：

```
oauth/token → loginbyservice
  → getobject(id) 读 reactionData.kanReageren / action
      ├ action=remove：已应征，直接当成功返回
      ├ 不能应征：按原因码归类返回，不发写请求
      └ action=add：把 reactionData.url 的参数原样 POST 给 react
  → getactievereacties（或 Hexia）核对确实进去了
```

## 6. 落地状态：端到端已验证（2026-09-10）

`PlazaBooker` 已注册进 `BOOKER_REGISTRY`，并用真实账号跑通全部路径：

| 路径 | 结果 |
|---|---|
| 登录（`oauth/token` → `loginbyservice`） | 真实凭据跑通 |
| `action="remove"`（已应征过） | 提前返回 success，**不发写请求** |
| `action="add"` + `dry_run` | 预检通过、信封取到、停在提交前 |
| `action="add"` + 真提交 | 应征成立 |

### 6.1 已开到 source 级，用户侧默认关

`plaza` 在 `monitor._AUTO_BOOK_SOURCES` 里（端到端验证之后进的），但进元组不等于对谁
都开，还有第二道闸：

```
_AUTO_BOOK_SOURCES 含 plaza        ← source 级，代码里
  ∧ auto_book.plaza_enabled        ← 用户级，面板开关，默认关
  ∧ plaza_username / plaza_password ← 凭据
```

Plaza 比另外三个平台多一道开关：那三个都还有一步在用户手里（H2S 下单后要付款、
Xior / OurDomain 停在存草稿），「填了凭据」约等于「授权到那一步」；Plaza 的应征一次
POST 就落地，中间没有人工关卡。凭据也不能省：没凭据就不产生候选，否则每条新房源都会
跑一次注定失败的登录，失败还会消耗上游尝试额度。

**`maxAantalReacties = "0"` 表示不限。** 门户配置里是字符串 `"0"`，bundle 里
`0 < maxAantalReacties` 是启用上限的前提，`0 < "0"` 为假，故不限。与实测一致：账号上
挂着 5 条在跑的应征时，新房源的 `kanReageren` 仍为 true。两点保留：这是**前端**的闸，
服务端有没有自己的上限没测过；该值是门户配置，Plaza 改了不会通知我们。

### 6.2 DTH 一律不自动应征

`is_dth(obj)` 为真直接返回 `unsupported`，**不发写请求，dry_run 也不放行**。

理由是一个查不到的事实：确认框「geen andere aanbieding meer krijgt」的**范围站点没有
说明**——只管这一轮，还是会连带影响用户手上其它在跑的应征。3595 条
`gettranslations` 全搜过，除确认框本身没有第二处提到它。

事实未知时两边不对称：不自动应征，代价是少自动化一类当前一条都够不着的房源（29 条
全是额外供给，已判 `Not available`），用户自己点一下即可；照常应征，则可能自动放弃
用户手上**全部**其它 offer，中间无人工介入。**刻意没做成用户开关**——开关的前提是用户
能做出知情选择，而这里谁都不知道那句话的范围。

两个实现细节：判据是 `advertentieSluitenNaEersteReactie`，不是本地化的 `label`，也
不是 `modelCategorie.code`；**闸放在预检之前**，否则一条不可应征的 DTH 会走成
`race_lost`，被 monitor 放进重试队列无限重试。

### 6.3 实现里的四条硬规矩

1. **只允许提交添加操作。** `action=remove` 表示已应征，其他非空且非 `add` 的动作被
   拒绝；提交参数必须含 `add` 且不得含 `remove`。`kanReageren` 单独为 true 不够。
2. **`kanReageren` 为 false 时绝不发写请求。** 未知原因码一律当作不能应征。
3. **下单参数原样回传 + 提交信封**（§3.4）。两半缺一不可。
4. **明确拒绝时返回失败；否则回查 `getactievereacties`。** 无法确认时保留不确定结果
   保护，等待用户核实。

另外 `_portal_post` 的 `expect` 要能关掉（react 的响应没有 `result`），`loggedin` 要
单独检查（未登录时 `kanReageren` 仍为 true）。

### 6.4 凭据

`AutoBookConfig.plaza_username` / `plaza_password` / `plaza_enabled`，面板里有独立一块，
含那道开关。**用户名不是邮箱**，字段名与另外三个平台不同是有意的。密码走既有的加解密
路径；开关是布尔，不加密。

Plaza **不参与** `_BACKFILLED_CRED_PAIRS` 那套 H2S 凭据回退——那是历史包袱，而且把
邮箱抄过来必然登录失败，失败还会消耗上游尝试额度。

## 7. 仍然没查清的

- **`redenMagNietReagerenCode` 没有完整清单。** 见过两个
  （`WINKEL-REACTIE-NIETMEERGEPUBLICEERD` / `WINKEL-REACTIE-DUBBEL`）。booker 对未知码
  fail-safe，遇到新码会白白放弃一次机会——日志里能看到原码，攒到了就补进
  `_REASON_PHASES`。
- **会话有效期未知。** 侦察当天遇到过掉线。booker 每次调用都重新登录，不影响正确性；
  若站点限制登录频率，则会成为问题。
- **写接口限流未知。** 读接口连打几十次没被拦，写接口是另一回事。
- **只用一个账号（regulier，非学生）测过。** 学生类型的 `inschrijving` 看到的
  `action` / `kanReageren` 是否不同，没验证。
- **自动下单的 ToS 暴露面没单独看过。** 抓取侧看过 disclaimer，自动**下单**是另一件事。
- **`verwijderreactie`（撤回）从没调用过。** booker 目前不需要它。
