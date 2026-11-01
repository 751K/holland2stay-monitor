# RENTCafe 预订流程与验证状态

本文记录 Xior、OurDomain、OurCampus 共用的 RENTCafe（SecureRC）自动预订流程：入口
差异、请求契约、验证码、已知边界与未验证事项。实现以
[bookers/rentcafe.py](../bookers/rentcafe.py)、[bookers/rentcafe_form.py](../bookers/rentcafe_form.py)、
[bookers/rentcafe_units.py](../bookers/rentcafe_units.py) 和
[captcha/rentcafe_pages.py](../captcha/rentcafe_pages.py) 为准。本文的实测记录只能
说明相应时间点的线上行为，不能证明站点当前仍然如此。各平台抓取侧见
[XIOR.md](XIOR.md)、[OURDOMAIN.md](OURDOMAIN.md) 与
[SCRAPING_RECON.md](SCRAPING_RECON.md)。

## 1. 适用范围与代码入口

三个平台运行同一套 RENTCafe，契约相同，`RentCafeBooker` 共用一份实现；子类只覆盖
「如何到达 Applicant Info」与账号选择：

| 平台 | booker | 入口特点 |
|---|---|---|
| Xior | `XiorBooker` | `applyOnlineURL` 已预填单元参数，选房在**登录之后** |
| OurDomain | `OurDomainBooker` | 入口自行构造，选房在**登录之前** |
| OurCampus | `OurCampusBooker` | 与 OurDomain 相同，单元表只认 POST + `floorPlans[]` |

**三个平台都未开放给用户。** `monitor._AUTO_BOOK_SOURCES` 由
[mcore/sources.py](../mcore/sources.py) 的 `SourceSpec.auto_bookable` 派生，当前只有
`holland2stay` 和 `plaza`。注册了 booker 不等于面板上能启用。

预订目标主机是 `*.securerc.co.uk`，不在 Cloudflare 托管挑战之后，因此这一条链路
**不需要浏览器**，使用 `curl_cffi` 加指纹轮换即可（与抓取侧的 Xior 不同）。

## 2. 验证状态

Xior 于 2026-08-03 使用真实账号与真实单元完整走过一次六步流程，前五步成立，第六步
被平台拒绝：

| 步骤 | 实现 | 结论 |
|---|---|---|
| ① 打开条款页 | `RentCafeSession.open()` | 走通 |
| ② 提交条款 | `submit_terms()` | 走通；Xior 的 v3 token 每次被拒，v2 回退是常态 |
| ③ 登录 | `login()` | 走通（密码登录，两段式） |
| ④ 定位单元 | `find_unit()` | 走通（一次解析出 20 个单元） |
| ⑤ 选中单元 | `select_unit()` | 走通，服务端创建 `ProspectId` |
| ⑥ 保存草稿 | `save_applicant_info()` | **被拒**：`Please upload required documents before Proceeding.` |

此后系统实现了代传证件（[applicant_docs.py](../applicant_docs.py)），但
**上传证件之后的保存行为尚未实测**。

各平台进度：

| 平台 | 已走到 | 未通的部分 |
|---|---|---|
| Xior | ⑥（被证件要求拦住） | 代传证件后能否保存 |
| OurDomain | ③（入口段，2026-08-04） | ④ Start Application 起未执行 |
| OurCampus | Start Application、登录（2026-09-17） | 登录后落到 Applicant Info |

OurCampus 的 v3 表现与 Xior 不同：2026-09-17 实测 Start Application 一次通过、
不回退 v2，约 15 秒；凭据错误时 16 秒内报 `auth_failed`。按平台分别观察，不要互相
套用。

## 3. 平台入口差异

### 3.1 Xior：URL 即入口

`applyOnlineURL` 直接指向 `oleapplication.aspx`，单元与户型参数都在 URL 中。流程是
「条款 → 登录 → 选房 → Applicant Info」，选房发生在登录之后。

### 3.2 OurDomain：先选房，再写会话

OurDomain 的条款步骤是独立页面，且入口需要自行构造（平台侧细节见
[OURDOMAIN.md](OURDOMAIN.md) §7.1）：

