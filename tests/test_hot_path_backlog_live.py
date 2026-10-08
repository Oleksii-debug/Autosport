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
