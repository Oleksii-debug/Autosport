from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from autosport.forensic_journal import ForensicSessionJournal, HeartbeatState


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_forensic_journal.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_forensic_journal", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
benchmark_forensic_journal = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(benchmark_forensic_journal)


def test_seeded_history_is_canonical_restart_valid_and_appendable(tmp_path):
    path = tmp_path / "forensic.jsonl"

    seeded_digest = benchmark_forensic_journal.seed_forensic_history(path, 8)

    journal = ForensicSessionJournal(path)
    integrity = journal.verify()
    assert integrity.record_count == 8
    assert integrity.last_record_sha256 == seeded_digest

    records = journal.read_records()
    assert len(records) == 8
    assert records[0].event_name == "startup"
    assert all(record.event_name == "heartbeat" for record in records[1:])

    appended = journal.record_heartbeat(
        "benchmark-test",
        HeartbeatState.RUNNING,
    )
    assert appended.sequence == 9
    assert journal.verify().record_count == 9


def test_benchmark_case_reports_separate_restart_append_and_verify_measurements(tmp_path):
    result = benchmark_forensic_journal.benchmark_case(32, tmp_path)

    assert result["history_records"] == 32
    assert result["record_count_after_append"] == 33
    assert result["seeded_file_bytes"] > 0
    assert result["file_bytes_after_append"] > result["seeded_file_bytes"]

    assert result["open_ns"] >= 0
    assert result["open_journal_read_calls"] == 1
    assert result["open_journal_read_bytes"] == result["seeded_file_bytes"]

    assert result["append_ns"] >= 0
    assert result["append_journal_read_calls"] == 1
    assert result["append_journal_read_bytes"] == result["seeded_file_bytes"]

    assert result["verify_ns"] >= 0
    assert result["verify_journal_read_calls"] == 1
    assert result["verify_journal_read_bytes"] == result["file_bytes_after_append"]

    assert result["theoretical_append_historical_record_validations"] == 32
    assert result["theoretical_append_lifecycle_record_visits"] == 65


def test_run_benchmark_preserves_requested_sizes_and_does_not_define_thresholds():
    results = benchmark_forensic_journal.run_benchmark((3, 7))

    assert [result["history_records"] for result in results] == [3, 7]
    assert all("open_ns" in result for result in results)
    assert all("append_ns" in result for result in results)
    assert all("verify_ns" in result for result in results)

    report = benchmark_forensic_journal.benchmark_document((2,))
    assert report["kind"] == "autosport.forensic_journal_scaling_profile"
    assert report["measurement_only"] is True
    assert report["performance_threshold_defined"] is False
    assert report["integrity_semantics_modified"] is False
    assert report["clock"] == "perf_counter_ns"


@pytest.mark.parametrize("record_count", [0, -1, True, 1.5])
def test_seed_forensic_history_rejects_non_positive_or_non_integer_sizes(
    tmp_path, record_count
):
    with pytest.raises(ValueError, match="positive integer"):
        benchmark_forensic_journal.seed_forensic_history(
            tmp_path / "invalid.jsonl", record_count
        )
