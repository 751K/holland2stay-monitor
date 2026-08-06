# Changelog

## v1.15.3 (2026-08-06)

### 代理降级是一段永远走不到的代码

2026-08-05 04:24–09:29，Webshare 对每一个 CONNECT 都回 `402 Payment Required`
（配额耗尽），5 小时零抓取，最后靠人工充值恢复。

系统里有一整套代理故障处置：标记故障 → 进 10 分钟冷却 → 切 `SCRAPE_PROXIES_FALLBACK`
里的备用 → 全都在冷却就降级为服务器原生 IP 直连并压到 10 分钟一轮 → 给 admin 发一条
「代理失效」告警。**这 5 小时里它一次都没触发过：**

```
grep "代理失效|代理故障|降级直连|备用代理"  →  0
```

原因是一个自己喂自己的闭环。`ProxyError` 在生产代码里只有一个构造点：

```python
# scrapers/__init__.py
if proxy_failure is not None:            # ← 条件
    raise ProxyError(...)                # ← 唯一构造点
...
if isinstance(e, ProxyError):            # ← 而 proxy_failure 只在这里赋值
    proxy_failure = e
```

要先有一个 `ProxyError` 才能产生 `ProxyError`。真正该做这件事的 `is_proxy_error()`
写好了、测过了，就是没人调——`grep` 出来的调用点全在 `tests/` 下面。

**有测试覆盖不等于被生产调用。** 一个纯函数可以百分之百覆盖率地正确，同时对系统毫无
影响。

修了四处：

| 位置 | 问题 |
|---|---|
| `dispatch_scrape_tasks` per-task 分支 | 改用 `is_proxy_error()` 判定，接上闭环 |
| `dispatch_scrape_tasks` 批次分支 | 浏览器 source 的代理故障发生在 `batch_session()` 里，连第一个 task 都进不去，此前**完全没有分类** |
| `is_proxy_error` | 认不出 Chromium 的 `ERR_TUNNEL_CONNECTION_FAILED`——判定文案是空格分词的，而 Chromium 用下划线。浏览器侧的失败因此全部漏判 |
| `is_proxy_service_error` | 只认 502/Bad Gateway。402（配额耗尽）、407（认证失败）、503 都不算「确认」，于是永远进不了冷却 |

确认码收窄到 `{402, 407, 502, 503}`，判据是**换个出口 IP 也没用**。403（该出口被禁用）
与 429（代理侧限流）不在其中：换个 session 或等一会就能恢复，据此关掉整条代理等于把
还能用的容量白白扔掉。

降级期间轮询压到 10 分钟一轮，**高峰期也不例外**——高峰的自适应间隔最低到 60 秒，
若让它生效，等于拿服务器自己的 IP 去高频撞 Cloudflare。

新增 32 个测试，分四层：判定认得出三种真实错误形态（浏览器 / curl / `probe_proxy`）、
dispatcher 两条失败路径都接上了、接上之后代理池确实冷却并降级、降级期间频率被压住。
其中一层用真实的 `probe_proxy` 输出（起一个回 402 的假代理）钉住跨模块的文案耦合，
另一层专门守调用链本身——那才是这次缺的东西。

### 全面宕机反而不告警

2026-08-05 04:24–09:29，代理断线 5 小时 5 分钟，59 轮抓取全部 source 失败，数据库
停在 390 条，admin 全程零告警。第一条 `⛔ xior 连续抓取失败` 是 09:29 恢复**之后**
才发出的。

盲区是反向的：**某个 source 挂了会告警，全部挂了反而静默。**

```
run_once  所有 source 都失败 → 上抛
main_loop except ScrapeNetworkError → logger.error(...)   ← 到此为止
          ...
          await _dispatch_watchdog_alerts(...)            ← 异常已跳过这里
```

两头都以为对方在管：`run_once` 的注释写着「上抛让 main_loop 做连续失败计数和冷却」，
`_dispatch_watchdog_alerts` 的注释写着「那种情况 main_loop 自己的 network / blocked
连续失败计数已经在告警了」。而 main_loop 那几个分支从来只写日志。

新增 `_OutageTracker`，接到四个「这一轮全灭」的分支上——网络不可达、全部被 403、
代理失效且无备用、主循环反复抛未预期异常：

- **首次达阈值立即发。** 门槛在调用方（网络分支要连续 3 轮，屏蔽分支一轮就是 15
  分钟起步的冷却），判定层再压一层「先观察几轮」只会推迟通知。
- **随后 15 / 30 / 60 分钟递增，封顶 1 小时。** 昨晚那种 5 小时故障发 5 条左右。
- **恢复时发一条**，带上持续时长与失败轮数。低于阈值的抖动自愈则全程安静。
- 告警正文带上 `probe_proxy` 的探测结论（如「流量配额耗尽或账户欠费」），不必再上
  服务器翻日志。

`_dispatch_watchdog_alerts` 在 run_once 上抛时仍然跳过——但它注释里那个前提现在才
真正成立，并且由 `tests/test_outage_alert.py` 守着。恢复判定紧跟 run_once 之后，
不放在轮末：剪枝与 watchdog 自己也会抛，那会被记成新一轮「全面故障」。

新增 34 个测试。除了单元与接线（AST）两层，还把 main_loop 真跑起来：注入 40 轮全
失败，断言 admin 确实收到且只收到一条——这一层才分得清「日志写了」和「通知发了」。

## v1.15.2 (2026-08-05)

### clearance 能搬出浏览器，但只活 15–20 分钟

`browser_fetcher` 的类注释长期写着「脱离浏览器把 cookie 搬给 HTTP 客户端通常无效，
因为 clearance 同时绑定了 TLS 指纹」。实测该结论不成立：

```
浏览器过一次挑战 → 导出 cf_clearance 与 UA
curl_cffi 带上二者 + 同一个出口 IP + Chrome 指纹
→ GraphQL 返回 200（chrome131 / chrome136 / chrome124 均可）
```

不带 cookie 直接打是 403 `Just a moment...`，所以 API 确实在 Cloudflare 后面；但票
一旦拿到，curl_cffi 的指纹伪装已足够接近。

**照此改造却省不了流量，反而更费。** 新增的 `tools/clearance_probe.py` 实测一张票
离开浏览器后的寿命：

```
 0 / 5 / 10 / 15 分钟   200
20 / 25 / 30 分钟       403 «Just a moment...»
→ 真实寿命 15–20 分钟
```

`cf_clearance` 标称的一年是摆设，真正管事的是同域下的 `h2s_clr`，过期时间正好
0.5 小时。而浏览器内的会话能撑 2 小时（`_BROWSER_MAX_AGE`），因为它一直在正常发
请求、cookie 由服务端持续刷新；票离开浏览器就没人续了。改用 curl_cffi 之后过挑战
的频率只会**上升**——从 2 小时一次变成 15–20 分钟一次。

两条结论都写进了注释与 §3.1。只记「搬不出去」而不写依据，后来者会重新验证、发现
可行，然后投进一次注定失败的改造——**否定结果和肯定结果一样值钱**。

挑战载荷（985MB 中 558MB）只能靠减少浏览器重建次数来省：持久化 profile（已做）与
放宽重建周期。探针脚本留在仓库里，CF 策略变了可以再跑一次。

## v1.15.1 (2026-08-05)

### 普通用户只收房源推送，运维消息一律只给 admin

抓取被 403 屏蔽、source 熔断、429 限流、每小时的运行心跳——这些回答的都是「监控还
正常吗」，属于运维问题。普通用户既无从判断也无从处置，而每小时几条这样的推送，足够
让人把整个通知渠道静音，连真正的房源通知一起埋掉。

用户渠道上现在只剩四类：

```
send_new_listing        订阅的内容本身
send_status_change      同上
send_booking_success    带付款链接，不送达等于自动预订白做
send_booking_failed     得让用户知道要手动补订
```

其余全部改走 `_notify_admin_only()`（面板通知 + admin 推送），节流机制不变。
`_broadcast_error()` 一并删掉——留着它，下一个人会顺手再用一次。

### 自动预订被屏蔽时，每个用户都收到了其他人的名字

这条消息**该**发给用户（他的自动预订没成，得手动补），但发出去的是给运维看的聚合
文案：

```
🚫 自动预订被 403 屏蔽（12 套候选 / 5 个用户）
...
影响用户: Wu, Yixin, Zhou, ...     ← 每个收件人都看到其他人的名字
```

改为按房源调用 `send_booking_failed`，聚合文案只留在 admin 一侧。两个候选仍然只发
一条——按候选逐条发就成了刷屏。

`tests/test_user_notification_scope.py` 用 AST 扫 `monitor.py`，守住的是清单本身：
用户渠道上只允许出现那四个方法。这条边界在加新告警时极容易被无意破坏——
`user_notifiers` 就在手边，循环一发就完事。

## v1.15.0 (2026-08-05)

### 「已预留」改由平台如实上报，不再靠消失推测

Holland2Stay 的 `available_to_book` 有六个取值，我们原先只抓两个（可订 179、
抽签 336）。那种配置下「消失」是**有歧义**的——可能被人下单了（Reserved 6203），
也可能彻底没了（Niet beschikbaar 180）——所以先推 `Reserved` 留出 2 小时付款窗口，
再判终态。

把 6203 也抓进来之后，消失就没有歧义了：该房源已经掉出我们跟踪的全部状态。此时
再推一次 `Reserved`，等于凭空造一个平台从没说过的状态，还会把
`status_is_inferred=1` 打在一条本可以如实上报的房源上。

所以判据不是「H2S 特殊」，而是一条更一般的道理：

> **「从 feed 里消失」的含义，取决于 feed 覆盖了什么。**

| feed 覆盖 | 消失意味着 | 收敛 |
|---|---|---|
| 只有可订 / 抽签 | 有歧义 | 先推 `Reserved`，再判终态 |
| 也含 `Reserved` | 无歧义 | 直接判终态 |

由 `Config.sources_with_full_lifecycle()` 从实际配置推出，不硬编平台名——
`AVAILABILITY_FILTERS` 是可改的，硬编会在别人调整配置时静默失准。读配置失败时退回
旧判据：宁可多一站推测，也不要把还在付款窗口里的房源直接判死。

其余三个平台的 feed 只列可订单元，没有等价的「已预留」可抓，判据不变。

**本项需同时更新 `.env`**，只改代码不生效：

```
AVAILABILITY_FILTERS=Available to book,179|Available in lottery,336|Reserved,6203
```

抓取量：监控的两个城市每轮从 30 条增至 71 条（Eindhoven 的 Reserved 有 41 条）。

不抓 `Niet beschikbaar`（180）：它是整个存量池，租出、未上架、已下架全归这一档且
不区分原因；仅这两个城市就有 2489 条，是其余状态总量的约 80 倍。也不抓
`To be in lottery`（6204，1 条）——那批还不能报名，通知过去无从行动。

## v1.14.2 (2026-08-05)

### 与其靠 fail-open 兜底，不如把事实写出来

Xior 与 OurDomain 的房源全部带家具，也就是 H2S 口径下的 `Furnished`；它们的 feed
只是没有这个字段。v1.14.1 靠 fail-open 兜底——「平台不提供该维度就整体放行」——
那条规则不区分用户勾的是哪一档：

```
勾 Furnished    → 出现（对，但理由是「缺字段」而不是「真的是」）
勾 Unfurnished  → 也出现（错，它们恰恰不是无家具的）
```

新增 `SOURCE_ASSUMED_FEATURES`：声明平台整体成立、feed 里不上报的属性，由 scraper
在组装 Listing 时带上，能力表同步登记该维度。生产快照实测 `Unfurnished` 从 86 条
回到 3 条，`Furnished` 270 条，四档相加 390 = 全库。

只登记「整栋楼统一、不随房源变化」的属性，且必须是**运营方确认过的事实**——平台
改了配置这里不会自动跟着变。OurCampus 不在其中：它至今没有返回过任何可订单元，
装修档位无从核实，宁可让它继续走 fail-open。

存量房源由启动时的回填补齐：库里绝大多数是 `Occupied`，不会再被 feed 返回，靠
`diff()` 的 UPDATE 永远等不到，而它们仍出现在浏览页里。已有该类目的行不动——
上游哪天真的开始上报了，抓到的值优先。

## v1.14.1 (2026-08-05)

### 统一取值匹配还不够，平台适用范围也得一致

v1.14.0 上线后在生产上比对，通知与浏览页仍差**恒定 83 条**：

```
装修=Furnished        通知 270   页面 187
装修=Fully furnished  通知 155   页面  72
装修=Semi furnished   通知 128   页面  45
```

83 正好是没有 `Finishing` 字段的房源数（Xior 66 + OurDomain 17）。v1.14.0 统一的
是取值匹配，没统一「平台缺该维度时怎么办」：

| | 非 H2S 房源 |
|---|---|
| 通知 | fail-open —— 条件整体跳过，放行 |
| 浏览页 | fail-closed —— 没这个字段就不匹配，排除 |

浏览页从来就是 fail-closed（`feature_contains` 没调用过能力表），不是 v1.14.0
改坏的。但 v1.14.0 给列表页加的徽标写着「其余平台的房源不受此条件影响」——那句话
描述的是通知的行为，放在列表页上是错的。

现由 `dim_applies()` 统一：浏览页的房型 / 入住人数 / 合同 / 租客 / 装修 / 能耗
六个维度都改为 fail-open，与通知一致。放行是对的——Xior 与 OurDomain 的房源实际
都带家具，只是 feed 里不上报该属性；按「没这个字段就不匹配」处理，等于因为上游
少给一个字段就把整个平台从结果里抹掉。

> 遗留：fail-open 意味着勾 `Unfurnished` 时那 83 条也会出现，而它们实际是带家具
> 的。要根治得让抓取层为这两家写出确定的装修档位，而不是靠「缺字段」兜底。

## v1.14.0 (2026-08-05)

### 荷兰语和英语是同一个选项，筛选却当成两个

Holland2Stay 的 feature 取值有两版，返回哪一版取决于房源录入时的语言，与房源
本身无关。同一批数据里 `Two (only couples)` 134 条、`Twee (alleen koppels)` 47 条
并存，而下拉里两版并排列着，看着像两个不同的选项。

同义表此前只有一条（`onbepaalde tijd`），现扩到 35 条，覆盖 Contract / Occupancy /
Type / Finishing / Tenant / Offer；归一也从只有 contract 扩到全部五个白名单维度。
在 H2S 的 307 条上量：

```
户型 Loft                  61 → 80    +19
入住 Two (only couples)   134 → 181   +47
装修 Furnished            251 → 307   +56
合同 Indefinite           178 → 234   +56
```

两侧都归一：只归一房源侧，存量配置里存着荷兰语原文的用户立刻失效；只归一用户侧
等于没修。

`Gestoffeerd` 归到 `Semi furnished` 是按语境不按字面——直译是「铺了地板窗帘」，
荷兰租房里就是这一档。带金额的优惠（`Receive €150 cash back`）没有登记：金额是
变量，静态表覆盖不全，硬编几个常见值反而给人「已经覆盖」的错觉。

### 选「有家具」会收到「无家具」

白名单是裸子串匹配，而 `"Furnished" in "Unfurnished"` 为真。改成词边界匹配：
`Unfurnished` 里 `furnished` 前面是字母 n，不构成边界，正好排掉；跨平台措辞差异
仍然保住——房型在 H2S 写 `1`、在 OurDomain 写 `1-Bedroom Apartment`。

装修四档改为整体相等：`Unfurnished` / `Semi furnished` / `Furnished` /
`Fully furnished` 各占一档，选哪档就是哪档。四档相加正好等于有装修字段的总条数，
不重不漏。想要多档就多勾几项。

匹配方式由 `_EXACT_MATCH_DIMS` 决定，维度名作为参数传入——写成布尔量时那张表可以
被清空而行为不变，表就退化成了装饰性注释。这一处是变异测试逼出来的：清空表之后
测试全过。

### 浏览页和通知各有一套匹配实现

`feature_contains` 是另写的裸子串。修好通知侧之后，浏览页仍按老规矩走：勾
「装修 = Furnished」页面回 251 条（含 Semi / Fully / Unfurnished），通知只发 187 条
——同一个条件两个答案。现共用 `whitelist_matches`。

### 平台不支持某维度时，界面上一个字都没提

一套过滤条件作用于四个平台，而各平台能提供的属性不同。平台不支持时该条件整体跳过
（fail-open），否则会把整批抓不到该属性的房源误杀。行为是对的，问题是用户不知道：
勾「能耗 ≥ A」的人以为收到的都是 A 级，而 Xior 的房源一条都没过这一关。

房源列表页在标签旁加 `仅 Holland2Stay` 徽标，完整说明放 tooltip；用户表单在筛选区
顶部完整说一次，各字段同样只留徽标——逐字段各写一遍的话，同一句话在一页里要出现
八遍，其中六遍一字不差。

措辞写的是「其余平台的房源**不受该条件影响**」。只写「仅对 X 生效」会被读成
「其它平台会被排除」，意思正相反。

### 装修改成多选

四档互斥意味着单选一次只能看一档，想看「有家具或全装修」得查两次。服务层兼容传
单个字符串，旧链接与 API 不受影响。

### 多选框选中两个长值时标签溢出到框外

`.ms-trigger` 同时挂着 `form-input` 类，被 `.listing-filter-card .form-input{height:38px}`
钉死；内容实际需要 74px。design.css 里那段注释本来就写了「多选要用 min-height，
写死 height 会把内容裁掉」，只是被这个额外的类打败了。

### 展示侧也归一

筛选下拉写 `Two (only couples)`，房源卡片上却是 `Twee (alleen koppels)`——用户会
怀疑筛选把这条漏了。展示侧现同样归一，但**只处理受控取值的类目**：同义表按整个值
查表，扫过自由文本会误伤，片区或楼盘若恰好叫 `Kaal` 会被改写成 `Unfurnished`。
数据库里的原始值保持不变。

### 逐用户核对

用 `git worktree` 拉出改版前的代码，同一份生产快照跑三遍，逐用户比对通知放行集合：

```
v1.13.4（本轮全部改动之前）→ 当前   变化 12/42 人   新增 59   减少 0
v1.13.6（已上线）→ 当前              变化  6/42 人   新增 22   减少 0
```

**没有任何用户收到的房源变少。** 59 条新增说明这套比对确实能测出差异。

需要说明：42 个用户里没有一个设了装修筛选，所以四档互斥当前零影响——它只有单元
测试和构造用例撑着，没有被真实数据验证过。

## v1.13.6 (2026-08-05)

### 每次部署都会让持久化 profile 失效

v1.13.4 上线当天就复现了。容器 `force-recreate` 时旧 Chromium 是被杀掉的，
它的单实例锁留在了 bind mount 里：

```
SingletonLock   -> 7c9092fabed6-49
SingletonSocket -> /tmp/org.chromium.Chromium.CXzoae/SingletonSocket
SingletonCookie -> 12798656688512932333
```

内容是 `<容器 hostname>-<pid>` 符号链接。新容器 hostname 变了、pid 也不存在，
Chromium 判定该 profile 正被别的实例占用，启动即退出，Playwright 报
`Target page, context or browser has been closed`。

**而失败是静默降级的**——日志里只有一行「退回临时 profile」，抓取照常，流量悄悄
涨回去。也就是说 v1.13.4 省下的那 93% 只在首次部署到下一次部署之间有效。

launch 之前清掉这三个文件即可。删掉是安全的：同一时刻只有一个实例在用这个目录，
这一点已由槽位 flock 保证，Chromium 这层锁对我们是多余的。

判断必须用 `lstat()` 而不是 `exists()`——这些是悬空符号链接，`exists()` 会跟随
链接返回 False，于是一个都删不掉，看起来清理过了实际什么也没做。

## v1.13.5 (2026-08-05)

### `city` 这一列在四个平台上存的不是同一种东西

H2S 存真城市（`Eindhoven`），Xior / OurDomain / OurCampus 存楼盘名
（`Utrecht Willem Dreeslaan`、`Amsterdam Diemen`）。而筛选是精确匹配，于是勾了
「Utrecht」的用户永远看不到 Xior 在 Utrecht 的那 25 套房。

**这条判据同时管着通知。** 查线上：**14 个用户**因此长期漏收，累计 56 条房源
——数据在库里、平台也勾了、抓取一切正常，面板上看不出任何异常。

```
yjn20040203@outlook.com   勾了 Utrecht     漏 Utrecht Willem Dreeslaan (xior)  25 条
Yixin                     勾了 Amsterdam   漏 Amsterdam Diemen (ourdomain)     16 条
13714314089@163.com       勾了 Eindhoven   漏 Eindhoven Zernikestraat (xior)    1 条
```

加一层归一：`listings.city_normalized` 存归一后的城市，原始 `city` 照常保留并
展示，所有城市筛选走归一列。归一表是**显式的**，不做前缀解析——
`Aachen Vaals Katzensprung` 的城市是 `Aachen Vaals` 不是 `Aachen`，猜一次错一次，
而猜错会把房源归到一个不存在的城市，比不归一还糟。Xior 的 `KNOWN_XIOR_CITIES`
本来就有 `city` / `bldg` 两个字段，直接用；OurDomain / OurCampus 补了 `city`。

Diemen 行政上是独立市镇，这里归 Amsterdam：平台按 Amsterdam 卖，用户也是按
Amsterdam 找房。改归属只需改 config 里那一处。

**两侧都归一。** 只归一房源侧的话，存量配置里存着楼盘名的用户会立刻失效；只归一
用户侧则等于没修。回填每次启动都跑，不是只在建列那天跑一次——改了楼盘归属之后，
存量行必须跟着走。

查询用 `COALESCE(NULLIF(city_normalized,''), city)` 而不是直接读归一列：只要将来
有哪条写入路径漏了它，那条房源就会从所有城市筛选里查不到，不报错也不告警。退回
原始 `city` 至少让它按字面值可查。索引相应改成表达式索引。

顺带修好城市下拉：

- 用户筛选那个下拉原先只用 `KNOWN_CITIES`（H2S 的 26 个），Xior 独有的
  Wageningen / Venlo / Breda / Leeuwarden 根本不在选项里——用户既选不到，一旦设了
  城市筛选，这些楼盘的房源还会被整体挡掉。现在取全平台并集。
- 房源列表页的下拉从 26 项收敛到 20 个真城市，`Amsterdam` / `Amsterdam Diemen` /
  `Amsterdam Naritaweg` 不再像三个城市。

按生产快照回放：14 个用户全部修好，**因改动而丢失的条目为 0**。

## v1.13.4 (2026-08-05)

本版只做一件事：把代理流量降下来。代理按流量计费，08-04 全天 985 MB / 天，
折合 29.6 GB / 月。把这一天的记录逐条拆开之后，钱花在哪一目了然：

| | 流量 | 占比 |
|---|---:|---:|
| Cloudflare 挑战载荷 | 558 MB | 56.6% |
| 业务数据（房源） | 330 MB | 33.5% |
| 页面静态资源 + 第三方脚本 | 97 MB | 9.8% |
| 通知出站 | 0.8 MB | 0.1% |

也就是说**一多半的钱花在过挑战上，不是花在房源数据上**。下面两项分别处理后两类
与第一类。业务数据那 330 MB 是真正要买的东西，不动。

### 浏览器在下载图片、字体和统计脚本，而这些都要按流量付钱

代理按流量计费。08-04 全天代理侧记录 985 MB，其中约 97 MB 是页面自己拉的第三方
资源——`media.holland2stay.com` 的房源图片 32 MB、`googletagmanager` 24 MB、
`fonts.gstatic` 15 MB、fontawesome 9 MB，还有 cookieyes、trustpilot、
google-analytics、ahrefs、chatbase、komoot。抓取需要的只有 DOM 和 cf_clearance。

`browser_fetcher` 之前没有任何 `page.route()`。现按两条规则拦：资源类型命中
`image`/`media`/`font`，或域名落在第三方清单里。按实测流量回放，**拦下 92 MB/天,
29.6 → 26.8 GB/月**——这是下限，Xior 与 H2S 主站那 200 MB 里的同域图片也会被拦掉，
但代理按隧道记账，回放看不出来。

风险全在拦过头，所以三类始终放行：

- `challenges.cloudflare.com` / `cloudflareinsights.com` 及其子域——挑战靠它们跑完；
- `stylesheet` 与 `script`——CF 的行为检测会读渲染结果，且两类合计不到 5 MB；
- `cdn.jsdelivr.net`——站点自己的 bundle 也走它，从域名分不出来。

域名匹配按点边界，否则 `nottrustpilot.com` 会被误判。反过来
`cdn-cookieyes.com` 是**独立注册域**而非 `cookieyes.com` 的子域，点边界匹配吃不到，
必须单列——这条是写测试时才发现的，而它恰好是那家三个域里最大的一个。

判定出异常一律放行：`route` 处理器抛出去会让请求悬到超时，为省流量把页面拖垮
不划算。`page.route()` 装不上也只告警。

`BROWSER_BLOCK_RESOURCES=0` 整体关闭——拦截改了加载行为，万一 CF 起疑，得有一条
不重新发版就能退回原状的路。每次会话结束记一行拦截计数，用来确认规则还在生效。

### 每次重建浏览器都重下一遍 Cloudflare 挑战载荷

代理流量里最大的一项：558 MB / 天，占 56.6%，单个隧道最大 11.5 MB。成因是每次
重建浏览器都是全新的空缓存，Turnstile 的 JS 和 WASM 一个字节都留不下。Xior 每
15 分钟重建一次，是主要来源。

**只给 `--disk-cache-dir` 一点用没有**——实测缓存目录里只留下几个索引文件，字节数
分毫未降。原因是 `launch()` + `new_page()` 走的是 incognito context，HTTP 缓存只在
内存里，浏览器一关即弃。改用 `launch_persistent_context()` 才行。

本地实测（H2S 首页，两轮同一 profile）：

```
冷 profile   3.93 MB    3 个请求命中缓存    页面正常
暖 profile   0.25 MB  143 个请求命中缓存    页面正常
```

第三轮曾失败，单独复测确认是 90 秒内连打三次被限流，与 profile 无关：暖 profile
间隔 100 秒再打一次，0.25 MB，页面正常。

cookie 每次启动都清。clearance 绑出口 IP，而 `rotating_proxy` 意味着下次开浏览器
多半换了 IP，带着旧 `cf_clearance` 去只会被当作可疑重新挑战。要复用的只是磁盘缓存
里那些静态资源。

一个 profile 目录同一时刻只能被一个 Chromium 打开，而 H2S 的 scraper 和 booker
共用一个 source、跑在不同线程上。按槽位加 `flock`，抢不到就退回临时 profile。锁必须
持有到浏览器关掉之后——提前放锁，别人拿到目录时 Chromium 还没退出。

profile 在 `data/browser_profiles/`，每槽位缓存上限 128MB。删掉不影响正确性，只损失
一次冷启动。`BROWSER_PERSIST_PROFILE=0` 整体关闭。

### 测试真的拉起了浏览器

`tests/test_browser_fetcher.py` 的 `_patch_launch` 只 mock 了 `launch`。改用
`launch_persistent_context` 之后，那条路径没被拦住，测试跑一遍就在 `data/` 下写出
了 12MB 的真实 profile。两个入口现在都 mock，并新增 autouse fixture 把 profile 根
目录指向临时目录。

## v1.13.3 (2026-08-05)

### 代理故障被报成 Cloudflare 故障

代理账户欠费停服，`CONNECT` 一律返回 `402 Payment Required`。Chromium 把它压成
`ERR_TUNNEL_CONNECTION_FAILED`，日志随之写出六百多行：

```
Holland2Stay 主站加载失败（CF 挑战可能未通过）: net::ERR_TUNNEL_CONNECTION_FAILED
```

这句话与实际成因毫无关系。同一时段走 curl_cffi 的 OurDomain 日志里明写着
`curl: (56) CONNECT tunnel failed, response 402`——故障是同一个，可诊断性相差悬殊，
差别只在传输层有没有把上游给的信息透传出来。真实状态码最终是手工发了一次
`CONNECT` 才拿到的。

新增 `config.probe_proxy()`：导航失败命中代理层错误码时，向**该浏览器实际在用的
那条线路**发一次 `CONNECT`，取回代理自己的状态码再决定怎么描述。402 / 403 / 407 /
429 / 502 / 503 各自附上含义。凭据只用于构造 `Proxy-Authorization` 头，不进返回值
也不进日志。

探测必须用浏览器实际在用的那条 URL——`rotating_proxy` 的 profile 重新调
`get_proxy_url()` 会拿到另一个 session，探到的是别的出口，结论无效。

排障入口新增一条命令，下次不必再手写 socket。

### 被 CF 挡住时不再原地重试同一个 IP

v1.13.0 为两个浏览器 profile 开了 `rotating_proxy`，但**没有任何失败路径去触发
它**：`ensure_initialized()` 的三次重试在同一个浏览器上原地重新导航，`fetch()` 的
403 分支也只是把 `_initialized` 置 False。换 IP 的能力有了，触发它的路径没有——
逃生口等同于不存在。

2026-08-03 那次事故的完整链条正是如此：IP 被标记 → 三次 90s 挑战全超时 → 熔断
30 分钟。三次尝试跑在同一个出口上，等于把同一次失败重复了三遍，唯一的效果是多烧
四分半钟。

新增 `_rebuild_browser()`，挑战超时、clearance 超时、非 clearance 类 403 都先关掉
浏览器重建再重跑挑战。两类 403 处置相反：命中 `clearance_pending_markers` 的是瞬时
状态，重新导航就好，换 IP 反而丢掉已有会话；其余才是这个出口被挡了。

固定 session 的 profile 不重建——重建后拿到的是同一个出口，白付一次冷启动。

## v1.13.2 (2026-08-04)

### 新建用户时填的收件邮箱，永远收不到验证邮件

`send_verification_email_sync` 只有两个调用点：`user_edit()` 里「`email_to` 变了」
那个分支，和用户手点的「重发验证邮件」按钮。**`user_new()` 一个都没有。**

于是在创建表单里直接填了 shared 收件邮箱的用户，形成一条走不出去的死路：

```
建完 → email_verified=0
     → 不会再经过「邮箱变了」分支（除非把邮箱改成别的再改回来）
     → 收不到验证链接
     → notifier 直接跳过整个 email 渠道
```

线上表现是用户一封通知都收不到，而日志里只有一行

```
[WARNING] [Yixin] Email(shared) 邮箱未验证，跳过（请到设置页完成邮箱验证）
```

没有报错、没有异常，面板上看一切正常——用户以为配好了，实际一直在等一封永远
不会到的邮件。已按此定位到线上一个真实用户并手工置位恢复。

修法是把判断和发送抽成两个函数，新建与编辑共用，避免两条路径继续各写一份：

- `_needs_email_verification(user, previous_email=None)` —— shared 模式、填了
  邮箱、尚未验证。`previous_email` 只有编辑路径传：邮箱没变说明是在保存别的
  字段，不该重发；新建路径没有「上一个邮箱」可比，有邮箱就得发。
- `_flash_verification_email(user)` —— 发送并 flash 四种结果。**只 flash 不抛**：
  调到这里时用户已经落库，发信失败不能把「创建成功」一起搭进去。

编辑路径的限流原样保留——普通用户发验证邮件和「发测试通知」共用一个配额，
否则它就成了一个不限次的对外发信接口。`user_new` 是 `@admin_required`，
不需要那道闸。

自助注册的两条路径（Web `/register` 与 `POST /api/v1/auth/register`）不受影响：
它们建出来的用户根本没有 `email_to`。

新增 `tests/test_user_new_email_verification.py`（7 条）。先跑测试确认能复现，
再改代码；改完做了三次变异，每次都被对应用例抓住：

| 变异 | 被抓住的用例 |
|---|---|
| 删掉新建路径的发送（还原成 bug） | `test_shared_email_triggers_verification` |
| custom 模式也发（越权借用 shared 发件域） | `test_custom_mode_no_send` |
| 编辑丢掉「邮箱没变就不重发」 | `test_edit_without_email_change_does_not_resend` |

---

## v1.13.1 (2026-08-04)

纯文档与注释，无行为变更。三件事：文档跟不上代码、文档里不该有生产库的数字、
以及新手照 README 走部署不下去。

### 文档里有三处说法已经被自己的侦察结果推翻

`ARCHITECTURE.md` §7 和 §9 都写着 RENTCafe 预订「卡在 reCAPTCHA 和未侦察的多步
表单，面板标记为开发中」——三句全错。reCAPTCHA 在 v1.12.0 就接了 2Captcha，多步
表单 08-03 用真实账号走完了，而「开发中」这个标记全仓 grep 不到。改成新增的
§7.1，用一张表对照早先记的阻塞点和现状。

同一类的还有一批：

- §2 流程图停在「距上次收敛满 24h」的旧模型，`dataflow_ch/en.mmd` 同错；
- 两段式收敛（v1.13.0 最大的行为改动）整个没进架构文档，新增 §5.13；
- §5.11 / §5.12 物理位置在 §7 和 §8 之间，章节顺序是坏的；
- `FUTURE_PLAN.md` 两处把「每 source 独立 stale 阈值」当待办，而 v1.13.0 恰恰是
  撤销了它；引用的 `XIOR.md §11` 也不存在（该文档只到 §8.7）；
- `ANDROID_PLAN.md` 现状表说 FCM 未开发，同文档下面的阶段表写着已完成；
- `XIOR.md` §8.5 把「证件上传不阻塞流程」列在已确认里，而 §8.6 明确推翻了它；
- 两份 `guide.html` 说「监控三个平台」，平台表里没有 OurCampus，Xior 城市数写的
  是 15（实际 14），且仍在讲 reCAPTCHA 是阻塞点；
