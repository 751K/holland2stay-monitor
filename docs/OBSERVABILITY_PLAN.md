# 可观测性建设方案（v2.0 方向一）

## 1. 要解决的问题

目前排查任何问题都必须登录服务器。这并非使用习惯问题，而是能力缺口——面板确实
无法回答这些问题。

在 2026-08-03 的代码审阅中，四个缺陷全部依靠人工 grep 日志定位：

| 问题 | 当时的定位方式 |
|---|---|
| 单个 source 失败拖垮整轮 | 以 `grep "监控将暂停"` 统计一整晚的日志 |
| errorCode 误判 | 以 `grep -c errorCode` 统计一整晚的出现次数 |
| 完整扫描率下滑 | 逐行阅读 `完整扫描 N/M` 日志行 |
| Xior 全天零单元 | 以 `grep "共抓取 0 个"` 检索 |

这些问题的共同点在于：**答案仅存在于日志文本中，没有任何结构化记录**。

### 1.1 现状盘点

现有的能力包括：

- `/health`：monitor 心跳新鲜度（2026 年 8 月加入，用于堵住 7 周静默停摆的缺口）
- `meta.last_scrape_at` 与 `last_scrape_count`：标量，每轮被覆盖
- `meta.uptime_alive_hours`：7 天存活小时采样
- 面板首页：总数、24 小时新增、24 小时变更、平均每轮条数、uptime 百分比
- `/logs` 页面：最多 tail 2000 行
- admin 告警：代理失效、Holland2Stay 熔断、未分类异常、管线异常（节流 30 分钟，
  状态存于内存）

### 1.2 三个缺口

**A. 缺少轮次历史。** `last_scrape_count` 是一个被反复覆盖的标量。「昨晚 Xior 为何
只有 2/6」这类问题没有对应的数据源，只能翻阅日志。每轮已经计算出的 `completeness`
字典，用后即被丢弃。

**B. 告警仅由异常触发，不由退化触发。** 某个 source 持续「成功」返回 0 条时，不会
产生任何异常，因而也不会产生任何告警。7 周静默停摆即为该类故障的极端形态：进程
并未崩溃，只是不再执行实际工作。

**C. 日志只能 tail，无法检索。** `/api/logs` 不支持关键字、级别与时间范围过滤，且
每次轮询都以 `f.readlines()` 将整个文件读入内存。要查看「凌晨三点发生了什么」只能
登录服务器。

## 2. 方案

共五个部分，依次解决上述 A、B、C 三项缺口。

### P1 轮次遥测落库

新表 `round_stats`，每轮每 source 一行：

```
round_at    ISO UTC，同一轮各 source 共用，用于分组
source      holland2stay / ourdomain / xior / ourcampus
listings    该 source 本轮抓到多少条
targets     该 source 本轮的任务数（城市/楼栋）
complete    其中完整扫描的任务数
duration_ms 该 source 本轮耗时
error_type  异常类名；成功为空串
error_msg   异常摘要（截断）
```

上述数据全部来自 `_dispatch_isolated()` 中已有的局部变量，无需新增任何抓取动作。

**写入时机**：每个 source 执行完毕后立即写入，不等待整轮结束。理由在于「整轮失败」
恰恰是最需要留痕的情形，而该路径会直接向上抛出异常。

**保留期**：默认 30 天，每小时最多剪枝一次（上次剪枝时间记录于 meta）。按 4 个
source、每天 288 轮、保留 30 天计算约 3.5 万行，对 SQLite 而言毫无压力。

**失败处理**：遥测写入过程中的任何异常一律吞掉。观测机制不应反过来影响被观测的
系统。

### P2 健康判据：从「循环活着」到「数据还对」

新服务 `app/services/health_service.py`，从 `round_stats` 聚合出每个 source 的：