1. `GET {slug}/floorplans.aspx` 建立会话，同时轮换 TLS 指纹与出口 IP；
2. `GET rcLoadContent.ashx?contentclass=availableunits` 取单元表，其中含
   `ApplyNowClick(...)` 的实参；
3. `POST {slug}/termsandotheritems.aspx`，只提交 `isViaForm=1`、`UnitID`、
   `FloorPlanID`、`myOlePropertyId`、`MoveInDate`、`src` 这几个参数，把单元上下文
   写进服务端会话。

第 3 步执行后，页面上的 `UnitId` / `FloorplanId` / `QuotedRent` 才从 `0` 变为真实值，
并附带服务端签名的 `QuotedRentEncr`。**不要回传 `QuotedRent`**：报价由服务端计算并
签名，提交历史价格轻则被拒，重则会以过期价格创建申请。

单元参数每次**实时查询**，不做持久化。已保存的 `FloorPlanID` 与 `MoveInDate` 会过期，
也无法回答「该单元是否仍然存在」；实时查询同时充当竞争检测，目标行消失即判定
`race_lost`，由上层转向备选房源。

`ApplyNowClick` 的第 5 个参数是目标页，`ContinueClick` 的第 6 个参数是目标页，
**二者位置不同，写错不会报错**，只会带一组错位参数提交。因此两个平台各自保留解析
函数（[bookers/rentcafe_units.py](../bookers/rentcafe_units.py)），不要合并。

### 3.3 OurCampus：同 OurDomain，但登录后重选有副作用

OurCampus 的单元表只认 POST + `floorPlans[]` 表单体，与抓取侧的
`OurCampusScraper._fetch_units_html` 同一形状。

2026-09-17 实测：登录后若未落到 Applicant Info，兜底的「重选单元」会让服务端返回
`Unit is not available. Please select another unit.`，而同一时刻抓取侧仍列着该房源
——**是这个兜底动作本身破坏了上下文**。排查时用
`tools/ourcampus_booker_probe.py --diagnose` 摊开登录后的页面，不要把它当成房源消失。

## 4. 流程结构

登录后的步骤导航列出全部 11 步：

```
Floorplan → Rental Options → Applicant Info → Additional Applicants
→ Additional Rental Options → Application Charges → Lease Summary
→ Lease Creation → Move-in Charges → Email Summary → Documents
```

**不能深链。** 直接访问 `oleapplication.aspx?stepname=Apartments` 能打开页面，但隐藏
字段 `myOlePropertyId` / `FloorPlanID` / `UnitID` / `MoveInDate` 全为空，结果恒为
`No apartments were found matching your search request`。选房所需的上下文保存在
**服务端会话**中，由前置步骤依次建立，URL 参数不被采信。自动化必须按顺序走完。

登录后的后续步骤同样不能直接打开：`stepname=<后续步骤>` 只返回页面外壳（form 数量
为 0），内容需通过
`rcLoadContent.ashx?contentclass=<步骤名>&stepname=<步骤名>&myOlePropertyId=…&ProspectId=<加密串>`
获取。`ProspectId` 形如 `MjA1NzEwNQ%3d%3d-8z%2faMtjeL9g%3d`，缺失该参数一律返回
500。猜测出来的 URL 只会得到空壳页，在空壳页上填表等于提交一份空白申请。

已过期单元的参数仍可打开条款页（实测：2026-05-28 的 `applyOnlineURL` 到 2026-08-03
仍返回完整表单），因此第 1、2 步的侦察不必等房源出现。

## 5. 请求契约

### 5.1 条款页

表单 `termsandotheritems` → `POST /onlineleasing/rcformsave.ashx`。

服务端下发、必须原样回填的字段：

```
formName2=termsandotheritems   formName=<base64>   cafeportalkey=<key>-<sig>
FloorplanId  FloorplanName  UnitTypeId  SchoolId  AcademicTermId/Name
RentalLevel  strRentalLevel  myOlePropertyId        myLeaseCafeType
IsRCOLE      leasingtype     PropLeadSource_<propId>
```

带签名的字段不能伪造：

```
MoveInDateEncr = <base64(日期)>-<签名>    例 My04LTIwMjY=-z8g85jGXmr8=  →  3-8-2026
QuotedRentEncr = <base64(金额)>-<签名>
```

