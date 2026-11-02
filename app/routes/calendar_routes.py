"""
路由：入住日历

挂载的 endpoint
- GET /calendar     → calendar
- GET /api/calendar → api_calendar

模块名加 _routes 后缀，避免与标准库 ``calendar`` 模块同名冲突。
"""
from __future__ import annotations

from flask import Flask, abort, jsonify, render_template, request

from app.auth import api_login_required, browse_required
from app.services.listing_service import get_calendar_payload
from app.services.inbox_service import build_inbox_scope, present_listing, workspace_url
from app.services.calendar_service import calendar_context
from app.services.web_identity import current_listing_user
from app.i18n import get_lang


@browse_required
def calendar() -> str:
    user = current_listing_user()
    zh = get_lang() == 'zh'
    context = build_inbox_scope(user,request.args,zh=zh)
    rows = context.pop('visible_rows')
    read_ids = context.pop('read_ids')
    match_ids = context.pop('match_ids')
    try:
        dates = calendar_context(rows,request.args,zh=zh)
    except ValueError as error:
        abort(400,description=str(error))
    agenda = dates.pop('agenda_rows')
    items = [present_listing(row,read_ids=read_ids,personal=context['inbox_user'] is not None,zh=zh,match_ids=match_ids) for row in agenda]
    selected = next((item for item in items if item['id'] == request.args.get('selected')),None)
    template_context = dict(context,**dates,items=items,selected=selected,
        inbox_url=workspace_url,calendar_mode=True,page=1,pages=1,zh=zh)
    if request.accept_mimetypes.best == 'application/json':
        response = jsonify(html=render_template('_calendar_workspace.html',**template_context),
            items=items,unread_count=context['unread_count'],
            selection=dict(month=dates['calendar_month'],
                date=None if dates['undated'] else dates['selected_date'],
                undated='1' if dates['undated'] else None))
        response.headers['Cache-Control'] = 'no-store'
        return response
    return render_template('calendar.html',**template_context)


@api_login_required
def api_calendar():
    """返回所有有入住日期的房源，供日历前端渲染。"""
    return jsonify(get_calendar_payload(current_listing_user()))


def register(app: Flask) -> None:
    app.add_url_rule("/calendar",     endpoint="calendar",     view_func=calendar,     methods=["GET"])
    app.add_url_rule("/api/calendar", endpoint="api_calendar", view_func=api_calendar, methods=["GET"])
