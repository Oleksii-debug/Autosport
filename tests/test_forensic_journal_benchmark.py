from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "benchmark_forensic_journal.py"
)


def _load_benchmark_module():
    spec = importlib.util.spec_from_file_location(
        "autosport_forensic_journal_benchmark",
        _SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


benchmark = _load_benchmark_module()


def test_percentile_nearest_rank_is_deterministic() -> None:
    values = [50, 10, 40, 20, 30]

    assert benchmark._percentile_nearest_rank(values, 1) == 10
    assert benchmark._percentile_nearest_rank(values, 50) == 30
    assert benchmark._percentile_nearest_rank(values, 95) == 50
    assert benchmark._percentile_nearest_rank(values, 100) == 50


def test_small_profile_measures_current_prefix_read_amplification(
    tmp_path: Path,
) -> None:
    profile = benchmark.run_profile(3, workspace=tmp_path)

    assert profile.requested_heartbeats == 3
    assert profile.final_record_count == 4
    assert profile.final_journal_bytes > 0

    # One full journal read occurs for each heartbeat append today.
    assert profile.append_journal_read_calls == 3
    assert profile.append_journal_read_bytes > profile.final_journal_bytes

    # Explicit verify remains a separate deliberate full-integrity scan.
    assert profile.verify_journal_read_calls == 1
    assert profile.verify_journal_read_bytes == profile.final_journal_bytes

    # Prior-prefix scans are 1 + 2 + 3; lifecycle scans additionally visit
    # prefix + candidate on every append: (1+2+3) + (2+3+4) = 15.
    assert profile.theoretical_prior_record_validations == 6
    assert profile.theoretical_lifecycle_record_visits == 15

    assert 0 < profile.append_latency_ns_p50
    assert profile.append_latency_ns_p50 <= profile.append_latency_ns_p95
    assert profile.append_latency_ns_p95 <= profile.append_latency_ns_max
    assert profile.append_elapsed_ns >= profile.append_latency_ns_max
    assert profile.verify_elapsed_ns > 0


def test_report_is_measurement_only_and_defines_no_latency_gate(
    tmp_path: Path,
) -> None:
    report = benchmark.build_report([2, 1], workspace=tmp_path)

    assert report["schema_version"] == 1
    assert report["kind"] == "autosport.forensic_journal_work_profile"
    assert report["measurement_only"] is True
    assert report["performance_threshold_defined"] is False
    assert report["integrity_semantics_modified"] is False
    profiles = report["profiles"]
    assert isinstance(profiles, list)
    assert [item["requested_heartbeats"] for item in profiles] == [1, 2]


def test_duplicate_or_non_positive_scales_fail_before_measurement(
    tmp_path: Path,
) -> None:
    for values in ([], [0], [-1], [2, 2]):
        try:
            benchmark.build_report(values, workspace=tmp_path)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {values!r}")


def test_cli_writes_machine_readable_report(tmp_path: Path, capsys) -> None:
    output = tmp_path / "report.json"

    assert benchmark.main(
        [
            "--records",
            "1",
            "2",
            "--workspace",
            str(tmp_path / "work"),
            "--output",
            str(output),
        ]
    ) == 0

    stdout = capsys.readouterr().out
    printed = json.loads(stdout)
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert printed == persisted
    assert [item["requested_heartbeats"] for item in printed["profiles"]] == [1, 2]


def test_cli_rejects_invalid_scale_without_creating_report(
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "report.json"

    assert benchmark.main(
        [
            "--records",
            "0",
            "--output",
            str(output),
        ]
    ) == 2

    captured = capsys.readouterr()
    assert "benchmark failed:" in captured.err
    assert not output.exists()
