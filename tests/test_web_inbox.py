"""Production inbox integration: actual sessions, SQLite and notification filters."""
from __future__ import annotations

from dataclasses import replace

import pytest

from config import ListingFilter
from models import Listing
from users import UserConfig, get_user, load_users, save_users


@pytest.fixture
def inbox_users(test_app):
    alice = UserConfig(name='Inbox Alice', app_login_enabled=True,
        listing_filter=ListingFilter(max_rent=1000, min_area=25, allowed_cities=['Eindhoven']),
        telegram_chat_id='keep-this-private', notifications_enabled=False)
    bob = UserConfig(name='Inbox Bob', app_login_enabled=True,
        listing_filter=ListingFilter(max_rent=2200))
    save_users([alice,bob])
    from app.db import storage
    st = storage()
    try:
        st.diff([
            Listing(available_from='2026-12-01',id='match',name='Matching home',city='Eindhoven',source='holland2stay',
                status='Available to book',price_raw='€950',features=['Area: 30 m²'],url='https://example.com/match'),
            Listing(available_from='2026-12-01',id='expensive',name='Outside budget',city='Eindhoven',source='plaza',
                status='Available to book',price_raw='€1800',features=['Area: 35 m²'],url='https://example.com/expensive'),
            Listing(available_from='2026-12-01',id='city',name='Different city',city='Delft',source='plaza',
                status='Available to book',price_raw='€900',features=['Area: 30 m²'],url='https://example.com/city'),
            Listing(available_from='2026-12-01',id='small',name='Too small',city='Eindhoven',source='holland2stay',
                status='Reserved',price_raw='€800',features=['Area: 20 m²'],url='javascript:alert(1)'),
        ])
    finally:
        st.close()
    return alice,bob


@pytest.fixture
def inbox_client(test_app,inbox_users):
    client = test_app.test_client()
    with client.session_transaction() as sess:
        sess.update(authenticated=True,role='user',user_id=inbox_users[0].id,csrf_token='test_csrf')
    return client


def test_user_home_only_contains_personal_matches(inbox_client):
    response = inbox_client.get('/')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Matching home' in html
    assert 'Outside budget' not in html and 'Different city' not in html and 'Too small' not in html
    assert 'keep-this-private' not in html
    assert 'data-listing-id="match"' in html
    assert 'data-listing-id="expensive"' not in inbox_client.get('/?selected=expensive').get_data(as_text=True)


def test_source_and_search_cannot_widen_personal_scope(inbox_client):
    html = inbox_client.get('/?view=plaza&q=Outside').get_data(as_text=True)
    assert 'data-listing-id=' not in html
    assert 'id="mail-items">[]' in html
    # A user id supplied by a browser must not select another user's profile.
    assert 'Outside budget' not in inbox_client.get('/?user_id=anything&max_rent=5000').get_data(as_text=True)


def test_admin_and_guest_browse_all(inbox_users,admin_client,guest_client):
    for client in (admin_client,guest_client):
        html = client.get('/').get_data(as_text=True)
        assert 'Outside budget' in html and 'Different city' in html
        assert 'javascript:alert(1)' not in html
    assert 'Administration' in admin_client.get('/').get_data(as_text=True)
    assert '/dashboard' not in guest_client.get('/').get_data(as_text=True)
    assert 'data-listing-id="expensive"' not in guest_client.get('/?max_rent=1000').get_data(as_text=True)


def test_filter_save_uses_database_and_preserves_other_settings(inbox_client,inbox_users):
    alice,bob = inbox_users
    before_bob = replace(bob.listing_filter)
    from users import update_users
    def advanced(users):
        get_user(users,alice.id).listing_filter.allowed_types = ['Studio']
    update_users(advanced)
    response = inbox_client.post('/inbox/filter',data={
        'csrf_token':'test_csrf','max_rent':'1400','min_area':'28','city':['Delft'],
        'source':['plaza'],'user_id':bob.id,'NOTIFICATIONS_ENABLED':'true',
    })
    assert response.status_code == 302
    alice = get_user(load_users(),alice.id)
    assert alice.listing_filter.max_rent == 1400
    assert alice.listing_filter.allowed_cities == ['Delft']
    assert alice.listing_filter.allowed_sources == ['plaza']
    assert alice.listing_filter.allowed_types == ['Studio']
    assert alice.telegram_chat_id == 'keep-this-private'
    assert alice.notifications_enabled is False
    assert get_user(load_users(),bob.id).listing_filter == before_bob


@pytest.mark.parametrize('bad',['nan','inf','-1','oops'])
def test_invalid_filter_does_not_replace_saved_conditions(inbox_client,inbox_users,bad):
    before = get_user(load_users(),inbox_users[0].id).listing_filter
    response = inbox_client.post('/inbox/filter',data={'csrf_token':'test_csrf','max_rent':bad})
    assert response.status_code == 302
    assert get_user(load_users(),inbox_users[0].id).listing_filter == before


