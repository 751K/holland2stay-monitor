"""Self-service account workspace: sections, rows and their saved-state summaries.

The page mirrors the inbox: sections in the sidebar, a row list with the saved
state of each setting, and a detail pane that edits the selected row. Every
editor stays inside one form because ``build_user_from_form_self`` rebuilds the
whole UserConfig from the POST; submitting a single panel would clear the rest.
Summaries describe what is saved, never unsaved edits in the browser.
"""
from __future__ import annotations

from app.services.inbox_service import filter_summary
from filters import ListingFilter
from mcore.sources import SPECS


def _t(zh: bool, cn: str, en: str) -> str:
    return cn if zh else en


def _names(values, labels=None, *, limit=3, zh: bool) -> str:
    labels = labels or {}
    shown = [labels.get(v, v) for v in values[:limit]]
    rest = len(values) - len(shown)
    if rest > 0:
        shown.append(_t(zh, f'另 {rest} 项', f'+{rest} more'))
    return ', '.join(shown)


def _number(value) -> str:
    return f'{value:,.2f}'.rstrip('0').rstrip('.')


def _row(key, title, summary, state='', tone='muted', quiet=False):
    # tone: ok / attention / off / muted. The state text always carries the meaning;
    # tone only adjusts emphasis. quiet: the detail panel shows the state itself
    # (a switch), so it skips the state line under its heading.
    return dict(key=key, title=title, summary=summary, state=state, tone=tone, quiet=quiet)


def _filter_rows(lf: ListingFilter, options: dict, zh: bool):
    any_text = _t(zh, '不限', 'Any')
    parts = []
    if lf.max_rent is not None:
        parts.append(_t(zh, '最高', 'Up to') + f' €{_number(lf.max_rent)}')
    if lf.min_area is not None:
        parts.append(_t(zh, '至少', 'At least') + f' {lf.min_area:g} m²')
    if lf.min_floor is not None:
        parts.append(_t(zh, f'{lf.min_floor} 层及以上', f'Floor {lf.min_floor}+'))
    location = []
    if lf.allowed_cities:
        location.append(_names(lf.allowed_cities, zh=zh))
    if lf.allowed_neighborhoods:
        location.append(_t(zh, f'{len(lf.allowed_neighborhoods)} 个片区', f'{len(lf.allowed_neighborhoods)} neighbourhoods'))
    if lf.allowed_buildings:
        location.append(_t(zh, f'{len(lf.allowed_buildings)} 个楼盘', f'{len(lf.allowed_buildings)} buildings'))
    home = [_names(v, dict(options[k]), zh=zh) for v, k in (
        (lf.allowed_types, 'type'), (lf.allowed_occupancy, 'occupancy')) if v]
    terms = [_names(v, dict(options[k]), zh=zh) for v, k in (
        (lf.allowed_contract, 'contract'), (lf.allowed_tenant, 'tenant')) if v]
    extras = [_names(v, zh=zh) for v in (lf.allowed_offer, lf.allowed_finishing) if v]
    if lf.allowed_energy:
        extras.append(_t(zh, f'能耗 {lf.allowed_energy} 及以上', f'Energy {lf.allowed_energy} or better'))
    sources = _names([SPECS[s].display_name if s in SPECS else s for s in lf.allowed_sources], limit=4, zh=zh)

    def summary(values, empty):
        return ' · '.join(values) if values else empty

    return [
        _row('rent', _t(zh, '租金与面积', 'Rent & size'), summary(parts, any_text)),
        _row('location', _t(zh, '城市与位置', 'Cities & location'),
             summary(location, _t(zh, '全荷兰', 'All Netherlands'))),
        _row('sources', _t(zh, '平台', 'Platforms'), sources or _t(zh, '全部平台', 'All platforms')),
        _row('home', _t(zh, '房型与入住人数', 'Home type & occupancy'), summary(home, any_text)),
        _row('terms', _t(zh, '合同与租客', 'Contract & tenant'), summary(terms, any_text)),
        _row('extras', _t(zh, '标签、装修与能耗', 'Labels, finishing & energy'), summary(extras, any_text)),
    ]


def _channel_state(user, channel, configured, zh):
    on = channel in user.notification_channels
    if on and configured:
        return _t(zh, '已启用', 'On'), 'ok'
    if on:
        return _t(zh, '已启用，信息不完整', 'On, incomplete'), 'attention'
    if configured:
        return _t(zh, '已填写，未启用', 'Saved, off'), 'off'
    return _t(zh, '未设置', 'Not set up'), 'muted'


