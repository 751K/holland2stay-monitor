"""The shared dispatch must preserve per-platform failure and recovery behavior."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from mcore import scrape_round
from scrapers.base import BlockedError, ProxyError, RateLimitError, ScrapeTask


def _task(source, city="City"):
    return ScrapeTask(source=source, city_key=city, city_display=city)


def _run(tasks, *, dry_run=False):
    async def execute():
        return await scrape_round.dispatch_with_h2s_circuit(
            asyncio.get_running_loop(), tasks, storage=object(),
            source_totals={task.source: 1 for task in tasks},
            multi_source=True, dry_run=dry_run,
        )

    return asyncio.run(execute())


@pytest.fixture
def dispatch(monkeypatch):
    circuits = Mock()
    circuits.plan.return_value = ("normal", 1)
    pacing = Mock()
    monkeypatch.setattr(scrape_round.runtime, "source_circuits", circuits)
    monkeypatch.setattr(scrape_round.runtime, "source_pacing", pacing)
    invoke = AsyncMock(return_value=([object()], {}))
    monkeypatch.setattr(scrape_round, "dispatch_scrape_tasks_async", invoke)
    telemetry = Mock()
    monkeypatch.setattr(scrape_round, "record_source_round", telemetry)
    proxy = Mock()
    monkeypatch.setattr(scrape_round, "report_source_proxy_failure", proxy)
    return circuits, pacing, invoke, telemetry, proxy


@pytest.mark.parametrize("source", ["holland2stay", "xior", "plaza"])
def test_success_uses_source_executor_and_relaxes_pacing(source, dispatch):
    _, pacing, invoke, telemetry, _ = dispatch
    fresh, _, blocked, _ = _run([_task(source)])
    assert len(fresh) == 1 and blocked is None
    assert invoke.await_args.kwargs["isolated"] is (source in {"holland2stay", "xior"})
    assert invoke.await_args.kwargs["browser_source"] == source
    pacing.relax.assert_called_once()
    assert pacing.relax.call_args.args[0] == source
    assert telemetry.call_args.kwargs["source"] == source


@pytest.mark.parametrize("source", ["holland2stay", "xior"])
def test_proxy_failure_is_reported_even_when_another_source_succeeds(source, dispatch):
    _, _, invoke, telemetry, proxy = dispatch

    async def scrape(loop, selected, **kwargs):
        if selected[0].source == source:
            raise ProxyError("proxy down")
        return [object()], {}

    invoke.side_effect = scrape
    fresh, completeness, _, _ = _run([_task(source), _task("plaza")])
    assert len(fresh) == 1
    assert completeness[f"{source}:City"] is False
    assert proxy.call_args.args[0] == source
    assert {call.kwargs["source"] for call in telemetry.call_args_list} == {source, "plaza"}


@pytest.mark.parametrize("source", ["holland2stay", "xior"])
def test_rate_limit_penalizes_each_browser_source(source, dispatch):
    _, pacing, invoke, _, _ = dispatch
    invoke.side_effect = RateLimitError("429")
    with pytest.raises(RateLimitError):
        _run([_task(source)])
    assert pacing.penalize.call_args.args[0] == source


def test_h2s_block_invokes_login_protection(dispatch, monkeypatch):
    _, _, invoke, telemetry, _ = dispatch
    invoke.side_effect = BlockedError("403")
    blocked = Mock()
    monkeypatch.setattr(scrape_round, "mark_h2s_scrape_blocked", blocked)
    _, _, error, mode = _run([_task("holland2stay")])
    assert isinstance(error, BlockedError) and mode == "normal"
    blocked.assert_called_once()
    assert telemetry.call_args.kwargs["source"] == "holland2stay"


def test_h2s_canary_success_uses_special_recovery(dispatch, monkeypatch):
    circuits, _, invoke, _, _ = dispatch
    circuits.plan.return_value = ("canary", 1)
    recovered = Mock()
    monkeypatch.setattr(scrape_round, "mark_h2s_scrape_recovered", recovered)
    _, _, _, mode = _run([_task("holland2stay", "one"), _task("holland2stay", "two")])
    assert mode == "canary"
    assert len(invoke.await_args.args[1]) == 1
    recovered.assert_called_once()


def test_other_source_canary_uses_its_own_circuit(dispatch):
    circuits, _, _, _, _ = dispatch
    circuits.plan.side_effect = lambda source, n_tasks: (
        ("canary", 1) if n_tasks else ("none", 0)
    )
    _run([_task("xior", "one"), _task("xior", "two")])
    assert circuits.recover.call_args.args[0] == "xior"


def test_h2s_open_does_not_prevent_other_sources(dispatch):
    circuits, _, invoke, telemetry, _ = dispatch
    circuits.plan.side_effect = lambda source, n_tasks: (
        ("open", 0) if source == "holland2stay" else ("normal", n_tasks)
    )
    circuits.remaining.return_value = 120
    circuits.reason.return_value = "blocked"
    fresh, _, blocked, mode = _run([_task("holland2stay"), _task("plaza")])
    assert len(fresh) == 1 and blocked is None and mode == "open"
    assert invoke.await_count == 1 and invoke.await_args.args[1][0].source == "plaza"
    by_source = {call.kwargs["source"]: call.kwargs for call in telemetry.call_args_list}
    assert by_source["holland2stay"]["error_type"] == scrape_round.CIRCUIT_OPEN_ERROR


def test_dry_run_does_not_report_proxy_failure(dispatch):
    _, _, invoke, telemetry, proxy = dispatch
    invoke.side_effect = ProxyError("proxy down")
    with pytest.raises(ProxyError):
        _run([_task("holland2stay")], dry_run=True)
    proxy.assert_not_called()
    telemetry.assert_not_called()


def test_diagnostic_uses_bounded_browser_dispatch_and_isolates_failures(monkeypatch):
    calls = []

    async def scrape(loop, selected, **kwargs):
        source = selected[0].source
        calls.append((source, kwargs["isolated"]))
        if source == "xior":
            raise ProxyError("proxy down")
        return [object()], {"plaza:City": True}

    monkeypatch.setattr(scrape_round, "dispatch_scrape_tasks_async", scrape)
    fresh, completeness = asyncio.run(
        scrape_round.dispatch_diagnostic([_task("xior"), _task("plaza")])
    )
    assert len(fresh) == 1
    assert completeness == {"xior:City": False, "plaza:City": True}
    assert calls == [("xior", True), ("plaza", False)]


def test_round_exposes_named_scrape_result(dispatch):
    outcome = _run([_task("plaza")])
    assert isinstance(outcome, scrape_round.ScrapeRoundResult)
    assert len(outcome.listings) == 1
    assert outcome.completeness == {} and outcome.h2s_blocked is None
    # Existing tuple consumers remain compatible during the migration.
    listings, completeness, blocked, mode = outcome
    assert listings is outcome.listings and completeness is outcome.completeness
    assert blocked is None and mode == outcome.h2s_mode


def test_failed_source_does_not_replace_an_existing_completeness_value(dispatch):
    _, _, invoke, _, _ = dispatch
    async def scrape(loop, selected, **kwargs):
        if selected[0].source == 'xior':
            raise RateLimitError('limited')
        return [object()], {'City': True}
    invoke.side_effect = scrape
    async def run():
        return await scrape_round.dispatch_with_h2s_circuit(
            asyncio.get_running_loop(), [_task('plaza'), _task('xior')], storage=object(),
            source_totals={'plaza': 1, 'xior': 1}, multi_source=False, dry_run=False,
        )
    result = asyncio.run(run())
    assert result.completeness == {'City': True}
