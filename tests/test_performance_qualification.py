from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from autosport.performance_qualification import (
    BUDGET_SCHEMA,
    PerformanceBudget,
    PerformanceQualificationError,
    qualify_endurance_report,
)

SOURCE_SHA = "a" * 40


def _report() -> dict[str, object]:
    return {
        "status": "PASS",
        "failures": [],
        "history_events": 20_000,
        "accepted_events_per_second": 5_000.0,
        "peak_traced_memory_bytes": 100_000_000,
        "ingest_elapsed_seconds": 4.0,
        "replay_elapsed_seconds": 1.0,
        "restart_elapsed_seconds": 0.5,
        "real_money_execution": False,
        "stable_invariant_fingerprint": "fixture",
    }


def _budget(**overrides: object) -> PerformanceBudget:
    values: dict[str, object] = {
        "min_history_events": 20_000,
        "min_accepted_events_per_second": 5_000.0,
        "max_peak_traced_memory_bytes": 100_000_000,
        "max_ingest_elapsed_seconds": 4.0,
        "max_replay_elapsed_seconds": 1.0,
        "max_restart_elapsed_seconds": 0.5,
    }
    values.update(overrides)
    return PerformanceBudget(**values)  # type: ignore[arg-type]


def test_exact_boundaries_pass_and_identity_is_deterministic() -> None:
    first = qualify_endurance_report(
        _report(), _budget(), source_sha=SOURCE_SHA, machine_profile="ci-windows-2026"
    )
    second = qualify_endurance_report(
        _report(), _budget(), source_sha=SOURCE_SHA, machine_profile="ci-windows-2026"
    )
    assert first.status == "PASS"
    assert first.failures == ()
    assert first.qualification_id == second.qualification_id
    assert first.target_machine_acceptance is False
    assert len(first.checks) == 6


def test_correctness_failure_cannot_become_performance_pass() -> None:
    report = _report()
    report["status"] = "FAIL"
    report["failures"] = ["integrity mismatch"]
    with pytest.raises(PerformanceQualificationError, match="correctness status must be PASS"):
        qualify_endurance_report(report, _budget(), source_sha=SOURCE_SHA, machine_profile="machine")


def test_empty_budget_is_rejected() -> None:
    with pytest.raises(PerformanceQualificationError, match="at least one metric"):
        PerformanceBudget()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("min_history_events", True),
        ("min_accepted_events_per_second", float("nan")),
        ("max_ingest_elapsed_seconds", float("inf")),
        ("max_peak_traced_memory_bytes", False),
    ],
)
def test_budget_rejects_bool_and_nonfinite_values(field: str, value: object) -> None:
    with pytest.raises(PerformanceQualificationError):
        PerformanceBudget(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("history_events", True),
        ("accepted_events_per_second", float("nan")),
        ("peak_traced_memory_bytes", -1),
        ("ingest_elapsed_seconds", 0.0),
        ("replay_elapsed_seconds", float("inf")),
        ("restart_elapsed_seconds", False),
    ],
)
def test_report_rejects_malformed_metric_values(field: str, value: object) -> None:
    report = _report()
    report[field] = value
    with pytest.raises(PerformanceQualificationError):
        qualify_endurance_report(report, _budget(), source_sha=SOURCE_SHA, machine_profile="machine")


@pytest.mark.parametrize(
    ("budget", "failed_metric"),
    [
        (PerformanceBudget(min_history_events=20_001), "history_events"),
        (PerformanceBudget(min_accepted_events_per_second=5_001), "accepted_events_per_second"),
        (PerformanceBudget(max_peak_traced_memory_bytes=99_999_999), "peak_traced_memory_bytes"),
        (PerformanceBudget(max_ingest_elapsed_seconds=3.9), "ingest_elapsed_seconds"),
        (PerformanceBudget(max_replay_elapsed_seconds=0.9), "replay_elapsed_seconds"),
        (PerformanceBudget(max_restart_elapsed_seconds=0.4), "restart_elapsed_seconds"),
    ],
)
def test_each_budget_dimension_fails_closed(
    budget: PerformanceBudget, failed_metric: str
) -> None:
    result = qualify_endurance_report(
        _report(), budget, source_sha=SOURCE_SHA, machine_profile="machine"
    )
    assert result.status == "FAIL"
    assert result.failures == (failed_metric,)


def test_identity_changes_with_source_profile_budget_or_report() -> None:
    baseline = qualify_endurance_report(
        _report(), _budget(), source_sha=SOURCE_SHA, machine_profile="machine-a"
    )
    changed_source = qualify_endurance_report(
        _report(), _budget(), source_sha="b" * 40, machine_profile="machine-a"
    )
    changed_profile = qualify_endurance_report(
        _report(), _budget(), source_sha=SOURCE_SHA, machine_profile="machine-b"
    )
    changed_budget = qualify_endurance_report(
        _report(), _budget(max_ingest_elapsed_seconds=4.1), source_sha=SOURCE_SHA, machine_profile="machine-a"
    )
    changed_report_data = _report()
    changed_report_data["stable_invariant_fingerprint"] = "changed"
    changed_report = qualify_endurance_report(
        changed_report_data, _budget(), source_sha=SOURCE_SHA, machine_profile="machine-a"
    )
    identities = {
        baseline.qualification_id,
        changed_source.qualification_id,
        changed_profile.qualification_id,
        changed_budget.qualification_id,
        changed_report.qualification_id,
    }
    assert len(identities) == 5


def test_pass_with_nonempty_failures_is_rejected() -> None:
    report = _report()
    report["failures"] = ["hidden"]
    with pytest.raises(PerformanceQualificationError, match="empty failures"):
        qualify_endurance_report(report, _budget(), source_sha=SOURCE_SHA, machine_profile="machine")


def test_malformed_source_sha_is_rejected() -> None:
    with pytest.raises(PerformanceQualificationError, match="40-character"):
        qualify_endurance_report(_report(), _budget(), source_sha="ABC", machine_profile="machine")


def test_budget_from_dict_rejects_unknown_fields() -> None:
    raw = {
        "schema": BUDGET_SCHEMA,
        "schema_version": 1,
        "min_history_events": 1,
        "surprise": 2,
    }
    with pytest.raises(PerformanceQualificationError, match="unexpected fields"):
        PerformanceBudget.from_dict(raw)


def test_script_returns_zero_for_pass_and_five_for_budget_failure(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "qualify_endurance_performance.py"
    report_path = tmp_path / "report.json"
    budget_path = tmp_path / "budget.json"
    output_path = tmp_path / "qualification.json"
    report_path.write_text(json.dumps(_report(), allow_nan=False), encoding="utf-8")
    budget_path.write_text(json.dumps(_budget().to_dict(), allow_nan=False), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")

    command = [
        sys.executable,
        str(script),
        str(report_path),
        str(budget_path),
        "--source-sha",
        SOURCE_SHA,
        "--machine-profile",
        "fixture-machine",
        "--output",
        str(output_path),
    ]
    passed = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, check=False)
    assert passed.returncode == 0, passed.stderr + passed.stdout
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["status"] == "PASS"
    assert payload["target_machine_acceptance"] is False

    failing = _budget(min_accepted_events_per_second=5_001).to_dict()
    budget_path.write_text(json.dumps(failing, allow_nan=False), encoding="utf-8")
    failed = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, check=False)
    assert failed.returncode == 5
    assert "performance_qualification=FAIL" in failed.stdout
