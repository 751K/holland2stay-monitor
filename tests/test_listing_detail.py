"""Full public feature presentation without losing unknown or repeated values."""
from app.services.listing_detail import detail_sections, price_note


def _item(features):
    return dict(features=features,area=30,display_available='2026-12-01',
        display_source='Plaza',display_seen='2026-11-01 12:00',last_seen='2026-11-02T09:00:00+00:00')


def test_complete_features_have_readable_groups_and_labels():
    sections = detail_sections(_item([
        'Type: Studio','Area: 30 m²','Floor: 0','Finishing: Furnished','Energy: A',
        'Building: Residence','Address: Street 1','Postcode: 1234 AB',
        'Base rent: €900','Service costs amount: €100','Deposit: €0',
        'Contract: Indefinite','Tenant: student only','Closing date: 2026-12-01',
        'Allocation: First come, first served','MinIncome: €2500','Target group: students',
    ]),zh=True)
    fields = {field['label']:field['value'] for section in sections for field in section['fields']}
    assert [section['title'] for section in sections] == ['房源概况','费用','租赁条件','来源与更新']
    assert fields['楼层'] == '0'
    assert fields['装修与家具'] == 'Furnished'
    assert fields['能效等级'] == 'A'
    assert fields['押金'] == '€0'
    assert fields['最低收入要求'] == '€2500'
    assert fields['最后出现']
    assert sum(field['label']=='面积' for section in sections for field in section['fields']) == 1


def test_unknown_free_text_and_distinct_repeated_values_are_preserved():
    sections = detail_sections(_item(['New attribute: Custom value','Balcony included',
        'Detail: First note','Detail: Second note','Detail: First note']),zh=True)
    values = [field['value'] for section in sections for field in section['fields']]
    assert 'Custom value' in values and 'Balcony included' in values
    assert values.count('First note') == 1 and 'Second note' in values


def test_long_multiline_details_are_not_truncated():
    text = 'Long source description. '*10+'\nSecond paragraph.'
    sections = detail_sections(_item(['Detail: '+text]),zh=False)
    field = next(f for s in sections for f in s['fields'] if f['label']=='Additional details')
    assert field['value'] == text
    assert field['long'] is True


def test_empty_optional_sections_are_omitted_and_area_fallback_is_explicit():
    item = _item([])
    item['area'] = None
    sections = detail_sections(item,zh=True)
    assert '费用' not in [section['title'] for section in sections]
    assert sections[0]['fields'][0]['value'] == '未提供'


def test_service_cost_note_does_not_invent_a_total_price():
    item = {'feature_map':{'service_costs':'€192–380 excl.'}}
    assert price_note(item,zh=True) == '基础租金；服务费另计：€192–380 excl.'
    assert price_note({'feature_map':{'deposit':'€500'}},zh=True) == ''


def test_internal_plan_references_do_not_imply_a_confirmed_layout():
    sections = detail_sections(_item(['Floorplans: 111,222','Floorplan: Studio A']),zh=True)
    values = [field['value'] for section in sections for field in section['fields']]
    assert 'Studio A' in values
    assert '111,222' not in values