def _notification_rows(user, device_count, is_macos, zh, official=True):
    rows = [_row('delivery', _t(zh, '通知开关', 'Notifications'),
                 _t(zh, '新房源符合条件时提醒你', 'Alert me when a matching listing appears'),
                 *((_t(zh, '已开启', 'On'), 'ok') if user.notifications_enabled else (_t(zh, '已关闭', 'Off'), 'attention')),
                 quiet=True)]
    # 官方 App 只连 flatradar.app，推送也要作者的 APNs / FCM 密钥：自部署的站点上这一条走不通
    if official:
        rows.append(_row('apppush', _t(zh, 'App 推送', 'App push'),
                         _t(zh, f'{device_count} 台设备已登录', f'{device_count} signed-in device(s)') if device_count
                         else _t(zh, '安装 App 并用本账号登录', 'Install the app and sign in'),
                         *((_t(zh, '已连接', 'Connected'), 'ok') if device_count else (_t(zh, '无设备', 'No devices'), 'muted'))))
    rows.append(_row('telegram', 'Telegram', 'Bot API',
                     *_channel_state(user, 'telegram', bool(user.telegram_token and user.telegram_chat_id), zh)))
    if user.email_to:
        email_summary = user.email_to + ' · ' + (_t(zh, '已验证', 'verified') if user.email_verified else _t(zh, '未验证', 'not verified'))
    else:
        email_summary = _t(zh, '使用 FlatRadar 发信', 'Sent by FlatRadar')
    state, tone = _channel_state(user, 'email', bool(user.email_to), zh)
    if tone == 'ok' and not user.email_verified:
        state, tone = _t(zh, '已启用，待验证', 'On, verify email'), 'attention'
    rows.append(_row('email', 'Email', email_summary, state, tone))
    rows.append(_row('whatsapp', 'WhatsApp', 'Twilio',
                     *_channel_state(user, 'whatsapp', bool(user.twilio_sid and user.twilio_to), zh)))
    if is_macos:
        rows.append(_row('imessage', 'iMessage', _t(zh, '仅本地 macOS 部署', 'Local macOS only'),
                         *_channel_state(user, 'imessage', bool(user.imessage_recipient), zh)))
    else:
        rows.append(_row('imessage', 'iMessage', _t(zh, '需要本地 macOS 部署', 'Requires a local macOS install'),
                         _t(zh, '不可用', 'Unavailable'), 'muted'))
    return rows


def _notification_summary(user, device_count, zh):
    if not user.notifications_enabled:
        return _t(zh, '通知已关闭，不会收到房源提醒', 'Notifications are off')
    count = len(user.notification_channels) + (1 if device_count else 0)
    if not count:
        return _t(zh, '已开启 · 尚未设置接收方式', 'On · no delivery method yet')
    return _t(zh, f'已开启 · {count} 种接收方式', f'On · {count} delivery method(s)')


SECTION_KEYS = ('filters', 'notifications', 'account', 'preferences')


def account_sections(user, *, device_count: int, is_macos: bool, options: dict, zh: bool, official: bool = True):
    lf = user.listing_filter
    return [
        dict(key='filters', icon='bi-funnel', title=_t(zh, '房源条件', 'Listing filters'),
             summary=filter_summary(lf, zh),
             note=_t(zh, '房源收件箱和通知使用同一套条件。留空表示不限。',
                     'Your inbox and notifications use the same filters. Leave a field empty for no limit.'),
             rows=_filter_rows(lf, options, zh)),
        dict(key='notifications', icon='bi-bell', title=_t(zh, '通知', 'Notifications'),
             summary=_notification_summary(user, device_count, zh),
             note=_t(zh, '符合房源条件的新房源会通过已启用的方式发给你。',
                     'New listings that match your filters are sent through the methods you turn on.'),
             rows=_notification_rows(user, device_count, is_macos, zh, official)),
        dict(key='account', icon='bi-person', title=_t(zh, '账号与登录', 'Account & sign-in'),
             summary=user.name + ' · ' + (_t(zh, '客户端登录已开启', 'App sign-in on') if user.app_login_enabled
                                          else _t(zh, '客户端登录未开启', 'App sign-in off')),
             note=_t(zh, '账号名称和客户端登录开关由管理员管理。', 'An administrator manages the account name and app sign-in.'),
             rows=[
                 _row('status', _t(zh, '账号状态', 'Account status'), user.name,
                      *((_t(zh, '已启用', 'Active'), 'ok') if user.enabled else (_t(zh, '已停用', 'Disabled'), 'attention'))),
                 _row('password', _t(zh, '登录密码', 'Password'),
                      _t(zh, 'App 与网页共用', 'Used by the app and the web'),
                      *((_t(zh, '已设置', 'Set'), 'ok') if user.app_password_hash else (_t(zh, '未设置', 'Not set'), 'muted'))),
             ]),
        dict(key='preferences', icon='bi-sliders', title=_t(zh, '偏好', 'Preferences'),
             summary=_t(zh, '语言、外观与条款', 'Language, appearance and terms'), note='',
             rows=[
                 _row('language', _t(zh, '语言', 'Language'),
                      _t(zh, '界面、通知和验证邮件', 'Interface, alerts and verification email'),
                      '中文' if (user.language or 'en') == 'zh' else 'English'),
                 _row('appearance', _t(zh, '外观', 'Appearance'), _t(zh, '仅对当前浏览器生效', 'This browser only')),
                 _row('legal', _t(zh, '条款与隐私', 'Terms & privacy'), _t(zh, '隐私条款 · 使用条款', 'Privacy policy · Terms of use')),
                 _row('signout', _t(zh, '退出登录', 'Sign out'), _t(zh, '当前登录：', 'Signed in as ') + user.name),
             ]),
    ]


