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


def test_repeated_windows_are_bounded_without_stored_state():
    calls = []
    for n in range(2000):
        origin = 1000 + n * 100
        ticks = (origin, origin, origin + 1, origin + 1, origin + 2,
                 origin + 2, origin + 3, origin + 3, origin + 4,
                 origin + 4, origin + 5)
        result = run_hot_path(source_sha=SHA, policy=POLICY, observed_at_ns=origin,
                              backlog=0, stages=make_callbacks(calls), clock_ns=clock(*ticks))
        assert result.disposition == "OK" and len(result.stage_latencies_ns) == 5
        assert result.total_elapsed_ns == 5
    assert len(calls) == 10_000


def test_process_restart_fixture_has_same_source_bound_result(tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys

    code = """import json
from autosport.hot_path_latency import STAGES, HotPathPolicy, run_hot_path
sha = 'a' * 40
calls = []
steps = {s: (lambda s=s: calls.append(s)) for s in STAGES}
ticks = iter((100,100,101,101,102,102,103,103,104,104,105))
r = run_hot_path(source_sha=sha, policy=HotPathPolicy(10,100,0,100), observed_at_ns=100, backlog=0, stages=steps, clock_ns=lambda: next(ticks))
print(json.dumps({'sha':r.source_sha,'disposition':r.disposition,'samples':r.stage_latencies_ns,'calls':calls}))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    outputs = []
    for _ in range(2):
        run = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=10, env=env, check=True)
        outputs.append(json.loads(run.stdout))
    assert outputs[0] == outputs[1]
    assert outputs[0]["sha"] == SHA and outputs[0]["calls"] == list(STAGES)


def test_final_stage_expiry_is_wait_not_success():
    """Regression: final stage finishes after the source expiry deadline."""
    calls = []
    p = HotPathPolicy(stage_budget_ns=20, total_budget_ns=100,
                      max_backlog=0, max_source_age_ns=15)
    ticks = (100, 100, 103, 103, 106, 106, 109, 109, 112, 112, 120)
    result = run_hot_path(source_sha=SHA, policy=p, observed_at_ns=100,
                          backlog=0, stages=make_callbacks(calls),
                          clock_ns=clock(*ticks))
    assert calls == list(STAGES)
    assert result.disposition == "WAIT"
    assert result.reason == "STALE_SOURCE"
    assert not result.execution_authority
    assert not result.target_machine_acceptance


def test_last_stage_source_age_boundary_is_inclusive():
    calls = []
    p = HotPathPolicy(stage_budget_ns=20, total_budget_ns=100,
                      max_backlog=0, max_source_age_ns=20)
    ticks = (100, 100, 103, 103, 106, 106, 109, 109, 112, 112, 120)
    result = run_hot_path(source_sha=SHA, policy=p, observed_at_ns=100,
                          backlog=0, stages=make_callbacks(calls),
                          clock_ns=clock(*ticks))
    assert result.disposition == "OK"
    assert result.total_elapsed_ns == 20
    assert calls == list(STAGES)


def test_mutable_caller_policy_cannot_expand_stage_budget_midflight():
    """object.__setattr__ can bypass frozen, but must not change active gates."""
    calls = []
    policy = HotPathPolicy(10, 100, 0, 100)
    stages = make_callbacks(calls)

    def ingest():
        calls.append("ingest")
        object.__setattr__(policy, "stage_budget_ns", 999_999)

    stages["ingest"] = ingest
    result = run_hot_path(
        source_sha=SHA,
        policy=policy,
        observed_at_ns=100,
        backlog=0,
        stages=stages,
        clock_ns=clock(100, 100, 111),
    )
    assert result.disposition == "WAIT"
    assert result.reason == "STAGE_BUDGET"
    assert calls == ["ingest"]
    assert policy.stage_budget_ns == 999_999
    assert not result.execution_authority


def test_mutable_caller_policy_cannot_expand_backlog_threshold_midflight():
    calls = []
    pressure = [0]
    policy = HotPathPolicy(10, 100, 2, 100)
    stages = make_callbacks(calls)

    def ingest():
        calls.append("ingest")
        pressure[0] = 50
        object.__setattr__(policy, "max_backlog", 100_000)

    stages["ingest"] = ingest
    result = run_hot_path(
        source_sha=SHA,
        policy=policy,
        observed_at_ns=100,
        backlog=0,
        stages=stages,
        clock_ns=clock(100, 100, 101),
        backlog_reader=lambda: pressure[0],
    )
    assert result.disposition == "WAIT"
    assert result.reason == "BACKLOG"
    assert result.backlog == 50
    assert calls == ["ingest"]
    assert policy.max_backlog == 100_000
    assert not result.execution_authority


def test_pre_mutated_policy_is_revalidated_before_any_stage():
    calls = []
    policy = HotPathPolicy(10, 100, 2, 100)
    object.__setattr__(policy, "max_backlog", True)
    with pytest.raises(HotPathError, match="max_backlog"):
        run_hot_path(
            source_sha=SHA,
            policy=policy,
            observed_at_ns=100,
            backlog=0,
            stages=make_callbacks(calls),
            clock_ns=clock(100),
        )
    assert calls == []