- `API.md` 缺 `status_is_inferred` 和 `source` 两个返回字段，缺 `source` /
  `sources` / `occupancies` 三个查询参数；`openapi.json` 也漏了 `occupancies`。

顺带发现一处真实的接口不一致（本次未改代码）：`/map` 和 `/calendar` 返回 `status`
却不返回 `status_is_inferred`，移动端在这两个页面上分不出平台报的和系统推的。
已在 `API.md` 写明并给了绕法。

### 文档和注释里不再出现生产库的数字

房源存量与分平台体量、用户数、残留记录条数、Reserved 时长分位、轮次计数、误判
次数，全部改成定性表述。范围覆盖 `docs/`（含 CHANGELOG 历史条目）、代码注释
（`mstorage/_listings.py`、`monitor.py`、`users.py`、两个 scraper）以及测试
docstring。

平台自身的行为常量保留：H2S 的 2 小时付款限时、CF 挑战 10–35 秒、Xior 每栋楼约
14 秒和 5/10 分钟不触发 429——这些是调 `STALE_*` / `SHARD_SIZES` /
`SOURCE_MIN_INTERVALS` 必须知道的，删了文档就没法用。

### 新手照 README 走部署不下去

`cp .env.example .env` 之后 `docker compose up -d`，entrypoint 预检直接 FATAL
退出（占位域名 + 空 `WEB_PASSWORD`）。两条都是对的保护，但文档只给了「买个域名」
这一条出路，而 `H2S_SKIP_PREFLIGHT` 在文档里一次都没出现过；compose 里 h2s 是
`expose` 不是 `ports`，单独 up 也连不上。

新增 `docker-compose.local.yml`：端口发布到 `127.0.0.1:8088`、跳过预检，末尾点名
`h2s` 就不会拉起 caddy。绑回环是有意的——该模式下面板是明文 HTTP 且默认无密码。

README 的快速开始随之拆成「本地试跑」和「带域名部署」两条路，并补上：

- 登录用户名默认是 `admin`（原来只说「留空则无需登录」，设了密码之后没人猜得到）；
- 预检 FATAL 的两种成因和对应处理；
- 原先完全缺失的**升级与备份**一节——代码不是挂载的，必须 `build` 加
  `--force-recreate`；`.env` 和库必须一起备份，否则拿到一堆解不开的凭据；WAL 下
  直接 `cp` 会漏写入，给了 `VACUUM INTO` 的命令。

### `.env.example` 补了九个代码在读、文档没提的键

`STALE_RESERVED_HOURS` / `STALE_OCCUPIED_HOURS` / `OURDOMAIN_ZERO_ROUNDS_TO_CONFIRM`
（前三个是 v1.13.0 刚加的，等于整套新行为对自部署者完全不可见）、
`CAPTCHA_API_KEY` / `BOOKING_STATUS_HOLD_MINUTES` / `SOURCE_MIN_INTERVALS` /
`GOOGLE_MAPS_API_KEY` / `DATA_DIR` / `OURDOMAIN_IMPERSONATES`，外加
`HTTP_PROXY` / `ALL_PROXY`。

修两处错：`SHARD_SIZES` 默认值写的是 `xior:5`，代码里是 `4`；`GOOGLE_MAPS_API_KEY`
的注释原本写「留空则退回不需要 key 的底图」，查 `templates/map.html` 发现是错的
——`{% else %}` 分支直接输出「未配置」，没有降级底图，`/map` 整页不可用。

### 中文文档统一改成书面语

通读逐段改写，覆盖 `ARCHITECTURE`、`README_cn`、`XIOR`、`OURDOMAIN`、
`SCRAPING_RECON`、`FUTURE_PLAN`、`OBSERVABILITY_PLAN`、`ANDROID_PLAN`、
`guide_cn.html`。引述界面文案的地方保留原样。CHANGELOG 的历史条目只清数据、
不改语体——它是按发布时间追加的记录，重写往期措辞会让它和当时的 tag 对不上。

### 补六张 mermaid

都用 `mermaid-cli` 实际渲染验证过（仓库现共 13 张，一起验，全通过）：

| 位置 | 讲什么 |
|---|---|
| `ARCHITECTURE` §3.1 | 过 CF 挑战的五方时序，含 clearance 那 2 秒空窗和两类 403 的分岔 |
| `ARCHITECTURE` §5.13 | 房源状态机 + 三条收敛路径的触发条件 |
| `ARCHITECTURE` §7 | H2S 自动预订，含候选分配、booking hold、不代付的边界 |
| `XIOR` §8.6 | RENTCafe 端到端六步，v2 回退和两段式登录都展开 |
| `XIOR` §8.2 | 深链失败画成旁支 |
| `OURDOMAIN` §7.2 | 入口三步 |

两份 README 和上述几处的 ASCII 图一并换成 mermaid。代价是纯文本查看会看到源码。

---

## v1.13.0 (2026-08-04)

### 房源状态收敛重做：消失 30 分钟转 Reserved，2 小时判 Occupied

原来的判据是「可订 7 天 / 抽签 2 天没见到 → 直接 Occupied」，两处都不对。

**一、7 天太长，中间那一周是纯谎报。** 线上实测 OurDomain 房源寿命 0.2–3.1
小时（一批出现，然后一条条被订走）：

```
od_211218   08:39 出现 → 08:52 消失    0.2h
od_211053   08:39 出现 → 11:48 消失    3.1h
```

而阈值是 7 天，差约 800 倍——中间这一周它在列表、地图和 API 上都还挂着「可订」。
H2S 的抽签同理：从抽签出去的状态迁移总共只有 3 次，其余都是直接消失，而当时
4 条抽签房源已经消失 31–44 小时还挂着「可抽签」。

**二、直接跳 Occupied 是把推断当终态。** 判错时房源从面板上彻底消失，等 feed
恢复再出现会产生 `Occupied → 可订`，用户收到一批假的「重新上架」——**收得太早的
代价是多通知，不是少通知**。

所以改成两段，四个平台、所有状态**统一一套**：

```
可订 / 抽签 / Unknown
  ↓ 消失 30 分钟
Reserved（推测）      ← is_available 不含它，「不再显示为可订」这件正事已经办到
  ↓ 消失 2 小时
Occupied（推测）      ← 终态
```

中间那一站不是妥协，是**降低判错代价**：`Reserved → 可订` 本来就是最常见的正常
迁移之一，语义是「别人的预留没成」，不是一次突兀的复活。

**2 小时对齐 H2S 官方的付款限时。** 这个数同时管住两种 Reserved：我们推出来的，
和平台自己报的（有人下单未付款）。一条已经消失超过 2 小时的 Reserved，付款窗口
必然已经关闭——要么付成了，要么作废了；作废的话它会以「可订」重新出现在 feed
里，而我们没看到它。所以两者用同一个窗口，不按 `status_is_inferred` 分。

30 分钟 ≈ 30 轮连续完整扫描里都没有它，而同一次响应里通常还有二十几条别的房源
作旁证。OurDomain / OurCampus 另有一道闸——抓取侧要求连续 3 轮返回 0 个单元才
承认「真没房」。

**只调阈值不改节奏等于没调。** 主收敛 24 小时才跑一次，房源满 2 小时该判终态了，
实际要等到下一次整点，最坏挂 26 小时。所以老化那两段改成**每轮都跑**
（`_sweep_aging`）。每轮跑没有累积开销：到终态的行之后一律被 WHERE 排除，稳态下
命中 0 行——**阈值本身就是节流器**。孤儿收敛（扫全库、宽限期 30 天）仍留在 24
小时那趟。

顺带解决了一个老问题：H2S 平台报的 Reserved 原来不参与收敛——`_STALE_GENERAL_STATUSES`
不含 Reserved、孤儿路径又要求城市掉出监控，于是一批消失了好几个月的记录**永远
卡着**。现在按同一套窗口收敛。

中途试过按 (source, 状态类) 分开配阈值，最后收掉了：那些差别描述的是「feed 会不
会保留下架房源」，而实测下来四个平台的终态都是**从 feed 里消失**——只有 Xior 的
feed 里真有 Occupied，其余三个平台的终态基本全靠推。既然消失是共同的下架信号，
就不该有四套判据。

上线前在生产库的副本上跑了四轮：前两轮各收敛一批，第 3、4 轮 0 条（幂等）。
**30 分钟内刷新过的可订房源全程没动。**

`STALE_RESERVED_HOURS` / `STALE_OCCUPIED_HOURS` 可覆盖；后者小于前者时会被抬平
并告警——终态窗口比中间站还短的话，第二段会抢在第一段之前把房源直接判死，
Reserved 那一站形同虚设，而它正是「判错时代价小」的全部来源。

### 「这栋楼一个单元都没有」现在要连着看到几轮才算数

`_looks_like_availability_panel` 能挡住「这压根不是单元面板」，挡不住「是面板，
可这一次的内容不对」——两种情况的 HTTP 状态、页面结构、长度完全一样，从单次
响应里区分不出来。而 `complete=True` 的含义是「这轮扫全了，没见到 = 真没了」，
`mark_stale_listings` 完全信任它，所以一次内容异常的空响应就够让整栋楼的存量
listing 走上收敛路径。

现在没出事，是因为老化阈值（7 天）比 OurDomain 房源的真实寿命（0.2–3.1 小时）
大了约 800 倍，一次误判来不及产生后果——**阈值在替这个漏洞兜底**。一旦阈值调到
和真实寿命同量级，兜底就没了，所以这道闸是缩短阈值的前置条件。

做法是进程级计数：某栋楼连着 N 轮（`OURDOMAIN_ZERO_ROUNDS_TO_CONFIRM`，默认 3）
返回 0 个单元才承认「真没房」；不足 N 轮报 `complete=False`，monitor 跳过这栋楼
的收敛，房源原样留着。抓到任何单元立刻清零——「4 个 → 0 个 → 4 个」不该攒成一次
确认。重启清空 = 重新数，方向安全（宁可晚收敛，不可误收敛）。

OurCampus 继承同一套实现。原来那条「真没房时单轮即判完整」的测试前提随之改成
「连够 N 轮才完整」。

### 推测出来的状态，用户看不出来是推测的

平台不会说「这个单元没了」，只是把那一行从列表里拿掉，所以 `mark_stale_listings`
在房源老化后把它标成 `Occupied` 并置 `status_is_inferred=1`。

这个字段从建库起就只活在存储层，**从来没离开过**——没有一个路由、模板或 API 读
它。于是「平台报的 Occupied」和「我们猜的 Occupied」在面板和 API 上长得一模
一样，推断被当成事实端给了用户。

对 OurDomain 这类平台尤其要紧：它的 feed 只列当前可订的单元，房源没了就是消失，
所以它库里几乎所有 Occupied 都是推测的。

- API v1 的 `Listing` 加 `status_is_inferred`（布尔，openapi 契约同步）；
- 房源列表的表格和移动端卡片在状态胶囊旁加一个描边小标，带解释性 title。
  刻意做成描边而非实底：它不是第五种状态，是贴在状态旁边的一个限定词。

**这条改的是「别把猜测当事实端出去」，不是「让状态更快变准」。** 后者要动老化
阈值——OurDomain 的房源 0.2–3.1 小时就被订走，而阈值是 7 天，中间这一周它在
列表、地图和 API 上都还挂着「可订」，这次没动。

## v1.12.0 (2026-08-04)

### OurDomain 的预订和 Xior 是同一套，但 booker 是个跑不通的空壳

侦察确认 OurDomain 和 Xior 跑的是**同一套 RENTCafe，契约逐字相同**：条款页的
reCAPTCHA 类型 / sitekey / action / 回退字段名一样，登录页的四个表单和「密码
登录那条路没有验证码」一样，表单提交端点、错误判据、证件上传接口也一样，连
页面 JS 的函数名都一样。

而 `OurDomainBooker` 当时只是 `source = "ourdomain"` 一行的子类，`book()` 整个
方法体写死 Xior——`building_key_for` 来自 `scrapers.xior`，对 OurDomain 的城市
恒返回空串，于是**第一步就退出，一个请求都发不出去**。另外 `find_unit` 只认
`xr_` 前缀，`od_211053` 恒找不到；而「找不到」这条路径的表现是 race_lost
（「已被他人选走」），所以就算前面的都对，它也只会静默失败。

`monitor._AUTO_BOOK_SOURCES` 里没有 `ourdomain`，这条链路对用户一直是关的，
没有线上影响。

**重构**：`RentCafeBooker` 现在只装两个平台真正共享的部分（会话层、登录、
验证码、表单解析、证件上传、存草稿），按平台不同的只剩「怎么从一条 listing
走到 Applicant Info」，收敛成几个钩子。`book()` 里不再有任何平台分支——一旦
那里出现 `if self.source == ...`，说明钩子切得不对。

**OurDomain 入口段**（已对着真站实测跑通）：

```
① GET  {base}/{slug}/floorplans.aspx                建会话 + 轮换 TLS 指纹
② GET  rcLoadContent.ashx?contentclass=availableunits…   拿 Book now 的参数
③ POST {base}/{slug}/termsandotheritems.aspx        单元上下文进服务端会话
```

`ApplyNowClick` 的第 5 参数是 ③ 的目标页，和 Xior `ContinueClick` 的第 6 参数
同构——位置不同，写错不会报错，只会带着一组错位的参数去提交，所以两个平台各
一个解析函数。单元参数**每次现查、不持久化**：存下来的会过期，而且回答不了
「这个单元现在还在不在」；现查那张表顺带就是竞争检测。

登录之后的部分**尚未端到端验证**（要真账号 + 验证码开销）。代码里的处理是：
核对是不是 Applicant Info → 不是就重 POST 一次 Book now → 还不是就明确报错
中止。不允许猜一个 stepname 深链过去——那只会拿到空壳页，而在空壳页上填表
等于在用户真实账号下提交一份空白申请。

### 用户的 H2S 密码被回填成了 Xior / OurDomain 的账号

拆分三套平台凭据时，给 Xior / OurDomain 留了「缺失就回退到旧共用值」的兜底。
那个「旧共用值」实际上就是**用户的 H2S 账号密码**（当时只有 H2S 支持自动
预订），所以回填不是「保住了旧配置」，而是**凭空替用户编了两个他从来没注册
过的账号**。三个平台是三家不同公司、三个独立的 RENTCafe 租户，账号不通用。

实测：全部用户的 `ourdomain_email` / `xior_email` 与其 H2S 邮箱**逐字相同**，
而开了自动预订的用户里没有一个真的注册过那两个平台。

后果不止「登录失败」：

- 把用户的 H2S 密码发给第三方站点；
- RENTCafe 按 **IP** 记登录失败（连续失败锁 30 分钟），同一代理池上的其他
  用户跟着遭殃；
- 它把 Xior「**没配该楼凭据 = 该楼不参与**」的设计短路了——凭据本身就是
  开关，凭空造一份等于把开关焊死在「开」上。

两个 source 都不在 `_AUTO_BOOK_SOURCES` 里，所以一直没爆。

改两处：加载时不再回退（缺失一律取空串 = 未配置），另加一次性清理
`_ensure_rentcafe_creds_unbackfilled` 洗存量——回填只发生在加载时，但之后任何
一次保存都把它固化进了 `auto_book_json`，光改加载层防不住已经落库的那份。

清理判据是**整对**与 H2S 那对逐字相同才清；只对上一半的不动（多半是用户两边
用了同一个邮箱、不同密码）。密码在库里是 Fernet 加密的，而 Fernet 不是确定性
加密，必须解密后再比——比密文的话一条都清不掉，且完全不报错。

清理会误伤一种情况：用户在两个平台上真的用了完全相同的邮箱和密码，得重填
一次。这是安全的方向。清理是一次性的（meta flag），清完之后填什么就是什么。

### 两栋 OurDomain 楼的地址是错的，地图 pin 各偏 4–5 km

`street_address` 只有一个用途：geocode。它错了不会有任何报错——房源照常抓、
照常通知，只是地图上钉在别的地方。而两条**都是错的，还互相串了**：

| | 原来 | 实际（取自 RentCafe 页脚） |
|---|---|---|
| Amsterdam Diemen | Wenckebachweg 51, 1096 AN Amsterdam | Dalsteindreef, 1112 XJ Diemen |
| Amsterdam South East | Dalsteindreef 20-40, 1112 XC Diemen | Markelerbergpad 5, 1105 AW Amsterdam |

South East 挂的是 Diemen 那栋的街道，Diemen 挂的是第三个地方。两条新值都和
楼名自洽（Diemen 在 Diemen，South East 在阿姆斯特丹东南），交叉验证过。
geocode 缓存按地址串做 key，改地址等于自然失效，不用迁移。

### 其它

- `bookers/__init__.py` 的支持矩阵还写着「ourdomain → （无）」，和代码里明明
  注册了 `OurDomainBooker` 矛盾——查问题的人会先信文档。改对并补上状态列。
- `captcha/rentcafe_pages.py` 加 `termsandotheritems` 一行（OurDomain 的条款页）。
  和 `oleapplication` 分列两行而不是做别名：那张表的用途是记录「哪一页实测是
  什么样」，合并会把「两页碰巧一致」写成「本来就是一页」。
  对应地，`test_every_captcha_page_has_a_distinct_action` 的前提被实测推翻了
  （同一步骤在两个平台上是两页，action 当然相同），改成断言**同 action 必须
  整份契约一致**——这才是真正要守的不变量。

## v1.11.1 (2026-08-04)

### 被移出监控的城市，它的房源永远收不了敛

`mark_stale_listings` 的范围限定（只收敛「本轮完整扫描成功的城市」）有个副
作用：一旦某个城市被移出监控，它就再也不会出现在完整扫描名单里，于是**永远
不会被收敛**——7 天阈值根本没机会生效。

因此攒下了一批鬼影，全都在列表和地图上挂着「可订」，分布在近二十个已经不再
监控的城市里，绝大多数最后一次见到已是三个月前。改一次监控城市就新增一批，
而 Xior 的城市这周已经调过两次。

加第二条收敛路径：`(source, city)` 完全不在**当前配置的抓取目标**里、且
`last_seen` 超过 `orphan_days`（默认 30 天）的，一并收敛。

三个必须说清楚的边界：

- **判据是「配置里有没有」，不是「本轮扫到没有」。** 分片和节流会让一个正常
  监控的城市这轮缺席——Xior 每轮只扫 3/4 栋楼。拿本轮完整名单当孤儿判据，
  剩下那栋的房源每轮都会被误杀。所以新加 `monitored_pairs` 参数，和原来那个
  「本轮扫全了的」分开传。
- **不知道监控范围就整条跳过。** `_monitored_pairs` 读配置失败时返回空列表，
  那一刻整库看起来「全都不在监控范围内」。传 None / 空一律跳过孤儿收敛，
  宁可留着鬼影，也不能因为一次配置读取失败把整库判死。
- **孤儿路径收敛 `status != 'Occupied'` 的全部状态**，包括 Reserved。一个
  已经完全不再观察的城市，我们手上任何非 Occupied 的状态都同样无从核实。
  在监控范围内的 Reserved 仍然不动——H2S 上它本来就能合法挂很久。

宽限期取 30 天而不是 7 天，是为了防误伤：临时关一天再打开的城市不该被判死。

收敛不产生通知：这是直接 UPDATE，同时改 `status` 和 `last_status`，不经过
`diff()`，所以既不写状态变更记录也不推送。执行后可用房源大幅减少，剩下的
全部落在当前监控范围内。

---

## v1.11.0 (2026-08-04)

四个平台全部对用户可见（OurCampus / Xior 移出影子模式），外加一轮逐页走查
挖出来的一批问题——其中五个有实际功能影响，都不报错。

### 现状盘点

| 平台 | 累计轮次 | 出错轮次 | 均产出 | 入库 | 判断 |
|---|---|---|---|---|---|
| holland2stay | 411 | 27 (6.6%) | 17.9 条 | 289 | 稳定，主力 |
| ourdomain | 420 | 1 (0.2%) | 0.4 条 | 17 | 稳定 |
| xior | 288 | 12 (4.2%) | 2.8 条 | 66 | 稳定 |
| ourcampus | 267 | 0 | **0.0 条** | **0** | **解析器未经验证** |

出错率里 H2S 的 6.6% 和 Xior 的 4.2% 基本都是 Cloudflare 与限流退避，有重试
兜底。OurCampus 零出错但也零产出——见下。

自动预订：H2S 实装并在真实账号上跑通；Xior 的链路**实测驱动到申请表这一步**
（系统填表 + 代传证件），但 `_AUTO_BOOK_SOURCES` 仍只有 holland2stay，用户
触发不了。

### OurCampus / Xior 移出影子模式

Xior 现在会正常给用户发通知了。解除影子不会补发积压：`diff()` 只对真正的新
id 产出 `new_listings`，库里那 39 条已存在的 Xior 房源不会再冒出来一次；
stale 收敛走的是直接 UPDATE（同时改 `status` 和 `last_status`），也不经过
diff，所以不会伪造出一批「变成 Occupied」的通知。自动预订仍然只对 H2S 开。

OurCampus 那边发现的其实不是「影子模式」，是**它压根没在跑**：

    SOURCES=holland2stay,ourdomain,xior          ← 没有 ourcampus
    SHADOW_SOURCES=ourcampus,xior

`load_config` 会把不在 `sources` 里的影子项静默丢掉，于是 ourcampus 既没被
抓取，也不会出现在数据健康面板的最近轮次里——而配置文件读起来完全正常，
日志里也没有任何线索。成因是设置面板保存一次就会重写 `SOURCES`，而旧版那个
白名单漏了 ourcampus（已修），它被无声删掉之后，`SHADOW_SOURCES` 里的残留项
就成了一个「它还开着」的假象。

静默丢弃本身是对的——影子名单不该反过来把一个没启用的平台打开。补的是一条
WARNING：配置读起来像开着、实际是关着，这种落差必须有地方能看见。

### 合同类型筛选把同一种合同拆成了两个选项

`Indefinite` 和 `Onbepaalde tijd` 是上游对同一件事的两种写法。图表层
（v1.10.0）已经合并，**筛选层完全没跟上**：下拉里两个并排列着，
`ListingFilter` 按字面子串比对——勾了 `Indefinite` 的用户收不到写着
`Onbepaalde tijd` 的房源。

实测约四分之一的用户设了 `allowed_contract=['Indefinite']`，而长租房源里有
近两成写的是荷兰语原文。这些用户一直在少收房源，不报错、不留日志。

同义表提到 `models.FEATURE_SYNONYMS`，三处共用一张表：图表合并计数、筛选
下拉去重、`ListingFilter.passes` 比对前归一。再加 `__post_init__` 在落库前
归一——这一条是给 iOS 端补的：`/api/v1/filter/options` 不再返回荷兰语原文，
而 `FilterEditView` 的勾选行只从 options 渲染，存量用户会看到「1 selected」
但一行都没打勾。归一放在 dataclass 而不是 API 和表单各写一遍，所以存量数据
读一次就自愈。

### 打开 /system 会改写整个进程的环境变量

这一页为了显示 `.env` 的当前值调了 `load_dotenv(override=True)`。那不是
「读」是「写」：它把 `.env` 里每个键强灌进 `os.environ` 且永久生效，而这一页
每 30 秒整页自动刷新一次。

后果是 docker-compose `environment:` 里设的值（`NO_PROXY`、代理等）会被
`.env` 的同名键顶掉；`WEB_PASSWORD` / `FLASK_SECRET` 一变，正在用的会话当场
失效。走查时就是被它踢回登录页才发现的——起初进程里 `WEB_PASSWORD` 是空的
（鉴权关着），打开 `/system` 之后 `.env` 里的真密码被灌进来，鉴权自己开了。

改成读前快照、读完还原，副作用不出这个函数。页面显示的仍是 `.env` 的当前值。

### 静态资源版本号改成按文件内容自动算

部署后验证线上时发现：`base.html` 已经是 `?v=34`，而 `/login` 返回的还是
`?v=28`——`login.html` 不 extends `base.html`，自己写了一份版本号，跟着漏了
6 次。**登录页是新访客看到的第一个页面**，它一直在发过期样式表。

同一个坑 v1.10.0 刚踩过一次（改了 `app.js` 忘了改版本号，统计页在有缓存的
浏览器里整页空白）。两个地方各写一份要手动维护的版本号，就一定有一份会忘。
改成 `web.asset('design.css')` 按 mtime+size 算摘要，文件一变版本号自动变。

### 全站 `<select>` 没有下拉箭头

`.listing-filter-card` / `.settings-form` 用 `background:` 简写设底色，简写会
把 `background-image` 一并重置成 `none`，而箭头正是 `.form-select` 的
`background-image`。筛选卡片和设置页的每一个下拉框看上去都和文本框一模一样。

改成 `background-color:`，并把箭头提成 `--caret` 变量，让原生 `<select>` 和
自定义多选共用同一张图。

### 一条错误通知能撑爆通知面板

`.notif-item-body` 没有 `overflow-wrap`，一条含 `TargetClosedError` 堆栈的
Monitor Error 把列表撑到 2632px（面板才 365px），整个通知面板出现横向滚动
条。通知正文不一定是人写的句子，加 `overflow-wrap:anywhere` 并截到 6 行。

### 仪表盘出现过「Active Cities 3 / of 1」

分子取的是库里出现过的城市数，分母取的是当前配置的目标数，两个口径拼成一个
比值，于是分子能大于分母。大数字改成配置目标数，副标题改成不含 “of” 的独立
说明。

### 排版一致性

- 筛选控件原生 `select`/`input` 40px、多选 42px → 统一 38px。两种控件要用
  不同属性才压得住：前者 `min-height` 拦不住自带的 padding + line-height，
  得用 `height`；后者基础样式是 `min-height:42px` 且内部 `flex-wrap`，写死
  `height` 会裁掉内容
- 状态徽标 `letter-spacing:.5em` 把 Book 渲染成 `B o o k`，看着像渲染坏了。
  去掉拉伸，等宽由 `width:72px` 保证
- 仪表盘首张卡标签 12px 大写，和同排另外四张（13px 原样大小写）对不上
- 设置页警告图标紧贴上一句句号，看着像少打了空格

### 中英文串台

逐页切中英文走了一遍 16 个页面：

- `/listings` 英文界面显示 “共 49”——模板把「共」写死了
- `/users/new` 标签页和面包屑显示「新增用户」——路由里 `title` 写死中文
- 客户端管理「推送设备」整个 tab 是硬编码中文：9 个表头 + 2 个状态徽标 +
  空态 + confirm 文案；`flash("会话已撤销")` 也绕开了早就存在的翻译键
- 设置页 “Enable H2S, OurDomain, or both” 只提了 4 个平台里的 2 个；
  OurDomain 楼盘提示写「当前支持 Amsterdam Diemen」但底下列了两个
- 4 个页面标题缺 `· FlatRadar`；`/system` 写「每 60 秒刷新」而代码是 30 秒
- 申请人档案说明里的 `**系统只填表，不付款**` 星号被原样显示——那里是纯文本，
  模板不做 Markdown 渲染

`test_templates_i18n` 盯着不让它们回来。这类问题不报错，只有真的把界面切成
英文一页页翻才看得见。

### 测试

新增 `test_system_env_isolation` / `test_contract_synonym_filter` /
`test_templates_i18n` / `test_asset_versioning` / `test_shadow_sources_config`，
每一个都验证过「去掉修复就会红」。1727 → 1757 passed。

API 侧用真实 Bearer token 打了 40 个路由复核：GET 全 200，写操作全 200，
鉴权边界 403/401/401 正确。

### 楼栋数变少反而更容易被限流——分片管不了「多久抓一次」

2026-08-04 生产实测。把 Xior 监控范围从 30 栋缩到 4 栋（只留 Eindhoven +
Amsterdam）之后，**Xior 反而更慢了**：

| | 轮次 | 均耗时 | 峰值 |
|---|---|---|---|
| 30 栋 | 156 | 54.7s | 377s |
| 4 栋 | 97 | **65.4s** | 274s |

近 3 小时里 5 轮 `RateLimitError`，平均耗时 273 秒（全是 30s+60s 退避堆出来的）。

根因不是"抓得多"，是**同一栋楼被打的频率**：

| | 单栋楼被抓的频率 |
|---|---|
| 30 栋 ÷ 分片 3/轮 | 每 10 轮 1 次 ≈ 10–15 分钟 |
| 4 栋 ÷ 分片 3/轮 | 几乎每轮 ≈ 60–90 秒（高峰时段 `MIN_INTERVAL=20`） |

约 **10 倍**。限流按单个 target 被打的频率算，30 栋轮着抓时每栋自然稀疏，
4 栋轮着抓就全挤在一起了。

**分片压根不控制频率**——它管的是「每轮抓几个 target」，怎么调都救不了这个。
新增 `SOURCE_MIN_INTERVALS`（形如 `xior:600`，秒），控制「同一个 source 多久
抓一次」，默认 `xior:600` ≈ 恢复到 30 栋时期每栋楼的实测频率。

节流是逐 source 的：H2S 才是真正出房源的那个，高峰高频轮询是有意为之，不能
被 Xior 拖累。执行顺序是**先节流再分片**——反过来会让被跳过的 source 白白推进
分片游标，后面的 target 被系统性少抓。

时间戳存 meta，重启后仍然生效：否则频繁重启会绕过节流，而重启往往正是因为
出问题了，那恰恰是最不该加压的时候。读写 meta 失败一律 fail-open（宁可多抓
一轮，也不能把整个 source 静默停掉）；被跳过的那一轮**不刷新时间戳**，否则
每次跳过都把闸门往后推，source 会被永久饿死。

顺带修掉一个只在测试里才显形的边界：`last == 0`（从没抓过）在时间戳很小时会被
算成「刚抓过」而跳过首轮。生产上 unix 时间戳很大，掩盖了它。

### Xior 半自动预订：整条链路实测走通，并修掉 6 个「不报错的错」

2026-08-03 用真实账号对真实单元逐步实测，前 5 步全部走通（open → 条款 → 登录 → 选房 → 申请表），第 6 步保存被平台的证件要求挡住，已改为由系统代传证件。

这一轮暴露的错误有一个共同点：**全都不会报错**。服务端对不认识的字段静默丢弃，HTTP 一律 200，日志干干净净。

#### 登录：四个细节，任一错都是 200 + 空 body

| 字段 / 行为 | 原实现 | 实测真值 |
|---|---|---|
| `formName2` | 写死表单 id `Login` | 服务端下发的 `mylistlogin` |
| `CheckUserAuth` | 探测 `1` / 登录 `0` | 探测 `1` / 登录**空串** |
| 空响应体 | 当成失败 | **空 = 成功**（AJAX 成功回调只填错误框） |
| JS 跳转 | 只认 `window.location` | 还有无前缀的 `location.href=` |

把空 body 当失败的后果特别有迷惑性：登录其实早就成功了，流程却一路报到 `race_lost`。

#### 选房：解析失败伪装成了业务结论

`onclick` 里的引号是 HTML 实体 `&#39;`，正则按真引号写，页面上 **20 个单元一个都没解析出来**，`find_unit()` 返回 None，流程如实报告「该单元已被他人选走」——而单元就好端端在页面上。

#### 申请表：15 个字段名全是错的，命中 0

字段名是从页面**可见标签**抄的（「First Name」→ `FirstName`），真名是 `ProspectFirstName`。而旧测试全绿，因为它写的是 `got[FIELD_MAP["first_name"]]`——**拿映射表去查映射表的输出，在验证自己**。

真实字段名有三处根本无法硬编码：名字里嵌 prospect id（`drpGender2057105`）、下拉提交内部数字 id（Netherlands→`2`）、Xior 自定义字段带上游的拼写错误（`ADDITIOAL`）。新增 `bookers/rentcafe_form.py` 从**当前页面**解析字段名、下拉取值和标签。

还有两格是**改了标签复用字段**，只看字段名会填错格：`Currentaddr{pid}Addr2` 标签是「University」，`drpDLCountry` 标签是「Nationality」。现在按标签判断这一格实际在要什么。

#### 保存被拒却报成功

`save_applicant_info()` 压根没检查响应体，服务端回「Please upload required documents before Proceeding.」，`book()` 照样返回 `draft_saved / success=True`。这是这条链路上后果最重的一种错法——用户读到「已为你起草申请」会安心去准备证件，而实际一个字都没存下。新增 `save_rejected` phase，并把「响应体空=成功、有 showMessage error=失败」这条全站判据抽成一个函数。

#### 预订链路没走代理

抓取侧一直走代理池，预订侧却是裸 `req.Session()` 直连。实测同一 IP 连跑 3 轮后 `POST rcformsave.ashx` **整片 403，而同时 GET 完全正常**——WAF 按「IP × 写接口」限流。等于把整条链路上最要紧的一步放在最容易被限流的位置。已改为从代理池取出口，且**一条会话固定一个 IP**（流程状态在服务端会话里，中途换 IP 可能被判失效）。

### 系统代传证件（`applicant_docs.py`）

平台在 `ID/Passport Upload*` 到位前**拒绝保存申请表的任何内容**，而自动预订是异步触发的——所以不存在「用完即走的透传」，文件必须提前存好。上传 URL 里带 `ProspectID`，文档是**按申请**传的，用户手动预先传一次并不能一劳永逸。

