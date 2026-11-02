"""Calendar and list share real Web identity, filtering and read state."""
from datetime import date
from urllib.parse import parse_qs,urlsplit

import pytest
from werkzeug.datastructures import MultiDict

from app.services.calendar_service import availability_date,calendar_context
from config import ListingFilter
from models import Listing,SENTINEL_AVAILABLE_FROM_YEAR
from users import UserConfig,save_users


def test_real_dates_and_unknown_dates_are_separate():
    rows=[{'id':'valid','available_from':'2026-12-01'},
        {'id':'unknown','available_from':None},
        {'id':'invalid','available_from':'2026-02-30'},
        {'id':'sentinel','available_from':f'{SENTINEL_AVAILABLE_FROM_YEAR}-01-01'},
        {'id':'other-month','available_from':'2027-01-01'}]
    ctx=calendar_context(rows,dict(month='2026-12'),zh=True,today=date(2026,11,2))
    assert ctx['month_count']==1 and ctx['unknown_count']==3
    assert ctx['selected_date']=='2026-12-01'
    assert [row['id'] for row in ctx['agenda_rows']]==['valid']
    ctx=calendar_context(rows,dict(month='2026-12',undated='1'),zh=False,today=date(2026,11,2))
    assert {row['id'] for row in ctx['agenda_rows']}=={'unknown','invalid','sentinel'}
    assert availability_date('2026-12-01T23:00:00+01:00')==date(2026,12,1)


def test_month_boundaries_leap_year_and_monday_start():
    ctx=calendar_context([],dict(month='2026-12'),zh=True,today=date(2026,11,2))
    assert ctx['prev_month']=='2026-11' and ctx['next_month']=='2027-01'
    nov=calendar_context([],dict(month='2026-11'),zh=True,today=date(2026,11,2))
    assert nov['weeks'][0][:6]==[None]*6 and nov['weeks'][0][6]['number']==1
    leap=calendar_context([],dict(month='2028-02'),zh=False,today=date(2026,11,2))
    assert sum(day is not None for week in leap['weeks'] for day in week)==29


@pytest.mark.parametrize('args',[{'month':'2026-13'},{'month':'2026-2'},
    {'date':'2026-02-30'},{'month':'2026-11','date':'2026-12-01'},{'undated':'yes'}])
def test_invalid_calendar_parameters_are_rejected(args):
    with pytest.raises(ValueError):
        calendar_context([],MultiDict(args),zh=False,today=date(2026,11,2))


@pytest.fixture
def calendar_user(test_app):
    user=UserConfig(name='Calendar User',app_login_enabled=True,
        listing_filter=ListingFilter(max_rent=1000,min_area=20,allowed_cities=['Eindhoven']))
    save_users([user])
    from app.db import storage
    st=storage()
    try:
        st.diff([Listing(id=key,name=name,available_from=when,status=status,price_raw=price,
            features=['Area: 30 m²'],city='Eindhoven',source=source,url='https://example.com/'+key)
            for key,name,when,status,price,source in [
                ('cal-match','Calendar match','2026-12-01','Available to book','€950','holland2stay'),
                ('cal-next','Next day','2026-12-02','Available to book','€950','plaza'),
                ('cal-expensive','Outside personal budget','2026-12-01','Available to book','€1800','plaza'),
                ('cal-unknown','Unknown move-in date',None,'Available to book','€800','holland2stay'),
                ('cal-sentinel','Undetermined date',f'{SENTINEL_AVAILABLE_FROM_YEAR}-01-01','Available to book','€850','holland2stay'),
                ('cal-invalid','Invalid recorded date','not-a-date','Available to book','€700','plaza'),
                ('cal-reserved','Reserved home','2026-12-01','Reserved','€900','holland2stay'),
            ]])
    finally:
        st.close()
    return user