实测把页面上的日期改写后，`MoveInDateEncr` 不重新计算，且同一日期在不同楼栋下签名
相同——原样回传服务端下发的取值即可。若需更换入住日期，只有修改明文字段并重新提交
服务器下发的签名才可行，相关接口尚未确认。

需用户输入：`sMoveInDate`（Xior 把界面文案改为 "Start application date"）。附加租赁
项 `hRentableitemstype` 由 JS 拼成 `<itemTypeId>^<qty>` 的逗号分隔串，未选择时为空。

### 5.2 登录

两段式，均 POST 至 `rcformsave.ashx`，通过 `formName2` 区分：

```
GET  guestlogin.aspx                              → 200
POST /onlineleasing/rcformsave.ashx               → 第一段：邮箱
POST /onlineleasing/rcformsave.ashx               → 第二段：密码
GET  multiloginwrapper.aspx?AllowRedirect=1       → 被重定向打断，正常
GET  oleapplication.aspx?…&stepname=<下一步>…     → 200
```

登录成功后会恢复上一次的申请上下文：账号里若有未完成的申请，会直接进入那一步。

**四处导致登录静默失败的细节**（2026-08-03 逐项实测）。任意一项出错都会返回 HTTP
200、无错误提示、响应体为空，最终表现为「该单元已被他人选走」：

| 字段或行为 | 容易想当然的写法 | 实测真值 |
|---|---|---|
| `formName2` | 表单 id `Login` | **`mylistlogin`**（表单自带 hidden 值，原样回传） |
| `CheckUserAuth` | 探测时为 `1` | 登录时为**空串 `''`** |
| 空响应体 | 视为失败 | **响应体为空即成功**（AJAX 成功回调无错误时不填内容） |
| 跳转指令 | `window.location=` | **`location.href=`**（不带前缀） |

表单选取也容易出错。`guestlogin.aspx` 上有 4 个表单：密码登录走 `Login`
（Username + Password + CheckUserAuth，无验证码）；`UserLogin` 是 OTP/免密路径，带
Enterprise 验证码。`captcha/rentcafe_pages.py` 中 `guestlogin` 记录的
`form_name="UserLogin"` 指的是**验证码所在的表单**，不能直接用于密码登录。

登录成功后需跟随 `multiloginwrapper.aspx` 的连续跳转（实现限制 5 跳，带环路检测）。
`EncodeFormElementsToBase64()` 仅作用于带 `IsBase64Encode="True"` 的字段，登录页没有
这类字段，无需额外编码。

### 5.3 选房与 Applicant Info

选房按钮有两种，解析前先执行 `html.unescape()`：服务端返回的 onclick 引号是 HTML
实体（`&#39;`），按真实引号写正则会一个单元都解析不出来，而 `find_unit()` 返回
`None` 会被上层报成「该单元已被他人选走」——**解析失败伪装成业务结论**。

Xior 的 `ContinueClick('398336', …)` 中第一个参数即 `xr_<id>` 的 id，无需额外映射。
「Reserve this room」按钮只是选中单元并跳转，**不会当场锁定房源**。

Applicant Info 表单：`formName2=ApplicantInformation`，POST 至 `rcformsave.ashx`。
`btnSave` 会先做客户端校验再 `onclickfunctions('Save')`；`btnNext`
`onclickfunctions('Next')` 不做客户端校验。**半自动化只应使用 Save**——它保存草稿，
不向付款与签约推进。

字段名必须从当前页面解析，不能硬编码：

- 一批字段名里嵌着 `prospect id`（每份申请不同）：`drpGender2057105`、
  `PrefMoveinDate2057105`、`ProspectDOB2057105` 等；
- 国家提交内部数字 id（`drpCurrentCountry="2"` 表示 Netherlands），不同物业未必一致；
- `STU_IDGUESTCARD_ADDITIOALINFO` 保留上游的拼写错误，须原样沿用。

解析由 [bookers/rentcafe_form.py](../bookers/rentcafe_form.py) 完成。以下三个字段
处于 `disabled` 状态，不应提交（其取值由服务端依据所选单元自行计算）：`LeaseTerm`、
`ProspectEmail`、`PrefMoveinDate{pid}`。