这是一个知情的取舍（当前部署只服务少量熟人，自动预订未对外开放）。取舍既然做了就把风险降到底：加密落盘、不进每轮都要加载的用户配置、面板上看得到也删得掉、校验规则抄自上传控件自己的 JS 好在面板上就拦下来。

### 边界确认：锁定在付款那一步

`ApplicationCharges` 的表单 `PaymentApplicationChargeStep1` 要填 `sAcct`(IBAN) / `sSwiftCode` / `sName`，页面原文「all administration fees will need to be paid」。代填银行账户是硬限制，所以边界只能停在 Save。`draft_saved` 因此**不代表抢到房**，通知文案据此写成「请点击链接付款，否则可能被他人抢先」。

完整流程实为 **11 步**（此前文档记的 9 步不全），且后面几步不能深链——内容走 `rcLoadContent.ashx?contentclass=<步骤名>` 拉。

### 申请人档案扩充

对着真实表单发现要的东西比档案里存的多：地址拆成四格（原来 `postcode_city` 一格塞两样）、住所性质、背景调查三问。

**背景调查三问不由系统作答。** `drpEverEvicted` / `drpEverConvicted` / `drpCriminalCharges` 是关于用户本人的**事实陈述**，代勾「我授权你做背景调查」是用户授权过的，代答「我没有前科」不是——留空即视为未回答，档案判为不完整，不提交。

老档案的 `postcode_city` 有兼容拆分，不会因为字段拆开就突然被判不完整；顺带修掉一个静默错标：只填邮编没填城市时，旧逻辑把整串当城市填进 City 格。

> `monitor._AUTO_BOOK_SOURCES` 里**仍然没有 `xior`**，这条链路对用户是关闭的。代传证件之后的保存尚未端到端验证（已知证件缺失时 Gender 和背景三问是唯一四个不落库的字段，推测传了证件会一并落库，但没验证）。

### H2S 被 Cloudflare 拦死后无法自愈——sticky 出口 IP 没有逃生口

2026-08-03 生产事故：H2S 在 18:45 收到「要求重新校验」，重解挑战连续 3 次 90s 全部失败，熔断退避 30 分钟。

根因不是「该不该固定出口 IP」——固定是对的，浏览器跨轮复用 2 小时，clearance 在这期间可复用。错的是把「稳定」实现成了**永久固定**：sticky session id 是 `sha1(source)` 的常量，同一个 source 永远拿到同一个出口 IP。于是 403 的恢复路径（`invalidate_session()` → 下轮重建浏览器）拿到的**还是那个被烧掉的 IP**，形同虚设。

修法是给 H2S 也开 `rotating_proxy=True`。关键在于**换 IP 的时机是「建浏览器」而不是「每请求」**：浏览器存活期内 IP 不变，clearance 照常复用；而重建浏览器本来就要重解挑战，那一刻换个新 IP 是免费的。也就是说固定 IP 在重建那一刻省不下任何东西，却让被烧的 IP 永远换不掉。

一般规律记进 ARCHITECTURE §5.11：**任何「为了稳定而固定」的资源，都要想清楚它坏掉之后怎么换。**

顺带修一条误导性日志：熔断期间原本打「H2S source 熔断中且本轮**没有其它 source 任务**」，但触发条件其实是 `not fresh`（本轮一条房源都没抓到）。Xior 分轮抓之后这很常见——某一片正好全是没库存的楼。旧文案会把人引去查 `SOURCES` 配置。

### Xior 凭据改为按楼栋（一栋楼一个账号）

实测发现 **Xior 每栋楼是独立的 RENTCafe property 门户**：各有自己的 host、property 代码和 `myOlePropertyId`，登录页原话是「your **<楼栋名>** Guest Account」，不是「Xior 账号」。生产库里 4 栋有数据的楼对应 4 个不同 host，cookie 不跨主机。

| 楼栋 | 属性代码 | `myPropertyId` |
|---|---|---|
| Eindhoven Zernikestraat | NLEZERNS | 185589 |
| Aachen Vaals Katzensprung | NLVSNEES | 185795 |
| Utrecht Willem Dreeslaan | NLUWIDRS | 186237 |

原来的单对 `xior_email` / `xior_password` 建立在「Xior 一个账号」的错误认知上。改为 `xior_accounts = {building_key: {email, password}}`，key 与 `XIOR_CITIES` / `BUILDINGS` 同一套，密码逐条加密。

**查不到该楼凭据时绝不回退到别楼的。** 拿 A 楼账号去 B 楼门户登录必然失败，而失败计入 RENTCafe 的 IP 级尝试限制（连续失败锁 30 分钟）——等于用一次注定失败的请求去消耗真正需要它的额度。存量单对字段只在用户完全没配过按楼凭据时兜底，一旦开始按楼配置就彻底忽略（那对值只可能对某一栋楼有效，而无法判断是哪栋）。

面板上改成按楼栋增删：下拉里只列**监控中且还没配**的楼（没在监控的楼不会产出候选，给它配账号没有意义），配上的移出下拉、删除后放回。密码留空 = 不修改，与其他平台字段一致；邮箱清空 = 删除该楼。

`_collect_booking_candidates` 里三处硬编码的 `source == "holland2stay"` 收敛成 `_can_auto_book(user, listing)`，它同时回答「这个 source 的预订流程实现了没有」和「用户有没有这栋楼的账号」——正如设计意图，**凭据本身就是开关**，不需要额外的 per-building 开关。

**Xior 暂不放进 `_AUTO_BOOK_SOURCES`**：`bookers/rentcafe.py` 里第 3 步之后的多步表单仍是没走过流程硬猜的草稿，放开等于拿用户真实账号提交半懂不懂的表单。凭据判定已就位，流程验证完只需改那个元组。

### 修正 — Xior 没有抽签，却有一批房源被标成「摇号中」

`Vacant Unrented Not Ready` 一直被映射成 `Available in lottery`。**Xior 根本没有抽签机制**——"lottery" 是 Holland2Stay 专有概念（H2S 的 availability filter id=336 摇号池）。当初大概是想表达「不能立刻入住」，顺手抓了个最近的现成状态。

两个 Yardi 状态的区别只在**为什么现在没人住**（`Notice Unrented` = 住户递交了退租通知还没搬；`Vacant Unrented Not Ready` = 已空置但房间没收拾好），对用户没有差别。实测两类单元都带 `applyOnlineURL`、`availableDate` 分布完全重叠、都得过闸② 的 floorplans.aspx 权威校验——**同样可订**。

错标的后果不只是标签难看：

- 面板给用户显示橙色 "Lottery" 徽标，等于告诉他们去参加一个不存在的摇号；
- stale 收敛对 lottery 用的是 **2 天**阈值而非 7 天（`_STALE_LOTTERY_STATUS`），这些单元会以 3.5 倍速度被推测成 `Occupied`。

改为映射到 `Available to book`。「还不能入住」这层信息由 `available_from` 表达，闸①（60 天窗口）已经把太远的滤掉了，不需要再借一个语义不符的状态来编码。

OurDomain 也有一条 `text-warning` / 含 `wait` → `Available in lottery` 的映射（`OURDOMAIN.md` 记作「等位中」），语义上更接近排队但仍不是摇号；目前库里 0 条命中，暂未改动，待有真实样本时再判断。

### Xior 监控扩到全部 30 栋，配分轮抓取

此前只监控 4/30 栋。扩容的前提是先解决单轮预算：新上线的轮次遥测量出 Xior **每栋楼 13.9 秒**（其余三个 source 每 target 只要 1–4 秒），30 栋 ≈ 417 秒/轮，而 `CHECK_INTERVAL` 是 300 秒。而且 H2S 排在其它 source **之后**执行——直接配 30 栋等于每轮把真正出房源、自动预订已跑通的那个 source 推迟 7 分钟。

慢是 2026-08-02 `4d71b9d` 有意为之：请求间隔 1.5s → 5s，因为限流按速率算，1.5s 时瞬时 ~40 req/min 撞上端点 ~15–20/window 的限制。那条 commit 当时就写明了「楼栋数增加时应改为分轮抓取，而不是把这个值调小」。

新增 `SHARD_SIZES`（默认 `xior:5`）：target 数超过阈值的 source 每轮只抓一个切片，游标存 SQLite 逐轮轮转，6 轮覆盖 30 栋，单轮 Xior ≈70 秒。游标持久化是必要的——放内存的话每次重启都从第一片开始，后面的楼栋会被系统性少抓。任何读写异常都回退成全量抓取：宁可慢一轮，也不能悄悄漏抓楼栋。

### Xior 自动预订：侦察（尚未实现）

**修正 `XIOR.md` §8.3 一处会花冤枉钱的错误。** 原文写「RENTCafe 全线使用 reCAPTCHA Enterprise，一个 v3 sitekey 通吃」。实测三页各不相同：

| 页面 | v3 类型 | sitekey | action |
|---|---|---|---|
| `oleapplication`（第 2 步条款） | **标准 v3**（`api.js`） | `6LcjBc4U…` | `start_application` |
| `guestlogin` | Enterprise | `6LfBeqEa…` | `UserLogin` |
| `register` | Enterprise | 同上 | `GuestRegistration` |
| `flexregistrationlandingpage` | **无** | — | — |

`captcha/solver.py` 的 `solve_v3()` 把 `enterprise=1` 写死、默认用 `6LfBeqEa`——对条款页两项都错，解出来的 token 服务端不认。已改为按页传参，实测表编码进新增的 `captcha/rentcafe_pages.py`（未侦察过的页返回 `None`，**不给默认值兜底**——随便挑一个当默认等于把踩过的坑重新埋回去）。

其余进展：

- **第 1–2 步可以脱离库存侦察。** 拿一条 2026-05 就已 Occupied 的 `applyOnlineURL`，2026-08-03 仍返回 HTTP 200 完整表单。RENTCafe 侧也不需要浏览器，curl_cffi 直连即可。
- **`flexregistrationlandingpage.aspx` 不是验证码旁路**（§8.5 原第一个问号，答案是否定的）。它自身确实无验证码，但两个出口都指回带 Enterprise 验证码的 `register.aspx`。
- **发现一个排在 reCAPTCHA 之前的风险**：`MoveInDateEncr` / `QuotedRentEncr` 是入住日与租金的**加签副本**（`base64(明文)-签名`）。改明文签名就对不上，换日期必须由服务端重新下发，接口未知。验证码是花钱能解的，签名机制不是。
- 第 2 步的完整字段契约已记录。

**第 3 步（Applicant Info）及之后仍未到达**，需要真实账号登录 + 一个在售单元。刻意没有继续写那部分代码——现有 `book()` 里那个 `for _step_index in range(6)` 循环就是没走过流程硬猜的，再加一层猜测只会让后面更难拆。

### 可观测性 — 排查不再依赖 ssh

v2.0 两个方向之一。起因很直接：这套系统里**每一个 bug 都是靠人肉 grep 日志找到的**。逐 source 隔离那条是数了 36 轮日志，errorCode 误判是统计了 144 次出现，完整率下滑是逐行读 `完整扫描 N/M`。而 2026-06-13 起那次 7 周静默停摆是同一短板的极端形态——进程活着、心跳正常、容器全程 healthy，只是不干活了。

问题在于 `/health` 只回答「循环还活着吗」，`last_scrape_count` 又是个每轮被覆盖的标量。「昨晚 Xior 为什么只有 2/6」这类问题在系统里**没有数据源**，只能翻日志。

方案与验收标准见 [OBSERVABILITY_PLAN.md](OBSERVABILITY_PLAN.md)，判据的设计理由见 ARCHITECTURE §5.12。

**轮次遥测落库。** 新表 `round_stats`，每轮每 source 一行：房源数、任务数、完整数、耗时、错误类型。数据全部来自 `_dispatch_isolated()` 已有的局部变量，不新增任何抓取动作。每个 source 跑完立即写，不攒到整轮结束——「整轮全失败」恰恰是最该留痕的情形，而那条路径会直接上抛。保留 30 天，剪枝自带每小时一次的节流。

**数据健康判定。** `mcore/health.py` 把遥测聚合成分 source 的 `ok` / `warn` / `down`。这里最要紧的一条是**「抓到 0 条」不等于「坏了」**：Xior 四栋楼常态零可订，OurCampus 官网自述排队 16–18 个月，单看零房源会把它们永久钉在告警上，而被无视的告警等于没有告警。所以零房源规则附带「该 source 在窗口内曾抓到过」的前提，语义变成**「本来有房，突然全没了」**——这正是解析器被上游改版打坏的特征。

**退化告警。** `mcore/watchdog.py` 每轮巡检，只发 admin。两处刻意的设计：节流状态写 SQLite 而不是内存（supervisor 的 autorestart 恰恰会在故障时频繁重启，节流放内存等于最该节流时失效）；恢复也发通知（只报警不报恢复，等于逼人继续 ssh 上去确认好了没有）。

全局静默规则的基线取 listings 表而不是判定窗口：窗口只有二十来轮（约 2 小时），而那次停摆持续 7 周——拿窗口做基线的话，故障满两小时后告警会自己闭嘴。

**数据退化不改 `/health` 的状态码。** 重启治不好解析器对不上，只会打断正在进行的抓取。

**日志可查。** `/api/logs` 之前只能 tail，页面上的搜索是纯前端的——只在已拉取的 500 行里找，所以「凌晨三点发生了什么」仍然只能 ssh 上去 grep。现在支持关键字 / 级别 / 时间范围，服务端过滤，并从文件尾部分块读取（原来每次轮询都 `readlines()` 整个文件）。

过滤按**记录**而不是按行——traceback 的续行既没有时间戳也没有级别，逐行过滤会把 traceback 拦腰截断，而那恰恰是最需要看的部分。命中 0 条时会同时返回扫描范围，因为「没有这条日志」和「没扫到那么远」是天差地别的两件事。

**面板。** 新增 `/monitoring`（admin）：分 source 健康卡、最近 30 轮明细表（格式 `房源数 (完整/任务)`）、当前活跃告警。

**时间一律按 `TIMEZONE` 显示。** 库里所有时间戳存 UTC（canonical，`/api/monitoring` 返回的也是 UTC），但页面必须转成配置时区再显示——容器跑在 `TZ=Europe/Amsterdam`，日志的 `asctime` 就是那个时区。面板一开始直接渲染 UTC 原文，夏令时期间和 `/logs` 差整整两小时，而这两个页面本来就是对着看的（在日志里定位到某一刻，再回面板看那一轮的各 source 明细）。

告警文案里的时间同样转换——那些文案会作为 admin 推送发出去，塞一个 UTC ISO 等于让收到的人自己换算。顺带修了 `/system` 页面上一直以来直接渲染 UTC ISO 的 `last_scrape`，新增 `local_time` Jinja 过滤器统一处理。

顺带修掉三个改动过程中暴露的问题：

- `/api/logs` 遇到不符合日志格式的文件（第三方输出、非标准 formatter）会把所有行并成一条记录，`lines` 上限彻底失效——tail 500 行会返回整个文件。
- H2S 抛出非 `BlockedError` 的异常时不会留下遥测行（熔断分支只捕获 `BlockedError`），遥测里 H2S 会在这类失败时凭空消失。
- 日志页多个在途请求返回顺序无保证，先发的后到会用旧结果盖掉新结果——实测表现是清空过滤后页面反而空了。已加请求序号，只有最新的允许渲染。

### 文档更正 — 自动预订早已在真实账号上跑通

`ARCHITECTURE.md` §9 一直写着「自动预订只覆盖 H2S，且**未在真实账号上跑通完整下单验证**」。这条不成立：2026-05-22 05:34:44 真实抢到 Eindhoven `beukenlaan-143-163`（€1120/月，入住 2026-06-04），库里留有真实 order_id 和 iDEAL 付款链接。

这条错误说法有实际代价——已有若干用户开着自动预订且 `dry_run=0`，按文档看等于他们在依赖一条从未验证过的路径。

同一次事件的记录还暴露了多用户竞争：另外两个用户在**同一秒内**尝试同一套房，分别收到「房源已被他人抢先预订」和「A process is already handling this booking」。§7 补上了这段，作为 `_assign_auto_book_candidates()`（同一 listing 每轮只交给一个用户）存在的实证理由，以及三种预订失败（`race_lost` / `blocked` / 平台侧并发锁）的区分。

顺带：§7 里 RENTCafe 预订引擎的说明补上 OurCampus，并写明当时的阻塞点是 reCAPTCHA + 未侦察的多步表单（技术上可行，约 $0.003–0.005/次解题，缺的是有人把条款页之后的流程走一遍）；§9「三个 source」改为「各 source」，并写明各平台体量极不均衡、H2S 是绝对主力。


### 决定 — 放弃 Android Play Store 上架

Android 客户端的分发方式定为 **Release 页直接下载签名 APK**，不进 Google Play。

连带取消：Google Play Billing 内购（它依附于 Play，脱离 Play 无法使用）、Data Safety 表格、12 人 14 天封闭测试、商店截图、Google Play App Signing（改为本地 upload key 自签，CI 已在这么做）。A6 里唯一与上架无关的 Material 3 视觉打磨不受影响，若要做则独立成项。

`ANDROID_PLAN.md` 顶部加了决定说明，A6 / RC5 / 依赖图 / 风险表 / 技术选型表里与上架相关的条目全部标注已放弃；规划内容本身保留，作为「当初考虑过什么」的记录。`FUTURE_PLAN.md` 的「第一期：Android Play Store 上架」同样标注放弃。

### CI — Android 只出 APK

`build.yml` 的 Android job 去掉 `bundleRelease` 和 AAB 上传步骤——AAB 只有上架才用得上。

**连带修掉一个一直存在的问题**：README（中英）、guide（中英）、登录页下载按钮总共 7 处 Android 下载链接指向的都是 `app-release.aab`。`.aab` 是 Google Play 的分发格式，**用户下载了装不上**——Android 不能直接安装 bundle。现已全部改为 `app-release.apk`。

（这些链接在停止产出 AAB 之后还会直接 404，与 v1.6.x 修过的那次「Android 下载链接 404」是同一个坑。）

### 文档 — iOS 维护说明里不该有 Android 的事

`iOS_README.md` 开头一直在讲 Android 的进度与分发（原文：大型功能开发已转向 Android Play Store 上架 A6……Android parity A0–A5 已完成）。那是 Android 的状态，放在 iOS 文档里既容易过时、又没人会去那儿找——事实上这次就是它先过时的。改为一句指向 `ANDROID_PLAN.md` 的链接，并写明本文件只讲 iOS。

保留的三处 Android 提及是 iOS 与 Android 的**接口关系**（共享 API 契约、共享 `openapi.json`、推送路由共用 `platform` 字段），那些属于 iOS 文档该说的。

## v1.10.0 (2026-08-03)

接入第四个平台 OurCampus，并为「新平台已上线、但先不对用户开放」这件事补上机制。
顺带修掉一个与前几个版本同源的判据 bug，以及把三份平台文档改成只写当前判断。

**升级须知**：无破坏性变更，无需数据迁移。新平台默认不启用——要开启需要同时设
`SOURCES` 里加 `ourcampus` 和 `OURCAMPUS_CITIES`。

---

### 新增平台 — OurCampus

Greystar 旗下与 OurDomain 同属一家的学生住房品牌，同一套 RentCafe/SecureRC 后端。

`OurCampusScraper` **继承** `OurDomainScraper`，只覆盖 `_fetch_units_html()` 一个方法。复用的部分：TLS 指纹池与冷却状态机（模块级共享——两边打的是同一个 SecureRC 集群，某个指纹被烧对双方同时生效）、每次尝试换出口 IP、同 session 内 403 重试、floorplans.aspx 发现、单元行解析的 `data-selenium-id` / `data-label` 双策略、状态映射、Occupancy 反推、Listing 映射。

覆盖的那一个是请求形状：OurCampus 用 **POST + `floorPlans[]` 表单体**（照抄它自己前端的 `$.load(url, {floorPlans: names})`），OurDomain 用 GET + query string。同栈不等于同接口；两边 host 的 WAF 策略也不同——OurDomain 的 host 上 POST 会 403，OurCampus 的不会。

`Listing.id` 用独立前缀 `oc_`（OurDomain 是 `od_`）。两者是不同的 RentCafe property（186609 vs 184283 / 182801），unit id 跨 property 是否全局唯一没有保证，撞车会让两条房源在 `storage.diff()` 里合并成同一行、互相覆盖字段。

配置：`SOURCES` 加 `ourcampus`，`OURCAMPUS_CITIES` 默认 `OurCampus Amsterdam Diemen,diemen`。UI 平台标签 `OurCampus` / `OC`，过滤维度与 OurDomain 相同。

**两条要如实说明的局限**：

1. **期望值要放低。** 只有一栋楼，且官网自述等待期 16–18 个月——排队制而非先到先得，「秒级通知」在这里的价值远低于 H2S。接入是产品决策，不是因为投入产出比划算；评估结论见 `docs/SCRAPING_RECON.md` §4。
2. **单元表 HTML 至今没有真实样本。** 接入时该楼零可订，所以解析器是复用 OurDomain 的、赌两边同模板——依据是两边空响应的结构指纹完全一致。选 POST 同样无法用响应验证（两种形状都只能拿到空面板），依据是「和它自己前端一致」。为此加了下面两道保险。

### 新增 — 影子 source（`SHADOW_SOURCES`）

列出的 source 照常抓取、写库、参与 stale 收敛和面板统计，但**不发任何通知**——用户渠道、面板 notification feed、APNs/FCM 全部跳过。用于新平台对用户开放前的静默验证。

过滤发生在 `storage.diff()` **之后**：diff 必须照常执行，房源才会进库、状态变更才会被记录；被拦掉的只有「告诉谁」这一步。`last_scrape_count` 仍按 `fresh` 计数——它回答的是「抓到多少」，不是「通知了多少」。

配置校验：不在 `SOURCES` 里的条目会被剔除（不在里面就压根不会抓，写了是笔误）。

一个刻意的取舍：影子期间这些 listing 的 `notified` 一直是 0，但解除影子后**不会补发历史**——`diff()` 只对真正的新 id 产出 `new_listings`，老的不会再冒出来。否则解除影子会给用户灌一堆积压通知。

OurCampus 首次上线即以影子模式运行。

### 新增 — OurCampus 抓取留档

`data/ourcampus_capture.txt`（`OURCAMPUS_CAPTURE_PATH` 可改）。每次 availableunits 请求记一行摘要：时间、floorplan、响应字节数、是否是合法面板、有无 unitrow 痕迹、解析出几个单元。

**只在有看头时才附完整 HTML**：解析出单元了（第一份真实样本），或响应里有 unitrow 痕迹但解析出 0 个（正是解析器对不上的信号，也正是下面那道守卫兜不住的情况，会同时打 WARNING）。平时零可订只留摘要行，一天几百轮几十 KB；写满 8MB 后停止，不轮转——这是一次性证据，不是运行日志。留档失败全吞异常，不影响抓取。

### Bug 修复 — 零单元不再等同于「没有房」

RentCafe 在无可用单元时返回的仍是一张**结构完整**的搜索结果页，与「拿到了别的页面 / 响应结构变了」一样都是 HTTP 200 + 解析出 0 个单元。此前只看解析结果，两种情况无法区分——后者会被当成「这栋楼没房」，进而让 stale 收敛把存量 listing 全部标记为已下架。

现在零单元时额外检查响应里有没有 `Apartment Search Result` 这个面板标题：有 → 真没房（`complete` 不变）；没有 → 标记 `complete=False`。OurDomain 一并受益。

这是 `ARCHITECTURE.md` §5.10 记录的同一类判据错误的**第四个实例**（前三个：CF 挑战判据、Xior `errorCode`、H2S 分页）——「没拿到数据」不等于「确认没有数据」。

### 文档 — 平台状态文档改为只写当前判断

`XIOR.md`（540 → 307 行）、`OURDOMAIN.md`（534 → 304 行）、`SCRAPING_RECON.md`（384 → 252 行）全部重写为单一时态，删掉动手前的设计草稿、工程量估算、带日期的数据快照，以及已被上游推翻的结论。历史留在 git 里，文档只回答「现在是什么」。

期间修正一处**事实错误**：`OURDOMAIN.md` 写着 Diemen 与 South-East「共用同一个 RentCafe property 184283，只需抓一次」，实际是两个 host、两个 property_id（184283 / 182801）、两个独立 task。照原文改代码会直接删掉一个楼盘的抓取。

补上了 OurDomain 此前一条都没写的反爬机制：每次尝试换出口 IP（与 H2S/Xior 相反——它没有 clearance 可复用，换 IP 才是解法）、指纹池的选取与冷却状态机、同 session 403 重试、`data-label` 双策略提取、以及「单元重复出现在所有 FP 下，因此 `floorPlans` 过滤器不可信」这个推论。

`SCRAPING_RECON.md` 定位收窄为「还没接入的平台值不值得做」，新增 OurCampus 与 Student Experience 两份实测记录，并按复测结果更正 HousingAnywhere——它的 JSON-LD 和 `__PRELOADED_STATE__` 两个月内已不再含房源数据，实际在 `window.__staticRouterHydrationData`。

### 其它

- `_get_text()` 支持 POST（给了 `data` 时）；被两个 source 共用的日志文案去掉写死的 "OurDomain"，改为中性的 "RentCafe" 或按 `self.source` 参数化
- `_to_listing()` 的 `detail` 兜底从写死的 `"OurDomain"` 改为 `source`
- 通知渠道的平台短标签补上 `xior`（此前会 fallback 成 `XIOR`）和 `ourcampus`
- `.env.example` 补上从未记录过的 `CLOAKBROWSER_HEADLESS`
- dataflow 图补上 OurCampus 与影子过滤环节
- `ARCHITECTURE.md` 注明 `tools.doctor` 要在宿主机仓库目录跑——Dockerfile 没有 `COPY tools/`，镜像里没这个模块

## v1.9.12 (2026-08-03)

系统审阅 monitor + 三个平台 scraper 时找出的其余问题。

### Bug 修复 — 403 后真的丢弃浏览器会话

两个浏览器型 scraper 的 `batch_session()` 里都写着「捕获 `BlockedError` → `_close_browser()` → 下轮重建」，但 dispatcher 是**按 task 隔离**的，`scrape()` 抛的异常全在那圈 per-task `try` 里被吃掉，根本到不了 `yield`。这段 `except` 是死代码——`HollandStayScraper` 文档写的「仅在 BlockedError（CF 会话过期）或进程退出时关闭重建」实际从未发生过。

后果：H2S 抓取 403 → source 熔断 30 分钟 → canary **复用同一个被烧的浏览器**（`_BROWSER_MAX_AGE` 2 小时内不重建）→ 大概率再 403 → 熔断翻倍，最长 6 小时。Xior 更直接：它靠重建浏览器来换出口 IP（`rotating_proxy=True`），不重建就一直卡在同一个被限流的 IP 上。

改由 dispatcher 负责：批次里出现过 403，批次**结束后**调一次 `invalidate_session()`。时机是关键——批次中间丢的话，同 source 的后续 task 会各自触发一次浏览器重建（每次一轮完整 CF 挑战，H2S 实测最长 90s+25s 且失败会连锁重试 3 次），一栋楼的 403 能把整批拖成分钟级。429 和维护态不丢：前者「等等就好」，重建只是白白多过一次挑战；后者是对方的事，浏览器没坏。

### Bug 修复 — OurDomain 指纹冷却对「成功过的指纹」失效

`_mark_fingerprint_blocked` 只写 `cooldown_until`、不清 `last_good_at`，于是「成功过 + 正在冷却」的指纹同时落进 `last_good` 和 `cool` 两个桶，`dict.fromkeys` 保留首次出现的位置——它被排到**最前**。模块注释写的是「403 失败的指纹标记 cooldown_until，期内不再优先用」，代码做的正好相反。每轮白扔一次必然 403 的尝试（含 2 次请求 + 2s 同 session 重试）。

现在先算 `cool` 并从 `last_good` 里排除。

### Bug 修复 — completeness key 的 source 前缀永远加不上

`_completeness_key` 靠 `len(by_source) <= 1` 判断要不要加前缀，但 monitor 从 v1.9.9 起按 source 分开调 dispatcher，每次调用里 `by_source` 恒为 1。生产日志里三个 source 同时开着，输出却是 `Amsterdam Diemen=✓, Eindhoven=✓` 这种裸城市名。

连带后果：`_mark_stale_listings_for_complete_cities` 全部走 `complete_cities` 分支，`mark_stale_listings` 用的是不带 source 条件的 `city IN (...)`——为多源隔离准备的 `source_city_pairs` 在生产里永远是空的。当前配置下 6 个 display 名互不相同所以没出事，但只要哪天两个 source 用了同名城市，就会用一个 source 的完整性去收敛另一个 source 的 listing。

新增 `dispatch_scrape_tasks(..., multi_source=)`，由知道整轮全貌的 monitor 给出。单源部署仍是裸城市名。

### Bug 修复 — stale 收敛的 24 小时计时器会被空轮消耗

原代码在 `finally` 里无条件重置 `last_stale_sweep_time`。run_once 的兜底路径（抓取阶段未分类错误 / 管线错误都 `return {}`）和 H2S 熔断期都会返回空 completeness，只要 24 小时那一轮刚好撞上，收敛就被白白跳过，鬼影 listing 再多挂一天。

抽出 `_stale_sweep_decision()`：`run` / `wait` / `defer`，defer 时不重置计时器，下一轮有完整城市就立刻补跑。

### Bug 修复 — H2S 响应结构异常时误判「完整扫描」

`products.page_info.total_pages` 缺失时默认成 `1`，于是 `current_page(1) >= 1` 直接判 `complete=True`——**「没拿到数据」被当成了「确认没有数据」**，正是当年那次 7 周静默故障的判据类型。字段改名 / schema 变更会得到「0 条房源 + 完整扫描」，而这恰好是让 stale 收敛清空整城的组合。`products` 为 `null` 时还会直接 `AttributeError` 崩掉。

现在拿不到 `total_pages` 就记 ERROR 并标不完整；已抓到的部分照常入库，只是不参与状态收敛。真正的零房源（结构完整、`total_pages=1`）仍判完整，否则 stale 永不收敛。

## v1.9.11 (2026-08-03)

### Bug 修复 — 单个 source 失败会炸掉整轮

v1.9.9 给 dispatcher 加了 per-task 隔离，但那层保护在生产里基本没生效，原因有两条，互相放大。

**其一：跨 source 的保护被调用方式抵消。** `dispatch_scrape_tasks` 在「本次调用的任务**全部**失败」时仍会上抛（这是给 monitor 冷却用的契约）。但 monitor 从 v1.9.9 起是**按 source 分开调用**它的，于是「全部失败」退化成了「单个 source 全失败」——跨 source 隔离等于不存在。

实测（2026-08-03 07:41–07:45）：Xior 四栋楼连续 429 → `RateLimitError` 逃出整个 dispatch → 同轮 OurDomain 已抓到的结果被丢弃、H2S 排在最后根本没被执行、每个用户都收到「监控将暂停 5 分钟」（这条路径**没有节流**，不像 403 有 30 分钟窗口）、全局冷却 5 分钟 + 自适应间隔翻倍。24 小时内三次。

OurDomain 更危险：它只有 1 个 task，任何一次 403 都满足「全失败」，会触发 `blocked_fail_streak` 指数退避，把**全局**冷却推到 15min → 30 → 60 → 120min，而 H2S 完全健康。

现在 monitor 逐 source 隔离，失败的 source 在完整扫描日志里标 `✗`（而不是从统计里消失），其余 source 照常入库通知。只有**所有** source 都失败时才上抛，让 main_loop 照旧冷却——新增 `_pick_round_failure()` 按「哪个根因更值得据以决策」挑异常：ProxyError → Maintenance → Blocked → RateLimit → Network。

**其二：`batch_session()` 的异常绕过 per-task 隔离。** 那圈 `try/except` 在 `with scraper.batch_session():` **内部**，而批次会话的进入/退出在外面。浏览器创建失败、CF 挑战没过、Playwright 崩溃都从这里抛出，直接穿透整个 dispatcher——正是 per-task 隔离想避免的事。8-03 00:58 的 `Xior 主站仍停在挑战页` 只差没连挂 3 次就会踩到。

现在整个 `with` 再包一层 try：批次会话失败按该 source 的所有未决 task 记账并 `invalidate_session()`，异常若来自 `__exit__` 则不覆盖已经跑完的 task 的结论。

## v1.9.10 (2026-08-03)

### Bug 修复 — Xior 把上游的成功码当成故障

`availability_response.errorCode` 装的是 **HTTP 风格状态码**，2xx 都表示上游调用成功（`200` 正常返回、`204` 无可用单元）。v1.9.6 加 204 例外时用的判据是「非 204 即故障」，于是返回 `200` 的楼栋被整栋标为抓取失败。

整晚约三分之一的轮次非满分，**全部是同一栋楼**：它的每一轮、每个房型都被误判，而同期真正的 429 只是零星几次——误判量高出一个数量级。

改为按 2xx 判断；无法解析的 code 保守当作成功——误判成失败会让正常的零可用被标 incomplete、stale 收敛永不执行，代价比少记一次故障大得多。

## v1.9.9 (2026-08-02)

### Bug 修复 — 未预期异常会拖垮整轮

dispatcher 的 per-task `try/except` 只捕获 `UpstreamMaintenanceError` / `RateLimitError` / `BlockedError` / `ScrapeNetworkError`，**其它异常直接穿透**，把同轮里已经抓好的其它 source 结果一起带走。实测：Xior 的 `greenlet.error` 导致同轮 H2S 与 OurDomain 已抓到的结果全部丢失，日志显示「本轮无完整扫描城市」。

