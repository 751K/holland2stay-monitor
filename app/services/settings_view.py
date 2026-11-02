"""Global settings workspace: groups, rows and their saved-state summaries.

Same layout as the account workspace: a row list with what is saved, and a
detail pane that edits the selected row. Every editor stays inside one form,
because the settings route rebuilds SOURCES and every city list from the whole
POST; submitting a single panel would clear the rest. Summaries describe what
is saved, never unsaved edits in the browser.
"""
from __future__ import annotations

from app.services.account_view import _names, _row, _t

SECTION_KEYS = ('status', 'scrape', 'platforms', 'maintenance')

_STATE = {
    'applied': (('已生效', 'Applied'), 'ok'),
    'pending': (('等待应用', 'Pending'), 'attention'),
    'stopped': (('监控未运行', 'Monitor stopped'), 'muted'),
    'unknown': (('未确认', 'Unconfirmed'), 'muted'),
}


def _on(value: str, default: bool) -> bool:
    value = (value or '').strip().lower()
    if not value:
        return default
    return value in ('1', 'true', 'yes', 'on')


def _picked(name: str, chosen: int, total: int, names: list[str], zh: bool):
    summary = _names(names, zh=zh) if names else _t(zh, '未选择', 'None selected')
    tone = 'attention' if not chosen else 'muted'
    return _row(name, '', summary, _t(zh, f'{chosen} / {total}', f'{chosen} / {total}'), tone)


def settings_sections(ctx: dict, *, zh: bool) -> list[dict]:
    """``ctx`` is the settings route's template context (env, selections, registries)."""
    env = ctx['env']
    get = lambda key, default: env.get(key) or default

    (cn, en), tone = _STATE.get(ctx['config_status'].get('state'), _STATE['unknown'])
    status = [_row('apply', _t(zh, '生效状态', 'Status'),
                   _t(zh, '已保存的配置是否已被监控应用', 'Whether the monitor runs the saved settings'),
                   _t(zh, cn, en), tone)]

    weekdays = _on(env.get('PEAK_WEEKDAYS_ONLY', ''), True)
    rest_on = _on(env.get('H2S_REST_ENABLED', ''), False)
    scrape = [
        _row('interval', _t(zh, '抓取间隔', 'Interval'),
             _t(zh, f"每 {get('CHECK_INTERVAL', '300')} 秒 · 日志 {get('LOG_LEVEL', 'INFO')}",
                f"Every {get('CHECK_INTERVAL', '300')} s · log {get('LOG_LEVEL', 'INFO')}")),
        _row('peak', _t(zh, '高峰加速', 'Peak hours'),
             f"{get('PEAK_START', '08:30')}–{get('PEAK_END', '10:00')}, "
             f"{get('PEAK_START_2', '13:30')}–{get('PEAK_END_2', '15:00')} · "
             + _t(zh, f"{get('PEAK_INTERVAL', '60')}→{get('MIN_INTERVAL', '15')} 秒",
                  f"{get('PEAK_INTERVAL', '60')}→{get('MIN_INTERVAL', '15')} s")
             + (_t(zh, ' · 仅工作日', ' · weekdays') if weekdays else '')),
        _row('h2s_rest', _t(zh, 'H2S 快速通道', 'H2S fast lane'),
             _t(zh, f"每 {get('H2S_REST_INTERVAL', '3')} 秒 · {get('H2S_REST_START', '08:30')}–{get('H2S_REST_END', '17:45')}",
                f"Every {get('H2S_REST_INTERVAL', '3')} s · {get('H2S_REST_START', '08:30')}–{get('H2S_REST_END', '17:45')}"),
             *((_t(zh, '已开启', 'On'), 'ok') if rest_on else (_t(zh, '已关闭', 'Off'), 'muted'))),
    ]

    labels = dict(ctx['source_options'])
    chosen = [k for k, _ in ctx['source_options'] if k in ctx['selected_sources']]
    platforms = [_row('sources', _t(zh, '监控平台', 'Platforms'),
                      _names([labels[k] for k in chosen], zh=zh) or _t(zh, '未选择', 'None selected'),
                      f"{len(chosen)} / {len(labels)}", 'muted' if chosen else 'attention')]
    lists = (
        ('h2s', 'Holland2Stay', ctx['known_cities'], 'id', ctx['selected_city_ids'], _t(zh, '城市', 'cities')),
        ('ourdomain', 'OurDomain', ctx['known_ourdomain_cities'], 'key', ctx['selected_ourdomain_keys'], _t(zh, '楼盘', 'buildings')),
        ('magis', 'Magis', ctx['known_magis_cities'], 'key', ctx['selected_magis_keys'], _t(zh, '城市', 'cities')),
        ('se', 'Student Experience', ctx['known_se_cities'], 'key', ctx['selected_se_keys'], _t(zh, '城市', 'cities')),
        ('plaza', 'Plaza', ctx['known_plaza_cities'], 'key', ctx['selected_plaza_keys'], _t(zh, '城市', 'cities')),
    )
    for key, brand, known, id_key, selected, noun in lists:
        names = [c['name'] for c in known if c[id_key] in selected]
        row = _picked(key, len(names), len(known), names, zh)
        row['title'] = f'{brand} {noun}'
        platforms.append(row)
    xior = ctx['known_xior_cities']
    xior_names = sorted({c['city'] for c in xior if c['key'] in ctx['selected_xior_keys']})
    row = _picked('xior', sum(c['key'] in ctx['selected_xior_keys'] for c in xior), len(xior), xior_names, zh)
    row['title'] = _t(zh, 'Xior 楼盘', 'Xior buildings')
    platforms.append(row)

    maintenance = [
        _row('apply', _t(zh, '应用与重启', 'Apply & restart'),
             _t(zh, '让监控立即重读配置，或重启进程', 'Reload the configuration now, or restart the monitor')),
        _row('reset', _t(zh, '清空数据库', 'Reset database'),
             _t(zh, '删除全部房源与变更记录', 'Delete every listing and change record')),
    ]

    # The detail pane shows only the editor: the row list already carries each
    # summary, so the line under the panel heading would just repeat it
    for rows in (status, scrape, platforms, maintenance):
        for row in rows:
            row['quiet'] = True
    return [
        dict(key='status', title=_t(zh, '运行状态', 'Status'), rows=status),
        dict(key='scrape', title=_t(zh, '抓取节奏', 'Scrape schedule'), rows=scrape),
        dict(key='platforms', title=_t(zh, '平台与城市', 'Platforms & cities'), rows=platforms),
        dict(key='maintenance', title=_t(zh, '维护', 'Maintenance'), rows=maintenance),
    ]


def resolve_settings_selection(sections: list[dict], section: str | None, item: str | None):
    """Valid (section, item) for the request, falling back to the first row."""
    for s in sections:
        if s['key'] == section:
            keys = [r['key'] for r in s['rows']]
            return section, item if item in keys else keys[0]
    return sections[0]['key'], sections[0]['rows'][0]['key']
