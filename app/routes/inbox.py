"""Production Web inbox: shared scope, persistence and explicit read actions."""
from __future__ import annotations

import math
from dataclasses import replace

from flask import Flask, abort, flash, jsonify, redirect, render_template, request

from app.auth import browse_required, is_user, login_required
from app.csrf import csrf_required
from app.db import storage
from app.i18n import get_lang
from app.services.inbox_service import (
    build_inbox_scope, filter_view_rows, matching_rows, parse_basic_filter,
    present_listing, workspace_return_url, workspace_url,
)
from app.services.web_identity import current_listing_user

PAGE_SIZE = 100


@browse_required
def inbox():
    zh = get_lang() == 'zh'
    user = current_listing_user()
    context = build_inbox_scope(user,request.args,zh=zh)
    visible = context.pop('visible_rows')
    read_ids = context.pop('read_ids')
    match_ids = context.pop('match_ids')
    personal = context['inbox_user'] is not None
    try:
        page = max(1,int(request.args.get('page',1)))
    except ValueError:
        page = 1
    pages = max(1,math.ceil(len(visible)/PAGE_SIZE))
    page = min(page,pages)
    items = [present_listing(row,read_ids=read_ids,personal=personal,zh=zh,match_ids=match_ids)
        for row in visible[(page-1)*PAGE_SIZE:page*PAGE_SIZE]]
    selected = next((item for item in items if item['id'] == request.args.get('selected')),
        items[0] if items and not personal else None)
    return render_template('inbox.html',**context,items=items,selected=selected,
        page=page,pages=pages,inbox_url=workspace_url,calendar_mode=False)


@login_required
@csrf_required
def inbox_filter_update():
    if not is_user():
        abort(403)
    user = current_listing_user()
    try:
        basic = parse_basic_filter(request.form)
    except ValueError as error:
        flash(str(error), 'danger')
        return redirect(workspace_return_url(request.form))
    from users import get_user, update_users

    def update(all_users):
        target = get_user(all_users,user.id)
        if target is None or not target.enabled or not target.app_login_enabled:
            abort(401)
        # Replace only fields in this editor. Preserve advanced filter dimensions,
        # notification credentials, booking settings and concurrent account edits.
        target.listing_filter = replace(target.listing_filter, **basic)
    update_users(update)   # 热重载请求由 update_users 统一发
    flash('个人筛选已保存；通知将在监控下一轮使用新条件。' if get_lang() == 'zh' else 'Filters saved. Notifications will use them on the next monitoring round.', 'success')
    return redirect(workspace_return_url(request.form))


@login_required
@csrf_required
def inbox_mark_read(listing_id=None):
    if not is_user():
        abort(403)
    user = current_listing_user()
    rows = matching_rows(user, user.listing_filter)
    view = request.form.get('view', 'all')
    query = request.form.get('q', '').strip()[:200]
    st = storage()
    try:
        if listing_id is not None:
            if not any(row['id'] == listing_id for row in rows):
                abort(404)
            ids = [listing_id]
        else:
            ids = [row['id'] for row in filter_view_rows(rows, view, query, st.read_listing_ids(user.id))]
        changed = st.mark_listings_read(user.id, ids)
        read_ids = st.read_listing_ids(user.id)
    finally:
        st.close()
    if request.accept_mimetypes.best == 'application/json':
        return jsonify(ok=True, unread_count=sum(row['id'] not in read_ids for row in rows))
    if listing_id is None:
        flash(f'已将当前范围的 {changed} 条房源标为已读。' if get_lang() == 'zh' else f'Marked {changed} listings in this view as read.', 'success')
    return redirect(workspace_return_url(request.form,selected=listing_id))


def register(app: Flask):
    app.add_url_rule('/',endpoint='index',view_func=inbox,methods=['GET'])
    app.add_url_rule('/inbox/filter',endpoint='inbox_filter_update',view_func=inbox_filter_update,methods=['POST'])
    app.add_url_rule('/inbox/read/<listing_id>',endpoint='inbox_mark_read',view_func=inbox_mark_read,methods=['POST'])
    app.add_url_rule('/inbox/read-all',endpoint='inbox_mark_all_read',view_func=inbox_mark_read,methods=['POST'])