这违背了 dispatcher 自己的设计意图——代码注释写着「避免 OurDomain 被挡时拖垮 H2S」，但那个隔离只对已知异常成立。

现在未预期异常同样按 task 隔离。连带处理一个问题：异常被 per-task 吞掉后 `batch_session` 就看不到了，**坏掉的浏览器会跨轮残留**，导致后续每轮重复失败。新增 `AbstractScraper.invalidate_session()` 钩子（默认 no-op），dispatcher 在捕获未预期异常时调用；H2S 和 Xior 实现为关闭浏览器，下轮重建。

### Xior 改用轮换出口 IP

Xior 的 AJAX 端点按 IP **累积**限流。5s 间隔只解决了轮内突发，解决不了跨轮累积——固定出口下每轮 12 个请求，实测第 2 轮的第一个请求就被 429 拒绝。

`SiteProfile.rotating_proxy` 新字段，Xior 置 True：每次创建浏览器换一个代理 session（即换出口 IP）。单个浏览器会话内 IP 仍然稳定，所以 clearance 照常可用；换浏览器才换 IP。配合把 Xior 的 `_BROWSER_MAX_AGE` 从 2 小时缩短到 15 分钟（≈3–4 轮 ≈40 请求/IP），代价是每小时多 4 次 CF 挑战——Xior 的挑战约 7–9s，比 H2S 便宜。

H2S 保持固定出口（`rotating_proxy=False`）：它的限流压力小得多，而稳定 IP 是 clearance 复用的前提。

### Bug 修复 — 浏览器型 source 的线程模型（两个连环问题）

**其一：Xior 浏览器被跨线程调用**，`greenlet.error: Cannot switch to a different thread`，整轮失败。

v1.9.7 把 Xior 迁到浏览器传输层时只改了传输层，**没改任务路由**：只有 `source == "holland2stay"` 走长存单线程，Xior 仍留在默认 executor。Playwright 对象绑定创建线程，默认 executor 的线程会漂移。潜伏了一段时间才暴露——线程池复用线程，碰巧命中同一个时一切正常。

**其二：修上面那条时把两个 source 塞进了同一线程**，于是撞上 `Playwright Sync API inside the asyncio loop`，这次轮到 H2S 整轮失败。根因是**两个独立的 Playwright sync 实例不能共存于一个线程**：第一个实例在该线程装上 event loop，第二个 `launch()` 立刻被判定为「在 asyncio loop 里用同步 API」。短生命周期线程每次都是干净的，长存线程会累积这个状态——这也是 c9f9b3a 当初用短线程规避问题的原因。

最终形态：`_get_browser_executor(source)`，**每个浏览器型 source 一条专属长存线程**。同时满足三个约束：不用默认 executor（约束 1）、线程活得比一轮长以便浏览器跨轮复用（约束 2）、每线程只有一个 Playwright 实例（约束 3）。纯 HTTP 的 OurDomain 保持在默认 executor。

`greenlet.error: Cannot switch to a different thread`，Xior 整轮抓取失败。

v1.9.7 把 Xior 迁到浏览器传输层时，只改了传输层，**没改任务路由**：`_dispatch_with_h2s_circuit()` 里只有 `source == "holland2stay"` 走 `_get_h2s_executor()` 那个长存单线程，Xior 仍留在默认 executor。Playwright 的对象绑定创建线程，默认 executor 的线程会漂移，跨线程调用即抛。

潜伏了一段时间才暴露——线程池会复用线程，碰巧命中同一个时一切正常。

新增 `_BROWSER_SOURCES` 常量，所有浏览器型 source 统一路由到隔离线程；纯 HTTP 的 OurDomain 保持在默认 executor（它没有 Playwright 对象，挤进去只会和浏览器抢那一个线程）。H2S 的熔断逻辑不受影响。

### OurDomain 恢复 IP 轮换

按 source 隔离 session 时**把 OurDomain 也固定住了，这是个回归**。

H2S / Xior 需要出口 IP 稳定（clearance 才能复用），但 OurDomain 没有 clearance 可复用——它的抗封手段是轮换 TLS 指纹，而那套机制此前是搭了「每请求换 IP」的便车。固定 IP 后便车没了：同一个 IP 被 CF 盯上时，`chrome124 → edge101 → chrome136 → chrome131` 四个指纹轮完仍然全是 403，错误信息自己都写着「等待无法恢复」。

`get_proxy_url(source, rotating=True)` 新增轮换模式，每次调用换一个新 session id（即换出口 IP）；OurDomain 在**每次指纹重试前**重新取代理，恢复「换指纹 + 换 IP」的组合。不需要在 webshare 额外配置端点——session id 本来就是我们自己生成的。

已确认 OurDomain 的 403 是可解的 CF 挑战（`Just a moment...` + `_cf_chl_opt`），所以也可以像 Xior 那样改用浏览器传输层。没有这么做的原因：它有两个不同子域（`thisisourdomain` / `southeast-thisisourdomain`），同源传输层需要两套 profile；且会多出第三个常驻浏览器（再吃 200–400MB）。而它本来就不需要 clearance，换 IP 是更对症也更省的解法。

### Xior 请求间隔 1.5s → 5s

按 source 隔离 session 后，H2S / OurDomain 不再被 Xior 拖累，但 **Xior 自己仍然超自己的额度**——单独一个出口 IP 上仍稳定触发 429。

限流是**按速率**算的，关键是瞬时突发而非每轮总量：12 个请求以 1.5s 间隔在 **18 秒**内打完，瞬时约 40 req/min，而该端点的限制是 ~15–20 req/window。5s 间隔把同样 12 个请求摊到 60 秒（12 req/min），留出余量；轮次间隔本身 3–5 分钟，这点耗时放得下。

间隔提取为 `_MIN_REQUEST_INTERVAL` 常量并注明：楼栋数增加时单轮耗时线性增长，楼栋很多时应改为分轮抓取，而不是把这个值调小。

### 各 source 使用独立的代理 sticky session

换成 sticky 代理后所有 source 挤在同一个出口 IP，**共享了限流额度**：Xior 一轮 12 个请求（4 栋楼共 12 个房型）触发 `429 Too Many Requests`，四栋楼全部失败，该轮只剩 2/6。是「新增阿姆两栋楼」和「固定出口 IP」两件事叠加才触发的。

但出口 IP 稳定又是 Cloudflare clearance 能复用的前提，不能退回每请求轮换。所以按 source 拆分 session：

- `get_proxy_url(source)` 新增可选参数，为该 source 派生稳定且互不相同的 sticky session id（webshare 用户名形如 `{user}-{country}-{session_id}`，替换末段数字）。
- 各平台因此拥有**各自稳定的出口 IP**，互不挤占限流额度。
- 不传 `source` 时返回基础 URL，`monitor` / `doctor` 只判断有没有配代理，行为不变。
- 只在用户名**已经**以数字 session id 结尾时才替换；`-rotate` 端点、无 session 段、无鉴权等形态一律原样返回——凭空拼接可能被 webshare 解析成国家码，反而把配置搞坏。
- 故障切换与冷却逻辑不受影响。

配置来源仍然只有 `.env` 一处。

## v1.9.8 (2026-08-02)

### Bug 修复 — 浏览器与其它抓取器走在不同出口 IP

`BrowserFetcher` 启动 Chromium 时**没有传代理**，一直指望它自己解析 `HTTP_PROXY` / `HTTPS_PROXY`。实测这条路和 curl_cffi 的解析结果不一致：

| 传递方式 | 浏览器出口 | curl_cffi 出口 |
|---|---|---|
| 仅环境变量 | `79.116.229.115` | `213.73.166.139` |
| 显式 `launch(proxy=...)` | `213.10.194.180` | `213.10.194.180` |

环境变量那条路拿到的出口甚至不在 sticky 端点用户名指定的国家，说明 Chromium 走的是另一套代理解析。

后果不只是「不一致」：

- **Cloudflare 的 clearance 绑 IP**（已实测：本地取得的 cookie 拿到服务器用直接 403）。浏览器和其它抓取器落在不同出口，就不可能共享会话。
- 浏览器完全**绕过了 `get_proxy_url()` 的故障切换与冷却逻辑**——主代理失效时其它抓取器会落到备用，浏览器不会。

现在显式传 `proxy=get_proxy_url() or None`。**配置来源仍然只有 `.env` 一处**，`get_proxy_url()` 读的就是那几个环境变量，这里只是把解析好的值交给浏览器。代理 URL 含凭据，日志里只打 `host:port`。

## v1.9.7 (2026-08-02)

### BrowserFetcher 泛化为通用浏览器传输层

`BrowserFetcher` 此前把 H2S 的细节硬编码在内部（主站 URL、GraphQL 路径、Magento 头、维护页判定、clearance 探针），只能服务一个站点。现在站点差异全部收进 `SiteProfile`，通用流程（过挑战 → 等 clearance → 发同源请求 → 处理 clearance 过期 / 屏蔽 / 限流）对所有站点复用。

- 新增 `SiteProfile` / `ProbeRequest`，内置 `H2S_PROFILE`、`XIOR_PROFILE`。
- 新增通用 `fetch()`（任意 method / path / body / headers），`fetch_gql()` 与新增的 `fetch_form()` 都只是在它外面套一层响应解析。GET/HEAD 自动不带 body。
- **clearance 判据可配**：H2S 回自己的 JSON `{"code":"clearance_required"}`，多数站点直接回 CF 挑战页——两种形态都要认出来，否则会把「重新导航就能好」误判成「IP 被封」。`_is_clearance_required` 因此从 staticmethod 变为实例方法。
- **clearance 探针可选**：没有廉价探针的站点（Xior 的端点需要 property/room id，profile 层拿不到）跳过初始化探测，由首个真实请求遇到 403 时触发重新导航兜底。
- 维护页判定改为 profile 提供的钩子，只有 H2S 配了。
- `BrowserFetcher(headless=...)` 默认仍是 H2S profile，booker 与 H2S scraper 调用方式不变。

### Xior 改用浏览器传输层

- **CF 403 已解决**。`curl_cffi` 的 TLS 指纹伪装过不了 Xior AJAX 端点的 Cloudflare 挑战（实测恒 403 + 挑战页）；改走浏览器后返回正常 envelope，`success=true`，结构完整。
- **并发改串行**。Playwright 对象绑定创建线程，浏览器传输层不能跨线程并发调用，`ThreadPoolExecutor` 移除。实际没有损失——原来 4 个 worker 共享同一把 1.5s 全局限流锁，吞吐本就等于串行。
- **浏览器跨轮复用**：与 H2S 同样的 `batch_session()` + 2 小时主动重建。
- `_post_ajax` 职责收窄为业务语义 + 限流退避；CF 屏蔽 / 维护由传输层抛出并原样上抛给 source 级熔断。

### Bug 修复 — Xior 把上游错误读成「没有房源」

`success=true` 不代表上游成功：Xior 的 WordPress 端点在向 Yardi 取可用性失败时，仍然返回 `success=true` + `units=[]`，真实错误只出现在 `availability_response.errorCode` 里。不查这个字段就会把「上游挂了」读成「零可用」，两者完全无法区分。现在命中即返回 `None`（本轮标记 incomplete），并把 `errorCode` / `availability_params` 记进日志。

### Xior 已启用

`errorCode: 204` 的语义已查清：**它就是「该房型当前无可用单元」，不是故障**。

判定方式是拿官方前端做对照——在 Xior 自己的 modal 里选房型、走完带 Turnstile 的完整流程，抓包看到前端收到的同样是 `{"success":true,"units":[],"total":0}` + `errorCode 204`。既然官方站点自己也显示空，我们的抓取结果就是对的。

据此修正上一版的判断：只有 **204 之外**的 `errorCode` 才按上游故障处理（返回 `None` → 本轮 incomplete）；204 视为正常的零可用。否则每一轮零可用都会被标成 incomplete，stale 收敛永远不执行，等于把「没房」误报成「抓取坏了」。

顺带排除的两条线索，记录下来免得重复排查：

- **`academicTermId=3281` 没有过期**：它出现在站点当前页面的 `input[name="semester"]` 里，与配置一致。
- **Turnstile token 不是必需的**：页面 JS 确实会带 `cf-turnstile-response`，但实测「无 token」「垃圾 token」「浏览器内 render 出的有效 token」三者响应完全相同——该端点并不校验它。

`SOURCES=holland2stay,ourdomain,xior`。

## v1.9.6 (2026-08-02)

### 多 source 现状核实

启用前对两个闲置 source 各跑了一次真实抓取（上次成功抓取还是 5 月下旬）：

- **OurDomain：可用**。Amsterdam Diemen 正常返回，当前 0 个可用单元是真实结果——站点页面显示的是「unit may become available at any time」这类候补文案，不是解析失败（403 会抛 `BlockedError`，本次没有抛）。已启用。
- **Xior：不可用**。AJAX 端点被 Cloudflare 挑战页拦下（`403` + `Just a moment...`），`curl_cffi` 过不去。保持停用，直到传输层能过 CF。

### Bug 修复 — Xior 403 被当成可重试的限流

- **403 退避用尽后抛 `BlockedError`**，不再返回 `None`。返回 `None` 只把本轮标成 incomplete，**source 级熔断因此永远不会对 Xior 生效**：每轮都重来一遍、每栋楼白等 90s 退避，而 Cloudflare 挡着永远不可能成功。5xx 等其它错误维持原来的 `None` 语义不变。
- **退避日志报实际状态码**。此前恒定写死 `"Xior 429"`，于是 Cloudflare 的 403 也被记成限流——排查方向被直接带偏（以为该降轮询频率，实际该换出口 IP）。
- 澄清一处此前的误判：`complete=False` 一直有正确设置，所以 stale listing 状态收敛被完整性标志挡住了，**不存在**把已有房源误标成已租的风险。问题只在于白白重试和日志误导。

### 说明

启用 OurDomain 不等于给 H2S 做冗余——两者是不同的房源池（OurDomain 只覆盖 Amsterdam Diemen）。H2S 熔断期间仍然拿不到 H2S 房源；收益是轮次不再整体空转，以及多一份覆盖。

## v1.9.5 (2026-08-02)

### Bug 修复 — Telegram 渠道刷错

- **区分永久性失败与临时故障**。用户拉黑 bot 后 Telegram 每次都回 `403 Forbidden: bot was blocked by the user`，旧实现把它和限流 / 5xx 一视同仁，于是每轮重试、每轮刷 4–6 条 ERROR，而且永远不会自愈。现在按 Telegram 的 `description` 分类：
  - **永久性**（`bot was blocked by the user`、`chat not found`、`user is deactivated`、`bot was kicked from the group chat` 等）→ 停止重试。
  - **临时性**（429 限流、5xx、`message is too long` 等）→ 行为不变，继续按原逻辑重试。
- **双层停用，缺一不可**：
  - 实例级——命中永久性错误后该 `TelegramNotifier` 直接短路，后续 `_post()` 不再打 API。只做持久化不够：notifier 要等下次热重载才重建，这中间每轮仍会照打照错。
  - 持久级——把 `telegram` 从该用户的 `notification_channels` 摘掉并落库，重启后不复发。
- **凭据保留**：只动 `notification_channels`，`telegram_token` / `telegram_chat_id` 原样保留，用户解除拉黑后在面板重新勾选即可恢复，无需重填。
- **告知用户**：Telegram 这条路已经不通，改为写一条 per-user Web 通知说明原因和恢复方式。
- 回调内的落库异常被吞掉并记日志，不会把通知链路带崩。

### 运维

- **容器内存上限 1G → 2G**。浏览器改为常驻后稳态约 500–800MB（实测峰值 789MB），1G 只剩约 23% 余量，抓取一旦扩到多城市就可能触顶。宿主 3.7G、caddy + 系统约 1.2G，给到 2G 仍有富余。

## v1.9.4 (2026-08-02)

### 依赖升级

- **CloakBrowser 0.3.31 → 0.5.3**（`requirements.txt` 下限提到 `>=0.5.3`）。跨两个 minor，在 0.x 语义下允许破坏性变更，因此先在隔离 venv 里验证再落地：
  - **API 未变**：`launch(headless=, humanize=, args=)` 三个参数签名一致，仓库用到的 Playwright API 只有 `goto` / `content` / `evaluate` / `title` 四个核心方法。
  - **Chromium 不变**：0.5.3 bundle 的免费构建正是服务器已在用的 `v146.0.7680.177.5`，升级只换 Python wrapper，不动过 CF 的那一层。
  - **Playwright 保持 1.60.0**：`cloakbrowser` 只要求 `>=1.40`，无需连带升级。
  - **仍是 free tier**：`binary_info()["tier"] == "free"`，不需要 license。
  - macOS 本地仍是 v145（免费 v146 只发布 Linux 构建，darwin-arm64 返回 404），与既有情况一致。
- **`CLOAKBROWSER_AUTO_UPDATE` 仍然有效**：该变量在 0.5.3 里从 `config.py` 移到了 `download.py` / `__main__.py`，Dockerfile 里那行不是空操作。实测确认：不设时每次 `launch()` 会 GET `pypi.org/pypi/cloakbrowser/json`，设为 `false` 后无任何外网请求——生产环境不会因升级多出对外调用（也不会误走抓取代理）。

### 备注

- 0.5.x 起 v150 构建对免费用户开放，但需要注册 key 且限 1 个并发会话。本项目 scraper 与 booker 各持一个浏览器实例，可能超出该限制，故未采用；如需评估请自行在 <https://cloakbrowser.dev/free> 申请。

## v1.9.3 (2026-08-02)

### 健康检查纳入 monitor

- **`/health` 现在 Web 和 monitor 都正常才返回 200**，否则 503。此前只看 Web 能否响应，monitor 状态仅作字段透出、不影响状态码——代价是 2026-06-13 起 monitor 停了 7 周，容器全程 `healthy`，所有用户都收不到任何通知，也没有任何告警。盲区正好盖住了唯一重要的进程。
- **判据是心跳新鲜度，不是「进程是否存在」**：
  - monitor 每轮开始时（`run_once` 之前）写 `monitor_heartbeat_at`，与抓取成败无关。
  - 不用 `last_scrape_at`：H2S 熔断冷却最长 4 小时，期间没有成功抓取但循环仍在转，用它会把设计内的正常退避误报成故障。
  - 不用 PID 检查：进程还在但循环卡死时 PID 看不出来，心跳能。
- **保留原设计里合理的顾虑**：管理员为部署 / 调试短暂停掉监控，在 `MONITOR_HEARTBEAT_MAX_AGE`（默认 900s ≈ 3–4 轮）内不会翻红；停够久才暴露。心跳尚未写过时（全新部署 / 首轮未完成）退回进程存活判断，避免冷启动误杀。
- **无需改 `docker-compose.yml`**：healthcheck 本来就在打 `/health`，改端点语义即可，部署时不会与本地配置漂移冲突。
- 注意：`restart: unless-stopped` 只在容器退出时重启，**不会**因 unhealthy 自动重启。这个改动让停摆变得可见，要变成告警仍需外部监控订阅容器健康状态。

## v1.9.2 (2026-08-02)

### Bug 修复 — H2S 抓不到房源

- **CF 挑战通过与否的判据修正（根因）**：`ensure_initialized()` 此前用 `[data-cy="FilterList-item"]` 选择器判断挑战是否完成，等不到只打一条 warning 就继续，并照样置 `_initialized = True`。实测该元素与「能否发请求」无关（GraphQL 已经 200 时它仍可能未渲染），于是挑战没过也把 GraphQL 打出去 → 必然 403 → 触发会话重建 → 重建时 `Page.goto` 崩溃 → source 熔断。改为轮询挑战页脚本标记 `_cf_chl_opt` 是否消失，超时抛 `BlockedError` 交给熔断，不再硬发注定失败的请求。
  - 顺带记录几个**不能**用作判据的信号：`challenges.cloudflare.com` 和 `/cdn-cgi/challenge-platform/` 在挑战解开后的真实页面里同样存在（CSP 头 + 站点自带 turnstile）；URL 上的 `__cf_chl_rt_tk` 由 CF 通过 `history.replaceState` 回写，时机不定，挑战早已解开时仍可能残留。
- **新增 clearance 就绪探测**：挑战页消失不等于 `cf_clearance` 已生效——实测两者之间有约 2s 空窗，期间 GraphQL 稳定返回 `403 {"code":"clearance_required"}`。初始化最后一步改为直接探一次最小 GraphQL 查询，拿到响应才算就绪，替换掉原来那个与可用性无关的 DOM 等待（同时省掉 25s 无谓超时）。
- **区分「clearance 未生效」与「被 CF 屏蔽」**：两者都是 403，旧代码一律按屏蔽处理并升级为 `BlockedError` + 熔断。现在按 `clearance_required` 标记区分：token 过期走重新导航 + 重试，只有真正的屏蔽才熔断。
  - 恢复方式必须是**重新导航主站走完挑战**：token 由页面通过挑战时下发，对着 GraphQL 轮询换不出新 token（生产验证：跨轮复用的浏览器在第 2 轮必然 `clearance_required`，轮询 60s 无效，重新导航即恢复）。初始化阶段的 `_wait_for_clearance` 轮询之所以有效，是因为那里刚做完 `goto`，等的是 cookie 落地而非 token 重签——两者不能混用。
  - 生产环境的 clearance 比本地短命得多：macOS 本地复用 6 分钟仍有效，1 CPU VPS 上约 5 分钟就要重签。
- **维护检测移到挑战解开之后**：挑战页由 Cloudflare 生成，其标题/正文与 H2S 真实状态无关，在挑战解开前判定等于拿 CF 的页面猜平台在不在维护。同时 `_raise_if_maintenance_page()` 增加防御性早退。
- **重建会话时不再吞掉维护异常**：`fetch_gql` 的 403 分支此前把重建过程中的所有异常压成 `BlockedError`，`UpstreamMaintenanceError` 也在内——会让 monitor 走熔断 + admin 告警而不是安静的维护冷却。现在维护异常原样上抛，其余异常保留 `__cause__`。

- **初始化改为「导航重试」而非长轮询**：既然 token 只能由导航签发，初始化阶段等 clearance 也不该一路轮询到超时——那只是朝一个拿不到 clearance 的会话打几十个必然 403 的请求，反而加重 CF 的怀疑。改为单次导航等 25s，等不到就重新导航，最多 3 次（生产实测冷启动第 1 次失败、第 2 次成功）。`UpstreamMaintenanceError` 不参与重试。耗时日志按**成功的那次导航**分别记录挑战与 clearance，不再把失败尝试的等待算进 clearance。
- **超时按最慢环境取值**：生产实测挑战耗时 macOS 本地约 2s、1 CPU 的 VPS 上约 35s（正是旧代码 25s 选择器超时必然误判的原因）。挑战上限取 90s、clearance 取 60s 留足余量；初始化日志改为分别记录两段耗时——挑战慢通常是机器/网络慢，clearance 慢更像 CF 在加码校验，排查方向不同。

- **恢复真正的浏览器跨轮复用（v1.9.0 起实际从未生效）**：CF 挑战次数从每轮一次降到按需重建时才有一次。两处都得改才成立：
  - `monitor._dispatch_scrape_tasks_async()` 以前每轮 `with ThreadPoolExecutor(...)` 新建又销毁 H2S 专用线程。Playwright 对象绑定创建线程，线程一换浏览器即作废。改为进程级长存单线程（仍不用默认 executor，保留 c9f9b3a 规避 "Sync API inside the asyncio loop" 的意图）。
  - `scrapers.get_scraper()` 以前每次调用 `cls()` 新建实例，而浏览器挂在实例上，于是 `HollandStayScraper` 里的跨轮复用分支永远命不中。改为按 source 缓存实例（连注册的类一起存，注册表被替换时跟着重建）。
  - 影响：数据中心 IP 上，每轮一次 CF 挑战会被 CF 逐步升级难度，表现为挑战耗时越来越长直至超时熔断——这是修掉主因后仍每隔几轮失败一次的原因。

### 重构

- 抽出 `BrowserFetcher._raw_fetch_gql()`：只发请求、原样返回 `{status, ok, text, headers}`，不做状态码处理也不触发 `ensure_initialized`（clearance 探测需要在初始化过程中调用，走公开的 `fetch_gql` 会无限递归）。`fetch_gql` 的首次请求、clearance 重试、会话重建后重试统一走它，不再各自内联一份 JS。

### 测试

- `tests/test_browser_fetcher.py` 新增 6 个用例，覆盖：挑战解开后返回 / 挑战持续时抛 `BlockedError` / 挑战失败时不得标记为已初始化（本次根因的回归锁）/ 挑战页上不做维护判定 / `clearance_required` 与真屏蔽的区分 / clearance 未生效时原地重试而非重建会话。

## v1.9.1 (2026-06-13)

### Bug 修复
- **H2S GraphQL 403 请求形态修正**：`BrowserFetcher.fetch_gql()` 的浏览器内请求显式使用 `credentials: "include"` / `mode: "same-origin"` / 当前页面 referrer，并补齐 Magento storefront 常用的 `Accept`、`Store`、`Content-Currency` headers，避免页面级 CF challenge 已通过但 GraphQL 请求因过于“裸 fetch”被 WAF 拒绝。403 重建会话后若重试成功，现在会刷新 HTTP status；仍失败时记录脱敏响应头和 body 摘要，便于区分 Cloudflare、H2S API 与代理服务端错误。
- **H2S 维护页识别**：CloakBrowser 主站加载后如果页面标题或 HTML 命中维护页（例如 `H2S-Maintenance`），立即抛 `UpstreamMaintenanceError`，让 monitor 进入平台维护冷却，不再继续打 GraphQL 并误报为 Cloudflare 403。
- **Web 维护横幅动态更新**：`/api/status` 现在返回 upstream maintenance 状态，Dashboard 顶部横幅会在后台 monitor 检测到 H2S 维护后通过轮询自动显示，无需刷新页面。
- **System Info 控制区对齐**：暂停监控 / 重启进程按钮移入进程状态表格的 value 列，与 PID 和运行状态左边缘对齐；暂停说明也随监控状态按整行显示/隐藏。
- **H2S 抓取线程隔离**：H2S/CloakBrowser 抓取改用短生命周期专用线程执行，避免 Playwright Sync API 在 monitor 的 asyncio 环境中误判为“inside the asyncio loop”并拒绝启动。
- **macOS 本地调试**：CloakBrowser macOS headless v145 可能 SIGABRT；本地运行时自动使用 headed 调试模式，Docker/Linux 生产环境仍保持 headless，并只在 Linux 注入 `--disable-dev-shm-usage` / `--disable-gpu`。
- **Docker 镜像补齐 captcha 包**：Dockerfile 显式复制 `captcha/`，修复镜像内 `bookers/rentcafe.py` 导入 `captcha` 失败导致 monitor 无法启动的问题。
- **Docker 构建兼容性**：`python:3.11-slim-bookworm` 使用 Bookworm 包名（非 `t64` 包名），修复 apt 安装 Chromium 依赖失败；同时将 `CLOAKBROWSER_CACHE_DIR` 固定到 `/app/.cloakbrowser` 并关闭 build-time auto-update，确保 root 构建阶段下载的 patched Chromium 对运行时 `appuser` 可用。

## v1.9.0 (2026-06-13)

### Breaking — H2S 传输层迁移至 CloakBrowser
- **背景**：Holland2Stay 将 GraphQL API 从 `api.holland2stay.com/graphql` 迁移至 `www.holland2stay.com/api/graphql`，并对旧子域名启用 Cloudflare Turnstile 保护。curl_cffi TLS impersonation 已无法通过（Turnstile 需要真实浏览器执行 JS challenge）。
- **抓取（scraper）**：`scrapers/holland2stay.py` 重写主体，新增 `BrowserFetcher`（共享模块 `browser_fetcher.py`），通过 CloakBrowser（patched Chromium，58 C++ 源码级反指纹 patch）自动执行 Turnstile challenge，然后通过 `page.evaluate(fetch)` 调用同域 GraphQL API。旧的 `scraper.py`（570 行）精简为 re-export 向后兼容。
- **自动预订（booker）**：`booker.py` 同步迁移，所有 GraphQL mutation 通过 `BrowserFetcher` 发送。`PrewarmedSession.session` → `.fetcher`。下单流程与真实浏览器对齐：
  - `AddNewBooking` 参数精简：移除 `contract_id`、`option_selected`（浏览器未传），仅保留 `cart_id` + `sku` + `contract_startDate`
  - 新增 `GetCheckoutAgreements` 步骤（照浏览器抓包，`setPaymentMethod` 后 `placeOrder` 前），fail-open
  - 完整链路：`CreateEmptyCart → AddNewBooking → SetPaymentMethodOnCart → GetCheckoutAgreements → PlaceOrder → IdealCheckOut`
- **新 API 字段变化**：H2S GraphQL 响应从 `custom_attributesV2` 嵌套对象变为扁平字段（`city: 29`, `basic_rent: 1395`, `energy_label: "A"` 等直接 int/string），大部分枚举字段返回 attribute option ID，通过 aggregations 接口构建 ID→label 映射。
- **工具链**：新增依赖 `cloakbrowser>=0.3.0`，Docker 镜像新增 Chromium 系统依赖 + `cloakbrowser install`（~300MB）。
- **资源开销**：CloakBrowser 空闲 ~190MB RAM，scraper 和 booker 各自独立实例。

### 改进
- **代码清洁**：`scraper.py` 从 570 行缩减至 28 行 re-export。H2S 爬取主体正式入驻 `scrapers/holland2stay.py`（当初 P0 多源重构的遗留 TODO）。
- **macOS 支持**：本地开发 CloakBrowser 可工作（v145，26 patches），但官方推荐 Linux 生产环境（v146，58 patches），后者 patches 更全、CF 绕过更稳定。
- **浏览器跨轮复用**：scraper 浏览器不再每轮创建/关闭，改为懒创建 + 跨轮复用。CF Turnstile 挑战从 ~40 次/小时降至 ~2 次/小时（首次创建 + token 过期重建）。BlockedError 时自动关闭重建；超过 2 小时主动重建避免会话过期。
- **Docker 兼容**：`BrowserFetcher` 内置 `--disable-dev-shm-usage` `--disable-gpu`（解决 `/dev/shm` 64MB 限制）。`docker-compose.yml` 内存限额 512M→1G，新增 `shm_size: 2gb`。`requirements.lock` 补全 `cloakbrowser`/`playwright`/`greenlet`/`pyee`/`2captcha-python` 精确版本。
- **Prewarm 异常修正**：`PrewarmCache.create()` 的 CF 屏蔽检测从 `BookingBlockedError` 改为 `BlockedError`（与 BrowserFetcher 抛出的异常类型一致）。

### 已知限制
- booker.py 迁移已完成代码层，尚未用真实 H2S 账号跑完整下单流程验证。

## v1.8.4 (2026-06-12)

### 功能改进 (Features)
- **H2S 防封策略收紧**：H2S GraphQL 403 现在进入 source-level circuit breaker，只暂停 Holland2Stay，Xior / OurDomain 等其它 source 继续抓取；冷却到期后只用 1 个 H2S 城市做 canary，成功后下一轮恢复完整 H2S 扫描。H2S prewarm 也从「抓取成功后刷新所有自动预订用户」改为「本轮确实有 H2S 自动预订候选时才为对应用户预登录」；登录/预订 403 后 1 小时内暂停 H2S 登录链路。连续第 3 次 H2S 403 起视为长时间 block，给 admin-only 发送“需要检查服务器”的告警，6 小时内不重复。
- **Web 监控暂停可见化**：Dashboard 在 monitor 未运行时向所有登录用户显示“系统监控已暂停”横幅，说明新房源通知、状态变更和自动预订均暂停。System Info 增加 admin-only 启动 / 暂停 / 重启监控按钮，复用现有 `/api/monitor/*` 控制接口。

## v1.8.3 (2026-06-11)

### 自动预订 — Xior / OurDomain (Auto-Booking)
- **RENTCafe 自动预订引擎**：新增 `bookers/rentcafe.py`，实现 RENTCafe（`securerc.co.uk`）多步表单自动化，Xior 和 OurDomain 共用同一引擎。
  - `RentCafeSession`：封装 HTTP 会话 + `cafeportalkey` 管理 + 自动 reCAPTCHA 求解
  - `RentCafeBooker`：`AbstractBooker` 子类，按 `self.source` 取平台专属凭据（`xior_*` / `ourdomain_*`），只做登录不自动注册
  - `XiorBooker` / `OurDomainBooker`：按平台区分配置
  - `bookers/__init__.py`：BOOKER_REGISTRY 新增 `xior`、`ourdomain`
  - **状态**：引擎框架已完成，但 RENTCafe 多步表单余下步骤（Applicant Info 等）尚未侦察，Web 面板标记为"开发中，暂不可用"
- **reCAPTCHA 求解模块**：新增 `captcha/`（`solver.py`），基于 2Captcha API 自动求解 reCAPTCHA v2/v3 Enterprise。
  - RENTCafe 固定 sitekey：v2=`6LfAdx8T...` / v3=`6LfBeqEa...`
  - v3 → v2 回退策略：v3 求解器 token 得分恒为 0.10（Google 指纹识别），RENTCafe 自动降级到 v2 checkbox → 用 2Captcha 真人求解（准确率 99%）
  - 新增依赖：`2captcha-python>=2.0.0`
