"""The map uses inbox identity, filters and read state without geocoding on GET."""
from urllib.parse import parse_qs, urlsplit

import pytest

from config import ListingFilter
from models import Listing, SENTINEL_AVAILABLE_FROM_YEAR
from users import UserConfig, save_users


@pytest.fixture
def map_user(test_app):
    user = UserConfig(name='Map User', app_login_enabled=True,
        listing_filter=ListingFilter(max_rent=1000, allowed_cities=['Eindhoven']))
    save_users([user])
    from app.db import storage
    st = storage()
    try:
        st.diff([Listing(id=key, name=name, city='Eindhoven', source=source,
            status=status, price_raw=price, features=['Area: 30 m²', 'Address: '+address],
            available_from=f'{SENTINEL_AVAILABLE_FROM_YEAR}-01-01', url='https://example.com/'+key)
            for key, name, price, address, source, status in [
                ('map-a', 'First unit', '€900', 'Shared building', 'holland2stay', 'Available to book'),
                ('map-b', 'Second unit', '€950', 'Shared building', 'plaza', 'Reserved'),
                ('map-missing', 'Unlocated home', '€800', 'Unknown address', 'plaza', 'Available to book'),
                ('map-invalid', 'Invalid coordinates', '€800', 'Invalid address', 'plaza', 'Available to book'),
                ('map-old', 'Older home', '€850', 'Older address', 'holland2stay', 'Occupied'),
                ('map-outside', 'Outside budget', '€1800', 'Private scope', 'plaza', 'Available to book'),
            ]])
        for key, lat, lng in [('map-a',51.4,5.4),('map-invalid',100,5.4),('map-old',52,5),('map-outside',52.5,5.5)]:
            st.cache_coords(st.get_map_listing_by_id(key)['address'],lat,lng)
        st.conn.execute("UPDATE listings SET last_seen=datetime('now','-40 days') WHERE id='map-old'")
        st.conn.commit()
    finally:
        st.close()
    return user


@pytest.fixture
def map_client(test_app,map_user):
    browser = test_app.test_client()
    with browser.session_transaction() as sess:
        sess.update(authenticated=True,role='user',user_id=map_user.id,csrf_token='test_csrf')
    browser.set_cookie('h2s-lang','zh')
    return browser


def partial(browser,query=''):
    response=browser.get('/map'+query,headers={'Accept':'application/json'})
    assert response.status_code==200
    return response.json


def test_map_uses_google_and_renders_personal_scope(map_client,monkeypatch):
    monkeypatch.setenv('GOOGLE_MAPS_API_KEY','test-maps-key')
    response=map_client.get('/map')
    html=response.get_data(as_text=True)
    assert response.status_code==200
    assert 'test-maps-key' in html and 'googlemaps-markerclusterer' in html
    assert 'leaflet' not in html.lower() and 'openstreetmap' not in html.lower()
    assert 'data-listing-id="map-a"' in html and 'data-listing-id="map-b"' in html
    assert 'Outside budget' not in html
    assert 'data-listing-id="map-old"' not in html


def test_coordinate_groups_counts_and_missing_cache(map_client):
    data=partial(map_client)
    assert data['counts']=={'located':2,'unlocated':2,'older':1}
    assert len(data['markers'])==1
    marker=data['markers'][0]
    assert marker['count']==2 and set(marker['ids'])=={'map-a','map-b'}
    assert marker['lat']==51.4 and marker['lng']==5.4
    assert {item['id'] for item in data['items']}=={'map-a','map-b'}
    missing=partial(map_client,'?positions=unlocated')
    assert {item['id'] for item in missing['items']}=={'map-missing','map-invalid'}
    # Unlocated listings remain accessible; they never become invented pins.
    assert missing['markers']==data['markers']


def test_history_is_explicit_and_focus_does_not_widen_personal_scope(map_client):
    data=partial(map_client,'?history=1')
    assert data['counts']['located']==3
    assert 'map-old' in {item['id'] for item in data['items']}
    focused=partial(map_client,'?focus=map-old')
    assert focused['selection']['history']=='1' and focused['selection']['selected']=='map-old'
    assert '历史记录' in focused['notice']
    outside=partial(map_client,'?focus=map-outside&max_rent=9999')
    assert outside['selection']['selected'] is None
    assert 'Outside budget' not in str(outside)
    assert 'map-outside' not in str(outside['markers'])
    assert '筛选范围' in outside['notice']