def test_guests_and_admin_cannot_write_personal_filter(inbox_users,admin_client,guest_client):
    for client in (admin_client,guest_client):
        assert client.post('/inbox/filter',data={'csrf_token':'test_csrf','max_rent':'20'}).status_code == 403


def test_personal_filter_requires_csrf(inbox_client):
    assert inbox_client.post('/inbox/filter',data={'max_rent':'1500'}).status_code == 403


def test_unknown_source_rejected_without_saving(inbox_client,inbox_users):
    before = get_user(load_users(),inbox_users[0].id).listing_filter
    assert inbox_client.post('/inbox/filter',data={'csrf_token':'test_csrf','source':'made-up'}).status_code == 302
    assert get_user(load_users(),inbox_users[0].id).listing_filter == before


def test_legacy_listing_page_and_calendar_use_personal_filter(inbox_client):
    html = inbox_client.get('/listings').get_data(as_text=True)
    assert 'Matching home' in html and 'Outside budget' not in html and 'Different city' not in html
    response = inbox_client.get('/api/calendar')
    assert response.status_code == 200
    assert all(row['id'] == 'match' for row in response.json['listings'])


def test_guest_can_open_real_login_page(guest_client):
    assert guest_client.get('/login').status_code == 200


def test_management_dashboard_stays_admin_only(inbox_users,inbox_client,admin_client):
    assert admin_client.get('/dashboard').status_code == 200
    assert inbox_client.get('/dashboard').status_code == 302


def test_old_matching_listing_not_lost_after_candidate_window(inbox_client):
    from app.db import storage
    st = storage()
    try:
        st.diff([Listing(available_from='2026-12-01',id=f'new-expensive-{i}',name=f'New expensive {i}',city='Eindhoven',
            status='Available to book',price_raw='€5000',features=['Area: 30 m²'],
            url='https://example.com') for i in range(2001)])
    finally:
        st.close()
    html = inbox_client.get('/').get_data(as_text=True)
    assert 'data-listing-id="match"' in html
    assert 'New expensive' not in html


def _unread(client):
    import re
    html = client.get('/').get_data(as_text=True)
    return int(re.search(r'id="inbox-unread-count">(\d+)',html)[1])


def test_opening_inbox_does_not_mark_a_listing_read(inbox_client):
    assert _unread(inbox_client) == 1
    assert _unread(inbox_client) == 1
    html = inbox_client.get('/?view=unread').get_data(as_text=True)
    assert 'data-listing-id="match"' in html
    assert 'class="mail-detail-empty"' in html


def test_explicit_read_is_persistent_and_idempotent(inbox_client,inbox_users,test_app):
    alice = inbox_users[0]
    response = inbox_client.post('/inbox/read/match',data={'csrf_token':'test_csrf'},headers={'Accept':'application/json'})
    assert response.status_code == 200
    assert response.json['unread_count'] == 0
    from app.db import storage
    st = storage()
    try:
        first_time = st.conn.execute('SELECT read_at FROM listing_reads WHERE user_id=?',(alice.id,)).fetchone()[0]
    finally:
        st.close()
    assert inbox_client.post('/inbox/read/match',data={'csrf_token':'test_csrf'}).status_code == 302
    assert _unread(inbox_client) == 0
    # A new browser session uses the same database state, rather than localStorage.
    other_browser = test_app.test_client()
    with other_browser.session_transaction() as sess:
        sess.update(authenticated=True,role='user',user_id=alice.id)
    assert _unread(other_browser) == 0
    st = storage()
    try:
        assert st.conn.execute('SELECT read_at FROM listing_reads WHERE user_id=?',(alice.id,)).fetchone()[0] == first_time
    finally:
        st.close()
    assert 'data-listing-id=' not in inbox_client.get('/?view=unread').get_data(as_text=True)
    assert 'data-listing-id="match"' in inbox_client.get('/').get_data(as_text=True)


def test_read_state_is_isolated_between_users(inbox_client,inbox_users,test_app):
    inbox_client.post('/inbox/read/match',data={'csrf_token':'test_csrf'})
    bob_client = test_app.test_client()
    with bob_client.session_transaction() as sess:
        sess.update(authenticated=True,role='user',user_id=inbox_users[1].id)
    assert _unread(bob_client) == 4


def test_read_requires_personal_visibility_and_csrf(inbox_client,inbox_users,guest_client,admin_client):
    assert inbox_client.post('/inbox/read/match').status_code == 403
    assert inbox_client.post('/inbox/read/expensive',data={'csrf_token':'test_csrf'}).status_code == 404
    assert inbox_client.post('/inbox/read/missing',data={'csrf_token':'test_csrf'}).status_code == 404
    for client in (guest_client,admin_client):
        assert client.post('/inbox/read/match',data={'csrf_token':'test_csrf'}).status_code == 403
        assert client.post('/inbox/read-all',data={'csrf_token':'test_csrf'}).status_code == 403
        assert client.get('/?view=unread').status_code == 403
    assert _unread(inbox_client) == 1