def resolve_selection(sections, section_key, item_key, *, default_section='filters'):
    """Return valid (section, item) keys; unknown values fall back instead of 400."""
    by_key = {s['key']: s for s in sections}
    section = by_key.get(section_key) or by_key[default_section]
    keys = [row['key'] for row in section['rows']]
    return section['key'], item_key if item_key in keys else keys[0]


# ── Administrator: create / edit any user ─────────────────────────────

ADMIN_SECTION_KEYS = ('basic', 'notifications', 'filters', 'autobook', 'profile')

#: Applicant profile groups, in the order the RENTCafe form asks for them.
PROFILE_GROUPS = {
    'name': ('first_name', 'last_name', 'middle_name', 'gender', 'date_of_birth'),
    'identity': ('nationality', 'place_of_birth', 'id_number', 'id_country'),
    'address': ('address', 'postcode', 'city', 'country', 'housing_type'),
    'study': (),
    'screening': ('ever_evicted', 'ever_convicted', 'criminal_charges'),
}


def _configured(value, zh):
    return (_t(zh, '已配置', 'Set up'), 'ok') if value else (_t(zh, '未配置', 'Not set up'), 'muted')


def _autobook_rows(ab, zh):
    sources = ab.listing_filter
    xior = len(ab.xior_accounts or {})
    return [
        _row('switch', _t(zh, '自动预订', 'Auto-booking'),
             _t(zh, '试运行：只走流程不下单', 'Dry run: goes through the steps without ordering') if ab.dry_run
             else _t(zh, '正式预订：会真实提交订单', 'Live: submits real orders'),
             *((_t(zh, '已开启', 'On'), 'attention' if not ab.dry_run else 'ok') if ab.enabled else (_t(zh, '未开启', 'Off'), 'muted'))),
        _row('abfilter', _t(zh, '预订条件', 'Booking filters'), filter_summary(sources, zh)),
        _row('h2s', 'Holland2Stay', ab.email or _t(zh, '邮箱登录', 'Email sign-in'), *_configured(ab.email, zh)),
        _row('xior', 'Xior', _t(zh, f'{xior} 栋楼', f'{xior} building(s)') if xior else _t(zh, '每栋楼一个账号', 'One account per building'),
             *_configured(xior, zh)),
        _row('ourdomain', 'OurDomain', ab.ourdomain_email or _t(zh, '邮箱登录', 'Email sign-in'), *_configured(ab.ourdomain_email, zh)),
        _row('ourcampus', 'OurCampus', ab.ourcampus_email or _t(zh, '邮箱登录', 'Email sign-in'), *_configured(ab.ourcampus_email, zh)),
        _row('plaza', 'Plaza', ab.plaza_username or _t(zh, '用户名登录', 'Username sign-in'),
             *((_t(zh, '自动应征', 'Auto-apply'), 'ok') if ab.plaza_enabled and ab.plaza_username
               else _configured(ab.plaza_username, zh) if not ab.plaza_enabled else (_t(zh, '已开启，缺账号', 'On, no account'), 'attention'))),
    ]