- `last_round_at` / `last_success_at`
- `fail_streak` —— 连续失败轮数
- `zero_streak` —— 连续抓到 0 条的轮数
- `completeness_rate` —— 窗口内 `sum(complete) / sum(targets)`
- `avg_listings` / `max_listings`

判级规则：

| 级别 | 条件 |
|---|---|
| `down` | `fail_streak >= 3`（约 15 分钟该 source 完全不工作） |
| `warn` | `completeness_rate < 0.8`，或 `zero_streak >= 3 且窗口内 max_listings > 0` |
| `ok` | 其余 |

**`zero_streak` 需与 `max_listings > 0` 绑定的原因**：Xior 的部分楼栋常态零可订，
OurCampus 官网自述排队 16–18 个月。仅凭「抓取到 0 条」会将这两个 source 永久置于
告警状态，噪音将淹没真实信号。加入「该 source 自身在窗口内出现过非零值」这一前提
后，规则的语义即变为**「原本有房源，突然全部消失」**——这正是解析器被上游改版
破坏的特征，也是当前最危险且完全不可见的一类故障。

**不改变 `/health` 的状态码。** 数据退化应当触发告警，而不应导致容器被重启——重启
无法修复解析器与上游结构不匹配的问题，只会中断正在进行的抓取。`/health` 继续仅
负责存活判定。

### P3 退化告警

新增模块 `mcore/watchdog.py`：在每轮结束后依据 P2 的指标执行规则，产出告警。

规则如下：

1. `source_down`：某个 source 连续失败
2. `source_zero`：某个 source 原本有房源，现连续返回零
3. `completeness_low`：某个 source 的完整扫描率跌破阈值
4. `silent_round`：全局连续 N 轮零房源（针对 7 周静默停摆的直接判据）

**节流状态写入 meta，而非驻留内存。** 现有的 `_should_notify_internal()` 使用模块
级变量，进程重启即清零；而 supervisor 的 autorestart 恰会在故障期频繁重启，导致
节流在最需要生效时失效。改为按规则 key 存入 meta 后，重启后仍然有效。

**恢复同样需要通知。** 只报告故障而不报告恢复，等同于要求接收者自行登录服务器
确认状态。

### P4 日志可查

`/api/logs` 增加：

- `q=` 关键字过滤（大小写不敏感子串）
- `level=` 级别过滤
- `since=` / `until=` 时间范围
- 从文件尾部分块反向读取，不再 `readlines()` 整个文件

多行 traceback 的续行既无时间戳也无级别，按「归属于上一条带头部的记录」处理
——否则过滤会将 traceback 从中截断，而 traceback 恰恰是最需要查看的内容。

### P5 面板页

新页面 `/monitoring`（admin）：

- 每 source 一张健康卡：级别、最近一轮、连续失败/零、完整率、平均条数
- 最近 N 轮明细表，按轮次分组，每格显示 `listings (complete/targets)`
- 当前活跃告警列表

## 3. 不做的事

- **不引入 Prometheus、Grafana 或 OTel。** 本系统为单机单容器、单个 SQLite 文件，
  引入指标后端的运维成本高于其所解决的问题。在这一规模下，SQLite 表即为正确答案。
- **不改动 iOS 与 Android 客户端。** 移动端处于维护阶段，本套机制供 admin 排查
  使用，Web 面板已经足够。待 Web 侧稳定后再决定是否纳入 `/api/v1`。
- **不做日志全文索引。** 对于数十 MB 规模的日志，分块反向读取加子串过滤已足够快。

## 4. 验收标准

方案落地后，以下问题必须在**不登录服务器**的前提下即可回答：

1. 昨晚 Xior 为什么只有 2/6 完整？
2. 哪个 source 最近一天失败率最高？
3. H2S 上次抓到 0 条是什么时候？
4. 过去 6 小时有没有出现过整轮全失败？
5. 凌晨三点前后日志里有没有 `BlockedError`？

以及一项主动能力：**Holland2Stay 突然无法抓取到房源时，应在收到用户反馈之前先行
收到告警。**