def test_mark_all_read_respects_current_search_and_platform(inbox_client,inbox_users):
    # All scope changes still start from Alice's personal filter.
    response = inbox_client.post('/inbox/read-all',data={'csrf_token':'test_csrf','view':'plaza'})
    assert response.status_code == 302 and _unread(inbox_client) == 1
    inbox_client.post('/inbox/read-all',data={'csrf_token':'test_csrf','q':'not present'})
    assert _unread(inbox_client) == 1
    inbox_client.post('/inbox/read-all',data={'csrf_token':'test_csrf','view':'holland2stay','q':'Matching'})
    assert _unread(inbox_client) == 0


def test_filter_changes_retain_previous_reads_and_new_matches_are_unread(inbox_client,inbox_users):
    inbox_client.post('/inbox/read/match',data={'csrf_token':'test_csrf'})
    inbox_client.post('/inbox/filter',data={'csrf_token':'test_csrf','max_rent':'2000'})
    assert _unread(inbox_client) == 3
    inbox_client.post('/inbox/filter',data={'csrf_token':'test_csrf','max_rent':'1000','min_area':'25','city':'Eindhoven'})
    assert _unread(inbox_client) == 0
    from app.db import storage
    st = storage()
    try:
        # Status changes are not a new notification/read event.
        st.conn.execute("UPDATE listings SET status='Reserved' WHERE id='match'")
        st.conn.commit()
    finally:
        st.close()
    assert _unread(inbox_client) == 0


def test_deleting_user_or_listing_cleans_read_state(inbox_client,inbox_users):
    from app.db import storage
    from users import update_users
    inbox_client.post('/inbox/read/match',data={'csrf_token':'test_csrf'})
    update_users(lambda users: users.__setitem__(slice(None),[u for u in users if u.id != inbox_users[0].id]))
    st = storage()
    try:
        assert st.conn.execute('SELECT COUNT(*) FROM listing_reads').fetchone()[0] == 0
        assert st.mark_listings_read(inbox_users[1].id,['match']) == 1
        st.conn.execute("DELETE FROM listings WHERE id='match'")
        st.conn.commit()
        assert st.conn.execute('SELECT COUNT(*) FROM listing_reads').fetchone()[0] == 0
    finally:
        st.close()


def test_platform_search_uses_display_name(inbox_client):
    from app.db import storage
    st = storage()
    try:
        st.diff([Listing(id='student-match',name='Matching studio',city='Eindhoven',
            source='studentexperience',status='Available to book',price_raw='€950',
            available_from='2026-12-01',features=['Area: 30 m²'],url='https://example.com/studio')])
    finally:
        st.close()
    html = inbox_client.get('/?q=Student+Experience').get_data(as_text=True)
    assert 'data-listing-id="student-match"' in html
    assert 'data-listing-id="match"' not in html


def test_read_history_export_is_scoped_to_owner(inbox_client,inbox_users):
    inbox_client.post('/inbox/read/match',data={'csrf_token':'test_csrf'})
    from app.db import storage
    st = storage()
    try:
        assert [r['listing_id'] for r in st.export_user_data(inbox_users[0].id)['listing_reads']] == ['match']
        assert st.export_user_data(inbox_users[1].id)['listing_reads'] == []
    finally:
        st.close()


def test_detail_renders_full_features_and_shares_the_client_schema(inbox_client):
    import json,re
    from app.db import storage
    features = ['Type: Studio','Area: 30 m²','Floor: 0','Contract: Indefinite',
        'Service costs amount: €50','Deposit: €0','New field: <script>alert(1)</script>']
    st = storage()
    try:
        st.conn.execute('UPDATE listings SET features=? WHERE id=?',(json.dumps(features),'match'))
        st.conn.commit()
    finally:
        st.close()
    inbox_client.set_cookie('h2s-lang','zh')
    html = inbox_client.get('/?selected=match').get_data(as_text=True)
    payload = json.loads(re.search(r'id="mail-items">(.*?)</script>',html,re.S)[1])
    sections = payload[0]['detail_sections']
    fields = {field['label']:field['value'] for section in sections for field in section['fields']}
    assert fields['押金'] == '€0' and fields['楼层'] == '0' and fields['合同类型'] == 'Indefinite'
    assert fields['New field'] == '<script>alert(1)</script>'
    assert '合同类型' in html and '服务费' in html and '最后出现' in html
    assert '<script>alert(1)</script>' not in html
