from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
from statistics import median
import sys
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from typing import Iterator
from unittest.mock import patch

from autosport.forensic_journal import ForensicSessionJournal, HeartbeatState


@dataclass(frozen=True, slots=True)
class WorkProfile:
    requested_heartbeats: int
    final_record_count: int
    final_journal_bytes: int
    append_elapsed_ns: int
    append_latency_ns_p50: int
    append_latency_ns_p95: int
    append_latency_ns_max: int
    append_journal_read_calls: int
    append_journal_read_bytes: int
    verify_elapsed_ns: int
    verify_journal_read_calls: int
    verify_journal_read_bytes: int
    theoretical_prior_record_validations: int
    theoretical_lifecycle_record_visits: int

    def to_payload(self) -> dict[str, int]:
        return asdict(self)


def _percentile_nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        raise ValueError("values must be non-empty")
    if not 1 <= percentile <= 100:
        raise ValueError("percentile must be in [1, 100]")
    ordered = sorted(values)
    rank = (percentile * len(ordered) + 99) // 100
    return ordered[rank - 1]


@contextmanager
def _probe_journal_reads(journal_path: Path) -> Iterator[dict[str, int]]:
    target = journal_path
    original = Path.read_bytes
    counters = {"calls": 0, "bytes_read": 0}

    def counted(path: Path) -> bytes:
        payload = original(path)
        if path == target:
            counters["calls"] += 1
            counters["bytes_read"] += len(payload)
        return payload

    with patch.object(Path, "read_bytes", new=counted):
        yield counters


def _fixed_clock() -> datetime:
    return datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _run_in_directory(heartbeats: int, root: Path) -> WorkProfile:
    if type(heartbeats) is not int or heartbeats < 1:
        raise ValueError("heartbeats must be a positive integer")
    root.mkdir(parents=True, exist_ok=True)
    journal_path = root / "forensic-journal.jsonl"
    checkpoint_path = root / "forensic-journal.jsonl.head.json"
    if journal_path.exists() or checkpoint_path.exists():
        raise ValueError("benchmark directory must not contain an existing journal")

    journal = ForensicSessionJournal(journal_path, clock=_fixed_clock)
    journal.record_startup(
        "forensic-benchmark",
        details={"measurement_only": True},
    )

    latencies: list[int] = []
    append_start = perf_counter_ns()
    with _probe_journal_reads(journal_path) as append_probe:
        for sequence in range(heartbeats):
            started = perf_counter_ns()
            journal.record_heartbeat(
                "forensic-benchmark",
                HeartbeatState.RUNNING,
                details={
                    "measurement_only": True,
                    "heartbeat_index": sequence + 1,
                },
            )
            latencies.append(perf_counter_ns() - started)
    append_elapsed = perf_counter_ns() - append_start

    final_journal_bytes = journal_path.stat().st_size
    verify_started = perf_counter_ns()
    with _probe_journal_reads(journal_path) as verify_probe:
        integrity = journal.verify()
    verify_elapsed = perf_counter_ns() - verify_started

    expected_records = heartbeats + 1
    if integrity.record_count != expected_records:
        raise RuntimeError(
            "benchmark journal record count changed unexpectedly: "
            f"{integrity.record_count} != {expected_records}"
        )

    # The startup is written before measurement. Before heartbeat k, the journal
    # contains k records (startup + k-1 earlier heartbeats), so full-prefix
    # validation visits 1 + ... + heartbeats prior records.
    prior_record_validations = heartbeats * (heartbeats + 1) // 2

    # Current append also validates lifecycle state over the already-read prefix
    # and then again over prefix + candidate. This count is structural work, not
    # a latency assertion and intentionally makes no pass/fail threshold.
    lifecycle_record_visits = heartbeats * (heartbeats + 2)

    return WorkProfile(
        requested_heartbeats=heartbeats,
        final_record_count=integrity.record_count,
        final_journal_bytes=final_journal_bytes,
        append_elapsed_ns=append_elapsed,
        append_latency_ns_p50=int(median(latencies)),
        append_latency_ns_p95=_percentile_nearest_rank(latencies, 95),
        append_latency_ns_max=max(latencies),
        append_journal_read_calls=append_probe["calls"],
        append_journal_read_bytes=append_probe["bytes_read"],
        verify_elapsed_ns=verify_elapsed,
        verify_journal_read_calls=verify_probe["calls"],
        verify_journal_read_bytes=verify_probe["bytes_read"],
        theoretical_prior_record_validations=prior_record_validations,
        theoretical_lifecycle_record_visits=lifecycle_record_visits,
    )


def run_profile(heartbeats: int, *, workspace: Path | None = None) -> WorkProfile:
    if workspace is not None:
        root = workspace / f"heartbeats-{heartbeats}"
        return _run_in_directory(heartbeats, root)

    with TemporaryDirectory(prefix="autosport-forensic-benchmark-") as temp:
        return _run_in_directory(heartbeats, Path(temp))


def build_report(record_counts: list[int], *, workspace: Path | None = None) -> dict[str, object]:
    if not record_counts:
        raise ValueError("at least one heartbeat count is required")
    if any(type(value) is not int or value < 1 for value in record_counts):
        raise ValueError("all heartbeat counts must be positive integers")
    if len(set(record_counts)) != len(record_counts):
        raise ValueError("heartbeat counts must be unique")

    profiles = [
        run_profile(value, workspace=workspace)
        for value in sorted(record_counts)
    ]
    return {
        "schema_version": 1,
        "kind": "autosport.forensic_journal_work_profile",
        "measurement_only": True,
        "performance_threshold_defined": False,
        "integrity_semantics_modified": False,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "profiles": [profile.to_payload() for profile in profiles],
        "interpretation": {
            "append_read_bytes": (
                "bytes returned by Path.read_bytes for the journal during measured "
                "heartbeat appends; checkpoint reads and OS cache effects are separate"
            ),
            "theoretical_prior_record_validations": (
                "deterministic full-prefix record validations implied by current "
                "append structure, excluding startup and final explicit verify"
            ),
            "latency": (
                "observed local wall-clock measurement including filesystem fsync "
                "and checkpoint publication; informational only, never a readiness gate"
            ),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure forensic-journal append/verify work without changing its "
            "integrity semantics or inventing a latency threshold."
        )
    )
    parser.add_argument(
        "--records",
        metavar="N",
        nargs="+",
        type=int,
        default=[100, 1000],
        help=(
            "heartbeat counts to profile in fresh journals (default: 100 1000). "
            "For endurance characterization run explicitly with "
            "--records 1000 10000 100000."
        ),
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help=(
            "optional directory in which per-scale benchmark journals are retained; "
            "without it each scale uses a temporary directory"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional JSON report path; stdout is always emitted",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_report(args.records, workspace=args.workspace)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 2

    encoded = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    print(encoded)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
