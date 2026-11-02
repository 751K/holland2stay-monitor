"""Shared Web inbox scope and presentation for list and calendar views."""
from __future__ import annotations

import math
from collections import Counter
from urllib.parse import urlsplit

from flask import abort, request, url_for

from app.db import storage
from app.jinja_filters import available_display, local_time, status_short
from app.services.listing_service import get_filter_options, query_listing_rows, serialize_listing, sort_listing_rows, status_rank
from app.services.listing_detail import detail_sections, price_note
from config import ListingFilter
from mcore.sources import SPECS

def _number(raw: str, label: str):
    if not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{label}必须是有效数字 / {label} must be a number") from None
    if not math.isfinite(value) or value < 0 or value >= 1e9:
        raise ValueError(f"{label}必须大于等于零且小于 10⁹ / Invalid {label}")
    return value


def parse_basic_filter(form):
    cities = list(dict.fromkeys(c.strip() for c in form.getlist('city') if c.strip()))
    sources = list(dict.fromkeys(s.strip() for s in form.getlist('source') if s.strip()))
    if len(cities) > 50 or any(len(city) > 120 for city in cities):
        raise ValueError('城市条件过多或过长 / Invalid cities')
    if any(source not in SPECS for source in sources):
        raise ValueError('未知平台 / Unknown platform')
    return dict(
        max_rent=_number(form.get('max_rent', ''), '租金'),
        min_area=_number(form.get('min_area', ''), '面积'),
        allowed_cities=cities, allowed_sources=sources,
    )


def filter_summary(filters: ListingFilter, zh: bool):
    parts = [', '.join(filters.allowed_cities) or ('全荷兰' if zh else 'All Netherlands')]
    if filters.max_rent is not None:
        parts.append('≤ €' + f'{filters.max_rent:,.2f}'.rstrip('0').rstrip('.'))
    if filters.min_area is not None:
        parts.append(f'≥ {filters.min_area:g} m²')
    if filters.allowed_sources:
        parts.append(', '.join(SPECS[s].display_name if s in SPECS else s for s in filters.allowed_sources))
    advanced = any(getattr(filters, key) for key in (
        'min_floor', 'allowed_occupancy', 'allowed_types', 'allowed_neighborhoods',
        'allowed_buildings', 'allowed_contract', 'allowed_tenant', 'allowed_offer',
        'allowed_finishing', 'allowed_energy',
    ))
    if advanced:
        parts.append('含更多个人条件' if zh else 'Additional personal filters')
    return ' · '.join(parts)


def matching_rows(user, filters):
    # Filter the complete candidate set before pagination, so old matches remain
    # visible and counts are independent of the current page/search/platform.
    return sort_listing_rows(query_listing_rows(user=user, limit=-1, **({
        'cities':filters.allowed_cities, 'sources':filters.allowed_sources,
        'max_rent':filters.max_rent, 'min_area':filters.min_area,
    } if user is None else {})), 'first_seen', True)


def filter_view_rows(rows, view, query, read_ids):
    if view not in ('all', 'available', 'unread', *SPECS):
        abort(400)
    def matches(row):
        if view == 'unread' and row['id'] in read_ids:
            return False
        if view == 'available' and status_rank(row.get('status')) != 0:
            return False
        if view in SPECS and row.get('source') != view:
            return False
        text = ' '.join(str(row.get(key) or '') for key in ('name','city','source','features'))
        spec = SPECS.get(row.get('source'))
        if spec is not None:
            text += ' ' + spec.display_name
        return not query or query.casefold() in text.casefold()
    return [row for row in rows if matches(row)]



def building_title(name: str, building) -> tuple[str, str]:
    """(title, address): lead with the building ("The Bakery") when the platform
    names one, because people remember buildings, not street numbers. The address
    (or unit) moves to the secondary line instead of disappearing."""
    building = str(building or '').strip()
    name = (name or '').strip()
    if not building or building.casefold() == name.casefold():
        return name, ''
    # "Amsterdam Zuidas — 7 E67" under the building "Amsterdam Zuidas" → "7 E67".
    # Only after a separator: "Jan de Oudeweg 92" under "Jan de Oudeweg" is a
    # street address and stays whole.
    rest = name[len(building):].lstrip() if name.casefold().startswith(building.casefold()) else ''
    rest = rest[1:].strip() if rest[:1] in ('—', '–', '-', '·', ':', ',') else ''
    return building, rest or name