def _profile_rows(ab, id_doc, zh):
    prof = ab.applicant_profile
    missing = set(prof.missing_fields())
    titles = {'name': _t(zh, '姓名与出生', 'Name and birth'), 'identity': _t(zh, '国籍与证件', 'Nationality and ID'),
              'address': _t(zh, '住址', 'Address'), 'study': _t(zh, '学业', 'Study'),
              'screening': _t(zh, '背景调查', 'Screening')}
    summaries = {
        'name': ' '.join(x for x in (prof.first_name, prof.middle_name, prof.last_name) if x),
        'identity': ' · '.join(x for x in (prof.nationality, prof.id_country) if x),
        'address': ', '.join(x for x in (prof.address, prof.postcode, prof.city) if x),
        'study': ' · '.join(x for x in (prof.university, prof.student_number) if x),
        'screening': '',
    }
    rows = []
    for key, fields in PROFILE_GROUPS.items():
        gap = len(missing.intersection(fields))
        state = (_t(zh, f'缺 {gap} 项', f'{gap} missing'), 'attention') if gap else \
                ((_t(zh, '已填写', 'Complete'), 'ok') if fields else ('', 'muted'))
        rows.append(_row(key, titles[key], summaries[key] or _t(zh, '未填写', 'Empty'), *state))
    consent = ab.has_screening_consent()
    doc_state = (_t(zh, '已就绪', 'Ready'), 'ok') if id_doc and consent else (
        _t(zh, '缺证件' if not id_doc else '缺授权', 'No ID document' if not id_doc else 'No consent'), 'attention')
    rows.append(_row('docs', _t(zh, '证件与授权', 'ID document and consent'),
                     (id_doc[0] if id_doc else _t(zh, '未上传证件', 'No document uploaded')), *doc_state))
    return rows


def admin_sections(user, *, is_new: bool, device_count: int, is_macos: bool, id_doc, options: dict, zh: bool,
                   official: bool = True):
    """Sections for the admin user editor. ``user`` is a blank UserConfig when creating."""
    ab = user.auto_book
    notification_rows = _notification_rows(user, device_count, is_macos, zh, official)
    if user.email_mode == 'custom':
        for row in notification_rows:
            if row['key'] == 'email':
                row['summary'] = (user.email_to or '') + ' · ' + _t(zh, '自定义 SMTP', 'Custom SMTP')
    profile_rows = _profile_rows(ab, id_doc, zh)
    gaps = sum(row['tone'] == 'attention' for row in profile_rows)
    return [
        dict(key='basic', icon='bi-person', title=_t(zh, '基本信息', 'Basics'),
             summary=_t(zh, '新用户', 'New user') if is_new else user.name + ' · ' + user.id,
             note='', rows=[
                 _row('identity', _t(zh, '名称与状态', 'Name and status'), user.name or _t(zh, '未命名', 'Unnamed'),
                      *((_t(zh, '已启用', 'Active'), 'ok') if user.enabled else (_t(zh, '已停用', 'Disabled'), 'attention'))),
                 _row('login', _t(zh, 'App 与网页登录', 'App and web sign-in'),
                      _t(zh, '密码已设置', 'Password set') if user.app_password_hash else _t(zh, '未设置密码', 'No password'),
                      *((_t(zh, '已开启', 'On'), 'ok') if user.app_login_enabled else (_t(zh, '未开启', 'Off'), 'muted'))),
                 _row('language', _t(zh, '语言', 'Language'), _t(zh, '通知和验证邮件', 'Alerts and verification email'),
                      '中文' if (user.language or 'en') == 'zh' else 'English'),
             ]),
        dict(key='notifications', icon='bi-bell', title=_t(zh, '通知', 'Notifications'),
             summary=_notification_summary(user, device_count, zh), note='', rows=notification_rows),
        dict(key='filters', icon='bi-funnel', title=_t(zh, '房源条件', 'Listing filters'),
             summary=filter_summary(user.listing_filter, zh),
             note=_t(zh, '收件箱和通知使用这套条件。留空表示不限。', 'Used by the inbox and notifications. Leave empty for no limit.'),
             rows=_filter_rows(user.listing_filter, options, zh)),
        dict(key='autobook', icon='bi-cart-check', title=_t(zh, '自动预订', 'Auto-booking'),
             summary=(_t(zh, '已开启 · 试运行', 'On · dry run') if ab.dry_run else _t(zh, '已开启 · 正式预订', 'On · live'))
             if ab.enabled else _t(zh, '未开启', 'Off'),
             note=_t(zh, '先确定抢哪些房，再配置各平台账号。', 'Decide which homes to book first, then set up the platform accounts.'),
             rows=_autobook_rows(ab, zh)),
        dict(key='profile', icon='bi-person-vcard', title=_t(zh, '申请人档案', 'Applicant profile'),
             summary=_t(zh, f'Xior 与 OurDomain 共用 · 缺 {gaps} 组', f'Shared by Xior and OurDomain · {gaps} incomplete') if gaps
             else _t(zh, 'Xior 与 OurDomain 共用 · 已完整', 'Shared by Xior and OurDomain · complete'),
             note=_t(zh, '自动填写 RENTCafe 的申请人信息。背景调查必须由本人如实回答。',
                     'Fills RENTCafe applicant details. Screening answers must come from the applicant.'),
             rows=profile_rows),
    ]