- **Xior RENTCafe 侦察更新（2026-06-04）**：`docs/XIOR.md` §11 根据最新实测全面修订。
  - 纠正旧结论：`register.aspx` 和 `guestlogin.aspx` **现在都有** reCAPTCHA v3 Enterprise
  - 新增完整 sitekey 记录、v3→v2 回退 JS 逻辑
  - 注册链接被 Xior JS 隐藏，但后端接口仍存活
  - 预订流程 reCAPTCHA 成本估算：$0.003-0.005/次
  - 确认 WordPress AJAX 数据可靠性：19/20 交叉验证准确率
- **平台独立账号配置**：`AutoBookConfig` 拆分为三套独立凭据——H2S（`email`/`password`）、Xior（`xior_email`/`xior_password`/`xior_first_name`/`xior_last_name`/`xior_phone`/`xior_birth_date`）、OurDomain（`ourdomain_*` 同）。Web 面板对应拆分为三个独立区段，Xior/OurDomain 加"开发中"标记。
- **移除自动取消旧订单**：`cancel_enabled` 功能不可用且不再计划实现，Web 面板入口已删除（数据模型保留字段兼容旧数据）
- **依赖更新**：`requirements.txt` 新增 `2captcha-python>=2.0.0`

### 功能改进 (Features)
- **代理故障直连降级**：抓取代理连续失败且错误内容可确认代理服务端异常（如 Webshare `502 Bad Gateway`、`X-Webshare-Error`、`internal_error_auth_circuit_breaker_open`、CONNECT 502）后才进入 10 分钟 cooldown；全部代理都进入 cooldown 时，不再继续硬试故障代理，而是临时降级为服务器原生 IP 直连。monitor 在该状态下把抓取频率限制为最多 10 分钟一次，代理 cooldown 到期后自动恢复优先使用代理。
- **H2S Cloudflare 403 降噪**：预登录 prewarm 从「抓取前并发」改为「抓取成功后再启动」，避免当前出口被 WAF 屏蔽时每轮额外打出多用户登录 GraphQL；prewarm 一旦遇到 Cloudflare 403，会清理缓存并在 15 分钟内停止主动预登录。连续 H2S 403 冷却改为指数退避（15 → 30 → 60 → 120 分钟，上限 2 小时），减少同一出口反复撞 WAF。

### Bug 修复 (Bug fixes)
- **Xior 可订假阳性**：WP `yardi_room_availability` feed 会把「实际订不了」的单元报成「Available to book」，新增两道可用性校验闸（均作用于 `scrapers/xior.py` 的 `_to_listing`，下调时降级为 `Occupied` 但留库，便于日后状态变更通知）：
  - **① 可用日期 60 天窗口**：`Notice Unrented` 的 `availableDate` 距今 > 60 天（现住户远期才搬走，实测见过 `2027-07-01`）→ 不报可订。日期缺失/不可解析时保守保留（不漏报）。
  - **② floorplans.aspx 权威校验**：用 RentCafe OLE 的 `floorplans.aspx`（curl_cffi 直取，无 CF challenge）求出真正可订（`(Available)`+`applyButton`）的户型集合，单元 `floorplanId` 不在集合内 → 降级（解决"点 apply 链接进去说没了"）。仅当有窗口内候选时才多查一次/栋，**fail-open**：抓不到就信 feed，绝不漏报真房源。
- **monitor 错误类型补全**：抓取后管线（入库/通知）异常过去裸冒泡到 main_loop 只打日志不告警，现统一归类为「数据/通知管线错误」给 admin 发带类型告警（30 min 节流）；抓取阶段未分类异常改为 admin-only + 节流（不再每轮广播给普通用户）。
- **代理失效通知去掉 emoji**。

## v1.8.2 (2026-06-03)

### Bug 修复 (Bug fixes)
- **Android 登录 401 错误消息丢失**：服务端 `/api/v1/auth/login` 登录失败时返回 HTTP 401 + JSON `{"ok":false,"error":{"code":"unauthorized","message":"用户名或密码错误"}}`，但 Retrofit 对非 2xx 响应直接抛 `HttpException` 而非解析为 `ApiResponse` 对象。旧代码未捕获 `HttpException`，错误落到泛型 `catch (e: Exception)` 分支，显示 "Network error" 而非服务端真实消息。修复：
  - `ApiClient.kt`：`ApiException` 新增 `fromHttpException()` 静态方法，从 `HttpException.errorBody` 解析 JSON 提取 error.message
  - `AuthViewModel.kt`：`login()` / `register()` / `restoreSession()` 三个方法新增 `catch (e: HttpException)` 分支（排在 `ApiException` 前），用 `ApiException.fromHttpException()` 提取真实错误消息展示给用户

### CI / 构建 (CI/CD)
- **Release APK 自动构建**：GitHub Actions tag 推送时原先只构建 AAB（仅限 Google Play 分发）。现同时运行 `:app:assembleRelease` 产出签名 release APK，与 AAB 一起上传到 GitHub Release。用户可从 Release 页直接下载 APK 安装，不再局限于 Google Play。
  - `build.yml`：`Build release AAB` step 添加 `./gradlew :app:assembleRelease`，新增 `Upload APK to Release` step 上传 `app-release.apk`

## v1.8.1 (2026-06-03)

### 功能改进 (Features)
- **用户优先级拖拽排序**：用户管理页原先只能通过每个卡片上的 ▲/▼ 按钮一次一格调整自动预订优先级，每次点击整页刷新。现改为 HTML5 拖拽排序——抓住卡片左侧 `⠿` 手柄拖到目标位置松手即完成，DOM 即时更新排名数字，后台 `POST /api/users/reorder` 一次请求批量持久化所有 `sort_order`。▲/▼ 按钮保留作为移动端备选方案。
  - `templates/users.html`：user card 加 `draggable="true"` + 拖拽手柄 + ~80 行 JS（dragstart/dragover/drop → DOM 重排 → fetch API 持久化）
  - `static/design.css`：`.drag-handle` grab 光标/hover 高亮，`.dragging` 半透明，`.drag-over-*` 蓝色落点指示线
  - `app/routes/users.py`：新增 `POST /api/users/reorder`，接收 `{order: [id, ...]}` 批量更新
  - `mstorage/_user_configs.py`：新增 `reorder_users_bulk()`，单事务完成全部 `sort_order` 重编号

### 法律文件更新 (Legal)
- **Google Maps / FCM 披露**：隐私政策与使用条款按平台区分第三方服务——iOS 推送=APNs / Android 推送=FCM (Firebase)；iOS 地图=Apple Maps / Android+网页地图=Google Maps。新增 Google Maps Platform ToS 引用及 Google 隐私政策链接。`app/legal/privacy.txt` / `privacyzh.txt` / `terms.txt` / `termszh.txt` 四份文件同步更新至 2026-06-03。

### Bug 修复 (Bug fixes)
- **地图刷新按钮失效**：Google Maps 迁移（v1.7.6）将 `templates/map.html` 内所有 JS 包入 IIFE，但 geocode 按钮仍用 HTML `onclick="runGeocode()"` 属性绑定事件。HTML onclick 在全局作用域执行，`runGeocode` 被 IIFE 隔离后不可访问 → 按钮完全无响应。修复：IIFE 末尾加 `window.runGeocode = runGeocode` 暴露全局引用。
- **Geocode 完成后自动刷新地图**：geocode 进度轮询结束后直接调用 `loadMapData()`（延迟 800ms 等缓存写入落盘），不再要求用户额外点一次手动"刷新地图"按钮。恢复 v1.4.5 原有行为（Google Maps 迁移时误删）。

## v1.8.0 (2026-05-31)

### iOS 崩溃诊断修复 (Crash Diagnostics Fixes)
- **kind 解析 bug 修复**：`CrashDiagnosticsCollector.pendingDiagnostics()` 中从文件名解析 kind 时用 `split("-")` 取 `comps[1]`，但 ISO 时间戳含 `-` 分隔符（如 `2026-05-31T185748Z-other-UUID.json`），导致取到月份字符串（`"05"`）而非 kind。改为 `comps[comps.count - 2]` 从右往左取，兼容所有命名格式。
- **iOS 26 `appLaunchDiagnostics` 支持**：iOS 26 将崩溃/挂起数据封装在 `MXDiagnosticPayload.appLaunchDiagnostics` 中而非 `crashDiagnostics`，旧逻辑未识别导致 kind 归为 `"other"`。新增 `launch` 分类并加入服务端白名单。
- **`appLaunchDiagnostics` 体积大**：典型 MetricKit 诊断包 800–920 KB（远超旧 256 KB 限制），且 `json.dumps(indent=2)` 将嵌套堆栈树膨胀至 5–7 MB 磁盘占用。

### iOS 性能优化 (iOS Performance)
- **通知列表真正懒加载**：`NotificationsView` 原结构为 `LazyVStack → Section → VStack → ForEach`，VStack 包裹使懒加载失效，所有行一次性全建。改为每行作为 LazyVStack 直接子节点，各自绘制 `UnevenRoundedRectangle` 切片（首行圆上角、尾行圆下角、中间方角），0 间距堆叠成连续卡片。数百行通知时只渲染可视区。
- **typeFilter 切换瞬时化**：旧逻辑每次切类型筛选都重做 O(n) 日期解析 + 动画式整列重建。改为双桶策略——数据变化时单次 O(n) 扫描产 `allToday/allYesterday/allEarlier`（全量未过滤），切类型时只从缓存桶按 `n.kind`（O(1) 存储字段）筛，零日期解析、零动画。
- **NotificationItem 预计算**：`listingTitleHint`（正则去前缀）和 `parsedDate`（多格式日期解析）从 computed property 移到 decode 时一次性计算。之前每行渲染都重跑正则+日期解析（最贵的单行操作）× N 行 → 卡。
- **通知 API 7 天窗口**：`mstorage/_notifications.py` 新增 `within_days` SQL 参数，`app/services/notification_service.py` 默认取最近 7 天；`unread` 从窗口内已过滤集计算，与列表/badge 一致。
- **Live 绿点动画稳定性**：`LoginView` 的 live dot 呼吸从单布尔驱动两段 `.animation(value:)` 改为 `liveRipple`/`liveCore` 各自 `withAnimation(.repeatForever)` 显式驱动，修复转场事务把 repeatForever 捕获成一次性弹跳的 bug。

### iOS 多语言适配 (Localization)
- **简中 (zh-Hans)**：补全 21 条缺失翻译（崩溃诊断、通知设置、密码修改等界面文本）
- **繁中 (zh-Hant)**：补全 57 条缺失翻译

### 服务端 (Server)
- **崩溃端点 payload 限制**：`MAX_PAYLOAD_BYTES` 从 256 KB → 2 MB，实测覆盖 iOS 26 诊断体积
- **kind 白名单扩展**：`ALLOWED_KINDS` 加入 `launch`、`other`
- **磁盘优化**：`crash_reports/*.json` 去掉 `indent=2` pretty-print，嵌套诊断树节省 80%+ 磁盘
- **健康检查绕过代理 (NO_PROXY)**：容器配置抓取代理后 Python urllib 默认把所有请求（含 localhost 健康检查）都走代理 → 代理拒连 → 403 → 容器被标 unhealthy。`docker-compose.yml` 加 `NO_PROXY=localhost,127.0.0.1,::1` + 健康检查显式空 `ProxyHandler({})` 强制直连。

## v1.7.11 (2026-05-30)

### Bug 修复 (Bug fixes)
- **Dashboard 启动时间修复**：监控进程重启后 7 天运行时间百分比从 ~1% 重新攀升的 bug 已修复。旧方案存单个 `monitor_started_at` 时间戳，超过 7 天后下次重启会被覆盖为当前时间 → 掉到 1%，且不感知中途宕机。新方案改为**每小时存活采样**（`record_uptime_sample()`）：每个 UTC 小时记一条幂等样本到 SQLite meta 表，uptime% = 168h 里有样本的小时数 / 168。持久化跟 listings 同库、同 Docker volume，重启/重建不丢，宕机的小时自然没样本 → 真实反映可用率。
- **Android 下载链接 404**：修复登录页 Android App 下载链接在新版本发布后 404 的问题。根因是 GitHub Actions CI workflow（`build.yml`）没有 Android 构建 job，每次发新版 `.aab` 资产缺失。同时修复了 `versionCode`/`versionName` 硬编码和 `LoginScreen`/`SettingsScreen` 中版本字符串硬编码的问题。
- **非 macOS 服务器 iMessage 灰掉**：服务器端检测平台（`sys.platform`），非 macOS（Linux / Docker）上用户设置页的 iMessage 通知选项自动变灰（`opacity:0.45` + `pointer-events:none` + checkbox `disabled`），标注"不可用 — iMessage 需要 macOS 环境"。新增 3 个测试（`test_user_routes.py`）。

### 文档与工程 (Docs & Engineering)
- **文档全面更新**：`README.md` / `README_cn.md` 加 Android 下载链接；`ANDROID_PLAN.md` 补当前快照表 + 架构实际落地说明 + RC1-RC4 通过标记；`FUTURE_PLAN.md` 更新路线图 + 里程碑；`API.md` 补条件缓存（ETag/304）章节、`/legal` 端点、`/admin/monitor/restart`；`dataflow_en.mmd` / `dataflow_ch.mmd` 补 FCM 分流、uptime 采样、条件缓存、webhook；`guide.html` / `guide_cn.html` 补移动 App 下载入口、修复 GitHub 仓库名；`openapi.json` 补 `/legal` 端点；`iOS_README.md` 更新状态和性能优化项。
- **工程配置**：`.dockerignore` 加 `android/` + `.github/`；`.gitignore` 清理冗余 Xcode/Android IDE 条目，`*.p12` 归拢到 Android 段。

### Android (Android)
- **CI 自动构建 AAB**：`build.yml` 新增 `android` job，tag push 时自动构建 release `.aab` 并上传到 GitHub Release。
- **版本号动态化**：`versionName` 从 `APP_VERSION` 环境变量注入（CI 从 git tag 派生，如 `v1.7.11` → `1.7.11`）；`versionCode` 自动派生（如 `1.7.11` → `1711`）。
- **UI 版本字符串**：`LoginScreen` 的 `"UNOFFICIAL · v1.7.9"` 和 `SettingsScreen` 的 `"About FlatRadar 1.7.1"` 改为 `BuildConfig.VERSION_NAME`，跟随构建版本自动更新。
- **签名配置兼容 CI**：`build.gradle.kts` 签名密码支持 `ANDROID_STORE_PASSWORD` / `ANDROID_KEY_PASSWORD` / `ANDROID_KEY_ALIAS` 环境变量，兼容 GitHub Secrets 注入。

## v1.7.10 (2026-05-30)

### Bug 修复 (Bug fixes)
- **GitHub Actions Release 缺 DMG/ZIP**：修复 `build.yml` 中 upload-artifact 和 action-gh-release 步骤引用的文件名与构建脚本实际产出不一致的问题。构建脚本 (`build_dmg.sh` / `build.bat`) 产出的文件名为 `FlatRadar.dmg` / `FlatRadar.zip`，但 workflow 中写的是 `Holland2Stay Monitor.dmg` / `Holland2Stay Monitor.zip`，导致每个 release 都报 "No files were found" 并空发布。v1.7.10 统一为 `FlatRadar.dmg` / `FlatRadar.zip`。
- **Xior 未知状态测试断言过期**：`test_xior_scraper.py` 中 `test_to_listing_unknown_status_falls_back_to_available` 仍断言未知状态返回 `"Available to book"`，但 v1.7.9 已将默认值改为 `"Occupied"`（fail-closed）。测试名和断言同步更新为 `test_to_listing_unknown_status_falls_back_to_occupied`。

### 代码质量 (Code Quality)
- **SQLite 连接池化**：Web 路由中每个请求不再重复创建 SQLite 连接。`app/db.py` 的 `storage()` 在 Flask 请求上下文中将连接存入 `g._storage` 并复用，`teardown_appcontext` 自动关闭。非请求上下文（monitor / CLI / 测试）行为不变。消除每请求 ~3ms 的重复 `sqlite3.connect()` + `executescript()` 开销。
- **图表查询下推至 SQL**：`mstorage/_charts.py` 的 `_count_feature_values()` 和 `_bucketed_number_dist()` 从 Python 侧逐行 `json.loads()` 改为 SQLite `json_each()` 在数据库引擎内完成 JSON 解析 + 前缀过滤，Python 仅做分类。`json_valid(features)` 守卫防止非法 JSON 导致查询抛错。大数据库（1000+ 房源）下图表加载提升 50-80%。
- **N+1 batch UPDATE 修复**：`mark_status_change_notified_batch()` 从 for 循环逐条执行 UPDATE 改为单条 `WHERE listing_id IN (...)` 批量更新，与同文件 `mark_notified_batch()` 模式一致。
- **异常捕获范围收窄**：`scraper.py` `_to_listing()` 的 `except Exception` 改为 `except (TypeError, KeyError, ValueError, AttributeError)`，避免 `KeyboardInterrupt` / `SystemExit` / `MemoryError` 被意外吞掉。`notifier.py` `_send_with_retry()` 两处 `except Exception: pass` 改为 `except (OSError, asyncio.TimeoutError)` 并加 DEBUG 日志。
- **Cloudflare 403 检测统一**：`booker.py` 内联的 CF 检测改为复用 `scrapers/base.py` 的 `is_cloudflare_body()`，消除两份不同实现（booker 旧版只匹配大写 `<!DOCTYPE html>` 会漏检测小写 HTML）。同时确认 `batch_session()` 机制已在 `HollandStayScraper` 实现，P1 多源上线后 HTTP Session 跨城市复用不会退化。

### iOS 性能优化 (iOS Performance)
- **DateFormatter 静态化**：`NotificationItem.createdDate` 每次访问新建 `ISO8601DateFormatter` + 最多 4 个 `DateFormatter`（~100–200μs/次），列表滚动时每行每帧重复分配。全部改为 `static let` 共享实例（`isoFractional`、`isoPlain`、`fallbackParsers`、`shortDateFormatter`），`dayBucket` 的 `Calendar` 也改为 `static let`。消除通知列表最大的单点分配开销。
- **Listing.featureMap 键预归一化**：`featureValue(matching:)` 每次调用都对 `featureMap` 所有键现调 `normalizeFeatureKey`（folding + 多次 replacingOccurrences + lowercased，~4 次分配/键）。`ListingRow` 一行读 5–6 个派生属性、每个再遍历 10–20 个键 → 50 行可见时每帧 ~5,250 次字符串操作。改为 decode 时一次性预算 `normalizedFeatureMap`（`normalizeFeatureKey` 改为 `static`），后续查找只需归一化少量别名。
- **URLCache 条件 GET**：`APIClient` 从 `URLSession.shared` 改为专用 `URLSession`，配 2MB 内存 + 20MB 磁盘 `URLCache`。服务端 GET 200 响应带 `ETag` + `max-age=10` → 10s 新鲜窗口内切 tab 直接命中本地缓存、零网络；超窗后自动带 `If-None-Match` 复验，304 无 body 复用缓存。消除列表/地图/日历/图表的重复下载。
- **通知首屏非阻塞**：`NotificationsStore.fetch()` 中 `loadMoreUntilUnreadIsVisible()` 从同步 `await` 改为后台 `Task` 执行——首屏拿第一页就结束 loading 立刻渲染，不再干等 N 次串行往返。加 `maxBackfillPages=5` 上限防止未读极多时串行拉几十页拖垮网络/电量。`backfillTask` 可取消（登出 / 新一轮 fetch）。
- **地图聚类后台化**：`MapClustering.cluster()` 拆出纯值类型重载（`Double` 替代非 Sendable 的 `MKCoordinateRegion`），`MapView.recomputeClusters()` 改用 `Task.detached(priority: .userInitiated)` 把 2000 条 grid 分桶 + 排序移出主线程。加 `clusterTask` 取消机制防止快速缩放时旧结果覆盖新结果。

## v1.7.9 (2026-05-30)

### 新特性 (Features)
- **用户优先级排序**：Admin 用户管理页新增 rank 排序功能。每个用户卡片显示 `#1` `#2` `#3` 优先级徽标 + ▲/▼ 按钮，点击即可调整顺序。rank 越小自动预订优先级越高——当多个用户同时匹配同一房源时，rank 小的优先拿到（`sort_order` 字段此前已建但无可操作入口）。

### Bug 修复 (Bug fixes)
- **Dashboard 运行时间**：修复 Docker 容器重启后 7 天运行时间从 1% 重新计数的 bug。根因是 `/proc/uptime` 和 `/proc/<pid>/stat` 的 `starttime` 都相对于容器启动时间，重启后两者同时归零导致 `started_at ≈ now`。修复方案是将 monitor 启动时间持久化到 SQLite `meta` 表（`monitor_started_at`），Dashboard 优先读取 DB 值计算运行时间，跨容器重启保持不变；`/proc` 计算保留为回退路径（macOS 开发环境）。
- **抓取 GraphQL data=null 崩溃**：修复 H2S API 返回 `{"data": null}`（GraphQL 字段级 non-null 错误传播至根）时 `.get("products")` 抛 `AttributeError` 导致整轮抓取中断的 bug。改用 `(data.get("data") or {}).get(...)` 安全链式访问，同时第 1 页遇 null 时显式抛出可感知错误。

### 安全加固 (Security)
- **存储型 XSS**：修复 `user_form.html` 中 `renderHoods()` 动态渲染街区名时未转义 HTML 的问题，改用 `escapeHtml(h)`。
- **用户名枚举**：`/check-user` 端点加 IP 限速（30 次/分钟），防批量枚举已注册用户名。
- **Xior 未知状态 fail-open**：`_STATUS_MAP` 未知状态默认值从 `"Available to book"` 改为 `"Occupied"`（fail-closed），避免新状态被误判为可预订。
- **Android 签名密钥**：移除 `build.gradle.kts` 中硬编码的签名密码，改为从 `local.properties` / 环境变量读取。

## v1.7.8 (2026-05-28)

### 代码质量 (Code Quality)
- **后端**：device_service 平台路由改为显式 allowlist；`asyncio.run()` → `_run_async()` 兼容 async worker；FCM env var 加 `_safe_int/_safe_float` 防配置错误崩溃；config.py env key 正则加 `\b` 防前缀碰撞；Dashboard uptime 改用 `/proc/<pid>/stat` 计算进程真实启动时间（修复 Docker 重启后 uptime 不变的问题）。
- **Web 前端**：`escapeHtml` 补全单双引号转义；所有 fetch 静默 catch 加 `console.error`；multi-select 加 `aria-haspopup`/`role`/`aria-expanded` 无障碍属性。
- **iOS**：`resolveBaseURL()` 移除 force-unwrap 启动崩溃风险；NotificationsStore 静默 catch 加 DEBUG 日志。
- **Android**：`rememberPullToRefreshState()` 从 recompose 重建改为外部 `remember`；AuthViewModel 全局 catch 前置 `CancellationException` 重新抛出。

### Bug 修复 (Bug fixes)
- **Android FCM 推送端到端**：客户端 FCM 通道已拉通并真机验收通过。后端 `POST /api/v1/devices/test` 按 `platform` 分流——iOS 走 APNs，Android 走 FCM（data-only payload）。
- **Android 启动 ANR**：修复 App 冷启动时主线程阻塞导致 ANR 的问题。根因是 `SseClient.connect()` 的 `callbackFlow` 继承了 `viewModelScope.launch` 的 Main dispatcher，`readUtf8Line()` 在主线程上阻塞等待 SSE 数据；修复方案是将整个 SSE 读取循环包在 `withContext(Dispatchers.IO)` 中。
- **Android 地图定位**：修复 MapScreen 定位功能不可用的问题。移除自定义定位按钮，添加 `play-services-location` 依赖，改用 `FusedLocationProviderClient.getLastLocation()` 获取缓存位置；暗色模式下地图自动切换暗色样式。
- **Android 通知分类**：修复测试推送在通知列表中被归类为 BOOK 的问题，新增中文/emoji 关键词（🧪、测试推送、推送链路）匹配为 TEST 类型。
- **Android 崩溃诊断**：新增 `CrashReporter` 全局异常捕获，自动收集堆栈 + 设备信息，POST 到 `/api/v1/diagnostics/crash`（bearer_optional），同时写入本地文件兜底。后端诊断端点新增 `platform` / `os_version` 字段兼容 Android。
- **Android 登录页**：移除 Staff 管理员登录入口，保留 Tenant / Guest 两种模式。
- **Android 通知页**：新增进入页面时自动刷新列表，避免首次加载后 SSE 未连接时数据陈旧。
- **Android 日历性能**：修复 `CalendarScreen` 月历网格每次 recompose 重建 42 个 `LocalDate` 对象的问题，加 `remember(month)` 缓存；`DashboardScreen` 价格排序 `Regex` 从每次调用重建改为顶层单例。
- **iOS 通知筛选性能**：修复通知列表切换 type filter 时卡顿问题。`NotificationItem.kind` 从计算属性改为 decode 时预计算的存储属性（O(n) 字符串匹配 → O(1)），`currentFilterScope()` 改用 `rebucketDayGroups()` 中缓存的 kindCounts（消除额外 O(n) 扫描）。500 条通知下 filter 切换从 ~500ms 降至 ~20ms。
- **iOS 列表页性能**：修复 ListingsView 每次 body 重渲染都做 O(n log n) 排序 + O(n) `isNew()` 扫描（含每条 date 解析）的性能问题。改为 `@State` 缓存排序和 new/earlier 分桶结果，仅在 `store.listings` 或排序条件变化时重算。
- **iOS 状态色统一**：修复 Reserved 胶囊在不同页面显示颜色不一致的问题（详情页红色、通知页系统灰、列表页灰蓝）。统一所有页面 Book/Lottery/Reserved 状态色为 asset catalog 语义 token（`.statusBook`/`.statusLottery`/`.statusReserved`），涵盖 ListingDetailView、CalendarView、MapView、NotificationRow。
- **街区筛选保存失效**：修复用户编辑页面中，选择街区后点击保存实际未保存的问题。原因是街区下拉框动态加载时，`loadNeighborhoods()` 重新渲染 DOM 时使用了页面初始快照值 `selNbh`，覆盖了用户当前的勾选状态。

## v1.7.8 (2026-05-27)

### 体验优化 (UX)
- **登录页注册确认弹窗**：按钮改为「登录 / 注册」，点击后弹出确认卡片，分两步说明（首次登录自动创建账户 + 同意条款/隐私政策），用户确认后才提交表单。
- **邮箱即时验证**：用户配置邮箱时，输入合法格式的邮箱地址后即时出现「发送验证邮件」按钮，无需先保存表单再重发验证。
- **登录页提示文字换行优化**：推送功能说明与自动注册说明分行显示，阅读更清晰。

### Bug 修复 (Bug fixes)
- **多选下拉菜单底部溢出**：修复页面底部多选组件（如租客类型）下拉菜单超出视口无法点击的问题，现会自动向上翻转展开。

## v1.7.7 (2026-05-27)

### 代码维护 (Maintenance)
- 修复 `config.py` `_parse_xior_cities()` 死代码（残留 return 语句）
- 修复 `load_config()` 中 DB_PATH/TIMEZONE 热重载时不从 os.environ 重新读取的问题
- 修复 `MultiNotifier` 未调用 `super().__init__()` 导致 language 属性未初始化
- 修正 `scraper.py` 403 维护探测阈值注释与代码不一致
- Settings 页面 flash 消息硬编码中文改为走翻译系统
- 移除 Settings 页面冗余的 User Management / Client Management 提示条
- CSS 暗色模式颜色切换统一走变量：新增 `--grad-green/amber/red`、`--pill-telegram/email` 变量
- 暗色模式下文字渐变色提亮一档；Telegram/Email 渠道标签自动适配主题
- 修复暗色模式下表单输入框、filter 卡片边框的硬编码白色内阴影

### 界面优化 (UI)
- Web 端全面换用毛玻璃（Glassmorphism）设计风格
- 仪表盘、日历、统计、地图等核心页面统一玻璃质感

## v1.7.6 (2026-05-26)

### 新特性与界面重构 (Features & UI Revamp)
- **全新 B2C 风格登录页**：彻底重构登录页面，采用现代毛玻璃（Glassmorphism）视觉风格，摆脱后台管理系统的刻板印象。
- **日夜交替动态主题**：登录页新增日间/夜间模式切换功能，包含平滑的日出日落、月亮升起动画。
- **荷兰风情地平线动画**：登录页地平线新增两座纯 CSS 绘制的传统荷兰风车剪影，包含动态旋转的风帆与日夜光影适配。
- **客户端下载入口**：登录页新增 iOS 和 Android App 下载入口（安卓版提示“积极上架中”）。
- **注册与访客模式优化**：
  - “访客模式（只读）”文案精简为“访客模式”。
  - “注册用户账号”精简为“注册账户”。
  - 为副按钮（注册、访客）补齐了发光效果（Glow）与悬浮动画。
  - 为注册面板展开增加了纯 CSS 实现的丝滑手风琴下拉动效。
  - 加大登录页底部的 帮助、隐私、条款、赞助 等辅助链接的字号，“赞赏”统一更名为“赞助”。

### 体验优化与调整 (Enhancements)
- **数据统计面板**：默认显示的时间维度由“近 30 天”调整为更聚焦的“近 7 天”。
- **侧边栏结构优化**：
  - “App 会话管理”更名为“客户端管理”。
  - “系统信息”菜单项移至侧边栏最底部，优化功能层级。
- **房源筛选栏**：重构筛选表单的 CSS 布局（引入 Grid），确保多行表单元素的完美对齐。
- **仪表盘状态徽标**：对齐了 Recent Listings 与 Status Changes 模块的徽标样式。
- **赞助页面优化**：修复了收款码容器比例问题，自适应长方形收款码，消除多余的白边。

### 缺陷修复 (Bug Fixes)
- **崩溃报告路径脱敏**：修复了崩溃报告中直接暴露本地服务器物理路径的问题，现统一脱敏展示为相对路径 (`/app/data/crash_reports`)。

## v1.7.5 (2026-05-25)

### 全量代码审查与安全加固

5 路并行扫描，26 个发现，修复 22 个：

**Android（6 修复）**
- **SSE 阻塞 Main 线程**：`SseClient.connect()` 内 `call.execute()` 包 `withContext(Dispatchers.IO)`
- **深链接断开**：`AppNavigation` 观察 `NavigationCoordinator.pendingListingId` → `navController.navigate("listing/$id")`
- **弱网误删 token**：`restoreSession()` 改为只对 `ApiException.isAuthError` 清 token，网络异常不清
- **Settings 状态擦除**：`saveServerUrl/saveColorScheme` 改用 `.copy(message=)` 而非新建 `SettingsUiState()`
- **LocationListener 泄漏**：`MapScreen` "My Location" 加 15s timeout，超时自动 `removeUpdates`
- **filter 不生效**：`PUT /api/v1/me/filter` 成功后加 `write_reload_request()`，让 monitor 热重载

**Python 后端（11 修复）**
- **SSE 绕过禁用用户**：加 `_user_token_still_allowed()` 检查
- **CSP 缺位**：`web.py` 加 `Content-Security-Policy` header
- **HSTS 缺失**：加 `Strict-Transport-Security: max-age=63072000`
- **booker None 崩溃**：`book_with_fallback` 返回 None 时 `continue`
- **Guest 无 CSRF**：`/guest` GET→POST + `@csrf_required`
- **房源 API 无限流**：guest 访问 100 req/min IP 限流，超限返 429
- **status change FCM gate 遗漏**：加 `get_fcm_client() is not None` 条件
- **测试推送 flash 条件错误**：`any("/" in m)` 替代复杂条件
- **CSS 无效值**：`active` → `var(--accent)`
- **测试推送无日志**：加 `logger.info(...)` 操作审计
- **stale docstring**：`legal_text.py` → `app.legal/`
- **Web 地图加载修复**：CSP 增加 Google Maps 所需的 `maps.googleapis.com` / `maps.gstatic.com` 许可，避免动态加载 Google Maps JS 被浏览器拦截后页面一直停在“加载中”；地图脚本增加 `onerror` 和初始化超时提示。
- **Web Google Map 性能优化**：保留 Google Maps 的同时接入 marker clustering；marker 改为 `requestAnimationFrame` 分批创建，InfoWindow 内容改为点击时懒创建，clusterer CDN 超时后自动降级为普通分批 marker，避免几百个点同步渲染卡住主线程。

**iOS（5 修复 + 31 单元测试）**
- **Biometric crash**：`SecAccessControlCreateWithFlags(...)!` → `guard let`
- **Dashboard 并发 mutation**：`fetchSummary()` 加 `guard !isLoading`
- **PushDelegate IUO**：`shared: PushDelegate!` → `PushDelegate?`
- **AdminStore 死代码**：清理未使用的 `original` 变量
- **LegalSheetView API fetch**：`.task {}` 拉取法律文本 + 本地 fallback
- **iOS 单元测试补齐**：新建 `FlatRadarTests` target（31 tests），覆盖 Listing/APIResponse/AuthModels/NotificationItem 模型编解码与状态逻辑。此前 13K 行代码零单元测试，现核心模型层已覆盖

### Android App — Map and Settings parity

