"""Month/day selection over the inbox's already-filtered listing scope."""
from __future__ import annotations

import calendar as _calendar
from collections import defaultdict
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo

from config import TIMEZONE
from models import is_sentinel_available_from

_MONTHS = ('January','February','March','April','May','June','July','August','September','October','November','December')
_WEEKDAYS = (('一','二','三','四','五','六','日'),('Mon','Tue','Wed','Thu','Fri','Sat','Sun'))


def _iso_date(value):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def availability_date(value):
    raw = str(value or '').strip()
    if not raw or is_sentinel_available_from(raw):
        return None
    return _iso_date(raw[:10])


def calendar_context(rows, args, *, zh, today=None):
    today = today or datetime.now(ZoneInfo(TIMEZONE)).date()
    raw_month = args.get('month','')
    raw_day = args.get('date','')
    selected = _iso_date(raw_day) if raw_day else None
    if raw_day and (selected is None or selected.isoformat() != raw_day):
        raise ValueError('Invalid calendar date')
    if raw_month:
        if not re.fullmatch(r'\d{4}-\d{2}',raw_month):
            raise ValueError('Invalid calendar month')
        try:
            month = date.fromisoformat(raw_month+'-01')
        except ValueError:
            raise ValueError('Invalid calendar month') from None
    else:
        month = (selected or today).replace(day=1)
    if selected and (selected.year,selected.month) != (month.year,month.month):
        raise ValueError('Selected date is outside the displayed month')
    if args.get('undated','') not in ('','0','1'):
        raise ValueError('Invalid undated scope')
    undated = args.get('undated') == '1'
    by_day = defaultdict(list)
    unknown = []
    for row in rows:
        day = availability_date(row.get('available_from'))
        if day is None:
            unknown.append(row)
        else:
            by_day[day].append(row)
    if selected is None:
        candidates = sorted(day for day in by_day if (day.year,day.month)==(month.year,month.month))
        upcoming = [day for day in candidates if day >= today]
        selected = (upcoming or candidates or [today if (today.year,today.month)==(month.year,month.month) else month])[0]
    weeks = []
    for week in _calendar.Calendar(firstweekday=0).monthdayscalendar(month.year,month.month):
        cells = []
        for number in week:
            if not number:
                cells.append(None)
                continue
            day = date(month.year,month.month,number)
            entries = by_day.get(day,[])
            label = (f'{day.year}年{day.month}月{day.day}日' if zh else f'{day.day:02d} {_MONTHS[day.month-1]} {day.year}')
            if day == today:
                label += '，今天' if zh else ', today'
            cells.append(dict(number=number,date=day.isoformat(),count=len(entries),
                cities=list(dict.fromkeys(row.get('city') or '' for row in entries if row.get('city')))[:2],selected=not undated and day==selected,today=day==today,
                label=label+ (f'，{len(entries)} 条房源' if zh else f', {len(entries)} listings')))
        weeks.append(cells)
    month_index = month.year*12+month.month-1
    def shift(delta):
        year,number = divmod(month_index+delta,12)
        return f'{year:04d}-{number+1:02d}' if 1 <= year <= 9999 else None
    selected_label = ('入住日期未确定' if zh else 'Move-in date unknown') if undated else (
        f'{selected.month}月{selected.day}日 · 周{_WEEKDAYS[0][selected.weekday()]}' if zh else f"{('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')[selected.weekday()]}, {selected.day:02d} {_MONTHS[selected.month-1]}")
    return dict(calendar_month=f'{month.year:04d}-{month.month:02d}',
        month_label=f'{month.year}年{month.month}月' if zh else f'{_MONTHS[month.month-1]} {month.year}',
        weeks=weeks,weekdays=_WEEKDAYS[0 if zh else 1],prev_month=shift(-1),next_month=shift(1),
        today_month=f'{today.year:04d}-{today.month:02d}',today_date=today.isoformat(),
        selected_date=selected.isoformat(),selected_date_label=selected_label,undated=undated,
        unknown_count=len(unknown),month_count=sum(len(entries) for day,entries in by_day.items()
            if (day.year,day.month)==(month.year,month.month)),
        agenda_rows=unknown if undated else by_day.get(selected,[]))
