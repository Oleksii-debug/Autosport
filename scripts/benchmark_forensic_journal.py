from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from time import perf_counter_ns
from typing import Iterable

import autosport.forensic_journal as forensic_journal
from autosport.forensic_journal import ForensicSessionJournal, HeartbeatState


DEFAULT_SIZES = (1_000, 10_000, 100_000)
_BASE_TIME = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)


def _write_durable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def seed_forensic_history(path: Path, record_count: int) -> str:
    """Create a canonical valid active-session history in O(record_count) work.

    The benchmark deliberately seeds the journal directly instead of building it through
    ``record_heartbeat``. Building a 100k-record fixture through the current append path
    would itself exercise the O(n^2) behavior that this harness is intended to measure.
    Private canonicalization helpers are reused so the fixture cannot silently drift from
    the exact forensic-journal format being profiled.
    """

    if type(record_count) is not int or record_count < 1:
        raise ValueError("record_count must be a positive integer")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = forensic_journal._GENESIS_SHA256

    with path.open("wb") as handle:
        for sequence in range(1, record_count + 1):
            if sequence == 1:
                kind = forensic_journal.ForensicEventKind.STARTUP
                event_name = "startup"
                heartbeat_state = None
            else:
                kind = forensic_journal.ForensicEventKind.HEARTBEAT
                event_name = "heartbeat"
                heartbeat_state = HeartbeatState.RUNNING.value

            occurred_at = forensic_journal._canonical_timestamp(
                _BASE_TIME + timedelta(microseconds=sequence - 1)
            )
            payload: dict[str, object] = {
                "schema_version": forensic_journal._SCHEMA_VERSION,
                "sequence": sequence,
                "occurred_at": occurred_at,
                "kind": kind.value,
                "source": "forensic-benchmark",
                "heartbeat_state": heartbeat_state,
                "event_name": event_name,
                "message": None,
                "details": {},
                "previous_sha256": previous,
            }
            digest = forensic_journal._record_digest(payload)
            payload["record_sha256"] = digest
            handle.write(forensic_journal._canonical_json_bytes(payload) + b"\n")
            previous = digest

        handle.flush()
        os.fsync(handle.fileno())

    checkpoint = ForensicSessionJournal._checkpoint_payload(record_count, previous)
    _write_durable(
        path.with_name(f"{path.name}.head.json"),
        forensic_journal._canonical_json_bytes(checkpoint),
    )
    return previous


def benchmark_case(record_count: int, directory: Path) -> dict[str, int]:
    """Measure one restart, one append, and one explicit verify for one history size."""

    path = Path(directory) / f"forensic-{record_count}.jsonl"
    seed_forensic_history(path, record_count)
    seeded_bytes = path.stat().st_size

    open_started = perf_counter_ns()
    journal = ForensicSessionJournal(path)
    open_ns = perf_counter_ns() - open_started

    append_started = perf_counter_ns()
    appended = journal.record_heartbeat(
        "forensic-benchmark",
        HeartbeatState.RUNNING,
        message="single append after seeded history",
    )
    append_ns = perf_counter_ns() - append_started

    verify_started = perf_counter_ns()
    integrity = journal.verify()
    verify_ns = perf_counter_ns() - verify_started

    if appended.sequence != record_count + 1:
        raise RuntimeError("benchmark append produced an unexpected sequence")
    if integrity.record_count != record_count + 1:
        raise RuntimeError("benchmark verification produced an unexpected record count")

    return {
        "history_records": record_count,
        "seeded_file_bytes": seeded_bytes,
        "file_bytes_after_append": path.stat().st_size,
        "record_count_after_append": integrity.record_count,
        "open_ns": open_ns,
        "append_ns": append_ns,
        "verify_ns": verify_ns,
    }


def run_benchmark(sizes: Iterable[int]) -> list[dict[str, int]]:
    normalized = tuple(sizes)
    if not normalized:
        raise ValueError("at least one history size is required")
    if any(type(size) is not int or size < 1 for size in normalized):
        raise ValueError("history sizes must be positive integers")

    with tempfile.TemporaryDirectory(prefix="autosport-forensic-benchmark-") as temp_dir:
        root = Path(temp_dir)
        return [benchmark_case(size, root) for size in normalized]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Profile forensic-journal restart/append/verify cost without changing "
            "integrity semantics. Results are measurements, not pass/fail thresholds."
        )
    )
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=list(DEFAULT_SIZES),
        help="seeded history sizes to profile (default: 1000 10000 100000)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    results = run_benchmark(args.sizes)
    print(
        json.dumps(
            {
                "schema_version": 1,
                "clock": "perf_counter_ns",
                "threshold_semantics": "NONE_MEASUREMENT_ONLY",
                "results": results,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