@pytest.fixture
def calendar_client(test_app,calendar_user):
    client=test_app.test_client()
    with client.session_transaction() as sess:
        sess.update(authenticated=True,role='user',user_id=calendar_user.id,csrf_token='test_csrf')
    client.set_cookie('h2s-lang','zh')
    return client


def test_calendar_only_shows_the_personal_scope_and_selected_day(calendar_client):
    response=calendar_client.get('/calendar?month=2026-12&date=2026-12-01')
    assert response.status_code==200
    html=response.get_data(as_text=True)
    assert 'data-listing-id="cal-match"' in html and 'data-listing-id="cal-reserved"' in html
    assert 'data-listing-id="cal-next"' not in html
    assert 'Outside personal budget' not in html
    assert '日历' in html and '来源价格' not in html
    assert 'calendar-agenda' in html
    # Browsing a day must not mark every listing on that day as read.
    from app.db import storage
    st=storage()
    try:
        assert st.read_listing_ids(calendar_user_id(calendar_client))==set()
    finally:
        st.close()


def calendar_user_id(client):
    with client.session_transaction() as sess:
        return sess['user_id']


def test_undated_list_keeps_missing_invalid_and_sentinel_records(calendar_client):
    html=calendar_client.get('/calendar?month=2026-12&undated=1').get_data(as_text=True)
    for key in ('cal-unknown','cal-sentinel','cal-invalid'):
        assert f'data-listing-id="{key}"' in html
    assert 'data-listing-id="cal-match"' not in html
    assert '入住日期未确定' in html


def test_calendar_applies_platform_search_and_availability(calendar_client):
    html=calendar_client.get('/calendar?month=2026-12&date=2026-12-01&view=available&q=Calendar').get_data(as_text=True)
    assert 'data-listing-id="cal-match"' in html
    assert 'data-listing-id="cal-reserved"' not in html
    html=calendar_client.get('/calendar?month=2026-12&date=2026-12-01&view=plaza').get_data(as_text=True)
    assert 'data-listing-id=' not in html
    assert '当天没有匹配房源' in html


def test_admin_guest_and_anonymous_keep_existing_role_boundaries(calendar_user,admin_client,guest_client,client,monkeypatch):
    for browser in (admin_client,guest_client):
        html=browser.get('/calendar?month=2026-12&date=2026-12-01').get_data(as_text=True)
        assert 'Outside personal budget' in html
        assert 'inbox-unread-count' not in html
        assert browser.get('/calendar?view=unread').status_code==403
    # 没登录 = 访客（默认开着访客模式）；关掉访客模式才跳登录页
    html=client.get('/calendar?month=2026-12&date=2026-12-01').get_data(as_text=True)
    assert 'Outside personal budget' in html and 'inbox-unread-count' not in html
    monkeypatch.setenv('WEB_GUEST_MODE','false')
    with client.session_transaction() as sess:
        sess.clear()
    assert client.get('/calendar').status_code==302


def test_native_read_action_returns_to_calendar_and_only_reads_selected_listing(calendar_client,calendar_user):
    response=calendar_client.post('/inbox/read/cal-match',data={
        'csrf_token':'test_csrf','screen':'calendar','month':'2026-12','date':'2026-12-01','view':'all'})
    target=urlsplit(response.location)
    assert target.path=='/calendar'
    assert parse_qs(target.query)['selected']==['cal-match']
    from app.db import storage
    st=storage()
    try:
        assert st.read_listing_ids(calendar_user.id)=={'cal-match'}
    finally:
        st.close()
    html=calendar_client.get(response.location).get_data(as_text=True)
    assert '返回房源列表' in html
    assert 'id="calendar-agenda"' in html


def test_filter_save_returns_to_the_calendar(calendar_client):
    response=calendar_client.post('/inbox/filter',data={'csrf_token':'test_csrf',
        'screen':'calendar','month':'2026-12','date':'2026-12-01','max_rent':'900'})
    assert urlsplit(response.location).path=='/calendar'
    html=calendar_client.get(response.location).get_data(as_text=True)
    assert 'data-listing-id="cal-match"' not in html
    assert 'data-listing-id="cal-reserved"' in html