**背景调查三问不由系统作答。** `drpEverEvicted`、`drpEverConvicted`、
`drpCriminalCharges` 是用户本人的事实陈述。代为勾选「授权背景调查」有用户在面板上的
授权作为依据；代为回答「我没有犯罪记录」没有。三项留空即视为未作答，档案判定为
不完整，**不予提交**。

## 6. reCAPTCHA

各页面的验证码形态互不相同，不能假定「RENTCafe 全线同一套」：

| 页面 | v3 类型 | v3 sitekey | action | 回退标志字段 |
|---|---|---|---|---|
| `oleapplication.aspx`（第 2 步条款） | 标准 v3（`api.js`） | `6LcjBc4UAAAAABfXlERv_hq_KE3IWDAqbiWkbPzl` | `start_application` | `failed-captcha-3-rentable` |
| `guestlogin.aspx` | Enterprise | `6LfBeqEaAAAAALsbENKGUsE98xFoA3ZpqkbzogBI` | `UserLogin` | `failed-captcha-3` |
| `register.aspx` | Enterprise | 同上 | `GuestRegistration` | `failed-captcha-3` |
| `flexregistrationlandingpage.aspx` | 无 | — | — | — |

共同点只有两处：v2 回退的 sitekey 均为 `6LfAdx8TAAAAAOiesnT8CNKNtb1C6doK-RKnB1V0`，
token 均填入 `g-recaptcha-response-v3`。该表编码在
[captcha/rentcafe_pages.py](../captcha/rentcafe_pages.py)，新增页面补在该处，不要在
调用点硬编码。

条款页的执行链：`failed-captcha-3-rentable === 'false'` 时先执行 v3
（`start_application`）并提交；服务端判定 v3 分数不足时触发
`callReCaptchaV2Rentable()`，把标志置为 `true` 并渲染 v2 checkbox。因此正常路径下
每次提交只需 1 个 v3 token，回退 v2 时再加 1 个。

**各平台表现不同**：Xior 的 v3 token 每次被拒、v2 回退是常态（约 100 秒/次）；OurCampus
2026-09-17 实测 v3 一次通过（约 15 秒）。成本估算按平台实际表现取，不要用一个数套
三个平台。

`flexregistrationlandingpage.aspx` 本身没有验证码，但它只是「选择租约类型」的落地页，
两个出口都指回带 Enterprise 验证码的 `register.aspx`，不是绕过入口。Xior 另外通过
JS 隐藏了 RENTCafe 的三处注册入口（`a#ClickHereToRegisterLink`、
`a[href*="flexregistrationlandingpage.aspx"]`、`a[href*="register.aspx"]`），引导用户
改由 WordPress 侧注册；后端接口仍可用。

## 7. 边界与限制

### 7.1 证件上传是保存的前置条件

Applicant Info 页面上有必填文档 `ID/Passport Upload*`（iframe 内嵌控件，状态
"Not Uploaded"）。**证件未上传前，服务端拒绝保存任何内容**：`btnSave` 可点击不代表
服务端接受。

`isDocumentSetupAvailbleAtThisStep=0` 不等于「该步骤不需要文档」，此前据此判定
「证件不阻塞」是错的。上传控件由
`rcLoadContent.ashx?contentclass=PropertySiteImageUpload…&SetupTitle=ID/Passport+Upload*`
单独加载，接口本身无验证码、无额外 token：

```http
POST {base}/onlineleasing/rcLoadContent.ashx?contentclass=PropertySiteImageUpload
     &objType=3&objPointer=<propertyId>&docType=8
     &ProspectID=<id>&VoyProspectID=<id>&SetupID=<id>
     &SetupType=Other&SetupTitle=ID/Passport+Upload*
Content-Type: multipart/form-data

image[]  文件
objType / objPointer / objSubPointer / docType    ← 值在 URL 查询串里
```

限制：文件 ≤ 5 MB；扩展名限 gif/jpeg/png/jpg/pjpeg/bmp/x-png/pdf/doc/docx/xlsx；文件
名 ≤ 100 字符，且不得包含 `\ / : * ? " < > |`。