- **Google Maps Compose 接入**：Android Map 页从城市分组列表升级为 GoogleMap marker 视图；接入 `maps-compose-utils` 官方 clustering，marker 按状态着色，初始 camera 根据房源 bounds 适配，点击 marker/cluster 显示底部房源卡片并可进入详情。
- **Android Map/Calendar 状态打磨**：Map 底部选中卡片补齐状态、价格、面积、入住日期和来源信息；Map/Calendar 错误态和空态增加 retry。
- **Android Map/Calendar DTO 对齐**：修复 `/map` 与 `/calendar` 返回 `data.listings`、`lat/lng`、`building` 轻量字段时的解析路径，移除开发期误加的 `items` fallback，避免进入 Map/Calendar 后出现 `Required value 'items' missing at $.data`。
- **Maps key 本地配置**：Gradle 从 `android/local.properties` 读取 `MAPS_API_KEY`，注入 Manifest 和 `BuildConfig`；未配置时 Map 页保留列表 fallback，避免开发/CI 白屏。
- **Settings 运行时配置**：新增 DataStore `PreferencesManager`，持久化 `server_url` 和 `color_scheme`；App 启动后自动应用 server URL，主题支持 System / Light / Dark。
- **Android Biometric sign-in**：user 登录/注册可选择保存本机生物识别登录；登录页通过系统 BiometricPrompt 解锁后复用正常登录 API，Settings 可移除本机保存凭据。
- **Android A1 错误展示**：新增 root `AppErrorBus` + snackbar，登录、注册、Dashboard、Listings、Listing Detail 的后端/网络错误统一进入全局提示。
- **Android 登录兼容存量账号**：Sign in 前端校验改为只要求密码非空，兼容后端已有 3 字符密码用户；注册和改密码仍保留新密码至少 4 字符。
- **Android 顶层导航修复**：从 Listing Detail 等二级页面点击 Dashboard/Browse/Alerts/Settings tab 时禁用详情栈 restore，避免 tab 看似无效、只能 Back 返回。
- **Android Browse 子模式入口**：phone 端 Browse tab 增加 List / Map / Calendar 二级 tabs，让 Map 和 Calendar 在 4-tab 布局下可见；tablet 端继续保留独立 Map/Calendar rail 项。
- **Android 品牌资源接入**：复用 `static/logo.png` 生成 Android launcher icon，并在登录页展示 FlatRadar logo，替换开发期默认图标体验。
- **Android Material 3 设计系统接入**：按 `FlatRadar Android M3.html` 设计规范落地第一批原生 Compose 改造，更新 M3 seed 色 `#0057CC`、light/dark color roles、Typography、Shape、状态色 token、80dp bottom navigation、Login、Dashboard hero 和 Alerts 列表/功能胶囊样式。
- **Android Dashboard Explore 统计修复**：`ChartEntry` 改为兼容后端 `source/status/range/hour/city/label/date` 动态字段，恢复 Explore 下平台、状态、价格、类型、能源、租客统计卡片展示，并按 iOS 逻辑合并 source/type/energy bucket。
- **Android Dashboard 统计交互修复**：Explore 统计卡片恢复点击展开能力，通过底部弹层展示完整分布明细和条形占比；Dashboard 根内容增加 status bar inset，避免标题与手机状态栏重合。
- **Android Browse 状态栏适配**：Browse 页 List / Map / Calendar 顶部切换栏增加 status bar inset，避免 edge-to-edge 模式下与系统状态栏重合。
- **Android Calendar 日期分组修复**：Calendar 不再复用会过滤 `2049/2050` 占位日期的通用 `ServerTime.dayKey()`，改为按 iOS Calendar 专用逻辑读取 `available_from` 前 10 位并校验日期，避免后端已有房源但选中日期列表为空。
- **Android M3 页面收口**：Listings 改为 M3 surface card 列表与 pill 搜索/筛选；Listing Detail 增加 M3 hero、tonal CTA 和 grouped detail sections；Settings 改为 profile card、tonal save button、40dp leading icon containers；Map/Calendar 统一 surfaceContainer、shape 和 FlatRadar 语义状态色。
- **Android Listing Detail 字段对齐**：详情页字段改为和 iOS 一样从后端 `feature_map` / `features` 派生 Type、Area、Building、Floor、Rooms、Energy、Finishing、Occupancy、Contract、Tenant，修复后端已有数据但 Android 显示 `—` 的问题。
- **Android Listing Detail parity**：详情页补齐 source/status/city 头部、价格/入住日期/面积/建筑 metric cards、Key Details、All Details、Monitoring 和官方平台链接；当前 API/model 无 listing 图片 URL，图片展示继续等待数据源。
- **Android 账号合规**：user Settings 增加 Change Password，调用 `/auth/password` 更新 app password，并显示其他 session 撤销结果。
- **Android 数据导出**：user Settings 增加 Export My Data，调用 `/me/export` 拆出 `data` JSON 后通过系统分享面板交付，不写入本地文件。
- **Android 法律入口**：Settings 增加 Terms of Use / Privacy Policy 页面，普通用户和 guest 可离线打开；admin 继续隐藏法律入口。
- **Android Calendar 月格**：Calendar 页从月份列表升级为月历网格，每日显示可入住房源数量，选中日期后展示当天房源并可进入详情，空态/错误态可重试。
- **Android Alerts inbox**：通知页按 TODAY / YESTERDAY / EARLIER 分组，增加类型色点、相对时间、Live 状态、单条 mark read、滑动已读、单条更多菜单和导航 unread 角标。
- **Android 计划文档复盘**：`docs/ANDROID_PLAN.md` 增加当前实现进度、A2/A5 状态和 FCM 阻塞说明。
- **Android Alerts 界面重设计**：重新设计 Alerts 界面，使用更具现代感的多色小药丸（如 New 绿点、Status 橘点）、未读角标叠层显示、横向过滤 Chip 及更现代扁平化分割线布局，提升列表的可读性与美观度。
- **Android Settings 界面优化**：去除了 `server_url` 服务器配置选项，防止普通用户误修改；并在“Push Notification Filter”设置栏下方动态显示当前应用中的活跃过滤条件摘要，点击可直接跳转到过滤配置页。
- **Android 登录界面打字性能优化**：将原本在重组时动态创建的 `BackMountainPoints` 与 `FrontMountainPoints` 坐标对列表抽离为顶层静态常量；重构 `MountainPath` 绘制函数，使用 `Modifier.drawWithCache` 将 `Path` 初始化移动到缓存区，避免打字重组触发 draw 帧时重新分配 Path 对象，实现零对象绘制和流畅打字；并使用 `remember(isDark)` 缓存顶部背景渐变。
- **Android 登出二级防误触**：在 Settings 界面点击 Log Out 时，加入 `showLogoutDialog` 状态并拉起二级确认弹窗（AlertDialog），防止用户误点导致会话非预期终止。
- **法律文本三端统一**：新增 `app/legal/*.txt` 作为 canonical source of truth（terms_en/zh + privacy_en/zh），`GET /api/v1/legal` 公开 API 端点（无需登录）。三端改为 API 优先 + 本地缓存 fallback：Android `LegalScreen` → `LegalViewModel` fetch，iOS `LegalSheetView` → `.task` async fetch，web `app/routes/legal.py` → `app.legal.get_legal()`。删除旧的 `legal_text.py`（web）、`LegalText.kt` 降级为 Android 离线 fallback、`LegalText.swift` 降级为 iOS 离线 fallback。免责条款同步更新为多平台中立声明（"not affiliated with any of the housing platforms it monitors"），去掉原先仅提 Holland2Stay 的单一措辞。
- **Android 中文字符翻译完整覆盖**：`values/strings.xml` + `values-zh/strings.xml` 各 ~170 条目完全对称，覆盖 Tab、仪表盘、登录注册、房源列表/详情/筛选、地图、日历、通知、设置、管理面板、使用条款和通用文案。
- **Android FCM 推送完整闭环**：
  - 客户端：`FcmService`（onNewToken + onMessageReceived）、`FcmTokenManager`（设备注册/注销 + 异常日志）、通知渠道（listings/general）、Android 13+ 运行时权限、通知点击 deep link 全部接入。
  - 后端：`notifier_channels/fcm.py`（OAuth2 服务账号认证 + FCM HTTP v1 API，send_one/send_many），`mcore/push.py` 按 `device_tokens.platform` 字段分流 iOS（APNs）/ Android（FCM）双发，所有 dispatch 函数双端覆盖。
  - 测试：Python FCM 35 tests（client 18 + dispatcher 17），Android 47 ViewModel tests。
- **Android Listing 模型对齐 iOS**：删除 `Listing` 中 9 个与 `featureMap` 重复的硬编码字段（areaText/energyLabel/buildingText/finishing/floor/rooms/occupancy/contractType/tenantRequirement），`display*` 计算属性统一从 `featureMap` 派生。`MapCalendarListingDto.toListing()` 将 DTO flat 字段 `putIfAbsent` 合并进 `featureMap`，后端改 key 名时两端同步自适应。
- **架构审查（5 Critical + 10 Warning）**：确认 SQLite WAL 模式多进程安全、users.json 仅作一次性迁移输入运行期只读 SQLite；修复 FCM 私钥日志泄漏风险（不再 dump traceback）；`FcmTokenManager` 不再静默吞异常；`push.py` 移除 `storage.conn` 直接访问、补齐 FCM 路径日志。


## v1.7.1 (2026-05-23)

### 平台维护态检测与安静降级

Holland2Stay 计划维护期间整站（含 GraphQL API）返回 403，旧路径将所有 403 一律当作 Cloudflare WAF 屏蔽处理——发用户告警、走 15 min 冷却、打 ERROR 日志。维护态下用户什么都做不了，凌晨告警是噪音。

v1.7.1 引入 **UpstreamMaintenanceError**，在 403 连续出现时主动探测主站，命中维护页则走"安静等待"路径：

- **`scrapers/base.py` — 维护检测基础设施**：新增 `UpstreamMaintenanceError` 异常类（与 `BlockedError` 语义区分——前者自己会恢复、后者需人工介入）；`is_maintenance_body()` 通过 5 组英文短语识别维护占位页；`probe_h2s_maintenance()` GET 主站探测，异常安全（网络错误吞掉返回 False）。
- **`scraper.py` — 连续 403 触发探测**：进程级 `_consecutive_403_count` 跨轮累计，达阈值 3 时 GET 主站；命中维护页 → 抛 `UpstreamMaintenanceError` 并清零 streak；未命中 → 维持原 `BlockedError` 路径。成功响应自动清零 streak。
- **`scrapers/__init__.py` — dispatcher 维护优先**：所有任务失败时 `UpstreamMaintenanceError` 优先于 `BlockedError` 上抛，确保 monitor 选择正确冷却策略。
- **`monitor.py` — 维护态两段处理**：
  - `run_once`：捕获后写 `upstream_maintenance_seen_at` / `upstream_maintenance_last_at` meta 键驱动 dashboard banner；**不给普通用户发告警**（避免凌晨维护吵醒人）；给 admin web 通知面板发一条（1 小时节流）。抓取成功时自动清空维护态 meta。
  - `main_loop`：15 min 冷却（与 BlockedError 同长度但语义不同——INFO 日志、不重置 adaptive_peak、不计入 network_fail_streak）。
- **Web dashboard 维护 banner**：新增 `.maintenance-banner` CSS（温和警告色，区别于 error alert）；`base.html` 顶部渲染维护标题 + "Since X time ago"；`_inject_upstream_maintenance` context processor 注入状态；`monitor_service.py` 新增 `get_upstream_maintenance()`。
- **翻译**：3 个新 key（`upstream_maintenance_title` / `_hint` / `_since`），中英双语。

### OurDomain TLS 指纹智能轮换

SecureRC（OurDomain 用的 RentCafe + Cloudflare）对 TLS 指纹做 per-fingerprint 跟踪——同一指纹短时间内重复使用会被标记进入"挑战中"状态返 403。旧实现每次 `scrape()` 固定从 chrome131 开始依次重试，等于反复把"被烧"的指纹往枪口上送，chrome131 / chrome124 看起来"特别容易被封"只是因为它俩总是排最前面。

v1.7.1 引入进程级指纹状态记忆 + 同 session 内 403 软重试：

- **指纹状态追踪**（`_FINGERPRINT_STATE`）：成功通过的指纹记录 `last_good_at`，下次 `scrape()` 优先用它；403 失败的标记 30 min cooldown，期内排到队尾。排序逻辑：上次成功 → 未冷却 → 冷却中兜底。进程重启清空（等于"忘掉旧烧"从配置顺序重探），指纹热度本身就是分钟级现象，无需持久化。
- **同 session 内 403 软重试**（`_get_text`）：Cloudflare JS challenge 返回 403 的同时也会下发 `cf_clearance` cookie，`curl_cffi` 不跑 JS 算不出 challenge token，但 cookie 已攒到 session 上——第二次 GET 同 URL 往往直接通过。拿到 Cloudflare 403 后先等 2s 再同 session 重试 1 次，仍失败才抛 `BlockedError` 让上层切指纹。大幅减少"换指纹"开销，稳态下一个指纹即可稳定服务。
- **`_impersonate_attempts()` 智能排序**：从固定顺序改为按状态分桶 → 合并（last_good → fresh → cooldown），受 `OURDOMAIN_WAF_RETRIES` 限制长度。

### 测试

- **`tests/test_scraper_maintenance.py`**（6 个类，17 个测试）：`is_maintenance_body` 单元测试、`probe_h2s_maintenance` 单元测试、`_post_gql` 403 streak → 维护探测全链路、dispatcher 维护优先上抛、monitor 维护态 admin 通知 + 节流 + meta 写入。

### iOS App — 性能优化

- **Dashboard chart 请求分批**：`fetchMiniCharts()` 从 7 并发改为 3 批串行（3→2→2），峰值并发从 7 降到 3，首页 sparkline + source/status mini card 最先返回。避免慢网络下 TCP 队头阻塞同时打到后端。
- **`Listing.isNew` / `ageText` 减少 `Date()` syscall**：新增 `isNew(asOf:)` / `ageText(asOf:)` 重载，调用方可外部快照 `now` 复用。`ListingsView` 分桶循环从每条 `Date()` 改为循环前快照一次（100 条 = 100→1 次 syscall）；`ListingRow.titleLine` 中 `isNew` 和 `ageText` 共用同一个 `now`（每行 2→1 次 syscall）。

### Web 前端 — 性能优化

- **SSE bfcache 支持**：admin 页面在 `pagehide` 时关闭 SSE EventSource 连接，`pageshow` 时若从 bfcache 恢复则重连。配合 `Cache-Control: no-cache`（而非 `no-store`），浏览器可将当前页放入 back-forward cache，返回键瞬间复原不再空白卡死。
- **状态胶囊 filter 归并**：新增 `status_capsule` filter，一次 `.lower()` 同时返回 label + CSS 类名。模板每行从 `status_short` + `status_badge` 两次 filter 调用（各做一次 `.lower()`）改为单次调用。`listings.html` / `index.html` 的表格和移动卡片均已简化。
- **LCP 优化**：`design.css` preload 加 `fetchpriority="high"`；sidebar logo preload；CSS 版本号升至 v16。
- **SQLite 索引补全**：新增 `listings(city)`、`listings(first_seen)`、`listings(status)`、`listings(last_seen)`、`status_changes(changed_at)`、`status_changes(listing_id)` 6 个索引。dashboard 首页城市筛选 / 状态计数 / 排序、status_changes JOIN 查询不再走全表扫描。
- **维护态查询缓存**：`_inject_upstream_maintenance` context processor 加 5s TTL 缓存。之前每个页面渲染都读 SQLite meta 表，现在最多每 5 秒读一次。
- **Dashboard 60s 自动刷新**（已知问题，待修）：当前用 `window.location.reload()` 整页硬刷新，浪费带宽和服务器资源。建议改为 AJAX 局部刷新。

---

## v1.7.0 (2026-05-22)

### 后端 — 多源抓取架构（P0→P1）

- **`scrapers/` 包**：新增 `AbstractScraper` ABC + `ScrapeTask`/`ScrapeResult` 协议。每个第三方平台实现 `scrape(task) → ScrapeResult`，`dispatch_scrape_tasks()` 按 `source` 路由、隔离故障、合并产出。
- **`scrapers/base.py`**：共享异常 `RateLimitError`/`BlockedError`/`ScrapeNetworkError` 从 `scraper.py` 迁入，所有 scraper 统一异常协议。
- **`scrapers/holland2stay.py`**：`HollandStayScraper` 封装现有 GraphQL 抓取逻辑，行为零变更。
- **`monitor.py` 全量切换到 `dispatch_scrape_tasks()`**：旧 `scraper.scrape_all()` 路径已移除。多源抓取结果合并后统一走 diff → notify → book 管线。
- **`Listing.source` 字段**：标识房源平台来源（`"holland2stay"` / `"ourdomain"`），UI/通知模板可据此显示 source badge。

### 后端 — OurDomain / RENTCafe 集成

- **`scrapers/ourdomain.py`**：`OurDomainScraper` — RENTCafe 两阶段抓取（`floorplans.aspx` → `availableunits`），单元级数据提取（房间号 #6045、面积单值 m²、月租单值 €、押金、楼层、朝向），`unit_id` 跨 FP 去重，`parse_ourdomain_floor()` 楼层解析（Ground → 0）。
- **HTTP 策略**：`curl_cffi` + `safari17_0` impersonation 通过 RENTCafe Cloudflare（Chrome 指纹在此路径被拦，Safari 可过 GET + POST）。
- **自动预订侦察**：RENTCafe 多步 ASP.NET 表单 POST → `rcformsave.ashx`；受 reCAPTCHA v3+v2 保护。第三方解决服务（capsolver/2captcha）可行但未实现——后续步骤待手动侦察。详见 [OURDOMAIN.md](OURDOMAIN.md) §10。
- **`OurDomainScraper` 已注册到 `SCRAPER_REGISTRY`**，`scrape_tasks_v2()` 展开 `OURDOMAIN_CITIES`。
- **Diemen & South-East 共用一个 RENTCafe property (184283)**，8 个物理单元，每个单元可签多种合同类型。

### 后端 — 预订管线重构

- **`mcore/booking.py`**：`book_with_fallback()` 抽取到独立模块，按面积降序尝试备选房源；`RetryQueue` 持久化竞败候选，跨轮重试。
- **`mcore/interval.py`**：自适应间隔 + 抖动逻辑独立模块。
- **`mcore/prewarm.py`**：`PrewarmCache` 进程级 session 缓存，TTL 刷新。
- **`mcore/push.py`**：APNs 推送调度独立模块，含去重节流。

### iOS — APNs 双语推送

- **`_T` 中英翻译表**：9 条通知模板（新房源标题/正文、状态变更、预订成功、聚合轮次、异常告警），`_t(text, lang)` 查表。
- **按设备语言分组发送**：`_send_to_user()` 取设备 `language` 字段，分组后每语言组构建独立 payload；同一用户中英设备各收各的语言。
- **推送去 emoji**：标题/正文移除所有 emoji，仅保留 `[H2S]`/`[OD]` source tag 前缀。

### iOS — 设备语言上报

- **`PushStore.currentLanguage`**：读取 `Locale.current.language.languageCode`，iOS 16+ 兼容。
- **`DeviceRegisterRequest.language`**：`POST /api/v1/devices/register` 新增 `language` 字段。
- **`device_tokens.language`**：DB 新增列（默认 `'en'`），幂等 migration 兼容老库。

### DB 迁移

- **`device_tokens.language`**：`TEXT NOT NULL DEFAULT 'en'`，幂等 `_add_column_if_missing`。
- **`user_configs.language`**：`TEXT NOT NULL DEFAULT 'en'`，幂等 `_add_column_if_missing`。用户推送语言偏好。
- **`mstorage/_listings.py`**：新增 `count_by_status()` 方法，仪表盘用。

### 通知多语言

- **`UserConfig.language`**：新增字段（`"en"` / `"zh"`），控制 iMessage/Telegram/Email/WhatsApp 推送语言。
- **`notifier.py`**：`_NOTIF_LABELS` 18 条中英翻译表 + `_tl(text, lang)`。`BaseNotifier.__init__` 接收 `language`，`_format_*` 所有标签走 `_tl()` 动态切换。
- **通知文案去中文硬编码**：`WebNotifier` 和所有 `_format_*` 中的硬编码中文（`/月`、`入住`、`新房源上架` 等）改为英文 + `_tl()`，全渠道统一。
- **APNs 推送**：此前已独立支持双语（按设备语言），不受此变更影响。

### 后端 — Xior 集成

- **`scrapers/xior.py`**：`XiorScraper` — WordPress AJAX JSON 抓取（`admin-ajax.php?action=yardi_room_availability`），单元级数据（房号 M1.30.53、精确面积 m²、月租 €、押金、入住日期、直达预订链接），`apartmentId` 去重。429 退避重用 `RATE_LIMIT_BACKOFF`。
- **建筑字典**：荷兰 30 栋楼（15 城市），含 `property_page_id`、`semester_id`、`room_type_ids`，自动发现 + 手动维护。
- **`discover_buildings()`**：城市页 → 建筑页 → 提取 Yardi modal 元数据，可一键刷新全量楼数据。
- **HTTP 策略**：`curl_cffi` + 1.5s 间隔防 CF 限流。Turnstile 不验证服务端（空 token 返回完整数据）。
- **Config**：`KNOWN_XIOR_CITIES`（30 栋），`XiorCityFilter`，`scrape_tasks_v2()` 集成，`.env` 默认 Eindhoven 两栋楼。

### iOS — Alerts 界面重设计

- **`NotificationsView` V3**：与 Dashboard / Browse 视觉语言对齐——`insetGrouped` 白色大圆角容器 + hairline 分割，不再逐行独立卡片。顶部双药丸 toolbar（type filter + Mark all read）。Live pill 绿点 + halo 呼吸动画。删除 emoji 和 32×32 icon tile → 8pt 小色点。

### Bug 修复

- **`stop_monitor()` 残留 PID 文件**：`terminate_process()` 杀进程后未清理 `monitor.pid`，导致 `monitor_pid()` 返回僵尸 PID→仪表盘误显示"监控运行中"。修复：`stop_monitor()` 增加 `PID_FILE.unlink(missing_ok=True)`。
- **仪表盘 toggleMonitor 状态竞争**：`visibilitychange` 事件在切回标签页时强制 `location.reload()`，与 `toggleMonitor` 成功的本地 DOM 更新竞争——本地刚改为"已停止"，切页回来 reload 又把后端状态（进程尚未完全退出）覆盖回"运行中"。修复：`toggleMonitor` 成功分支改为 `location.reload()`，去掉脆弱的 16 行手动 DOM 操作。
- **`scrapers/holland2stay.py` 缺汇总日志**：日志只显示内部 `scraper: [Eindhoven] 共抓取 12 条`，缺少 `scrapers.holland2stay:` 前缀的 source 级汇总，与 OurDomain/Xior 日志格式不一致。修复：`HollandStayScraper.scrape()` 返回前加 `logger.info("[%s] Holland2Stay 共抓取 %d 条房源", ...)`。
- **`scraper.py` / `ourdomain.py` 重复常量**：`_RATE_LIMIT_BACKOFF` 和 `_is_cloudflare_body` 在两处重定义。修复：移至 `scrapers/base.py` 并导入复用。

### Web — Xior 适配

- **`app/jinja_filters.py`**：`source_label` / `source_short` 加 Xior（`"Xior"` / `"XR"`）。
- **`templates/settings.html`**：平台勾选加 `XR · Xior`，新增 Xior 楼盘复选框（30 栋）。
- **`app/routes/settings.py`**：读写 `XIOR_CITIES` env，`allowed_sources` 加 `"xior"`。
- **`app/services/listing_service.py`**：`_xior_display_name()` 处理 Xior 房源名显示。
- **`translations.py`**：`settings_xior_cities`、`settings_xior_hint` 中英标签。

### 文档

- **`docs/XIOR.md`**：完整设计文档（10 节）— 平台概况、技术验证、数据快照、三阶段抓取流程、Listing 映射、平台对比、实现设计、通知模板、风险、工程量。
- **`docs/OURDOMAIN.md`**：完整设计文档（11 节）— 含自动预订可行性分析（§10）和 reCAPTCHA 绕过方案。
- **`docs/SCRAPING_RECON.md`**：Xior 加入速览矩阵（第 1 位）；§5 Xior 独立侦察报告；原有 §4 OurDomain 更新；§7 推荐路径重排（Xior 排第一）。
- **`docs/README.md` / `docs/README_cn.md`**：项目描述 H2S 单平台 → 多平台（H2S + OurDomain + Xior）；数据流图、模块职责表、技术决策表全面更新。
- **`docs/CHANGELOG.md`**：v1.7.0 条目。

### 测试

- **`tests/test_ourdomain_scraper.py`**：27 个测试（FP ID、单元解析、楼层映射、occupancy 推断、抓取流程、403 异常、TLS 指纹重试）。
- **`tests/test_push_dispatcher.py`**：推送测试适配新 `payload_fn` 接口，17/17 通过。
- **`tests/test_scraper_dispatch.py`**：多源 dispatch 部分成功/全量失败场景，2/2 通过。

---

## v1.6.1 (2026-05-21)

### iOS — Settings 重构

- **Settings 按角色精修**：admin 登录后的 Settings 隐藏「Legal」入口（admin 自己维护条款 / 隐私政策，再放一遍是噪音）。
- **User 推送开关**：user 端 Push Notifications 板块去掉 Permission / Device ID 诊断行，改为一个 `Enable Notifications` 开关；OFF 时删除当前设备后端绑定，ON 时申请权限 + 重新注册。系统 Notification 权限为 `denied` 时开关禁用 + 引导去 iOS Settings。admin 端保留诊断信息和 Test Push / Re-register 按钮。
- **`PushStore.setEnabled(_:)`**：与 `logout` 区分——只清后端 device 绑定、不清缓存的 APNs token，用户再次开启时可直接复用。
- **Buy me a coffee 视觉**：section header 用 SF Symbol `cup.and.saucer.fill` 替代 ☕ emoji（HIG 推荐 UI chrome 用 SF Symbol，VoiceOver 朗读语义化"cup and saucer"），文字在前 / 图标在后。

### iOS — Live 心跳点动画与 LoginView 修复

- **LoginView "live" 绿点呼吸动画修复**：原本 ripple 外层圈被 badge `.clipShape(RoundedRectangle(cornerRadius: 12))` 在左上角剪掉一块，动画看起来朝右下"鼓出去"而不是原地呼吸。改用 `.background(_, in: RoundedRectangle(...))`，与 DashboardView.liveBadge 写法对齐——圆角只作用于背景层、content 不参与裁剪。
- **核心点加柔光晕**：7×7 核心点叠 `.shadow(color: iconColor.opacity(0.4), radius: 5)`，1.0 → 1.12 微缩放在小尺寸下也清晰可感。
- **reduceMotion 同步停起**：补 `.onChange(of: shouldAnimate)`，会话中途切换"减弱动态效果"时正确停 / 起循环（与 DashboardView 对齐）。

### iOS — Accessibility（覆盖 6 / 9 ASC Nutrition Label 条目）

**VoiceOver + Voice Control**
- icon-only 按钮全部补 `.accessibilityLabel`：AdminMonitorView 刷新箭头、ListingsView 搜索框 ✕、CalendarView 月份 ◀/▶、MapView Safari 图标、BrowseView mode menu、过滤 chip ✕。
- 关键自定义视图 `.accessibilityElement(children: .ignore)` + 自定义 label：
  - DashboardView `liveBadge` → "Live, updated 8 minutes ago"
  - ListingsView `heartbeatRow` → "127 listings, updated 8m"
  - `ListingRow` → "New listing, Apartment 305, €1,067, Available to Book, Eindhoven, 28 m², from 5 Jan"
  - `NotificationRow` → 整卡 event + title + body + 时间合并朗读 + tappable 行加 hint "Double tap to open listing details"
  - CalendarView 月份标题加 `.isHeader` trait
- 装饰性 glyph（chip 内 xmark、Menu chevron.down、Menu icon 等）`.accessibilityHidden(true)`。
- AdminUsersView 用户启用 Toggle 补 `.accessibilityLabel("Enable \(name)")` + `.accessibilityValue("On"/"Off")`，VO 不再读到无名开关。

**Reduced Motion**
- OnboardingView 接 `@Environment(\.accessibilityReduceMotion)`：Back / Next 按钮 + TabView page 切换的 `.spring` 在开启时降级为瞬时切换。
- 与 DashboardView / LoginView 现有 reduceMotion 处理形成完整覆盖。

**Sufficient Contrast**
- LoginView 4 个自定义 RGB 灰阶（`domainColor` / `footerTextColor` / `descriptionColor` / `subtitleColor`）接 `@Environment(\.colorSchemeContrast)`，Increase Contrast 时全部拉到 WCAG AA 4.5:1 以上（`domainColor` 从 1.5:1 提到 ~4.6:1）。
- NotificationRow 已读卡的 `.tertiary` body / 时间在 Increase Contrast 时上抬到 `.secondary`（避开 ~3.4:1 低对比）。
- NotificationRow mono caps 事件标签（`statusLottery` 橙色在白底仅 ~3.4:1）在 Increase Contrast 时切到 `.primary`，类别色信号由左侧 icon 方块 + cardTint 承担、不丢语义。
- ListingRow `detailColumn` 10pt mono caps 列标题在 Increase Contrast 时 `.tertiary` → `.secondary`。
- ListingRow 状态徽章在 Increase Contrast 时 tint 0.13 → 0.20 + 加 1pt 同色 stroke（同时强化「Differentiate Without Color Alone」——形状轮廓不再纯靠颜色差）。

**Differentiate Without Color Alone**
- 上述 status badge stroke、NotificationRow icon 块 + 文字标签 + cardTint 三冗余、Calendar 数字计数（不只蓝色）、live dot 配 "Live"/"Offline" 文字——所有色彩信号都有等价的形状 / 文字冗余。

**Dynamic Type 下限保护**
- NotificationRow mono caps 事件标签 10.5pt → 11pt（达 iOS HIG 正文最小字号），tracking 0.5 → 0.4 保持紧凑视觉密度。

### Publish 建议

- 已可在 ASC Nutrition Label 勾选：**VoiceOver / Voice Control / Reduced Motion / Sufficient Contrast / Differentiate Without Color Alone / Dark Interface**（6 条）。
- 仍不建议勾选 **Larger Text**（代码内大量 `.font(.system(size: N))` 固定字号未做 Dynamic Type scaling）；**Captions / Audio Descriptions** App 无视频音频内容，自动不适用。

### Web 端 — 侧边栏与主题切换打磨

- **修复 sidebar 顶端紫色横线泄漏**：新增的 `.skip-link`（无障碍"跳到主内容"按钮）原本用 `transform: translateY(-120%)` 隐藏到视口外，但元素实高 ≈ 36px、配上 `top: 8px` 后底边落在 y ≈ 0.8px，导致 accent 色泄漏 1–2px 在 FlatRadar 图标上方显出一条横线。改用 `top: -100px` + `:focus` 时拉回 `top: 8px` + 0.15s `top` 过渡，无障碍跳转功能完整保留。
- **修复切日夜主题时"横线不跟着动"**：`html.theme-transitioning` 规则原本只覆盖 `background-color` / `color` / `box-shadow`，所有带 border 的横线元素（`<hr>` / table 分割线 / card 外框 / `sidebar-label` 下划线 / `.breadcrumb` 等）切换时颜色瞬间跳变，跟旁边卡面慢慢渐变形成不协调。补 `border-color` / `outline-color` / `fill` / `stroke`（inline SVG 一并覆盖）。
- **修复 KPI 大数字"晚一点才变色"**：原 `color .25s` 跟 `background-color .35s` 错位 100ms，导致 `.kpi-num` / `.lc-rent` / 表格数字在 250ms 就跑完、背景还在转，中间帧看起来像数字晚到。所有过渡属性统一 `.3s ease`，JS 端 `setTimeout` 400ms 移除 class 仍留 100ms 缓冲。
- **主题按系统时间自动判断**：未显式 toggle 过的用户首次访问时，根据本地时钟判定 `19:00–06:59` 自动走 dark。优先级 `localStorage` > 系统时间 > `prefers-color-scheme`，已显式 toggle 的用户选择仍然 stick。`base.html` + `login.html` 两个内联 `<head>` 脚本同步更新。
- **静态资源缓存版本**：`design.css?v=6` → `v=9`，强制浏览器拉新样式。

---

## v1.6.0 (2026-05-20)

### 后端 — 抓取完整性与 stale 状态收敛

- **完整扫描信号**：`scraper.scrape_all()` 现在返回每个城市的完整性状态；monitor 每轮记录 `本轮完整扫描: x/y 城市`，便于区分真实无房源与抓取不完整。
- **只对完整城市执行 stale 收敛**：7 天未见房源推测为 `Occupied` 的逻辑只在对应城市本轮完整抓取成功后运行，避免代理/网络故障时误判状态。
- **Lottery 独立 stale 窗口**：`Available in lottery` 使用更短的 2 天未见阈值；`Available to book` / `Unknown` 仍使用 7 天阈值，更贴近 lottery 房源短周期行为。
- **列表展示 last seen**：Web 房源列表新增 `Last seen`，避免把 `First seen` 误当成 stale 判断依据，排查状态收敛更直接。

### Web — 注册、账号与安全

