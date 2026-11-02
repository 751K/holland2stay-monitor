"""Storage — SQLite 持久化层（mixin 组合）。

从 storage.py 拆分而来，按领域组织为多个 mixin：
  _base          — 连接 / schema / meta / 生命周期
  _listings      — diff / mark_notified / 面板查询
  _listing_reads — 按用户保存的房源已读记录
  _charts        — 统计图表
  _notifications — web_notifications CRUD
  _map_calendar  — 地图坐标缓存 + 日历查询
  _retry         — 竞败重试队列持久化
  _rounds        — 抓取轮次遥测（每轮每 source 一行）
  _announcements — 公告与逐用户投递记录
  _devices       — 推送设备
  _tokens        — App 会话令牌
  _user_configs  — 用户配置
  _settings      — 应用设置
  _email_verify  — 邮箱验证 token
  _feedback      — 用户反馈写入
  _export        — GDPR 用户数据导出

对外接口不变：from storage import Storage（纯 re-export）。
"""

from mstorage._announcements import AnnouncementOps
from mstorage._base import StorageBase
from mstorage._charts import ChartOps
from mstorage._devices import DeviceOps
from mstorage._email_verify import EmailVerifyOps
from mstorage._export import ExportOps
from mstorage._feedback import FeedbackOps
from mstorage._listings import ListingOps
from mstorage._listing_reads import ListingReadOps
from mstorage._map_calendar import MapCalendarOps
from mstorage._notifications import NotificationOps
from mstorage._retry import RetryQueueOps
from mstorage._rounds import RoundStatsOps
from mstorage._settings import AppSettingOps
from mstorage._tokens import TokenOps
from mstorage._user_configs import UserConfigOps


class Storage(
    ListingOps,
    ListingReadOps,
    NotificationOps,
    AnnouncementOps,
    ChartOps,
    MapCalendarOps,
    RetryQueueOps,
    RoundStatsOps,
    AppSettingOps,
    TokenOps,
    UserConfigOps,
    DeviceOps,
    EmailVerifyOps,
    FeedbackOps,
    ExportOps,
    StorageBase,
):
    """SQLite 持久化层，通过 mixin 继承组合所有领域方法。

    实例化参数和行为与原 storage.Storage 完全相同。
    """