URL 携带 `ProspectID`，说明文档按**申请**上传而非按账号上传：每预订一个新单元都要
重传一次，「用户预先上传一次即可长期使用」不成立。实现由系统代传，文件加密落盘
（[applicant_docs.py](../applicant_docs.py)）。

### 7.2 锁定发生在付款环节，止于 Save

`ApplicationCharges` 的表单 `PaymentApplicationChargeStep1` 要求填写 `sAcct`（IBAN）、
`sSwiftCode`、`sName`、`sCPAShortName` 等。页面原文：「in order to continue the
application process, all administration fees will need to be paid」。

代填银行账户是**硬性限制**，系统的边界止于 Save。`draft_saved` 不代表预订成功，
通知文案必须明确写出「房源尚未占住，请立即前往付款」。反方向同样未观察过：没有人
验证过「不付款就一定不锁定」，因此代码在 `application_started` 的文案里也不说
「已锁定」。

OurCampus 2026-09-17 的情况更明确：条款页写着
`submitting this application enters you into the lottery for this unit; it does not
reserve the apartment`，且 Furnished 房源已改为抽签，所以它的 booker 只负责把申请
开到填表页，不承诺占住。

### 7.3 预订侧的写接口限流

抓取与预订访问不同主机，限流规则也不同（2026-08-03 实测）：

- 同一出口 IP 连续执行 3 轮预订后，`POST rcformsave.ashx` 开始全部返回 403；
- 同一时刻 `GET oleapplication.aspx` 完全正常，**不是整站封 IP，而是写接口被单独
  限制**；
- 等待 7 分钟仍未放行，冷却在 30 分钟量级，未精确测定。

由此有两条设计要求：预订会话必须经代理池取出口（`RentCafeSession._new_session()`）；
单条会话固定使用一个出口 IP，因为流程状态保存在服务端会话中。更换 IP 只发生在
`open()` 因 403 重建会话时。会话中途出现的 403 只能归类为 `blocked` 交由上层稍后
重试，不应在会话内部重试——换指纹需重建 Session 并丢弃 cookie，等同于重新开始。

RENTCafe 另设 IP 级尝试次数限制，连续失败锁定 30 分钟，因此自动重试必须克制。

## 8. 未验证事项

1. **代传证件后能否保存。** 已知证件缺失时 Gender 与背景调查三问
   （`drpGender{pid}`、`drpEverEvicted`、`drpEverConvicted`、`drpCriminalCharges`）是
   仅有的四个未落库字段，其余 13 个即使保存被拒仍会写入申请。据此推测它们属于筛查
   区块、被证件要求阻断，但**未验证**。
2. **单元在付款前的准确状态。** 页面文字指向付款环节，但没有观察到付款前后的库内
   状态变化。
3. **application fee 的金额与支付方式**，页面未给出。
4. **登录锁定的作用域是账号还是 IP。** 已确认存在连续输错密码后的锁定；若是 IP 级，
   同一出口上的其他用户会被牵连。
5. **OurDomain / OurCampus 的剩余项**：登录后单元上下文是否保留、账号是全局一套
   还是每栋楼一套、申请表字段是否与 Xior 相同（`STU_*` 三项很可能不存在，缺失时
   抛 `FormShapeChangedError` 是正确行为，宁可中止也不提交字段不全的申请）。

## 9. 历史教训

以下错误各发生过一次，且都不会报错，修改这段链路前值得先读：

- **字段名取自可见标签。** 上一版 15 个字段名全部写错，服务端静默丢弃，提交了一份
  空白申请；单测之所以通过，是因为断言拿映射表去查映射表的输出。
- **把可点击当成可提交。** `isDocumentSetupAvailbleAtThisStep=0` 与按钮 `enabled`
  都不能证明服务端接受保存。
- **漏掉 `html.unescape()`。** onclick 的引号是实体编码，正则匹配为空，最终被报成
  「单元已被他人选走」。
- **假定「RENTCafe 全线同一套验证码」。** 条款页、登录页、注册页的 v3 类型、sitekey、
  action 各不相同，按统一配置求解的 token 会被拒绝。

更早的推翻过程与逐条实测记录见 Git 历史（`git log -- docs/RENTCAFE.md` 之前的
`docs/XIOR.md` 第 8 节）。