def test_focusing_an_unlocated_listing_opens_its_details(map_client):
    data=partial(map_client,'?focus=map-missing')
    assert data['selection']['positions']=='unlocated'
    assert data['selection']['selected']=='map-missing'
    assert '地址尚未完成定位' in data['notice']
    assert '房源详情' in data['html']
    assert data['items'][0]['display_available']=='未提供'


def test_map_grouping_search_and_availability_share_the_inbox_scope(map_client):
    group=partial(map_client,'?location=map-b')
    assert group['selection']['location']=='map-a'
    assert {item['id'] for item in group['items']}=={'map-a','map-b'}
    available=partial(map_client,'?view=available&q=unit')
    assert {item['id'] for item in available['items']}=={'map-a'}
    assert available['markers'][0]['count']==1
    platform=partial(map_client,'?view=plaza')
    assert {item['id'] for item in platform['items']}=={'map-b'}
    assert platform['counts']['unlocated']==2


def test_partial_response_is_read_only_and_does_not_contain_the_page_shell(map_client,map_user,monkeypatch):
    import app.routes.map_routes as routes
    monkeypatch.setattr(routes,'geocode_addresses',lambda *a,**k:pytest.fail('GET must not geocode'))
    from app.db import storage
    st=storage()
    before=list(st.conn.execute('SELECT * FROM geocode_cache'))
    st.close()
    data=partial(map_client)
    assert '<html' not in data['html'] and '<script' not in data['html']
    assert 'map-side' in data['html'] and data['unread_count']==5
    st=storage()
    try:
        assert st.read_listing_ids(map_user.id)==set()
        assert [tuple(row) for row in st.conn.execute('SELECT * FROM geocode_cache')]==[tuple(row) for row in before]
    finally:
        st.close()
    assert map_client.get('/map',headers={'Accept':'application/json'}).headers['Cache-Control']=='no-store'


def test_native_mark_read_and_filter_save_return_to_map(map_client,map_user):
    data={'csrf_token':'test_csrf','screen':'map','history':'1','positions':'located','location':'map-a'}
    response=map_client.post('/inbox/read/map-a',data=data)
    url=urlsplit(response.location)
    assert url.path=='/map' and parse_qs(url.query)['selected']==['map-a']
    assert parse_qs(url.query)['location']==['map-a']
    unread=partial(map_client,'?view=unread')
    assert {item['id'] for item in unread['items']}=={'map-b'}
    assert unread['markers'][0]['count']==1
    response=map_client.post('/inbox/filter',data={**data,'max_rent':'920'})
    assert urlsplit(response.location).path=='/map'
    assert {item['id'] for item in partial(map_client)['items']}=={'map-a'}


def test_roles_keep_personal_and_browsing_boundaries(map_user,admin_client,guest_client,client,monkeypatch):
    for browser in (admin_client,guest_client):
        data=partial(browser)
        assert 'map-outside' in {item['id'] for item in data['items']}
        html=browser.get('/map').get_data(as_text=True)
        assert ('id="map-geocode"' in html)==(browser is admin_client)
        assert data['unread_count'] is None
        assert browser.get('/map?view=unread').status_code==403
        assert 'map-outside' not in {item['id'] for item in partial(browser,'?max_rent=1000')['items']}
    # 没登录 = 访客（默认开着访客模式）；关掉访客模式才跳登录页
    assert 'map-outside' in {item['id'] for item in partial(client)['items']}
    assert partial(client)['unread_count'] is None
    monkeypatch.setenv('WEB_GUEST_MODE','false')
    with client.session_transaction() as sess:
        sess.clear()
    assert client.get('/map').status_code==302


@pytest.mark.parametrize('query',['history=yes','positions=unknown'])
def test_invalid_scope_returns_400(map_client,query):
    assert map_client.get('/map?'+query).status_code==400


def test_cross_view_links_preserve_filters_and_drop_view_specific_state(test_app):
    from app.services.inbox_service import workspace_url
    with test_app.test_request_context('/map?history=1&location=x&positions=unlocated&view=plaza&q=Unit&max_rent=900'):
        for endpoint in ('index','calendar'):
            query=parse_qs(urlsplit(workspace_url(endpoint=endpoint)).query)
            assert query['view']==['plaza'] and query['q']==['Unit'] and query['max_rent']==['900']
            assert not {'history','location','positions'} & query.keys()


def test_pagination_does_not_truncate_marker_or_scope_counts(map_client,monkeypatch):
    import app.services.map_workspace as service
    monkeypatch.setattr(service,'PAGE_SIZE',1)
    data=partial(map_client,'?page=2')
    assert len(data['items'])==1 and data['counts']['located']==2
    assert data['markers'][0]['count']==2
    assert data['selection']['page']==2