- **登录页引流与注册入口**：登录页增加 App Store 下载链接，并支持 Web 端账号注册；注册前弹出使用条款与隐私政策确认。
- **侧边栏法律入口**：登录后的侧边栏底部新增完整「隐私条款」与「使用条款」入口。
- **Admin 设备管理入口**：admin 侧边栏新增 App 设备管理入口，不再只能从 Settings 深层进入。
- **邮箱验证加固**：验证链接强制依赖 `PUBLIC_BASE_URL`，缺失时 fail-closed；避免 Host header poisoning 生成攻击者域名链接。
- **用户邮件配置收紧**：普通 user 仅可使用 shared 邮件模式并修改收件邮箱；custom SMTP 限定 admin 配置，降低 SSRF / 出站滥用风险。
- **前端 XSS 防护补强**：用户名称、测试通知结果、渠道错误等用户可控内容改为安全渲染，避免 inline handler / `innerHTML` 注入。

### 通知

- **Telegram 品牌化 HTML 消息**：Telegram 发送使用 `parse_mode=HTML`、`disable_web_page_preview=true`，统一 FlatRadar 标题、加粗字段，并转义动态内容。
- **Email HTML 模板统一**：邮箱验证、测试通知与新房源邮件使用 FlatRadar 品牌模板，不再显示旧 H2S 命名。
- **配置提示完善**：Web 用户表单补充 Telegram BotFather / `getUpdates` 配置说明；iMessage 标明仅本地 macOS 部署可用。

### 统计与可观测性

- **统计范围联动修复**：Stats 页 7 / 30 / 90 days 切换现在同时影响 KPI 卡片、趋势图和分布图；公开 chart API 也按 `days` 过滤。
- **更清晰的网络失败链路**：第 1 页网络失败会向上抛出并参与连续失败计数/cooldown，不再伪装成“成功抓取 0 条”。

### iOS / App Store

- **版本更新**：项目版本推进到 `v1.6.0`；iOS App Store build `161`，面向 App Store Connect 完成截图、隐私、年龄分级、加密与内购资料准备。
- **StoreKit 打赏**：新增 consumable “Buy me a coffee” 内购档位，作为自愿支持入口。
- **移动端交互打磨**：Browse/List/Map/Calendar 在 iPhone / iPad 横竖屏下继续优化布局、搜索入口、地图按钮位置与深色模式表现。

### 测试

- 补充完整扫描、stale 收敛、lottery stale 窗口、统计范围联动、Telegram HTML 格式、前端安全渲染等回归测试。

---

## v1.5.0 (2026-05-16)

### 后端 — 账号注册与存储一致性

- **用户配置完全迁入 SQLite `user_configs`**：`users.json` 不再作为运行时数据源；首次启动按 `users_storage_migrated_v1` meta flag 一次性导入，并永久保留 `.bak` 备份。
- **移除 SQLite `app_users` 镜像表**：App 登录字段并入 `user_configs`，避免 `users.json`/`app_users` 双源不一致。
- **`POST /api/v1/auth/register`**：用户自助注册端点，bcrypt 密码哈希，注册即登录自动签发 token；同 IP 每小时限 3 次 + 复用登录爆破防护；并发注册冲突检测，失败自动回滚。
- **`DELETE /api/v1/me`**：用户注销账号端点，撤销所有 token + 删除 SQLite 用户配置。
- **`PUT /api/v1/me/filter`**：user 自助修改过滤条件，白名单校验 + 边界值检查。
- **`GET /api/v1/filter/options`**：返回所有过滤维度候选值（cities/types/contract/energy...），bearer_optional。
- **Listings 多维过滤**：`GET /api/v1/listings` 新增 `cities`、`types`、`contract`、`energy` 参数，Python 端过滤。
- **`update_users()` SQLite 事务化**：统一 read-modify-write 入口，使用 `BEGIN IMMEDIATE` 避免并发写丢失。
- **安全增强**：TTL 上限 365→90 天；用户名长度上限 64 字符；`err_conflict`（409）处理重名；`check_register_rate` 注册专用限流。

### 后端 — 发布前健康检查

- **`python -m tools.doctor`**：发布/部署前一键检查，支持 `--no-network` / `--smtp-login`，敏感信息脱敏。

### 后端 — 测试

- 并发注册测试、SQLite 用户迁移测试、网络失败传播测试、doctor smoke test。

### iOS — 登录页 V5 设计

- **Hero 山脉动画**：蓝色渐变背景 + 双层山脉剪影（`MountainPath` Shape）+ 呼吸 Logo（scaleEffect 循环动画）。
- **展开式角色卡片**：Tenant / Guest / Staff 三张卡片，点击展开内联登录表单，.spring 动画 + rotationEffect chevron。
- **注册流程**：Tenant 卡片底部 "Register" → 注册 sheet → username + password → POST /auth/register → 自动登录。
- **自适应深色模式**：20+ 颜色属性按 `colorScheme` 切换（hero 深海军蓝 / 浅蓝；卡片/文字/边框全适配）。
- **实时统计 badge**：从 `/stats/public/summary` 获取 live count / time ago / new today。
- **法律文档**：首次启动强制 Terms 弹窗（`.interactiveDismissDisabled`）；Settings + Login 内嵌完整使用条款和隐私政策。
- **版本号动态读取**：`Bundle.main.infoDictionary["CFBundleShortVersionString"]`。

### iOS — Dashboard V1 重设计

- **问候语 + 用户胶囊**：时段感知（Good morning/afternoon/evening）+ 角色自适应（user=蓝色/Menu，admin=红色/Menu，guest=灰色/Menu）。
- **Live badge**：绿点 + "Live · 199 listings · updated 2m ago" 统合胶囊；网络异常时变橙色 "Offline"。
- **统合统计卡片**：单张卡片含 TOTAL LISTINGS 大数字 + Sparkline 折线图（`Sparkline` Shape 从 daily_new 数据绘制）+ ↑N this week + 3 个 mini stat（New 24h / New 7d / Changes）。
- **Your matches**：user 专属区域，从 `/listings` 获取 3 条预览 mini 卡片（价格+城市），点击跳转详情。
- **Explore 2×2 网格**：By status（分段条绿/橙/灰 + 具体数字）/ By price（9 根柱状图 + 范围标签）/ By type（3 行横向进度条）/ By energy（A-F 竖条从绿到红）。
- **点击展开 ChartDetailView**：4 个 mini 卡片均可点击打开完整图表详情 sheet。

### iOS — Listings 增强

- **多维筛选 sheet**：城市多选、状态单选、户型多选、合同单选、能源单选（FilterOptions API 动态加载候选项）。
- **后端多维过滤参数**：`cities`/`types`/`contract`/`energy` 查询参数，Python 端过滤。
- **NEW 徽章颜色修正**：`Color(red: 52/255, green: 199/255, blue: 89/255)` #34C759 success 绿。
- **Listing 详情页免责**："Always verify listing details on the official Holland2Stay website before making decisions."

### iOS — 通知 V2 卡片式设计

- **卡片式 inbox**：TODAY / YESTERDAY / EARLIER 三区分组，SF Mono 标题，section header 显示条数。
- **右上角 "Read all"**：绿色勾药丸按钮（`.buttonBorderShape(.capsule)`）。
- **灰底白卡**：`.systemGroupedBackground` + `.plain` list row。
- **行列紧凑型重设计**：内联 `NEW · 38m` 徽章 + monospacedDigit 价格 + ●Book/●Lottery/●Reserved 状态胶囊。

### iOS — 设计系统

- **主色 #0A84FF**（替换原 `#1683FF`）：LoginView brandBlue 统一为 `Color(red: 10/255, green: 132/255, blue: 255/255)`。
- **语义色**：#34C759 success / #FF9500 warning / #FF3B30 error。
- **Tabular-nums**：Dashboard KPI 大数字、mini stat、matchedTotal、listing price 全部加 `.monospacedDigit()`。
- **Energy 条多色方案**：深绿 A+++/A++ → 成功绿 A+ → 浅绿 A → 黄 B → 橙 C → 红 D 及以下。
- **移除装饰色**：.purple/.pink/.indigo/.teal/.mint 全部替换为主色或语义色。

### iOS — 更多功能

- **Settings 重排**：Push Filter → Appearance → Push Notifications → Account → Admin → Legal → Coffee → About。
- **Server 入口隐藏** + `buildBaseURL`/`endEditing` 死代码清理。
- **账户管理**：Log Out + Delete Account（二次确认弹窗，DELETE /me）。
- **通知过滤器编辑器**：10 维度多选表单，FilterOptions 动态加载候选项。
- **深色模式优化**：Dashboard 灰底白卡 + Calendar 灰底。
- **用户胶囊增强**：admin/guest/user 全部显示；guest 加 Menu 可登出。
- **错误提示升级**：登录失败从统一"Session Expired"改为分类显示（"Login Failed" / "Access Denied" / "Too Many Requests" / "Connection Failed"），alert 消息显示后端实际错误原因。
- **Live indicator 精简**：删掉绿点圆圈，Live 绿色文字右上角。

### iOS — App Store 准备

- **PrivacyInfo.xcprivacy**：Required Reasons API（UserDefaults CA92.1）+ 数据收集声明（User ID / Email / Device ID / Search Hints / Crash Data / Diagnostic Data）。
- **App 图标**：新增 AppIcon-Dark.png / AppIcon-Tinted.png / AppIcon.png。
- **StoreKit 2 捐赠**："Buy me a coffee ☕" IAP，3 档 consumable（Espresso €0.99 / Latte €2.99 / Flat White €5.99），`CoffeeStore` 管理产品加载和购买。
- **Release 日志安全**：41 处 `print()` 全部包 `#if DEBUG`，Release 不泄漏 token/URL。

### iOS — 代码质量

- **消除死代码**：`hasToken()`、`buildBaseURL(from:)`、`endEditing()`。
- **消除重复**：`relativeTime` 提取到 `ServerTime.relativeTime(_:)`。
- **Force unwrap 安全**：`defaultServerHost` 常量下的 URL force unwrap 无风险。

### iOS — 多语言

- **174 条本地化**：en / zh-Hans 全覆盖（登录、Dashboard、Listings、Settings、错误、法律、管理面板）。

### 文档

- **README.md / README_cn.md**：Project Status 表新增 iOS 21 行条目 + 独立 iOS App 章节（架构/功能/端点表）。
- **iOS_README.md**：完整重写（功能矩阵/文件结构/端点/安全/版本历史 v1.5.0）。
- **CHANGELOG.md**：v1.4.1–v1.4.5 合并为 v1.5.0，涵盖所有改动。

---

## v1.4.1 (2026-05-15)

### iOS — 错误展示打磨

- **APIError 分类化 UI**：`errorDescription`（短标题）/ `failureReason`（详情）/ `recoverySuggestion`（操作建议）三层结构；每类错误配独立 SF Symbol 图标（401→lock.shield / 403→hand.raised.slash / 网络→wifi.slash 等）
- **全局 401/403 自动登出**：`APIClient` 检测到 auth 错误时 post `authFailedNotification`，`AuthStore` 监听并自动 `logout()`，任何页面任何请求触发都会清除会话
- **所有视图 Try Again 按钮**：`ContentUnavailableView` 错误状态加"Try Again"操作按钮，401 显示"请重新登录"、网络错误显示"检查网络"等分类提示
- **刷新失败弹窗**：数据已有但刷新失败时弹出 alert（title 按错误类型区分），不再静默吞错
- `LoginView` alert 标题随错误类型变化（网络错误→"Connection Failed"，401→"Session Expired"），不再固定"Login Failed"
- `DashboardView` 重构为 `summaryContent` / `errorView` / `roleBadge` 三个子组件，避免 type-checker 超时

### iOS — 多语言 en + zh-Hans

- 新建 `Localizable.xcstrings`（154 条），覆盖所有视图、Store、APIError、权限状态、测试推送消息
- SwiftUI `Text("...")` 自动查询 catalog，`String(localized:)` 用于非 View 代码路径（`APIError`、`LoginMode.label`、`BrowseMode.label`、`MapView.shortStatus`、`SettingsView.pushPermissionLabel`、test push 消息）
- 跟随系统语言自动切换，无需手动选择

### iOS — 深色模式 + Settings 切换

- Settings 新增 "Appearance" section，Picker 三选一：System / Light / Dark
- `@AppStorage("color_scheme")` 持久化偏好，`FlatRadarApp` 读取并 `.preferredColorScheme()` 应用到根视图
- 修复深色下两处对比度：`ChartDetailView` 交替行 `Color.gray.opacity(0.08)` → `Color.primary.opacity(0.04)`；`CalendarView` 非选中日背景 `0.08` → `0.12`
- App 全程使用 SwiftUI 语义色，无硬编码 hex，无 `preferredColorScheme` 覆盖

### iOS — iPad 适配 + 键盘快捷键

- **响应式 TabView**：iPhone compact 保持 4 tab（Dashboard / Browse / Notifications / Settings）；iPad regular 展开 6 tab（Dashboard / Listings / Map / Calendar / Notifications / Settings），无二级嵌套
- **液态玻璃底栏**：`.toolbarBackground(.ultraThinMaterial)` 毛玻璃效果
- **键盘快捷键**：iPad ⌘1-⌘6 切 tab，iPhone ⌘1-⌘4
- **响应式网格**：Dashboard `gridColumns` compact=2、regular=3
- `MainTabView` 拆为 `compactTabView` + `wideTabView` 两个子布局，通过 `@Environment(\.horizontalSizeClass)` 自动切换
- `AppTab` 新增 `.listings` / `.map` / `.calendar` 三个 case（iPad only）
- `openListing(id:)` deep link 统一使用 `.listings`，iPhone 侧 `onChange` 自动重定向到 `.browse` + `.list` mode

### iOS — APNs 推送优化

- **一次性本地客户端**：`notifier_channels/apns.py` 从复用全局 `httpx.AsyncClient` 改为每次推送创建独立 client 并 `async with` 关闭，避免事件循环竞争导致的连接泄漏和不稳定
- **Settings 测试推送**：`PushStore` + `SettingsView` 新增 "Send Test Push" 按钮，调用 `POST /api/v1/devices/test`，结果弹窗显示成功/失败设备数及失败原因，验证 APNs 端到端链路
- **设备管理增强**：Settings 显示当前设备注册 ID、权限状态、Registration failed 错误详情；支持 Re-register Device 手动重注

### iOS — 服务端 / 构建

- 新增 `POST /api/v1/devices/test` 测试推送端点（`app/routes/api_v1/devices.py`），绕过 `push.dispatch` 的 user_id/throttle 限制，直接向当前 session 所有活跃设备推送
- `FUTURE_PLAN.md` 同步更新：错误展示/多语言/深色模式/iPad 适配/APNs 标记完成

---

## v1.4.0 (2026-05-15)

### iOS 后端 — 只读数据端点（Phase 2）

新增 9 个 `/api/v1/*` 端点，user 角色按 `listing_filter` 数据隔离，admin 全量：

- `GET /listings` / `GET /listings/<id>` — 分页列表 + 单条详情（`app/routes/api_v1/listings.py`）
- `GET /notifications` / `POST /notifications/read` / `GET /notifications/stream` — 通知分页 + 标记已读 + SSE 推送（`app/routes/api_v1/notifications.py`）
- `GET /map` / `GET /calendar` — 地图坐标 + 日历数据（`app/routes/api_v1/map.py` / `calendar.py`）
- `GET /me/summary` / `GET /me/filter` — 当前用户统计 + 过滤条件（`app/routes/api_v1/me.py`）

共享模块：`_helpers.py`（`row_to_listing` / `apply_user_filter` / `serialize_listing`）；`mstorage/_notifications.py`（`NotificationOps`，支持 `user_id` 过滤）。

SSE 鉴权支持 `?token=` query 参数（兼容浏览器 `EventSource` 不支持自定义 header）。

### iOS 后端 — APNs 子系统（Phase 3）

- **推送调度** `mcore/push.py`：`dispatch` / `dispatch_status_change` / `dispatch_aggregate` / `dispatch_error`，节流去重（同 user+listing+kind 5min / 每分钟 ≤10 条 / ≥3 聚合为 round），`APNS_ENABLED!=true` 时全 no-op
- **APNs HTTP/2 客户端** `notifier_channels/apns.py`：JWT ES256 `.p8` 签名 + httpx 异步发送，`ApnsConfig.from_env()` 惰性启用，403 `InvalidProviderToken` 自动重签
- **设备持久化** `mstorage/_devices.py`：`DeviceOps` — `register_device` / `get_active_devices_for_user`（JOIN `app_tokens` 过滤 revoked） / `disable_device`（APNs 410/400 软停） / `delete_device`
- **设备端点** `app/routes/api_v1/devices.py`：`POST /register` / `GET /` / `DELETE /<id>`，设备隔离按 `app_token_id`

### iOS 客户端 — Phase 2 适配（Phase 4）

14 个文件新增/修改，Listings / Notifications 从 "Coming Soon" 占位切换到真实数据：

**模型层** — `Listing` 新增 `priceValue`/`featureMap`/`firstSeen`/`lastSeen`；新增 `ListingsResponse`/`NotificationsResponse`/`MeSummary`/`Device*` 分页和设备模型；`NotificationItem` 新增 `markedRead()`

**网络层** — `APIClient` 新增 6 个 API 方法（listings/notifications/me/devices）；新增 `buildURL()` 修复 `appendingPathComponent` 把 `?` 编码成 `%3F` 的 bug

**Store 层** — `ListingsStore`（分页/搜索/loadMore/refresh）、`NotificationsStore`（分页/标记已读/全部已读，optimistic update）、`DashboardStore`（`fetchMeSummary`）、`PushStore`（设备注册/解绑）

**View 层**：
- `ListingsView` — searchable + 无限滚动 + 状态胶囊标签（绿=可订/橙=抽签）+ loading/empty/error
- `ListingDetailView` — **新建**，全字段 + `feature_map` 网格 + H2S 链接
- `NotificationsView` — 左滑标记已读 + 全部已读 + 类型图标（SF Symbol + 颜色）+ 无限滚动
- `NotificationRow` — 新增 type→图标/颜色映射（new_listing=绿 house / status_change=橙 arrow.swap / booking=蓝 cart）
- `DashboardView` — user 角色显示匹配/可订卡片；退出加确认框并锚定按钮
- `SettingsView` — 已注册设备列表；退出确认框锚定按钮
- `MainTabView` — Notifications tab 未读 badge

### 服务端增强

- **H2S 凭据验证**：user 登录优先 `app_password_hash`，未设置或失败时回退到 H2S GraphQL `generateCustomerToken` 验证，用户可直接用 H2S 邮箱+密码登录（`app/routes/api_v1/auth.py`）
- **bcrypt 容错**：`_dummy_bcrypt_verify` / `_bcrypt_hash` / `verify_app_password` 三处 `import bcrypt` 失败时优雅降级，不再 500（`auth.py` / `users.py`）
- **Web 表单容错**：`user_form.py` 捕获 bcrypt 未安装异常 → `ValueError`；`users.py` 路由捕获并 flash 提示

### Bug 修复（6 个）

- `URL.appendingPathComponent` 把 `?` 编码成 `%3F` → `buildURL()` 拆分 path + query
- refresh control 非空闲替换 → `isLoading` 条件加 `&& items.isEmpty`
- logout API double-wrapping decode → 返回类型改为 `RevokePayload`
- 不存在的用户名登录 500 → `_dummy_bcrypt_verify` 处理 ImportError
- Web 界面设置 App 密码崩溃 → `_bcrypt_hash` + 路由层 ValueError 捕获
- iPad/Mac 退出确认框位置错误 → `confirmationDialog` 锚定按钮

### 构建 / 部署

- **Dockerfile**：新增 `COPY notifier_channels/`（APNs 模块之前漏拷）
- **`.dockerignore`**：新增 `ios/` / `.claude/` / `tests/` / `*.p8` / `*.p12`
- **`.gitignore`**：新增 `**/xcuserdata/` / `**/.build/` / `*.p8` / `*.p12` / `DerivedData/`
- **`requirements.txt`**：已包含 APNs 依赖（`pyjwt[crypto]` / `httpx` / `h2` / `cryptography`）

### 文档

- 新增 `docs/iOS_README.md` — iOS 客户端 & API 后端架构文档（中文）
- 更新 `docs/FUTURE_PLAN.md` — Phase 2/3/4 标记完成，补全 bug 修复记录和待办清单

---

## v1.3.2 (2026-05-15)

### Booker 403 屏蔽精准处理

v1.2.2 为 scraper 引入了 `BlockedError`，但 booker 的 403 一直被 `except Exception` 当作 `unknown_error` 吞噬，导致三个连锁问题：
1. 日志看不出是 Cloudflare 拦截，用户不知道该换代理
2. `book_with_fallback` 继续尝试备选房源（每个都 403，浪费时间 + 加重风控）
3. `run_once` 给每个候选发一条 booking_failed 通知（刷屏）

v1.3.2 将 403 屏蔽提升为 booker 的一等异常，与 scraper 同级处理：

- **`booker.py`**：新增 `BookingBlockedError` 异常类 + `_check_blocked()` 检测函数（与 scraper 共享同一 Cloudflare 特征签名）。`_gql()` 和 `add_to_cart()` 两处 HTTP 调用后立即检测 403 并抛专用异常，不落入 `except Exception` 通用路径。
- **`try_book()`**：捕获 `BookingBlockedError` → `BookingResult(phase="blocked")`，与 `race_lost` / `unknown_error` 路径独立
- **`mcore/booking.py`**：`book_with_fallback()` 遇 `phase="blocked"` 立即停止重试（IP/指纹级问题，换房无意义）
- **`monitor.py`**：`run_once()` 聚合所有 blocked 候选，全轮发一条节流通知（30 min，与 scraper 共享 `_should_notify_block`）；失效 prewarm 缓存；保留 retry_queue 状态（非房源级问题，不丢弃重试队列）

### 代码质量

- **`config.py`**：`_energy_rank()` → `energy_rank()`，`_ENERGY_LABELS` → `ENERGY_LABELS`（公开 API，去掉下划线前缀）。所有调用方（`app/routes/dashboard.py`、`users.py`、`user_form.py`、`tests/`）同步更新。
- **`mstorage/_base.py`**：新增 `conn` property 替代 `_conn` 直接访问，6 处测试 + `system.py` 统一使用公开访问器；新增 `_migrated_paths` 进程级缓存，同一 db_path 只跑一次 schema migration（原每个请求 ~3ms）

### Bug 修复

- **Dockerfile / PyInstaller 遗漏模块 (v1.3.0 回归)**：v1.3.0 新增 `mcore/` 和 `mstorage/` 两个包，但 `Dockerfile` 缺少对应 `COPY` 指令导致容器内 import 失败；`h2s_monitor.spec` 缺少 `collect_submodules` 导致打包后同样的模块缺失。本次补全两处构建配置。

### 测试

- 新增 `test_booker_blocked.py`：验证 403 → `BookingBlockedError` → `phase="blocked"` 完整链路（`_check_blocked`、`try_book`、`book_with_fallback`、`run_once` 聚合通知 + prewarm 失效）
---

## v1.3.1 (2026-05-13)

### Bug 修复

- **prewarm future 阻塞下单 (P2)**：`run_once()` 中取 prewarm 时若 future 未完成，不再 `await` 阻塞，改为跳过让 `try_book()` 走正常登录。同时给每个 future 加 `add_done_callback`，完成后自动写入缓存，解决慢 prewarm 的 session 泄漏问题
- **`_stash_pending_prewarms` 改为非阻塞 (P2)**：从 `await fut` 改为只收 `.done()` 的 future，未完成的跳过不阻塞通知
- **RetryQueue 空 key 残留 (P2)**：`discard()` / `remove_gone()` 在集合清空后 `del self._queue[user_id]`，避免持久化 `{"user":[]}` 脏数据
- **`load_retry_queue` 类型校验 (P2)**：顶层 JSON 非 dict 时 warning + 重置；子值只接受 list/set，其他类型跳过
- **`Optional` 导入缺失 (P2)**：`config.py`、`models.py`、`scraper.py`、`users.py` 补充 `from typing import Optional`

### 代码质量

- **monitor.py**：合并重复的 `from config import`、删除孤儿注释分隔线、`ab_candidates` 类型标注补全
- **test 模式**：`_safe_print` 包装 `UnicodeEncodeError`，Windows/管道环境不乱码
- **`mstorage/_base.py`**：`_migrate()` 加注释说明 executescript 隐式提交约束；清理未使用的 import
- **`web.py`**：移除未使用的 `import logging`
- **`users.py`**：移除未使用的 `from pathlib import Path`

### 测试

- RetryQueue 空 key 清理 ×2、`load_retry_queue` 顶层类型异常 ×2（`[]` / `"abc"`）

---

## v1.3.0 (2026-05-13)

### `monitor.py` 重构 — 提取 `mcore/` 包

`monitor.py` 1,235 行承担了间隔计算、预登录缓存、自动预订回退、重试队列等多种职责。
本次将纯逻辑和小型服务抽到 `mcore/` 包，`monitor.py` 降至 971 行（-21%）。

- **`mcore/interval.py`**（58 行）：`get_interval()` / `apply_jitter()`，智能轮询间隔计算（纯函数，无状态）
- **`mcore/prewarm.py`**（96 行）：`PrewarmCache` 类，预登录 session 缓存管理（get / set / is_valid / create / invalidate / clear）
- **`mcore/booking.py`**（171 行）：`book_with_fallback()` + `RetryQueue` 类（load / save / add / discard / remove_gone）
- **`mcore/__init__.py`**（15 行）：统一 re-export

原有 6 个预登录辅助函数（`_safe_create_prewarmed`、`_close_prewarmed_quietly`、`_is_cached_session_valid` 等）合并为 `PrewarmCache` 类方法；
`_book_with_fallback` + 全局重试队列字典合并为 `book_with_fallback` 函数 + `RetryQueue` 类。外部行为不变。

### `storage.py` 重构 — 拆分为 `mstorage/` 包

`storage.py` 1,177 行 / 42 个方法全部集中在一个 `Storage` 类中。
本次按领域拆为 6 个 Mixin，通过多重继承组合，对外接口完全不变（`storage.Storage` 继续可用）。

- **`mstorage/_base.py`**（114 行）：`StorageBase` — 连接 / schema 迁移 / meta 读写 / reset / close
- **`mstorage/_listings.py`**（258 行）：`ListingOps` — diff / mark_notified×4 / 面板查询×9 / filter helper
- **`mstorage/_charts.py`**（219 行）：`ChartOps` — 10 个统计图表 + 2 个共享 helper
- **`mstorage/_notifications.py`**（72 行）：`NotificationOps` — web_notifications CRUD×6
- **`mstorage/_map_calendar.py`**（96 行）：`MapCalendarOps` — 地图坐标缓存 + 日历查询
- **`mstorage/_retry.py`**（35 行）：`RetryQueueOps` — 竞败重试队列持久化
- **`mstorage/__init__.py`**（33 行）：Mixin 组合声明
- **`storage.py`**：1,177 → 17 行，纯 `from mstorage import Storage` re-export

### 测试补充

- **`test_mcore_interval.py`**（12 tests）：`get_interval` 6 场景 + `apply_jitter` 6 边界
- **`test_mcore_booking.py`**（21 tests）：`area_key` / `RetryQueue` / `book_with_fallback` 全覆盖
- **`test_mcore_prewarm.py`**（17 tests）：`PrewarmCache` CRUD / is_valid / invalidate / clear / create
- **`test_mstorage_notifications.py`**（12 tests）：通知 CRUD / 分页 / 已读 / 清理
- **`test_mstorage_listings.py`**（16 tests）：面板查询 / filter helper / counts
- **`test_mstorage_map_calendar.py`**（10 tests）：日历 / 地图 / geocode 缓存 / reset_all

### 测试清理

- 移除 `test_monitor_cooldown.py` 中与 `test_mcore_interval.py` 重复的 `TestApplyJitter`（3）、`TestGetInterval`（3）
- 移除 `test_prewarm_cache.py` 中与 `test_mcore_prewarm.py` 重复的 `TestIsCachedSessionValid`（5）

---

## v1.2.10 (2026-05-13)

### 移动端 Web 体验全面升级

对全部 8 个页面进行了移动端适配，覆盖布局、触摸、安全区、iOS Safari 兼容性。

- **房源列表 (P0)**：≤768px 自动切换为卡片视图，每张卡片纵向展示名称、状态、租金、面积、户型、城市、可租日期，替代 10 列横滑表格
- **Dashboard (P0)**：最近房源表格同步改为卡片视图
- **全局触摸目标 (P0)**：`@media (pointer: coarse)` 下所有交互元素（侧边栏导航、按钮、表单、多选、toggle）最小高度 ≥44px（WCAG 推荐），`@media (hover: none)` 移除 hover 闪烁
- **日历 (P1)**：新增月视图/列表视图切换按钮，列表视图按月筛选、按日期分组展示房源；月视图 grid 改用 `minmax(0, 1fr)` 防止窄屏溢出
- **统计页 (P1)**：4 列图表网格从脆弱的 inline style 选择器改为 `.grid-4` CSS 类，响应式 4→2→1 列
- **安全区适配 (P2)**：nav-toggle、toast、登录页按钮、通知面板均使用 `env(safe-area-inset-*)` 避开刘海/底部指示条
- **iOS Safari (P2)**：地图页和日志页 `100vh` → `100dvh`，避免地址栏展开/收起导致高度跳动
- **Dashboard 刷新 (P2)**：`<meta http-equiv="refresh">` 替换为 Page Visibility API 驱动的 JS 定时刷新，标签页隐藏时暂停
- **页面标题 (P2)**：移动端 `.page-header` 加 `padding-left:48px`，不再被 hamburger 按钮遮挡
- **Toast (P2)**：移动端 `max-width:calc(100vw - 32px)`，`min-width:0`，窄屏不再溢出
- **System 页 (P2)**：配置表和环境表包裹 `overflow-x:auto`，长路径用 `.cell-break` 自动换行

### Bug 修复

- **通知面板 `calc()` 语法错误**：`calc(100vw-32px)` 缺少空格，浏览器视为无效值。修复为 `calc(100vw - 32px)`
- **CSS 级联 — 房源卡片被隐藏**：`.listing-cards{display:none}` 位于 mobile media query 之后，覆盖了 `display:flex`。移至 media query 之前
- **地图 geocode 错误面板**：inline `position:absolute` 优先级高于移动端 CSS `position:relative`，错误面板覆盖页面头部。提取为 `.geocode-errors` 类
- **日历列表视图空白**：JS `style.display = ''` 无法覆盖 CSS `.cal-list{display:none}`，改为 `'block'`
- **日历列表视图翻月不生效**：`renderListView()` 未按 `currentMonth` 过滤，始终显示全部日期。增加月份过滤
- **多选筛选器空值显示空白**：JS `update()` 将 `textEl.textContent` 清空为 `''`，覆盖了模板的"不限"/"All"占位文本。改为捕获并恢复初始 placeholder
- **多选占位文案**：`multi_select_placeholder` 从"点击选择..."改为"不限"/"All"，明确未筛选 = 全部

### 细节

- 日历列表视图支持城市筛选联动，切换筛选后保持当前视图
- 登录页语言切换按钮从 inline `style="right:62px"` 改为 `.login-lang-btn` 类，统一 safe-area 适配
- 翻译新增 `cal_month_view` / `cal_list_view` 两个 key

---

## v1.2.9 (2026-05-13)

### 移除 v1→v2 迁移逻辑

v1.2.0 起用户配置从 `.env` 迁移至 `data/users.json`，此后的 8 个版本一直携带从 `.env` 自动创建默认用户的迁移代码。该逻辑已无调用场景，本次彻底移除：

- **`users.py`**：删除 `migrate_from_env()` 函数（~95 行）
- **`monitor.py`**：移除 `migrate_from_env` 导入和调用，更新 `users.json` 不存在/为空时的提示文案
- **`.env.example`**：删除底部 13 行旧版迁移注释
- **`docs/README.md` / `docs/README_cn.md`**：移除"自动迁移"描述，改为"在 Web 面板手动添加用户"
- **`translations.py`**：更新 `users_empty_hint`，移除迁移提示
- **注释修正**：`monitor.py:1178` 从"避免迁移逻辑覆盖现有数据"改为"避免忽略或覆盖现有数据"
- **测试**：删除 `TestMigrateFromEnv` 类（2 个测试）

### 功能增强

- **跨平台进程终止**：`_terminate()` 替代裸 `os.kill()`，Windows 通过 `ctypes.windll.kernel32.TerminateProcess` 实现，POSIX 保持 SIGTERM
- **asyncio 兼容 Gunicorn worker**：`_run_async()` 检测已有 event loop（gevent/asyncio worker），在新线程中跑独立 loop，避免 `asyncio.run()` 抛错
- **`ListingFilter.is_empty()` 自动化**：用 `dataclasses.fields()` 迭代替代手动枚举所有字段，新增过滤字段无需同步修改此处
- **`get_impersonate()` 权重修复**：排除上次选择时同步移除对应权重，避免池/权重列表错位

### 性能

- **SQL 批量更新**：`mark_many_notified()` 从逐条 `UPDATE` 改为单条 `WHERE id IN (...)` 批量更新

### 细节

- **Web 日志静化**：屏蔽 Werkzeug HTTP 访问日志（`GET /static/...` 等），仅保留 WARNING+
- **翻译整理**：`map_geocode_btn` / `map_geocode_hint` / `map_loading` 从 Calendar 区移到 Map 区；删除重复 `settings_heartbeat` key
- **设置页补充提示**：weekdays-only 复选框下方增加说明文字
- **测试**：新增 `test_invalid_numeric_not_written`（非法/空值不写入 .env）；conftest 补充 `web.log` fixture

---

## v1.2.8 (2026-05-13)

### 功能增强

