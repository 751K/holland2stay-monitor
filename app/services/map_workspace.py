"""Map presentation over the inbox scope, using only existing coordinate cache."""
from __future__ import annotations

import math
from collections import defaultdict

from app.db import storage
from app.runtime_config import web_runtime_environment
from app.services.inbox_service import build_inbox_scope,present_listing
from mstorage._map_calendar import _map_max_age_days, row_to_map_entry

PAGE_SIZE = 100


def _coordinates(value):
    if not value:
        return None
    try:
        lat,lng=map(float,value)
    except (ValueError,TypeError):
        return None
    if not math.isfinite(lat) or not math.isfinite(lng) or not (-90<=lat<=90 and -180<=lng<=180):
        return None
    return lat,lng


def cached_positions(st, listing_ids):
    """Derive addresses from raw records and read their cache in bounded batches.

    Display-normalized names must not change the existing address cache keys.
    The number of SQL queries should grow by batches rather than by listing.
    """
    entries = {}
    ids = list(listing_ids)
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        placeholders = ','.join('?' for _ in batch)
        for row in st.conn.execute(f'SELECT * FROM listings WHERE id IN ({placeholders})', batch):
            entry = row_to_map_entry(row)
            entries[entry['id']] = entry
    addresses = list({entry['address'] for entry in entries.values()})
    cached = {}
    for start in range(0, len(addresses), 500):
        batch = addresses[start:start + 500]
        placeholders = ','.join('?' for _ in batch)
        for row in st.conn.execute(f'SELECT address,lat,lng FROM geocode_cache WHERE address IN ({placeholders})', batch):
            cached[row['address']] = _coordinates((row['lat'], row['lng']))
    return {key: cached.get(entry['address']) for key, entry in entries.items()}


def map_context(user,args,*,zh):
    context=build_inbox_scope(user,args,zh=zh)
    rows=context.pop('visible_rows');read_ids=context.pop('read_ids');match_ids=context.pop('match_ids')
    if args.get('history','') not in ('','0','1'):
        raise ValueError('Invalid history scope')
    positions=args.get('positions','located')
    if positions not in ('located','unlocated'):
        raise ValueError('Invalid position scope')
    history=args.get('history')=='1'
    focus=args.get('focus','')
    selected_id=args.get('selected') or focus
    days=_map_max_age_days(environ=web_runtime_environment())
    by_id={row['id']:row for row in rows}
    notice=''
    st=storage()
    try:
        recent={row[0] for row in st.conn.execute(
            "SELECT id FROM listings WHERE julianday(last_seen)>=julianday('now')-?",(days,))} if days else set(by_id)
        if focus and focus in by_id and focus not in recent and not history:
            history=True
            notice='已包含历史记录，以定位这条房源。' if zh else 'Historical records are included to locate this listing.'
        elif focus and focus not in by_id:
            notice='该房源不在当前筛选范围内，或记录已不存在。' if zh else 'This listing is outside your current scope or no longer exists.'
        eligible=[row for row in rows if history or row['id'] in recent]
        older_count=sum(row['id'] not in recent for row in rows)
        located=[];unlocated=[];groups=defaultdict(list)
        coordinates=cached_positions(st,(row['id'] for row in eligible))
        for row in eligible:
            coords=coordinates.get(row['id'])
            if coords is None:
                unlocated.append(row);continue
            located.append(row)
            groups[(round(coords[0],6),round(coords[1],6))].append(row)
    finally:
        st.close()
    markers=[];group_by_id={}
    for (lat,lng),members in groups.items():
        members.sort(key=lambda row:str(row['id']))
        ids=[row['id'] for row in members]
        marker=dict(id=ids[0],ids=ids,lat=lat,lng=lng,count=len(ids),
            label=(members[0].get('price_raw') or ('房源' if zh else 'Listing')) if len(ids)==1 else (f'{len(ids)} 条房源' if zh else f'{len(ids)} listings'))
        markers.append(marker)
        for member in members:group_by_id[member['id']]=marker
    if focus and focus in {row['id'] for row in unlocated}:
        positions='unlocated'
        notice='这条房源的地址尚未完成定位，暂不能在地图上标出。' if zh else 'This address has not been located yet. Its listing details remain available.'
    location=args.get('location','')
    if selected_id in group_by_id and not location:
        location=selected_id
    elif selected_id in {row['id'] for row in unlocated}:
        positions='unlocated'
    selected_group=group_by_id.get(location) if positions=='located' else None
    if location and positions=='located' and not selected_group:
        notice='所选位置没有符合当前条件的房源。' if zh else 'No listings at this location match your current scope.'
    side_rows=unlocated if positions=='unlocated' else located
    if selected_group:
        ids=set(selected_group['ids']);side_rows=[row for row in side_rows if row['id'] in ids]
    try:page=max(1,int(args.get('page',1)))
    except ValueError:page=1
    if selected_id and any(row['id']==selected_id for row in side_rows):
        page=next(index for index,row in enumerate(side_rows) if row['id']==selected_id)//PAGE_SIZE+1
    pages=max(1,math.ceil(len(side_rows)/PAGE_SIZE));page=min(page,pages)
    items=[present_listing(row,read_ids=read_ids,personal=context['inbox_user'] is not None,zh=zh,match_ids=match_ids) for row in side_rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE]]
    selected=next((item for item in items if item['id']==selected_id),None)
    context.update(items=items,selected=selected,page=page,pages=pages,map_mode=True,calendar_mode=False,
        map_markers=markers,map_history=history,map_age_days=days,map_positions=positions,
        map_location=selected_group['id'] if selected_group else '',map_group=selected_group,
        map_located_count=len(located),map_unlocated_count=len(unlocated),map_older_count=older_count,
        map_side_count=len(side_rows),map_notice=notice)
    return context
