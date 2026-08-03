# 可观测性建设方案（v2.0 方向一）

## 1. 要解决的问题

现在排查任何问题都得 ssh 上服务器。这不是习惯问题，是能力缺口——面板确实
回答不了那些问题。

2026-08-03 的代码审阅里，四个 bug 全部靠人肉 grep 日志定位：

| 问题 | 当时的定位方式 |
|---|---|
| 单 source 失败拖垮整轮 | `grep "监控将暂停"` 数了 36 轮日志 |
| errorCode 误判 | `grep -c errorCode` 统计 144 次出现 |
| 完整扫描率下滑 | 逐行读 `完整扫描 N/M` 日志行 |
| Xior 全天零单元 | `grep "共抓取 0 个"` |

这些问题的共同点：**答案只存在于日志文本里，没有任何结构化记录**。

### 1.1 现状盘点

已经有的：

- `/health` —— monitor 心跳新鲜度（2026-08 加的，堵住了 7 周静默停摆那个洞）
- `meta.last_scrape_at` / `last_scrape_count` —— 标量，每轮覆盖
- `meta.uptime_alive_hours` —— 7 天存活小时采样
- 面板首页 —— 总数 / 24h 新增 / 24h 变更 / 平均每轮 / uptime%
- `/logs` 页面 —— tail 最多 2000 行
- admin 告警 —— 代理失效、H2S 熔断、未分类异常、管线异常（30 min 内存节流）

### 1.2 三个缺口

**A. 没有轮次历史。** `last_scrape_count` 是个被反复覆盖的标量。
「昨晚 Xior 为什么只有 2/6」这类问题没有数据源，只能翻日志。
每轮已经算出来的 `completeness` 字典，用完即弃。

**B. 告警只对异常触发，不对退化触发。**
某个 source 一直成功返回 0 条——没有任何异常，因此没有任何告警。
7 周静默停摆是这一类的极端形态：进程没崩，只是不干活了。

**C. 日志只能 tail，不能查。**
`/api/logs` 没有关键字、级别、时间范围过滤，而且每次轮询都
`f.readlines()` 把整个文件读进内存。想看「凌晨三点发生了什么」只能 ssh。

## 2. 方案

五个部分，A → B → C 依次解锁。

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

数据全部来自 `_dispatch_isolated()` 已有的局部变量，不需要新增任何抓取动作。

**写入时机**：每个 source 跑完立即写，不等整轮结束。
理由是「整轮失败」恰恰是最需要留痕的情形，而那条路径会直接上抛。

**保留期**：默认 30 天，每小时最多剪枝一次（meta 记上次剪枝时间）。
4 source × 288 轮/天 × 30 天 ≈ 3.5 万行，SQLite 无压力。

**失败处理**：遥测写入的任何异常都吞掉。观测不能反过来把被观测的东西弄崩。

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

**为什么 `zero_streak` 要跟 `max_listings > 0` 绑定**：
Xior 目前四栋楼常态零可订，OurCampus 官网自述排队 16–18 个月。
单看「抓到 0 条」会把这两个 source 永久钉在告警状态，噪音会淹掉真信号。
加上「该 source 自己在窗口内出现过非零」这个前提，规则就变成了
**「本来有房，突然全没了」**——这正是解析器被上游改版打坏的特征，
也是最危险、现在完全看不见的一类故障。

**不改 `/health` 的状态码。** 数据退化应该告警，不应该让容器被重启——
重启治不了解析器对不上，只会打断正在进行的抓取。`/health` 继续只管存活。

### P3 退化告警

新模块 `mcore/watchdog.py`：每轮结束后拿 P2 的指标跑规则，产出告警。

规则：

1. `source_down` —— 某 source 连续失败
2. `source_zero` —— 某 source 本来有房、现在连续零
3. `completeness_low` —— 某 source 完整扫描率跌破阈值
4. `silent_round` —— 全局连续 N 轮零房源（7 周静默的直接判据）

**节流写进 meta，不放内存。** 现有的 `_should_notify_internal()` 是模块级
变量，进程一重启就清零。而 supervisor 的 autorestart 恰好会在故障时频繁
重启——最该节流的时候节流失效。按规则 key 存 meta，重启后仍然生效。

**恢复也要通知。** 只报警不报恢复，等于逼人继续 ssh 去确认好了没有。

### P4 日志可查

`/api/logs` 增加：

- `q=` 关键字过滤（大小写不敏感子串）
- `level=` 级别过滤
- `since=` / `until=` 时间范围
- 从文件尾部分块反向读取，不再 `readlines()` 整个文件

多行 traceback 的续行没有时间戳也没有级别，按「归属于上一条有头部的记录」
处理——否则过滤会把 traceback 拦腰截断，而 traceback 正是最需要看的部分。

### P5 面板页

新页面 `/monitoring`（admin）：

- 每 source 一张健康卡：级别、最近一轮、连续失败/零、完整率、平均条数
- 最近 N 轮明细表，按轮次分组，每格显示 `listings (complete/targets)`
- 当前活跃告警列表

## 3. 不做的事

- **不引入 Prometheus / Grafana / OTel。** 单机单容器、一个 SQLite 文件，
  引入指标后端的运维成本高于它解决的问题。SQLite 表就是这个规模的正确答案。
- **不改 iOS / Android 客户端。** 移动端在维护模式，这套东西是给 admin 排查
  用的，Web 面板足够。等 Web 侧稳定后再决定要不要进 `/api/v1`。
- **不做日志全文索引。** 分块反向读 + 子串过滤对几十 MB 的日志足够快。

## 4. 验收标准

方案做完后，下面这些问题必须**不 ssh** 就能回答：

1. 昨晚 Xior 为什么只有 2/6 完整？
2. 哪个 source 最近一天失败率最高？
3. H2S 上次抓到 0 条是什么时候？
4. 过去 6 小时有没有出现过整轮全失败？
5. 凌晨三点前后日志里有没有 `BlockedError`？

以及一条主动能力：**H2S 突然抓不到房源时，我在收到用户反馈之前先收到告警。**
