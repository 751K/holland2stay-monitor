from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Listing:
    """单个房源的完整快照。"""

    id: str                        # URL slug，全局唯一，e.g. "pastoor-petersstraat-170-6"
    name: str                      # "Pastoor Petersstraat 170-6, Eindhoven"
    status: str                    # "Lottery" | "Book directly" | "Occupied"
    price_raw: Optional[str]       # "€780.00 per month excl.*"
    available_from: Optional[str]  # "Mar 13, 2026"
    features: list[str]            # ["Studio", "26.0 m²", "Single", ...]
    url: str                       # 完整页面 URL
    city: str = ""                 # 来源城市名，方便多城市场景区分

    # ------------------------------------------------------------------ #

    @property
    def price_value(self) -> Optional[float]:
        """从 price_raw 中解析出数字部分，方便排序/过滤。"""
        if not self.price_raw:
            return None
        m = re.search(r"[\d]+[,\d]*\.?\d*", self.price_raw.replace(",", ""))
        return float(m.group()) if m else None

    @property
    def price_display(self) -> str:
        if not self.price_raw:
            return "价格未知"
        # 只保留 "€780.00" 部分
        m = re.search(r"€[\d,\.]+", self.price_raw)
        return m.group() if m else self.price_raw

    @property
    def is_available(self) -> bool:
        return self.status.lower() in ("lottery", "book directly")

    def feature_map(self) -> dict[str, str]:
        """
        将 features 列表解析为 {key: value} 字典。
        features 格式为 "Key: Value"，例如 "Type: Studio"、"Area: 20 m²"。
        """
        result = {}
        key_map = {
            "Type": "type",
            "Area": "area",
            "Occupancy": "occupancy",
            "Floor": "floor",
            "Finishing": "furnishing",
            "Energy": "energy_label",
            "Neighborhood": "neighborhood",
            "Building": "building",
        }
        for feat in self.features:
            if ": " in feat:
                raw_key, value = feat.split(": ", 1)
                mapped = key_map.get(raw_key, raw_key.lower())
                result[mapped] = value
        return result

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "price_raw": self.price_raw,
            "available_from": self.available_from,
            "features": self.features,
            "url": self.url,
            "city": self.city,
        }