def test_view_links_keep_query_scope_and_remove_calendar_only_state(test_app):
    from app.services.inbox_service import workspace_url
    with test_app.test_request_context('/calendar?month=2026-12&date=2026-12-01&view=plaza&q=House&max_rent=900'):
        url=workspace_url(endpoint='index',selected=None)
        query=parse_qs(urlsplit(url).query)
        assert query['view']==['plaza'] and query['q']==['House'] and query['max_rent']==['900']
        assert 'month' not in query and 'date' not in query


@pytest.mark.parametrize('query',['month=2026-13','date=2026-02-30','month=2026-11&date=2026-12-01'])
def test_invalid_calendar_urls_return_400(calendar_client,query):
    assert calendar_client.get('/calendar?'+query).status_code==400


def test_display_date_selection_is_not_a_listing_sentinel():
    ctx=calendar_context([],dict(month=f'{SENTINEL_AVAILABLE_FROM_YEAR}-01',date=f'{SENTINEL_AVAILABLE_FROM_YEAR}-01-01'),zh=True,today=date(2026,11,2))
    assert ctx['agenda_rows']==[] and ctx['selected_date']==f'{SENTINEL_AVAILABLE_FROM_YEAR}-01-01'
    first=calendar_context([],dict(month='0001-01'),zh=True,today=date(2026,11,2))
    assert first['prev_month'] is None and first['next_month']=='0001-02'
    assert first['calendar_month']=='0001-01'
    last=calendar_context([],dict(month='9999-12'),zh=True,today=date(2026,11,2))
    assert last['next_month'] is None


def test_partial_calendar_response_is_scoped_and_contains_no_page_shell(calendar_client,calendar_user):
    response=calendar_client.get('/calendar?month=2026-12&date=2026-12-01',headers={'Accept':'application/json'})
    assert response.status_code==200 and response.is_json
    data=response.json
    assert {item['id'] for item in data['items']}=={'cal-match','cal-reserved'}
    assert '<html' not in data['html'] and '<script' not in data['html']
    assert 'calendar-workspace' in data['html'] and 'Outside personal budget' not in data['html']
    assert data['selection']=={'month':'2026-12','date':'2026-12-01','undated':None}
    assert data['unread_count']==6
    assert response.headers['Cache-Control']=='no-store'
    from app.db import storage
    st=storage()
    try:
        assert st.read_listing_ids(calendar_user.id)==set()
    finally:
        st.close()


def test_partial_undated_and_empty_days_keep_normal_calendar_semantics(calendar_client):
    data=calendar_client.get('/calendar?month=2026-12&undated=1',headers={'Accept':'application/json'}).json
    assert {item['id'] for item in data['items']}=={'cal-unknown','cal-sentinel','cal-invalid'}
    assert data['selection']['undated']=='1' and data['selection']['date'] is None
    empty=calendar_client.get('/calendar?month=2026-12&date=2026-12-03',headers={'Accept':'application/json'}).json
    assert empty['items']==[]
    assert '当天没有匹配房源' in empty['html']


def test_partial_request_keeps_auth_and_filter_boundaries(calendar_client,calendar_user,client,guest_client,monkeypatch):
    headers={'Accept':'application/json'}
    monkeypatch.setenv('WEB_GUEST_MODE','false')
    assert client.get('/calendar?month=2026-12',headers=headers).status_code==302
    monkeypatch.delenv('WEB_GUEST_MODE')
    assert guest_client.get('/calendar?view=unread',headers=headers).status_code==403
    assert calendar_client.get('/calendar?month=bad',headers=headers).status_code==400
    data=calendar_client.get('/calendar?month=2026-12&date=2026-12-01&view=plaza',headers=headers).json
    assert data['items']==[]
