# Future Plan / 未来规划

本文记录后端尚需推进的工作和 v2.0 发布检查。已完成的设计细节见
[ARCHITECTURE.md](ARCHITECTURE.md)，版本历史见 [CHANGELOG.md](CHANGELOG.md)。

> 核对范围：当前工作区代码、测试和工作流；其中可能包含尚未提交或部署的改动。
> “已实现”表示本地代码具备该能力，不代表服务器已部署、CI 已运行或真实平台已复测。

## v2.0：当前进度

v2.0 侧重监控正确性、可维护性和回归保障。当前仍保持单机 SQLite 与 Web / monitor
双进程架构，不以增加平台或客户端功能作为发布前提。

| 方向 | 当前状态 | 主要位置 |
|---|---|---|
| CI | push 和 PR 已配置 Ruff、pytest 工作流；远程执行结果与分支保护需发布前确认。 | [tests.yml](../.github/workflows/tests.yml)、[ruff.toml](../ruff.toml) |
| 监控管线 | 抓取计划、单平台执行、事件准备、预订提交、通知和收尾已分阶段；已提交的预订不会因其他通知阶段异常而被丢弃。继续根据实际故障完善，而不是以函数行数为目标。 | [monitor.py](../monitor.py)、[scrape_round.py](../mcore/scrape_round.py)、[post_round.py](../mcore/post_round.py) |
| 平台登记与调度 | 平台标识、显示名、抓取器入口和基础能力由 `SourceSpec` 登记并派生；各平台共用调度、代理故障与遥测路径。新增平台仍需实现抓取器及其城市、过滤等平台特有规则。 | [sources.py](../mcore/sources.py)、[scrape_round.py](../mcore/scrape_round.py) |
| 配置与热重载 | 已明确外部环境、`.env`、数据库与默认值的优先级；候选配置与通知器准备成功后才切换。浏览器、代理和预订按轮使用快照，面板可显示监控确认、覆盖及重启提示。 | [settings_store.py](../settings_store.py)、[config_status.py](../mcore/config_status.py)、[settings.html](../templates/settings.html) |
| Web 运行配置 | 地图、首页覆盖与统计、用户表单按请求解析已保存配置；设置页的应用确认仍只代表 monitor。各 Web worker 独立读取数据库，读取或校验失败沿用该 worker 上次可用的配置。 | [runtime_config.py](../app/runtime_config.py) |
| 配置解析 | `load_config()` 已使用 `target_config` 解析结构化配置，已登记的平台目标键均有对应解析器；基础布尔及整数解析集中于 `env_utils.py`。 | [config.py](../config.py)、[target_config.py](../target_config.py)、[env_utils.py](../env_utils.py) |
| 常驻浏览器与预订保护 | 浏览器抓取和 H2S 预登录、预订使用独立工作进程；超时可回收。Holland2Stay 按账号、Plaza 按账号和房源保存不确定结果保护，核实后解除。 | [browser_worker.py](../mcore/browser_worker.py)、[booking_worker.py](../mcore/booking_worker.py)、[booking_guard.py](../mcore/booking_guard.py)、[plaza_guard.py](../mcore/plaza_guard.py) |
| 运行状态 | 熔断、节流、预登录缓存等已部分收进 `RuntimeState`；仍有模块级状态，后续结合实际问题整理。 | [state.py](../mcore/state.py) |
| 通知分发 | 用户通知器通过统一工厂构造；`MultiNotifier` 按事件调用渠道，并保留渠道失败隔离与传输异常重试。 | [notifier.py](../notifier.py) |

### 已核实的正确性修复

以下事项已有实现，不再作为未完成缺陷重复列入计划：

