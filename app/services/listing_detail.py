"""Readable presentation of public listing features for the Web inbox.

The same sections feed server rendering and client-side selection. Values remain
source data; unknown attributes and free text stay visible rather than vanishing.
"""
from __future__ import annotations

from app.jinja_filters import local_time
from models import LISTING_KEY_MAP

# key: section, Chinese label, English label. These are display labels, not a
# second model/filter schema. The model still owns feature-key normalization.
_FIELDS = {
    'type': ('home','房型','Type'),
    'dwelling type': ('home','住宅类型','Dwelling type'),
    'postcode': ('home','邮编','Postcode'),
    'area': ('home','面积','Area'),
    'floor': ('home','楼层','Floor'),
    'occupancy': ('home','入住人数','Occupancy'),
    'furnishing': ('home','装修与家具','Furnishing'),
    'energy_label': ('home','能效等级','Energy label'),
    'construction year': ('home','建成年份','Construction year'),
    'bedrooms': ('home','卧室数量','Bedrooms'),
    'rooms': ('home','房间数量','Rooms'),
    'bathrooms': ('home','卫浴数量','Bathrooms'),
    'building': ('home','楼盘','Building'),
    'unit': ('home','房号','Unit'),
    'address': ('home','地址','Address'),
    'neighborhood': ('home','片区','Neighborhood'),
    'floorplan': ('home','户型名称','Floor plan'),
    'finishing note': ('home','装修说明','Furnishing notes'),
    'base rent': ('costs','基础租金','Base rent'),
    'net rent': ('costs','净租金','Net rent'),
    'service_costs': ('costs','服务费说明','Service costs'),
    'service costs amount': ('costs','服务费','Service costs amount'),
    'deposit': ('costs','押金','Deposit'),
    'contract': ('lease','合同类型','Contract'),
    'offer': ('lease','出租方式','Offer'),
    'tenant': ('lease','租客要求','Tenant eligibility'),
    'length of stay': ('lease','租期','Length of stay'),
    'minincome': ('lease','最低收入要求','Minimum income'),
    'target group': ('lease','适用人群','Target group'),
    'allocation': ('lease','分配方式','Allocation'),
    'registration': ('lease','注册要求','Registration'),
    'access': ('lease','申请资格','Access requirements'),
    'closing date': ('lease','申请截止日期','Application deadline'),
    'detail': ('other','补充说明','Additional details'),
}
_SECTIONS = (
    ('home','房源概况','Property'),
    ('costs','费用','Costs'),
    ('lease','租赁条件','Lease & eligibility'),
    ('other','其他信息','Additional information'),
    ('record','来源与更新','Source & record'),
)


def detail_sections(item: dict, *, zh: bool) -> list[dict]:
    groups = {key:[] for key,_,_ in _SECTIONS}
    def add(group,label,value):
        if value not in (None,''):
            groups[group].append({'label':label,'value':str(value),'long':len(str(value))>70 or '\n' in str(value)})
    seen = set()
    area_present = False
    for feature in item.get('features',[]):
        if not isinstance(feature,str) or not feature.strip():
            continue
        if ': ' in feature:
            raw_key,value = feature.split(': ',1)
            key = LISTING_KEY_MAP.get(raw_key,raw_key.lower())
            value = value.strip()
            # RentCafe associates a unit with multiple internal floor-plan IDs;
            # those references do not describe this unit's actual layout.
            if key == 'floorplans':
                continue
            if not value or (key,value) in seen:
                continue
            seen.add((key,value))
            area_present |= key == 'area'
            group,cn,en = _FIELDS.get(key,('other',raw_key,raw_key))
            add(group,cn if zh else en,value)
        elif ('',feature) not in seen:
            seen.add(('',feature))
            add('other','补充说明' if zh else 'Additional details',feature)
    if not area_present:
        area = item.get('area')
        add('home','面积' if zh else 'Area',f'{area:g} m²' if isinstance(area,(int,float)) else ('未提供' if zh else 'Not provided'))
    # Keep known fields in a stable order even when platforms change feed order.
    rank = {value[1 if zh else 2]:index for index,value in enumerate(_FIELDS.values())}
    for group in ('home','costs','lease'):
        groups[group].sort(key=lambda field:rank.get(field['label'],len(rank)))
    add('lease','可入住日期' if zh else 'Available from',item.get('display_available'))
    add('record','来源平台' if zh else 'Platform',item.get('display_source'))
    add('record','首次发现' if zh else 'First seen',item.get('display_seen'))
    if item.get('last_seen'):
        add('record','最后出现' if zh else 'Last seen',local_time(item['last_seen']))
    return [{'title':cn if zh else en,'fields':groups[key]} for key,cn,en in _SECTIONS if groups[key]]


def price_note(item: dict, *, zh: bool) -> str:
    service_costs = (item.get('feature_map') or {}).get('service_costs')
    if not service_costs:
        return ''
    return f'基础租金；服务费另计：{service_costs}' if zh else f'Base rent; service costs extra: {service_costs}'
