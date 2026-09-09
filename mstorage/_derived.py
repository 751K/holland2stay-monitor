"""从 ``features`` 派生出可排序的列。

为什么要落成列
--------------
``area`` 和 ``energy`` 埋在 ``features`` JSON 里是**文本**——``"87.28 m²"`` 和
``"B"``。按文本排是字典序：``"9 m²"`` 会排在 ``"87.28 m²"`` 后面，能耗的
``"A+++"`` 会排在 ``"A"`` 前面（``+`` 的码位比空小）。所以要先抽成数值列。

两个列的方向不一样，写在这里免得下次要猜：

    area_value    平方米，**越大越大**
    energy_rank   ENERGY_LABELS 里的下标，**越小越好**（A+++ = 0）

因此「按 energy 升序」= 最好的排在前面，这正是用户想要的默认理解。

取不到值一律是 NULL
-------------------
抓不到面积、能耗标签不在白名单里（上游冒出个没见过的写法），都写 NULL，
**不写 0 也不写哨兵**。0 是一个合法的面积/等级，用它表示「不知道」的话，排序时
那些房子会挤在最优的一端——比排在最后更糟，因为它看起来像真值。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def derived_from_features(features_json: str | None) -> tuple[float | None, int | None]:
    """``features`` 的 JSON 文本 → ``(area_value, energy_rank)``。

    参数收的是**已经序列化好的那个字符串**，而不是列表——五个写入点写进列里的都
    是它，收同一个东西才能保证「列里的值」和「features 列的内容」永远一致。
    """
    from config import energy_rank
    from models import parse_features_list, parse_float

    try:
        feats = json.loads(features_json or "[]")
    except (TypeError, ValueError):
        return None, None
    if not isinstance(feats, list):
        return None, None

    fmap = parse_features_list([f for f in feats if isinstance(f, str)])
    area = parse_float(fmap.get("area", ""))
    # 0 平米是脏数据，不是「零平米的房子」。当未知处理，否则它会排在"最小"那端。
    # （负数进不来：parse_float 按数字子串抽，"-5 m²" 给的是 5.0。写成 <= 0 只是
    # 比 == 0 稳，不代表真会有负数。）
    if area is not None and area <= 0:
        area = None
    # 注意是 ``energy_label`` 不是 ``energy``——LISTING_KEY_MAP 里
    # ``"Energy" → "energy_label"``。写成 energy 的话每一条都算不出等级，而且
    # 不会报错：列全是 NULL，排序"能用"，只是所有房子并列最后。
    return area, energy_rank(fmap.get("energy_label", ""))