- Resend 请求带幂等键；当前键根据收件人、标题、内容和十分钟时间桶计算。
- 房源状态事件按 `status_changes.id` 标记；未完成通知可重放，影子平台过滤在重放之后。
- geocode 放入执行线程；心跳统计、心跳发送和通知清理分别隔离异常。
- Web 与 Caddy 配置请求体大小上限；密码在哈希前检查 bcrypt 的 72 字节上限。
- SSE 支持短期票据，拒绝 URL 中的长期 token。
- 公告已有逐用户、逐渠道投递记录及失败 / 未完成项重试。
- 多渠道通知按事件调用渠道；原先的纯文本分发会绕过邮件跳过心跳的覆写。当前监控心跳只写面板，未发现用户邮件心跳调用路径；抽签聚合原本仍能生成聚合文本。
- 推送去重与限流的进程内状态会定期清理过期项；H2S 详情缓存最多保留 10,000 条，淘汰后仍可能重新请求详情，持久化字段保留依赖存储层的粘性合并。
- 监控进程每天清理撤销或过期超过 30 天的 App 会话及关联设备；撤销时间未知的历史数据从升级时开始保留，两个表在同一事务清理。
- 已核查 RENTCafe 的项目内预订、测试和诊断工具调用链：移除了无调用者的通用 `submit_step` 和其专用验证码判断；登录响应解析、平台专用条款与选房流程仍有调用，予以保留。项目外脚本的调用情况无法由仓库确认。
- 用户城市选择器按启用平台收敛；监控主链路的抓取、预订与通知异常边界已有行为测试。
- Web 侧的进程内限流统一到 `app/rate_limit.py`：登录、注册、测试通知、访客房源、
  崩溃上传与用户名枚举共用一个滑动窗口实现，空闲键定期清理。运行时配置仍由
  `settings_store` 注水进 `os.environ`；deploy / secret / tuning 保持重启生效。
  monitor 侧的代理冷却、推送去重与抓取缓存是进程或来源级状态，未纳入本次整理。

这些是对代码路径的核对，线上配置、外部服务和平台的当前行为仍需单独验证。

### 尚待处理或核查

1. **存储层整理。** 路由中散落的 SQL 和 `feedback` 建表逻辑已迁入存储层
   （`mstorage/_export.py`、`mstorage/_feedback.py` 与各领域 mixin）；设备和 App
   会话清理已实现，部署后需验证维护任务正常运行。
2. **CI 与发布验证。** 确认远程目标分支的 workflow 实际通过、分支保护按预期生效；
   持续保留预订、认证、代理、推送与通知重放的关键行为测试。`ruff.toml` 对部分测试
   文件规则有例外，应按实际配置解读结果。

### v2.0 发布前确认

- 目标分支的 Ruff 和 pytest 通过；涉及浏览器工作进程的测试确认运行环境和跳过原因。
- 复核抓取 → 入库 → 重放 / 过滤 → 预订提交 → 通知 → 预订结果处理的顺序及故障隔离，确认文档与实现一致。
- 验证配置热重载失败回退、重启生效参数提示、平台故障隔离和预订结果不确定保护。
- 保留通知和预订的真实调用链测试；发布前确认尚未完成项是否影响实际使用，并记录限制。
- 部署与服务器验证单独进行，不能从本地测试推断线上已生效。

## 平台与预订后续工作

当前注册的抓取平台为 Holland2Stay、OurDomain、OurCampus、Xior、Magis、Student
Experience 和 Plaza。实际启用的自动预订平台由 [SourceSpec](../mcore/sources.py)
声明：Holland2Stay 与 Plaza；Plaza 还需要用户单独启用自动应征。

RENTCafe 相关流程已有实现，但 Xior、OurDomain 和 OurCampus 尚未作为已验证的自动
预订平台开放。Xior 草稿保存及 OurDomain 登录后的流程仍需合适的真实账号和房源
进行端到端验证；OurCampus 的实际流程也需单独确认。OurDomain 的账号是否需要按
楼盘分别配置仍待验证。验证条件和历史研究见 [RENTCAFE.md](RENTCAFE.md)、
[XIOR.md](XIOR.md) 与 [OURDOMAIN.md](OURDOMAIN.md)。不得仅凭存在 Booker 实现
就将平台标为已验证或自动开放。

未接入平台的候选与历史评估统一记录在
[SCRAPING_RECON.md](SCRAPING_RECON.md) 的第 2 节，本文不再维护第二份平台清单。
这些候选并非 v2.0 已承诺的交付；新增前应重新核查平台规则、数据可用性、账号要求、
反爬风险和实际维护成本，每个平台的预订能力必须独立验证。原先按周估算的排期已经
失效，不再作为承诺。

跨平台统计、房源更新速度对比和扩展健康看板可以作为后续增强。每平台单独设置
stale 阈值的旧方案已放弃；当前按统一收敛规则处理，见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 客户端与其他后续工作

- iOS 和 Android 客户端已迁到各自仓库：[FlatRadar-iOS](https://github.com/751K/FlatRadar-iOS)、
  [FlatRadar-Android](https://github.com/751K/FlatRadar-Android)。客户端排期以对应仓库为准。
- Android Play Store 上架计划此前已放弃；历史方案是从 Release 分发签名 APK。
- 管理 API 已提供用户列表、启停和删除，以及监控启停、重载和重启；如需通过 API
  创建或编辑用户，应单独设计并验证，现有 Web 管理功能不等于完整 CRUD API。
- 已完成里程碑和版本号以 [CHANGELOG.md](CHANGELOG.md) 为准，避免在规划文档维护
  第二份容易过时的版本清单。
