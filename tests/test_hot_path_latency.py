import pytest
from autosport.hot_path_latency import STAGES, HotPathPolicy, HotPathError, run_hot_path

SHA = "a" * 40
POLICY = HotPathPolicy(stage_budget_ns=10, total_budget_ns=100, max_backlog=2, max_source_age_ns=100)


def make_callbacks(calls):
    return {name: (lambda n=name: calls.append(n)) for name in STAGES}


def clock(*ticks):
    it = iter(ticks)
    return lambda: next(it)


def test_success_is_observational_and_bounded():
    calls = []
    t = (100, 100, 105, 105, 110, 110, 115, 115, 120, 120, 125)
    r = run_hot_path(source_sha=SHA, policy=POLICY, observed_at_ns=100, backlog=1, stages=make_callbacks(calls), clock_ns=clock(*t))
    assert calls == list(STAGES)
    assert r.disposition == "OK" and r.total_elapsed_ns == 25
    assert r.stage_latencies_ns == tuple((n, 5) for n in STAGES)
    assert not r.execution_authority and not r.target_machine_acceptance


def test_overload_short_circuits_all_stage_effects():
    calls = []
    r = run_hot_path(source_sha=SHA, policy=POLICY, observed_at_ns=100, backlog=3, stages=make_callbacks(calls), clock_ns=clock(100))
    assert (r.disposition, r.reason, calls) == ("WAIT", "BACKLOG", [])


def test_stale_source_stops_before_ingest():
    calls = []
    r = run_hot_path(source_sha=SHA, policy=POLICY, observed_at_ns=0, backlog=0, stages=make_callbacks(calls), clock_ns=clock(200, 200))
    assert (r.reason, calls) == ("STALE_SOURCE", [])


def test_stage_breach_prevents_downstream_decision():
    calls = []
    r = run_hot_path(source_sha=SHA, policy=POLICY, observed_at_ns=100, backlog=0, stages=make_callbacks(calls), clock_ns=clock(100, 100, 111))
    assert (r.reason, calls) == ("STAGE_BUDGET", ["ingest"])


def test_total_breach_prevents_next_stage():
    calls = []
    p = HotPathPolicy(stage_budget_ns=20, total_budget_ns=5, max_backlog=0, max_source_age_ns=100)
    r = run_hot_path(source_sha=SHA, policy=p, observed_at_ns=100, backlog=0, stages=make_callbacks(calls), clock_ns=clock(100, 100, 106))
    assert r.reason == "TOTAL_BUDGET" and calls == ["ingest"]


def test_freshness_expires_between_stages():
    calls = []
    p = HotPathPolicy(stage_budget_ns=10, total_budget_ns=100, max_backlog=0, max_source_age_ns=5)
    r = run_hot_path(source_sha=SHA, policy=p, observed_at_ns=100, backlog=0, stages=make_callbacks(calls), clock_ns=clock(100, 100, 106, 106))
    assert calls == ["ingest"] and r.reason == "STALE_SOURCE"


@pytest.mark.parametrize("value", [True, -1, 1.2, "3"])
def test_invalid_backlog_fails_before_effect(value):
    calls = []
    with pytest.raises(HotPathError):
        run_hot_path(source_sha=SHA, policy=POLICY, observed_at_ns=0, backlog=value, stages=make_callbacks(calls))
    assert not calls


def test_exception_secret_is_not_echoed_or_chained():
    calls = make_callbacks([])
    def fail():
        raise RuntimeError("token=SENSITIVE_TEST_CANARY")
    calls["ingest"] = fail
    with pytest.raises(HotPathError) as e:
        run_hot_path(source_sha=SHA, policy=POLICY, observed_at_ns=100, backlog=0, stages=calls, clock_ns=clock(100, 100))
    assert "CANARY" not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_backward_monotonic_fails_closed_no_decision():
    calls = []
    with pytest.raises(HotPathError, match="backwards"):
        run_hot_path(source_sha=SHA, policy=POLICY, observed_at_ns=100, backlog=0, stages=make_callbacks(calls), clock_ns=clock(100, 100, 99))
    assert calls == ["ingest"]


def test_invalid_source_revision_and_callback_order_fail_before_effect():
    calls = []
    for src in ("A" * 40, "a" * 39, "z" * 40):
        with pytest.raises(HotPathError):
            run_hot_path(source_sha=src, policy=POLICY, observed_at_ns=0, backlog=0, stages=make_callbacks(calls))
    with pytest.raises(HotPathError):
        run_hot_path(source_sha=SHA, policy=POLICY, observed_at_ns=0, backlog=0, stages=dict(reversed(list(make_callbacks(calls).items()))))
    assert not calls
