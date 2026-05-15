"""Storage — SQLite 持久化层（mixin 组合）。

从 storage.py 拆分而来，按领域组织为 6 个 mixin：
  _base         — 连接 / schema / meta / 生命周期
  _listings     — diff / mark_notified / 面板查询
  _charts       — 10 个统计图表
  _notifications— web_notifications CRUD
  _map_calendar — 地图坐标缓存 + 日历查询
  _retry        — 竞败重试队列持久化

对外接口不变：from storage import Storage（纯 re-export）。
"""

from mstorage._base import StorageBase
from mstorage._charts import ChartOps
from mstorage._devices import DeviceOps
from mstorage._listings import ListingOps
from mstorage._map_calendar import MapCalendarOps
from mstorage._notifications import NotificationOps
from mstorage._retry import RetryQueueOps
from mstorage._tokens import TokenOps


class Storage(
    ListingOps,
    NotificationOps,
    ChartOps,
    MapCalendarOps,
    RetryQueueOps,
    TokenOps,
    DeviceOps,
    StorageBase,
):
    """SQLite 持久化层，通过 mixin 继承组合所有领域方法。

    实例化参数和行为与原 storage.Storage 完全相同。
    """
