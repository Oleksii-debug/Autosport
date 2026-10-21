"""Plan-5 Section-7: dynamic backlog gate must not trust only cycle-entry state."""

import pytest
from autosport.hot_path_latency import STAGES, HotPathError, HotPathPolicy, run_hot_path


def _steps(calls):
    return {name: (lambda n=name: calls.append(n)) for name in STAGES}


def test_backlog_rise_midcycle_blocks_downstream_decision():
    calls = []
    backlog = [0]
    steps = _steps(calls)

    def ingest():
        calls.append("ingest")
        backlog[0] = 50

    steps["ingest"] = ingest
    ticks = iter((100, 100, 101, 101, 102, 102, 103, 103, 104, 104, 105))
    report = run_hot_path(
        source_sha="a" * 40,
        policy=HotPathPolicy(10, 100, 2, 100),
        observed_at_ns=100,
        backlog=0,
        stages=steps,
        clock_ns=lambda: next(ticks),
        backlog_reader=lambda: backlog[0],
    )
    assert (report.disposition, report.reason) == ("WAIT", "BACKLOG")
    assert report.backlog == 50
    assert calls == ["ingest"]
    assert report.execution_authority is False


@pytest.mark.parametrize("sample", [True, -1, "3", None])
def test_invalid_dynamic_backlog_is_fail_closed_without_callbacks(sample):
    calls = []
    with pytest.raises(HotPathError, match="backlog sample"):
        run_hot_path(
            source_sha="a" * 40,
            policy=HotPathPolicy(10, 100, 2, 100),
            observed_at_ns=100,
            backlog=0,
            stages=_steps(calls),
            clock_ns=lambda: 100,
            backlog_reader=lambda: sample,
        )
    assert calls == []


def test_backlog_sampler_exception_is_redacted_and_does_not_retry():
    calls, reads = [], []

    def hostile():
        reads.append(1)
        raise RuntimeError("PRIVATE_API_KEY_SECRET_CANARY")

    with pytest.raises(HotPathError, match="backlog sampling unavailable") as captured:
        run_hot_path(
            source_sha="a" * 40,
            policy=HotPathPolicy(10, 100, 2, 100),
            observed_at_ns=100,
            backlog=0,
            stages=_steps(calls),
            clock_ns=lambda: 100,
            backlog_reader=hostile,
        )
    assert "CANARY" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert reads == [1] and calls == []


def test_previous_high_pressure_cannot_be_cleared_before_final_decision():
    calls = []
    samples = iter((0, 99, 0))
    ticks = iter((100, 100, 101, 101, 102, 102, 103, 103, 104, 104, 105))
    report = run_hot_path(
        source_sha="a" * 40,
        policy=HotPathPolicy(10, 100, 2, 100),
        observed_at_ns=100,
        backlog=0,
        stages=_steps(calls),
        clock_ns=lambda: next(ticks),
        backlog_reader=lambda: next(samples),
    )
    assert report.disposition == "WAIT" and report.reason == "BACKLOG"
    assert report.backlog == 99
    assert calls == ["ingest"]


@pytest.mark.parametrize(
    ("max_age", "total_budget", "expected"),
    [
        (50, 500, "STALE_SOURCE"),
        (500, 50, "TOTAL_BUDGET"),
    ],
)
def test_costly_backlog_sample_cannot_start_stale_decision(max_age, total_budget, expected):
    """A slow dynamic pressure probe must not bypass the pre-decision time gate."""
    calls = []
    reads = [0]
    now_ns = [100]

    def costly_backlog_probe():
        reads[0] += 1
        # Four earlier stages have two probes each. The ninth probe is
        # immediately before decision, after its prior timestamp was sampled.
        if reads[0] == 9:
            now_ns[0] = 200
        return 0

    result = run_hot_path(
        source_sha="a" * 40,
        policy=HotPathPolicy(10, total_budget, 2, max_age),
        observed_at_ns=100,
        backlog=0,
        stages=_steps(calls),
        clock_ns=lambda: now_ns[0],
        backlog_reader=costly_backlog_probe,
    )
    assert reads == [9]
    assert result.disposition == "WAIT" and result.reason == expected
    assert calls == list(STAGES[:-1])
    assert result.execution_authority is False


def test_slow_backlog_probe_cannot_start_stale_intermediate_stage():
    """Regression: mirror must not begin after a slow, zero-pressure probe."""
    calls = []
    now_ns = [100]
    probes = [0]

    def sample_backlog():
        probes[0] += 1
        # ingest has two probes; the third precedes mirror.
        if probes[0] == 3:
            now_ns[0] = 300
        return 0

    result = run_hot_path(
        source_sha="a" * 40,
        policy=HotPathPolicy(500, 500, 2, 50),
        observed_at_ns=100,
        backlog=0,
        stages=_steps(calls),
        clock_ns=lambda: now_ns[0],
        backlog_reader=sample_backlog,
    )
    assert probes == [3]
    assert calls == ["ingest"]
    assert result.disposition == "WAIT" and result.reason == "STALE_SOURCE"
    assert result.total_elapsed_ns == 200
    assert result.execution_authority is False


def test_slow_final_backlog_probe_cannot_forge_ok_after_deadline():
    """Regression: final probe time counts even when pressure stays zero."""
    calls = []
    now_ns = [100]
    probes = [0]

    def sample_backlog():
        probes[0] += 1
        if probes[0] == 10:
            now_ns[0] = 300
        return 0

    result = run_hot_path(
        source_sha="a" * 40,
        policy=HotPathPolicy(500, 500, 2, 50),
        observed_at_ns=100,
        backlog=0,
        stages=_steps(calls),
        clock_ns=lambda: now_ns[0],
        backlog_reader=sample_backlog,
    )
    assert probes == [10]
    assert calls == list(STAGES)
    assert result.disposition == "WAIT" and result.reason == "STALE_SOURCE"
    assert result.total_elapsed_ns == 200
    assert result.execution_authority is False


def test_slow_pre_stage_overload_probe_is_counted_in_wait_elapsed():
    """A slow sampler must not backdate the evidence to before sampling."""
    now_ns = [100]
    calls = []

    def overloaded():
        now_ns[0] = 300
        return 5

    report = run_hot_path(
        source_sha="a" * 40,
        policy=HotPathPolicy(1000, 1000, 2, 1000),
        observed_at_ns=100,
        backlog=0,
        stages=_steps(calls),
        clock_ns=lambda: now_ns[0],
        backlog_reader=overloaded,
    )
    assert (report.disposition, report.reason) == ("WAIT", "BACKLOG")
    assert report.total_elapsed_ns == 200
    assert report.backlog == 5
    assert calls == []
    assert not report.execution_authority


def test_slow_post_stage_overload_probe_is_counted_in_wait_elapsed():
    """A dynamic overload after ingest must include sampler cost, not only callback cost."""
    now_ns = [100]
    calls = []
    samples = [0]

    def overloaded_after_ingest():
        samples[0] += 1
        if samples[0] == 2:
            now_ns[0] = 350
            return 5
        return 0

    report = run_hot_path(
        source_sha="a" * 40,
        policy=HotPathPolicy(1000, 1000, 2, 1000),
        observed_at_ns=100,
        backlog=0,
        stages=_steps(calls),
        clock_ns=lambda: now_ns[0],
        backlog_reader=overloaded_after_ingest,
    )
    assert samples == [2]
    assert (report.disposition, report.reason) == ("WAIT", "BACKLOG")
    assert report.total_elapsed_ns == 250
    assert report.backlog == 5
    assert calls == ["ingest"]
    assert not report.execution_authority