def present_listing(row, *, read_ids, personal, zh, match_ids=None):
    item = serialize_listing(row)
    item['is_unread'] = personal and item['id'] not in read_ids
    # Browsing all listings while signed in: flag the ones the user's own filters
    # (and so notifications) would also pick up
    item['is_match'] = match_ids is not None and item['id'] in match_ids
    label = status_short(item['status'])
    # 英文里单写 "Book" 像个按钮，列表上显示成状态 "Available"
    item['display_status'] = ({'Book':'可预订','Lottery':'抽签中','Reserved':'已预留','Occupied':'已入住'}.get(label,label)
                              if zh else {'Book':'Available'}.get(label,label))
    if zh and 'not available' in item['status'].lower():
        item['display_status'] = '不可申请'
    item['display_available'] = available_display(item['available_from'], '未提供' if zh else 'Not provided')
    item['display_source'] = SPECS[item['source']].display_name if item['source'] in SPECS else item['source']
    item['display_title'], item['display_address'] = building_title(
        item['name'], item['feature_map'].get('building'))
    item['display_seen'] = local_time(item['first_seen'])
    item['day'] = local_time(item['first_seen'], '%Y-%m-%d')
    item['area'] = row.get('area_value')
    try:
        if urlsplit(item['url']).scheme not in ('http','https'):
            item['url'] = ''
    except ValueError:
        item['url'] = ''
    item['detail_sections'] = detail_sections(item,zh=zh)
    item['price_note'] = price_note(item,zh=zh)
    return item


def resolve_scope(user, args):
    """(scope, personal user or None, filters, rows) for the current request.

    A signed-in user sees their own matches by default; scope=all browses every
    listing with temporary filters, like a guest, without touching saved filters.
    """
    scope = args.get('scope', 'mine')
    if scope not in ('mine', 'all'):
        abort(400)
    if scope == 'all':
        user = None
    try:
        filters = user.listing_filter if user else ListingFilter(**parse_basic_filter(args))
    except ValueError as error:
        abort(400, description=str(error))
    return scope, user, filters, matching_rows(user, filters)


def scope_listing_ids(user, args):
    """Listing ids the stats page should cover, or None for the whole site
    (browsing all listings with no temporary filter)."""
    _, personal, filters, rows = resolve_scope(user, args)
    if personal is None and filters.is_empty():
        return None
    return [row['id'] for row in rows]


def build_inbox_scope(user, args, *, zh):
    account = user
    scope, user, filters, rows = resolve_scope(user, args)
    match_ids = {row['id'] for row in matching_rows(account, account.listing_filter)} if account and not user else None
    st = storage()
    try:
        # Read state belongs to the signed-in account, also while browsing all listings
        read_ids = st.read_listing_ids(account.id) if account else set()
        last_scrape = st.get_meta('last_scrape_at', default='')
    finally:
        st.close()
    view = args.get('view', 'all')
    if view == 'unread' and user is None:
        abort(403)
    query = args.get('q', '').strip()[:200]
    visible = filter_view_rows(rows,view,query,read_ids)
    options = get_filter_options()
    return dict(inbox_user=user,account_user=account,scope=scope,match_ids=match_ids,inbox_filter=filters,filter_summary=filter_summary(filters,zh),
        source_specs=SPECS,source_counts=Counter(row.get('source') or 'holland2stay' for row in rows),
        total=len(rows),available_count=sum(status_rank(row.get('status')) == 0 for row in rows),
        # While browsing all listings, the sidebar still shows the unread count of
        # the user's own matches (that entry links back to them), so it never jumps
        unread_count=(sum(row['id'] not in read_ids for row in rows) if user
            else sum(i not in read_ids for i in match_ids) if account else None),
        view=view,search=query,cities=sorted(set(options['cities']) | set(filters.allowed_cities)),
        last_scrape=last_scrape,visible_total=len(visible),visible_rows=visible,read_ids=read_ids)


def workspace_url(endpoint=None, **changes):
    target = endpoint or request.endpoint
    values = {key:value for key,value in request.args.to_dict(flat=False).items()
        if key in ('scope','view','q','city','source','max_rent','min_area','page','selected','month','date','undated','focus','history','positions','location')}
    for key,value in changes.items():
        if value is None:
            values.pop(key,None)
        else:
            values[key] = value
    if target == 'index':
        for key in ('month','date','undated','focus','history','positions','location'):
            values.pop(key,None)
    elif target == 'calendar':
        for key in ('page','focus','history','positions','location'): values.pop(key,None)
    elif target == 'map_view':
        for key in ('month','date','undated'): values.pop(key,None)
    return url_for(target,**values)


def workspace_return_url(form, *, selected=None):
    target = {'calendar':'calendar','map':'map_view'}.get(form.get('screen'),'index')
    values = dict(view=form.get('view','all'),q=form.get('q','')[:200],selected=selected)
    if target == 'calendar':
        values.update({key:form[key] for key in ('month','date','undated') if form.get(key)})
    else:
        values['page'] = form.get('page',1)
        if target == 'map_view':
            values.update({key:form[key] for key in ('history','positions','location') if form.get(key)})
    return url_for(target,**values)