- **心跳改为按时间间隔**：从固定 12 轮发送一次改为按分钟配置（`HEARTBEAT_INTERVAL_MINUTES`，默认 60 min），设为 0 禁用心跳。首轮不再立即发心跳，需等待完整间隔。设置页可在智能轮询区直接修改。
- **新增下午高峰窗口**：智能轮询从单一窗口（8:30–10:00）扩展为双窗口（早 8:30–10:00 + 下午 13:30–15:00），`PEAK_START_2` / `PEAK_END_2` 可在设置页配置，Web 面板可直接修改。
- **设置/用户变更写入日志**：全局配置保存和用户创建/更新/删除时，将完整配置快照记录到 `data/web.log`，日志查看器可追溯操作历史。
- **Web 进程独立日志**：新增 `data/web.log`（Flask 应用日志），与 monitor 的 `monitor.log` 分离；日志查看器新增 Web 日志 Tab（中/英标签），`updateTabSize` 复用 `LOG_LABELS` 映射。

### Bug 修复

- **设置页空值导致启动失败 (P2)**：清空数值设置框（`HEARTBEAT_INTERVAL_MINUTES`、`PEAK_INTERVAL` 等）后保存会写入空字符串，导致 `load_config()` 中 `int("")` / `float("")` 抛错，热重载失败，重启无法启动。修复：数值键空值不覆盖旧值；`config.py` 所有 `int()`/`float()` 改用 `or "default"` 兜底。
- **设置页非法数字值导致启动失败**：非空非法值（如 `PEAK_INTERVAL=abc`）同样会写入 `.env` 导致 `int("abc")` 抛错。修复：数值键写入前校验 format，非法值跳过并记录日志。
- **地图 geocode 错误详情 DOM XSS**：`s.errors[].address/reason` 拼入 HTML 后 `innerHTML` 渲染，地址来自外部抓取数据。修复：改用 `createElement` + `textContent`。
- **geocode 旧错误未清空**：新任务启动和"所有地址已缓存"返回时未重置 `errors=[]`，导致旧失败详情残留显示。修复：两处路径均清空。
- **WARNING 级别日志未落地**：`web.py` 给 root logger 加了 INFO handler 但未 `setLevel(INFO)`，`logger.info()` 被默认 WARNING 级别过滤。修复：加 `logging.getLogger().setLevel(logging.INFO)`。

### 测试

- 486 测试全部通过。修复 `test_booker_flow.py::test_attrs` 浮点精度 flaky 测试（`pytest.approx`）。

---

## v1.2.7 (2026-05-13)

### 修复

- **Den Bosch 地理编码解析到德国**：Photon 将 "Den Bosch"（口语别称）匹配到德国同名小镇而非荷兰的 's-Hertogenbosch。修复：`get_map_listings` 地址拼接追加 `"Netherlands"` 国家限定；新增 `_CITY_FORMAL` 别称映射 `"Den Bosch" → "'s-Hertogenbosch"`。后续有其他口语别称只需在映射表中加一条。

---

## v1.2.6 (2026-05-13)

### 统计页 10 图表 + 筛选增强

**新增 6 个统计图表（4→10）**
- 户型分布、能耗标签分布（环形图，标准颜色映射）
- 面积分布（<20 / 20-30 / 30-50 / 50-80 / >80 m²）、楼层分布（Ground / 1-2 / 3-5 / 6+）
- 租客要求分布（student only / employed only 等）、合同类型分布（Indefinite / 6 months max 等）
- 租金分布区间细化：€1000 以上拆为 4 档

**能耗等级改为「最低可接受等级」**
- 从多选白名单改为单选下拉（A+++ → F）
- 选择 "B" = 匹配 B 及以上的所有等级（A+++/A++/A+/A/B）
- 严格白名单校验：`_ENERGY_LABELS` 精确匹配，非法值（"banana"/"Z"）→ WARNING + 忽略
- `_energy_rank()` 从启发式解析改为白名单索引法，消除误匹配
- 表单 POST 加 `_sanitize_energy()` 防护，防恶意提交非法等级

**用户过滤新增 2 项**
- 通知过滤 + 自动预订过滤：新增「装修类型」（Upholstered / Shell）和「能耗等级」（最低可接受）
- Dashboard / Listings 显示楼盘名
- 房源列表新增城市/租客/能耗/装修筛选（城市和租客为多选）

### Bug 修复（2 个）

- **`or 99` 陷阱**：`_energy_rank("A+++")` 返回 0，`0 or 99 == 99` 导致排序错误
- **非法能耗值触发 500**：`?energy=Z` 使 `_energy_rank` 返回 None，`min_rank <= actual_rank` TypeError

### 安全加固（1 个）

- **存储 JSON 解析加固**：`_safe_features()` 统一 try/except，坏数据 WARNING 后返回 `[]`

### 重构（1 个）

- **前端 multi-select 标签刷新**：提取 `window.refreshMultiSelect()`，copyNotifFilters 不再内联重复逻辑

### 测试（183 个新测试，14 个模块）

**从 303 → 486（+60%）**

- `test_energy_filter.py`（42）：`_energy_rank` 白名单、ListingFilter passes/fail-closed、旧 list 兼容、`/listings?energy=` API
- `test_monitor_cooldown.py`（12）：`_apply_jitter` 边界、`_get_interval` 峰/谷/周末、`_should_notify_block` 节流
- `test_control_routes.py`（11）：start/stop/reload/shutdown 权限、CSRF、PID None、kill 异常
- `test_settings_routes.py`（6）：POST 写 .env、CSRF、智能轮询参数
- `test_notif_routes.py`（17）：分页、limit clamp、mark read、SSE 权限
- `test_storage_charts.py`（9）：能耗排序、面积/楼层 bucket、坏 JSON 跳过、坐标缓存
- `test_listings_filter.py`（10）：状态/城市/搜索/feature 查询、坏 JSON
- `test_users_edge.py`（10）：文件损坏/空/迁移、save/load round-trip
- `test_notifier_channel.py`（26）：MultiNotifier fanout/retry、email 规范化、WebNotifier
- `test_booker_flow.py`（9）：非 Available 拒绝、dry_run、过期/有效 prewarmed
- `test_map_guest.py`（7）：guest GET 不启动 geocode、POST 被拒、CSRF
- `test_frontend_helpers.py`（9）：`_mask_email`、Jinja2 自动转义、模板语法、AppleScript
- `test_i18n.py`（6）：翻译 key 完整性、tr fallback、localize_options
- `test_tools_smoke.py`（4）：tools/launcher import
- `test_user_form.py`（+5）：`TestEnergySanitization`

---

## v1.2.5 (2026-05-12)

### Web 面板增强

**房源列表筛选升级**
- 城市、租客要求改为多选下拉组件（和用户过滤页一致），合同类型保留单选
- 后端：单城市走 SQL 过滤（快），多城市走 Python 内存过滤

**Dashboard / Listings 显示楼盘名**
- Dashboard 新增「楼盘」列，紧挨房源名称，同字体权重
- Listings 页房源名称后显示 `· 楼盘名`

**统计页新图表**
- 「房源上线时间分布」：24 小时柱状图，按荷兰本地时间统计 `first_seen` 小时分布，一眼看出 H2S 几点集中放房
- 「租金分布」区间细化：€1000 以上拆为 €1000-1200 / €1200-1400 / €1400-1600 / >€1600 四档（原全挤在 >€1000 一栏）

**桌面端自适应布局**
- 768–2560px 区间内容宽度跟随视口缩放
- 2560px+ 锁定 2000px 内容区防止超宽屏松散

**用户表单：一键复制通知过滤到自动预订**
- 自动预订过滤条件旁新增「从通知过滤复制」按钮
- 数值字段（租金/面积/楼层）和多选字段（户型/城市/片区/合同/租客/促销）一键同步

### 翻译
- 新增 `col_building`、`filter_contract`、`filter_tenant`、`stats_hourly_dist`、`user_form_copy_filter` 等翻译 key

---

## v1.2.4 (2026-05-12)

### Bug 修复（3 个）

**预登录 session 过期导致自动预订静默失败**
`try_book()` 传入过期 prewarmed session 时，session 来源走 else 分支（新建），但登录判断仍用 `if prewarmed is None`（False，因为 prewarmed 非空但已过期），token 未赋值直接进入 `_do_book()` 触发 `NameError`。
- 引入 `using_prewarmed` 布尔变量，session 来源和登录决策统一使用
- 过期 prewarmed → `using_prewarmed=False` → 正常创建 session + 调用 `login()` + `own_session=True`

**抓取第 1 页网络失败静默返回 0 条**
`_scrape_city_pages` 中网络异常走 `except Exception: break`，返回空列表。`scrape_all` 将其当"该城市无房源"处理，monitor 更新 `last_scrape_at` 并继续正常轮询——坏代理/断网时监控空转刷 error log 不知情。
- 新增 `ScrapeNetworkError` 异常类（区别于 429/403）
- `_scrape_city_pages`：第 1 页网络失败抛 `ScrapeNetworkError`（后续页仍 break 保留已有数据）
- `scrape_all`：全部城市均失败才上抛；个别失败记日志继续
- `run_once`：捕获后不更新 `last_scrape_at`、不发用户通知，直接 re-raise
- `main_loop`：连续 3 次后触发 5 分钟冷却，成功后自动清零

**`ensure_secret_key()` 首次运行不持久化**
条件 `if ENV_PATH.exists() or not ENV_PATH.parent.exists()`——当项目目录存在但 `.env` 缺失时（本地首次运行），两个条件都不满足，跳过写入，返回临时 key。重启后所有 session 失效。
- 去掉了前置条件，无条件尝试 `mkdir -p` + `write_env_key()`
- 写入失败才降级为进程内临时 key
- 同时写入 `os.environ` 确保进程内读取一致

### 安全加固（3 个）

**地图 API 自动 geocode 绕过访客只读限制**
`GET /api/map` 是 `api_login_required`（访客可访问），但在首次查询时自动启动后台 geocode 线程（外部 Photon 请求 + 数据库写入），访客模式只读承诺被破坏。
- 删除 `api_map()` 中的 auto-geocode 逻辑块
- 端点改为纯只读：只返回已缓存坐标，`uncached` 计数透出供前端提示
- admin 手动触发 geocode 仍通过 `POST /api/map/geocode`（`admin_api_required` + CSRF）

**日志脱敏：email 在错误日志中明文**
`booker.py` 预订失败的 WARNING/ERROR 日志含完整 email（个人身份信息）。
- 新增 `_mask_email()`，输出 `tes***@domain.com`
- 两处日志（debug + error 上下文）脱敏

**日志脱敏：代理 URL 含认证凭证**
`scraper.py` 的 DEBUG 日志完整打印 `http://user:pass@host:port`。
- 新增 `_mask_proxy_url()`，密码段替换为 `***`
- 一处 DEBUG 日志脱敏

### 重构

**统一代理读取**
`os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")` 在 scraper、booker、monitor 中共 5 处重复，且未覆盖 `ALL_PROXY`。Docker 文档建议设置 `ALL_PROXY` 但代码不支持。
- `config.py` 新增 `get_proxy_url()`，优先级 `HTTPS_PROXY > HTTP_PROXY > ALL_PROXY`
- 5 处内联读取全部替换；`scraper.py` / `booker.py` 移除闲置 `import os`
- `docker-compose.yml` 新增代理环境变量注释模板

### 新测试（68 个，3 个模块）

- **`tests/test_scraper_parse.py`**（16 个，3 个类）
  - `TestToListingNormal`（4）：完整字段、features、lottery、contract_start_date
  - `TestToListingMissingFields`（6）：price/status/url_key/available_from 缺失降级
  - `TestToListingEdgeCases`（6）：null 属性、空 selected_options、损坏数据、精度、日期截断
- **`tests/test_notifier_format.py`**（14 个，4 个类）
  - `TestFormatNew`、`TestFormatStatusChange`、`TestFormatBookingSuccess`、`TestFormatBookingFailed`
- **`tests/test_booker_helpers.py`**（22 个，3 个类）
  - `TestIsBookedByOther`（6）、`TestIsReservedByUser`（8）、`TestToH2sDate`（8）

附带修复：`_to_listing` 的 except 块对非 dict item 调用 `.get()` 的崩溃

### 文档

- 新增 `docs/dataflow_ch.mmd` / `docs/dataflow_en.mmd`：中英文 Mermaid 系统数据流图

---

## v1.2.3 (2026-05-12)

### 日志查看器界面升级

v1.2.1 引入了独立的 `errors.log`（WARNING+），但 Web 面板的日志查看页面只硬编码查看 `monitor.log`，缺少文件切换和基本浏览辅助。

v1.2.3 将日志查看器升级为完整的日志浏览界面：

- **文件切换 Tab**：Monitor Log / Errors Log 两个 tab，显示实时文件大小，一键切换 `?file=` 参数
- **行号**：左侧 48px gutter 显示行号（右对齐，灰色，不可选中），方便定位和引用
- **日志级别着色**：`[CRITICAL]` / `[ERROR]` 红色，`[WARNING]` 橙色，`[INFO]` 蓝色，`[DEBUG]` 灰色 — 一眼区分严重程度
- **关键词搜索**：顶部搜索框实时过滤日志行，匹配行数即时显示（如 "23 / 500 lines"）
- **自动滚动**：独立于暂停的 checkbox，靠近底部时自动追随新日志
- **保留功能**：3 秒轮询、暂停刷新、清空当前日志（带二次确认）均保留

### 新端点

- **`GET /api/logs/files`**：返回可用日志文件列表及各自大小（`{"files": [{"key": "monitor", "size": ..., "exists": true}, ...]}`），供前端动态渲染文件切换 tab

### 翻译

- 新增 5 个翻译 key：`logs_monitor`、`logs_errors`、`logs_search`、`logs_auto_scroll`
- 更新 `clear_logs` / `clear_logs_confirm` 语义更明确（含 "当前" 字样）

### 测试

- `test_log_routes.py` 新增 `TestApiLogsFiles` 类（3 个测试）：返回结构正确、匿名 401、guest 403

### 文件整理

根目录从 35 个文件精简至 22 个，按用途分入子目录：

- **`docs/`**：README.md、README_cn.md、CHANGELOG.md（GitHub 自动识别 `docs/README.md` 作为仓库首页）
- **`docker/`**：supervisord.conf、entrypoint.sh（仅 Dockerfile 引用的两个辅助文件）
- **`packaging/`**：h2s_monitor.spec、build_dmg.sh、build.bat、asset/（构建打包相关）
- **`tools/`**：geocode_all.py、reset_db.py（一次性工具脚本）
- **修复**：移动后同步更新所有路径引用 — `h2s_monitor.spec` 的 `_base` 指向项目根、build 脚本分离 `SCRIPT_DIR`/`ROOT_DIR`、Dockerfile 两行 COPY 路径、`.github/workflows/build.yml` Windows 拼写纠正（`packing` → `packaging`）、`.gitignore`/`.dockerignore` 去重和对齐
- **LICENSE** 保留在根目录（GitHub 许可证检测要求）

---

## v1.2.2 (2026-05-11)

### 403 / Cloudflare WAF 屏蔽处理

当 Holland2Stay API 返回 403（Cloudflare WAF 屏蔽）时，旧代码将其当普通失败处理，monitor 每 3–5 分钟刷一轮 error log，用户不知情，无法行动。

v1.2.2 将 403 提升为一等异常，与 429（可自动恢复）完全区分：

- **scraper 层**：`_post_gql` 检测 403 响应（含 Cloudflare 挑战页签名识别：`no-js ie6 oldie`、`challenge-platform` 等），立刻抛出 `BlockedError`，不进入重试循环（与 429 不同）
- **传播链**：`_scrape_city_pages` / `scrape_all` 将 `BlockedError` 透传，不被 `except Exception` 吞掉
- **monitor.run_once**：捕获 `BlockedError` → ERROR 日志（含城市数/用户数/代理状态）→ 通过用户通知渠道推送告警 → re-raise
- **monitor.main_loop**：捕获 `BlockedError` → 15 分钟冷却（vs 429 的 5 分钟），避免刷屏；恢复需换代理或重启进程
- **通知节流**：30 分钟内最多发 1 条屏蔽告警，避免持续屏蔽时重复推送
- **可操作建议**：错误消息包含三条恢复路径 — 换 HTTPS_PROXY 出口 IP / 重启 monitor 重建 session + TLS 指纹 / 暂停几小时让 Cloudflare 冷却

### 测试

- **`tests/test_scraper_403.py`**（15 个测试，4 个类）：
  - `TestPostGqlBlockedError`（5 个）：403 立即抛 BlockedError + Cloudflare 识别 + 不重试 + 429 回归保护 + 200 回归保护
  - `TestBlockedErrorPropagation`（2 个）：验证 BlockedError 不被中间层吞掉
  - `TestMonitorBlockedHandling`（4 个）：run_once re-raise + 用户通知 + 30 分钟节流 + 节流后恢复
  - `TestShouldNotifyBlock`（4 个）：节流函数单元测试（首次/二次/超时/间隔合理性）

### 文档

- README.md / README_cn.md 新增 [flatradar.app](https://flatradar.app) 在线演示链接

---

## v1.2.1 (2026-05-11)

### 测试套件（Pytest）

v1.2.1 引入 10 个 pytest 测试模块，覆盖核心逻辑和安全边界：

- **纯函数测试**（6 个）：
  - `test_models_filter.py` — ListingFilter pass/reject 边界
  - `test_crypto.py` — 加密/解密往返 + 密钥轮换
  - `test_safety.py` — safe_next_url 开放重定向防护
  - `test_storage_diff.py` — SQLite diff 检测（新增/变更/过期）
  - `test_applescript_escape.py` — AppleScript 转义覆盖所有特殊字符组合
  - `test_prewarm_cache.py` — Prewarm 缓存生命周期
- **HTTP 集成测试**（3 个）：
  - `test_auth_routes.py` — 登录/登出/访客/session 角色保护
  - `test_user_routes.py` — 用户 CRUD RBAC 鉴权
  - `test_log_routes.py` — 日志 API 文件白名单 / 清空 / 路径穿越防护
- **表单测试**（1 个）：`test_user_form.py`
- **共享 fixture**：`temp_db`（隔离 SQLite）、`client` / `admin_client` / `guest_client`（预注入 session）、`fresh_crypto`（隔离密钥状态）、`isolated_data_dir`（tmp_path 重定向）

零外部网络依赖，可通过 `python -m pytest tests/ -v` 在任何环境运行。

### 预登录缓存 v2 — Phase B 跨轮复用

v1.2.0 的预登录（Phase A）在每轮 scrape 前并行建立 session，节省了 ~450ms，但**每轮都重新登录**，多轮无候选场景下浪费 `generateCustomerToken` 调用。

Phase B 将预登录 session 缓存到进程级 `dict`，跨轮复用：

- **命中**：直接同步取用，零网络 IO
- **Token TTL 剩余 < 5 分钟**：在 executor 中后台刷新（与 scrape 并行，不额外等待）
- **email 变更 / 用户被禁用 / 热重载**：自动失效并关闭旧 session
- **booking 后保留缓存**；仅 `unknown_error` 失效（session 疑似损坏）
- **Race 防护**：refresh margin（300 s）远大于一次 booking 耗时（~10 s），保证 try_book 内部不会触发 session 过期路径

每轮无候选时也保留缓存供下轮复用，每天从 288 次登录（5 min 间隔）降至 ~4 次（4 小时 token + margin 刷新）。

### 错误日志（errors.log）

`monitor.log` 长跑下 INFO 噪音（轮询节奏、正常 diff 等）淹没真正的告警。v1.2.1 新增独立的错误日志：

- **`data/errors.log`**：仅记录 WARNING / ERROR / CRITICAL，专用于事后排查
- **详细 formatter**：`%(name)s.%(funcName)s:%(lineno)d`，一眼定位问题源
- **更大保留**：`backupCount=5`（vs monitor.log 的 3），错误稀疏但时间窗口更长
- **全局接管**：所有模块（scraper、booker、monitor、notifier）的 `logger.warning` / `logger.error` 均自动写入

### 日志上下文增强

所有关键路径的日志消息加入更多上下文信息，方便定位问题：

- **scraper**：429 退避显示累计等待时间；网络异常含 traceback；非 429 HTTP 错误含响应片段；城市抓取失败含 city_id / proxy 状态
- **booker**：`addNewBooking` / `placeOrder` 错误含 sku / contract_id / start_date / cart_id
- **monitor**：限流告警含城市数/用户数/代理状态；抓取失败含城市名列表
- **预订失败**：含 listing_id / sku / email / dry_run / prewarmed / 各阶段耗时

### 修复

- **`hmac.compare_digest` TypeError**：含非 ASCII 字符（中文/emoji）的 CSRF token 或登录用户名/密码会使 `hmac.compare_digest()` 抛出 `TypeError`，导致 POST 路由返回 500。`app/csrf.py` 和 `app/routes/sessions.py` 改用 `.encode("utf-8")` 后的 bytes 进行比较，任意 Unicode 安全比较且时序常数保留
- **Dashboard 城市列表截断**：`get_all_listings(limit=2000)` 可能漏掉只在早期记录中出现的老城市；改用 `get_distinct_cities()`（`SELECT DISTINCT city`），无 LIMIT 截断风险
- **AppleScript 注入防护**：`_build_applescript` 的 recipient 参数此前未转义，admin→admin 注入或多用户配置场景存在横向攻击面；抽取 `_escape_applescript_literal()`，recipient 和 message 统一转义
- **日志查看器路径穿越**：`/api/logs` / `/api/logs/clear` 新增文件白名单（`monitor` / `errors`），拒绝任一 `file` 参数值，防止 `file=../../etc/passwd` 类路径穿越

### 新增功能

- **日志查看器支持切换文件**：`/api/logs?file=monitor|errors` 可在 Web 面板查看不同日志

---

## v1.2.0 (2026-05-11)

### 重构：web.py 模块化拆分

web.py 长期积累至 1,200 行，涵盖路由、鉴权、表单、i18n、进程控制等所有 Web 面板逻辑，维护和理解成本高。v1.2.0 将其拆分为 18 个内聚模块，每个模块 15–240 行，职责单一。

**架构设计：**

- `web.py`（154 行）精简为 Flask app 引导层：实例化 → 安全头 → CSRF → Jinja 过滤器 → context processor → 路由注册
- `app/` 共享模块（7 个）：`auth.py`、`csrf.py`、`db.py`、`env_writer.py`、`i18n.py`、`jinja_filters.py`、`process_ctrl.py`、`safety.py`
- `app/routes/` 路由模块（10 个）：`dashboard.py`、`calendar_routes.py`、`map_routes.py`、`notifications.py`、`control.py`、`sessions.py`、`settings.py`、`stats.py`、`system.py`、`users.py`
- `app/forms/` 表单模块（1 个）：`user_form.py`

**关键设计决策：**

- **保留扁平 endpoint**：放弃 Flask Blueprint（会强制 `url_for("bp.index")` 前缀），改用 `app.add_url_rule()` 直接挂载路由，模板和前端 17 处 `url_for()` + 所有 fetch URL 零改动
- **`register(app)` 模式**：每个路由模块导出 `register(app)` 函数，`web.py` 依次调用，新增模块只需在 `__init__.py` 中 import 并在引导层加一行 register 调用
- **PyInstaller 兼容**：`h2s_monitor.spec` 使用 `collect_submodules("app")` 自动收集所有子模块为 hiddenimports，未来新增模块无需手动维护清单
- **Docker 构建**：`Dockerfile` 新增 `COPY app/ app/`，将整个 app 包复制进镜像

### 技术细节

- **TLS fingerprint 动态函数**：`get_impersonate()` 替代静态 `CURL_IMPERSONATE` 常量，在运行时根据目标域名返回 Chrome 指纹版本，便于后续扩展多目标
- **路由不按 Blueprint 组织的原因**见 `app/routes/__init__.py` 文档注释

---

## v1.1.9 (2026-05-08)

### 修复

- **DB_PATH / TIMEZONE 配置不生效**：v1.1.8 将 `DB_PATH` / `TIMEZONE` 提升为 `config.py` 模块级常量时，定义位置在 `load_dotenv()` 之前，导致 `.env` 中自定义值被忽略（始终使用默认值）；修复方式为移至 `load_dotenv()` 和 `resolve_project_path()` 之后
- **Caddyfile 无效指令**：`roll_keep_days` 不是 Caddy 合法指令，正确的日志保留时长指令为 `roll_keep_for`（带单位时间值）；改为 `roll_keep_for 720h`（等价 30 天）

---

## v1.1.8 (2026-05-08)

### 安全修复

- **DOM XSS — 日历页**：`templates/calendar.html` 中 `l.url` / `l.name` / `l.price_raw` / `l.city` 直接拼入 `innerHTML`；改为 `createElement` + `textContent`，`href` 加 `https?://` 协议白名单
- **DOM XSS — 地图页**：`templates/map.html` Leaflet popup 通过字符串拼接构造 HTML 传给 `bindPopup()`；改为 DOM 节点传入，`href` 同样加协议校验；鼠标悬停状态栏从正则反解析 HTML 改为读 `marker._listingName`
- **Docker 启动预检**：`entrypoint.sh` 新增两项安全检查，任一失败则 `exit 1` 阻止容器启动：
  - `WEB_PASSWORD` 未设置（读 `.env` 文件，非继承环境变量，防假通过）
  - `Caddyfile` 仍含占位域名 `your.domain.com`
  - 隔离/本地环境可通过 `H2S_SKIP_PREFLIGHT=1` 跳过；`docker-compose.yml` 已预置注释示例

### 修复

- **Healthcheck 语义**：`/health` 此前在 monitor 停止时返回 503，导致管理员主动停止监控也让容器变 `unhealthy`；改为始终 200，monitor 运行状态仅通过响应体 `"monitor"` 字段透出
- **自动预订快速通道**：新上线 Available to book 房源此前进 `ab_pending`，等通知全部发完才提交预订（1–3 s 延迟）；现与状态变更房源统一，立即 `run_in_executor`；同步移除已无用的预登录（prewarm）机制

### 生产环境

- **Gunicorn 替代 Flask 内置服务器**：`supervisord.conf` 改用 `gunicorn --workers=1 --threads=8 --timeout=0`；`requirements.txt` 新增 `gunicorn>=22.0.0`
  - `--workers=1`：SQLite 单进程，避免多进程写锁冲突
  - `--threads=8`：支持多路 SSE 长连接并发
  - `--timeout=0`：禁用 worker 超时，防止 SSE 连接被 30 s 默认超时强杀
- **Caddy 访问日志**：`Caddyfile` 从 `/dev/null` 改为 `/var/log/caddy/access.log`，10 MiB 自动轮转，保留 7 份 / 30 天；`docker-compose.yml` 新增 `./logs/caddy:/var/log/caddy` 卷挂载
- **依赖版本锁定**：新增 `requirements.lock`，以 `==` 精确版本覆盖全部直接 + 传递依赖；`Dockerfile` 改用 lock 文件安装，构建可重复

### 代码质量

- **单一数据源**：`DB_PATH` / `TIMEZONE` 提升为 `config.py` 模块级常量，`load_config()` 直接引用；`web.py` 删除重复读取，改为从 `config` 导入，`resolve_project_path` 不再在 `web.py` 中重复调用
- **Storage 封装**：`web.py` 两处裸 `sqlite3` 连接（`_get_filter_options` / `api_neighborhoods`）替换为 `Storage.get_feature_values(category, cities)`，绕过抽象层的问题消除
- **死代码清理**：`templates/users.html` 中 `lf.max_area` 引用（`ListingFilter` 无此字段）、`translations.py` 中 `user_form_max_area` 翻译键一并删除
- **`.env.example` 精简**：删除已迁移至 Web UI 的 40+ 行通知渠道 / 过滤 / 自动预订配置项，保留系统级配置；底部补充 v1→v2 迁移说明，消除新用户困惑

---

## v1.1.7 (2026-05-08)

### 修复

- **设置页保存报 500**：`dotenv.set_key()` 内部使用 `os.replace()`（原子 rename），在 Docker bind-mount 的 `.env` 文件上触发 `OSError [Errno 16] Device or resource busy`；改用自实现的 `_write_env_key()`（读取 → 内存修改 → 原地写回）彻底规避

### 变更

- **访客权限进一步收紧**：
  - 铃铛通知按钮与通知面板对访客隐藏（`{% if is_admin %}`）
  - `/api/notifications`、`/api/notifications/read`、`/api/events` 改为 `@admin_api_required`，访客无法轮询通知或订阅 SSE
  - 地图页「解析地址」按钮对访客隐藏，防止触发 geocode 写入
  - 前端通过 `window._isAdmin` 变量跳过通知初始化，避免产生无意义的 403 请求

---

## v1.1.6 (2026-05-08)

### New

- **访客模式（Guest Mode）** — 登录页新增"访客模式"按钮，无需密码以只读身份进入面板；可查看仪表盘、房源、日历、地图、统计；用户管理、设置、系统信息、日志查看仍需 admin 登录
- **RBAC 角色鉴权** — `session["role"]` 区分 admin / guest；新增 `admin_required` / `admin_api_required` 装饰器，17 条路由按角色保护
- `WEB_GUEST_MODE` 环境变量：默认 `true`，设为 `false` 关闭访客入口
- **Caddy 反代 + HTTPS** — 新增 `Caddyfile`，`docker-compose.yml` 集成 Caddy 服务，自动签发 Let's Encrypt 证书；h2s 容器改为内部 `expose`，仅 Caddy 暴露 80/443

### Fixed

- 访客可见监控开关 / 关闭按钮 → Dashboard 相关控件对 guest 隐藏
- 通知面板中自动预订付款 URL（idealCheckOut 直链）对访客可见 → API 层对 `booking` 类通知的 `url` 字段过滤，guest 无法获取付款链接
- `/guest` 路由可将已登录 admin 静默降级为 guest → 增加角色保护，admin session 访问 `/guest` 直接跳首页

### Changed

- `.env.example` 新增 `WEB_GUEST_MODE`、`SESSION_COOKIE_SECURE`、`SESSION_LIFETIME_HOURS` 配置项
- `NOTIFICATION_CHANNELS` 默认值由 `imessage` 改为 `telegram`（VPS 环境更通用）
- `docker-compose.yml` 重构：Caddy 前置反代，仅 80/443 对外暴露

---

## v1.1.5 (2026-05-08)

### New

- **房源列表筛选拆分** — 状态、城市（下拉）、名称（文本）、最高租金、最小面积独立筛选
- **Dashboard 城市过滤** — 仪表盘按城市过滤 KPI 和列表

### Fixed

- 自动预订跳过通知可用性检查 → 加 `notifications_enabled` + `has_channels` 三道防线
- 快速预订并非立即执行 → 状态变更候选直接提交线程池
- 地理编码 31 条失败（地址含 neighborhood 干扰 Photon）
- 自动/手动地理编码并发冲突 → 统一 `_geocode_status` 管理
- 通知 URL XSS（`renderNotifications` 改为 DOM `addEventListener`）
- 加密密钥线程不安全（`_get_cipher` double-checked locking）
- Session 默认 31 天 → 24 小时
- Dockerfile 缺少 `.env.example` 导致首次部署崩溃
- `admin` 硬编码默认用户名
- `location =` JS 导航失效、房源时间中英切换等 UI 修复

### Changed

- 标签命名规范化：`allowed_offer` → `allowed_contract` / `allowed_promo` → `allowed_offer`
- 地理编码 worker 重复代码抽取为 `_run_geocode_worker`
- `status_changes` 表加索引、城市列表提取为 `get_distinct_cities()`
- 安全头注入（`X-Frame-Options` 等）、supervisord 日志分离
- 清理 `max_area` 残留引用、移除荷兰境外遮罩功能

---

## v1.1.0 (2026-05-07)

### New

- **用户过滤条件** — 租金、面积、楼层、户型、入住类型、城市、片区、合同类型、租客要求、标签/促销，通知和自动预订独立配置
- **多选下拉组件** — 替换文本输入为下拉多选，Checkbox 方式选择，选中的标签显示在输入框内
- **中英双语标签** — 过滤选项根据界面语言自动切换显示名称
- **片区按城市动态加载** — 选择城市后片区列表自动过滤
- **短租/长租识别** — 从 GraphQL 提取 Contract / Tenant / Offer 标签，房源列表可区分
- **Photon 地理编码** — 替换 Nominatim，速度快 4 倍，地图页新增手动解析按钮

### Fixed

- 监控重启后配置丢失（子进程继承旧环境变量）
- 通知角标多标签页同步（从服务端查询真实未读数）
- 地图解析失败
- SMTP 端口 587 被校验拒绝

### Changed

- 移除"最大面积"过滤条件
- 合同类型和标签字段命名规范化（`allowed_contract` / `allowed_offer`）
- 通知发送失败重试机制
- 翻页加 `MAX_PAGES=50` 安全上限
- 地理编码加线程锁防并发

---

## v1.0.1 (2026-05-06)

- 修复打包问题
- GitHub Actions CI/CD — 推送 tag 自动构建双平台产物并挂到 Release

---

## v1.0.0 (2026-05-06)

- 首次正式发布
- 26 城市监控、多通知渠道（iMessage / Telegram / Email / WhatsApp）
- Web 管理面板（仪表盘、房源、用户、设置、地图、日历、统计）
- 自动预订（加入购物车 → 下单 → 支付链接）
- 智能轮询、限流防护、热重载
- 支持打包发布，MacOs和Windows双平台兼容
