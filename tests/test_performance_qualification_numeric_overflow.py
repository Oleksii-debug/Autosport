from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from autosport.performance_qualification import (
    BUDGET_SCHEMA,
    MetricQualification,
    PerformanceBudget,
    PerformanceQualificationError,
)


HUGE_JSON_INTEGER = 10**400
SOURCE_SHA = "a" * 40


def _budget_payload(value: object) -> dict[str, object]:
    return {
        "schema": BUDGET_SCHEMA,
        "schema_version": 1,
        "min_history_events": None,
        "min_accepted_events_per_second": value,
        "max_peak_traced_memory_bytes": None,
        "max_ingest_elapsed_seconds": None,
        "max_replay_elapsed_seconds": None,
        "max_restart_elapsed_seconds": None,
    }


def test_budget_rejects_integer_that_overflows_float_conversion() -> None:
    with pytest.raises(
        PerformanceQualificationError,
        match="finite positive number",
    ):
        PerformanceBudget.from_dict(_budget_payload(HUGE_JSON_INTEGER))


def test_metric_rejects_integer_that_overflows_float_conversion() -> None:
    with pytest.raises(
        PerformanceQualificationError,
        match="observed/threshold must be finite",
    ):
        MetricQualification(
            metric="accepted_events_per_second",
            comparator=">=",
            observed=HUGE_JSON_INTEGER,
            threshold=1,
            status="PASS",
        )


def test_cli_returns_invalid_for_overflowing_json_number_without_traceback(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "qualify_endurance_performance.py"
    report_path = tmp_path / "report.json"
    budget_path = tmp_path / "budget.json"
    output_path = tmp_path / "qualification.json"
    report_path.write_text("{}", encoding="utf-8")
    budget_path.write_text(
        json.dumps(_budget_payload(HUGE_JSON_INTEGER), allow_nan=False),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get(
        "PYTHONPATH", ""
    )

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            str(report_path),
            str(budget_path),
            "--source-sha",
            SOURCE_SHA,
            "--machine-profile",
            "overflow-fixture",
            "--output",
            str(output_path),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 3
    assert "performance_qualification=INVALID" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    assert not output_path.exists()
