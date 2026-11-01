"""A later stage must not abandon bookings already sent to a platform."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from booker import BookingResult
from models import Listing
from mcore import booking_round, post_round


class Storage:
    def diff(self, fresh):
        return list(fresh), []
    def set_meta(self, *args):
        pass
    def get_meta(self, *args, **kwargs):
        return ''
    def retire_stale_pending(self):
        pass
    def count_all(self):
        return 1


class Prewarm:
    cached = {}
    futures = {}
    def heartbeat(self):
        pass
    def start_for_candidates(self, ids):
        pass
    async def wait_for_candidates(self):
        pass
    async def stash_pending(self):
        pass


@pytest.mark.parametrize('failure_stage', ['new', 'status', 'submit', 'stash', 'prewarm', 'count'])
def test_booking_results_are_processed_after_another_stage_fails(monkeypatch, failure_stage):
    processed = []
    calls = []
    monkeypatch.setattr(post_round, 'merge_pending_events', lambda *args: 0)
    monkeypatch.setattr(post_round, 'drop_shadow_sources', lambda cfg, new, changed, **kw: (new, changed))
    monkeypatch.setattr(post_round, 'collect_booking_candidates', lambda *args: ({}, {}))
    monkeypatch.setattr(post_round, 'should_notify_internal', lambda: False)
    monkeypatch.setattr(post_round.runtime.retry_queue, 'save', lambda st: calls.append('save'))
    def submit(*args, submitted, **kwargs):
        submitted.append('booking already submitted')
        if failure_stage == 'submit':
            raise RuntimeError('executor error')
    monkeypatch.setattr(post_round, 'submit_bookings', submit)
    async def new(*args, **kwargs):
        calls.append('new')
        if failure_stage == 'new':
            raise RuntimeError('new notification error')
        return 0, []
    async def status(*args, **kwargs):
        calls.append('status')
        if failure_stage == 'status':
            raise RuntimeError('status notification error')
        return []
    async def results(futures, *args, **kwargs):
        processed.extend(futures)
        return []
    monkeypatch.setattr(post_round, 'notify_new_listings', new)
    monkeypatch.setattr(post_round, 'notify_status_changes', status)
    monkeypatch.setattr(post_round, 'process_booking_results', results)
    prewarm = Prewarm()
    storage = Storage()
    if failure_stage == 'stash':
        prewarm.stash_pending = AsyncMock(side_effect=RuntimeError('prewarm error'))
    if failure_stage == 'prewarm':
        prewarm.heartbeat = lambda: (_ for _ in ()).throw(RuntimeError('heartbeat error'))
    if failure_stage == 'count':
        storage.count_all = lambda: (_ for _ in ()).throw(RuntimeError('count error'))
    async def run():
        return await post_round.run_post_scrape(
            SimpleNamespace(), storage, [], [object()], {'city': True},
            web_notifier=None, dry_run=False, booking_deadline=float('inf'),
            loop=asyncio.get_running_loop(), prewarm=prewarm,
        )
    result = asyncio.run(run())
    assert processed == ['booking already submitted']
    assert calls == ['new', 'status', 'save']
    assert result == ({'city': True} if failure_stage in {'stash', 'prewarm', 'count'} else {})


def test_one_user_notification_error_does_not_block_booking_push_or_other_user(monkeypatch):
    room = Listing(id='L', name='L', status='Available to book', price_raw=None,
                   available_from='', features=[], url='', source='holland2stay')
    first = SimpleNamespace(id='u1', name='first')
    second = SimpleNamespace(id='u2', name='second')
    class BadNotifier:
        async def send_booking_success(self, *args):
            raise RuntimeError('channel down')
    good = SimpleNamespace(send_booking_success=AsyncMock(return_value=True))
    push = SimpleNamespace(dispatch=AsyncMock(return_value=1))
    class BrokenStorage:
        def mark_listing_reserved_after_booking(self, lid):
            raise OSError('database unavailable')
    async def run():
        loop = asyncio.get_running_loop()
        futures = []
        for user, notifier in ((first, BadNotifier()), (second, good)):
            future = loop.create_future()
            future.set_result(BookingResult(listing=room, success=True, message='ok',
                                            pay_url='https://example.test/pay', phase='success'))
            futures.append((user, notifier, [room], future, None))
        return await booking_round.process_booking_results(futures, None, BrokenStorage(), push)
    assert asyncio.run(run()) == []
    assert push.dispatch.await_count == 2
    assert good.send_booking_success.await_count == 1


def test_partial_executor_failure_preserves_submitted_futures(monkeypatch):
    first = Listing(id='1', name='1', status='Available to book', price_raw='€1',
                    available_from='', features=[], url='', source='plaza')
    second = Listing(id='2', name='2', status='Available to book', price_raw='€1',
                     available_from='', features=[], url='', source='plaza')
    user = SimpleNamespace(id='u', name='user', auto_book=SimpleNamespace(enabled=True, dry_run=True))
    monkeypatch.setattr(booking_round, 'book_with_fallback', lambda *args, **kw: None)
    async def run():
        loop = asyncio.get_running_loop()
        original = loop.run_in_executor
        n = 0
        def executor(*args):
            nonlocal n
            n += 1
            if n == 2:
                raise RuntimeError('executor stopped')
            return original(*args)
        monkeypatch.setattr(loop, 'run_in_executor', executor)
        submitted = []
        with pytest.raises(RuntimeError, match='executor stopped'):
            booking_round.submit_bookings(loop, {'u': [first, second]}, [(user, object())],
                                          {}, {}, {}, float('inf'), submitted=submitted)
        assert len(submitted) == 1
        assert isinstance(submitted[0], booking_round.BookingJob)
        assert submitted[0].user is user and submitted[0].future is submitted[0][3]
        assert await submitted[0].future is None
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['blocked', 'operation_rejected'])
def test_grouped_booking_errors_do_not_block_other_recipients(monkeypatch, phase):
    room = Listing(id='L', name='L', status='Available to book', price_raw=None,
                   available_from='', features=[], url='', source='holland2stay')
    users = [SimpleNamespace(id='u1', name='first'), SimpleNamespace(id='u2', name='second')]
    bad = SimpleNamespace(send_booking_failed=AsyncMock(side_effect=RuntimeError('channel down')))
    good = SimpleNamespace(send_booking_failed=AsyncMock(return_value=True))
    push = SimpleNamespace(dispatch=AsyncMock(return_value=1), dispatch_admin=AsyncMock(return_value=1))
    web = SimpleNamespace(send_error=AsyncMock(side_effect=RuntimeError('web down')))
    monkeypatch.setattr(booking_round, 'should_notify_block', lambda: True)
    monkeypatch.setattr(booking_round, 'should_notify_operation_rejected', lambda: True)
    monkeypatch.setattr(booking_round, 'mark_h2s_login_blocked', lambda *args: None)
    async def run():
        loop = asyncio.get_running_loop()
        bookings = []
        for user, notifier in zip(users, (bad, good), strict=True):
            future = loop.create_future()
            future.set_result(BookingResult(listing=room, success=False, message='error', phase=phase))
            bookings.append((user, notifier, [room], future, None))
        tasks = await booking_round.process_booking_results(bookings, web, Storage(), push)
        await asyncio.gather(*tasks)
    asyncio.run(run())
    assert good.send_booking_failed.await_count == 1
    assert push.dispatch.await_count == 2
    assert push.dispatch_admin.await_count == 1


def test_new_listing_notification_failure_stays_pending_while_booking_is_processed(tmp_path, monkeypatch):
    from storage import Storage as RealStorage
    listing = Listing(id='L', name='L', status='Available to book', price_raw=None,
                      available_from='', features=[], url='', city='Eindhoven', source='holland2stay')
    web = SimpleNamespace(send_new_listing=AsyncMock(side_effect=RuntimeError('feed down')))
    monkeypatch.setattr(post_round, 'collect_booking_candidates', lambda *args: ({}, {}))
    monkeypatch.setattr(post_round, 'should_notify_internal', lambda: False)
    seen = []
    def submit(*args, submitted, **kwargs):
        submitted.append('submitted')
    async def results(futures, *args, **kwargs):
        seen.extend(futures)
        return []
    monkeypatch.setattr(post_round, 'submit_bookings', submit)
    monkeypatch.setattr(post_round, 'process_booking_results', results)
    st = RealStorage(tmp_path / 'round.db')
    async def run():
        return await post_round.run_post_scrape(
            SimpleNamespace(), st, [], [listing], {'Eindhoven': True},
            web_notifier=web, dry_run=False, booking_deadline=float('inf'),
            loop=asyncio.get_running_loop(), prewarm=Prewarm(),
        )
    try:
        assert asyncio.run(run()) == {}
        assert seen == ['submitted']
        assert [item.id for item in st.pending_new_listings()] == ['L']
    finally:
        st.close()


def test_post_scrape_passes_each_stage_output_to_next_stage(monkeypatch):
    events = post_round.RoundEvents([object()], [])
    bookings = post_round.BookingSubmissions(['submitted'], [])
    delivery = post_round.DeliveryResult(1, [])
    calls = []
    def prepare(cfg, storage, fresh, *, dry_run):
        calls.append('prepare')
        return events
    async def submit(got_events, fresh, user_notifiers, storage, **kwargs):
        assert got_events is events
        calls.append('submit')
        return bookings
    async def deliver(got_events, got_bookings, user_notifiers, storage, web_notifier, push):
        assert got_events is events and got_bookings is bookings
        calls.append('deliver')
        return delivery
    def finish(storage, got_events, got_delivery):
        assert got_events is events and got_delivery is delivery
        calls.append('finish')
    monkeypatch.setattr(post_round, 'prepare_round_events', prepare)
    monkeypatch.setattr(post_round, 'submit_round_bookings', submit)
    monkeypatch.setattr(post_round, 'deliver_round_events', deliver)
    monkeypatch.setattr(post_round, 'finish_round', finish)
    async def run():
        return await post_round.run_post_scrape(
            object(), Storage(), [], [], {'city': True}, web_notifier=None,
            dry_run=False, booking_deadline=10, loop=asyncio.get_running_loop(), prewarm=Prewarm(),
        )
    assert asyncio.run(run()) == {'city': True}
    assert calls == ['prepare', 'submit', 'deliver', 'finish']


def test_replayed_events_are_merged_before_shadow_filter(monkeypatch):
    calls = []
    class EventStorage(Storage):
        def diff(self, fresh):
            calls.append('diff')
            return ['new'], []
        def retire_stale_pending(self):
            calls.append('retire')
    def replay(storage, new, changed):
        calls.append('replay')
        new.append('pending')
        return 1
    def shadow(cfg, new, changed, **kwargs):
        calls.append('shadow')
        assert new == ['new', 'pending']
        return ['new'], changed
    monkeypatch.setattr(post_round, 'merge_pending_events', replay)
    monkeypatch.setattr(post_round, 'drop_shadow_sources', shadow)
    events = post_round.prepare_round_events(object(), EventStorage(), [], dry_run=False)
    assert calls == ['diff', 'replay', 'retire', 'shadow']
    assert events.new_listings == ['new']


@pytest.mark.parametrize('failure_stage', ['new', 'status'])
def test_push_tasks_survive_notification_stage_error(monkeypatch, failure_stage):
    room = Listing(id='L', name='L', status='Available to book', price_raw=None,
                   available_from='', features=[], url='', source='plaza')
    user = SimpleNamespace(id='u1', name='u1', listing_filter=SimpleNamespace(is_empty=lambda: True))
    notifier = SimpleNamespace(send_new_listing=AsyncMock(return_value=True),
                               send_status_change=AsyncMock(return_value=True))
    push = SimpleNamespace(get_client=lambda: object(), get_fcm_client=lambda: None,
                           should_aggregate=lambda count: False,
                           dispatch=AsyncMock(return_value=1),
                           dispatch_status_change=AsyncMock(return_value=1))
    class EventStorage:
        def mark_notified_batch(self, ids):
            if failure_stage == 'new':
                raise OSError('cannot mark new events')
        def mark_status_change_notified_batch(self, ids):
            pass
    web = SimpleNamespace(send_new_listing=AsyncMock(), send_status_change=AsyncMock(
        side_effect=RuntimeError('feed down') if failure_stage == 'status' else None))
    events = post_round.RoundEvents([room] if failure_stage == 'new' else [],
                                   [(room, 'old', 'new', 1)] if failure_stage == 'status' else [])
    async def bookings(*args, **kwargs):
        return []
    monkeypatch.setattr(post_round, 'process_booking_results', bookings)
    result = asyncio.run(post_round.deliver_round_events(
        events, post_round.BookingSubmissions(), [(user, notifier)], EventStorage(), web, push,
    ))
    assert len(result.errors) == 1
    if failure_stage == 'new':
        assert push.dispatch.await_count == 1
    else:
        assert push.dispatch_status_change.await_count == 1
