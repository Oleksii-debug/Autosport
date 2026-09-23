from __future__ import annotations

import argparse
import json
import os
import platform
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from autosport.dataset_worker import OneShotDatasetValidationWorker
from autosport.event_lifecycle import CatalogPage
from autosport.live_observation import OneShotObservationWorker
from autosport.product_runtime import build_autonomous_product_runtime
from autosport.replay_worker import OneShotReplayWorker
from autosport.runtime_resource_census import (
    ThreadCensusStatus,
    capture_owned_thread_snapshot,
    compare_owned_thread_snapshots,
    probe_disposable_workspace_move_delete,
    probe_workspace_move_round_trip,
    stable_evidence_sha256,
)


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_WORKER_JOIN_TIMEOUT_SECONDS = 10.0
_BARRIER_TIMEOUT_SECONDS = 10.0


class _ExpectedQualificationFailure(RuntimeError):
    pass


class _DeterministicClock:
    def __init__(self) -> None:
        self._base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._step = 0

    def __call__(self) -> str:
        instant = self._base + timedelta(seconds=self._step)
        self._step += 1
        return instant.isoformat()


class _IdleProductSource:
    source_id = "runtime-resource-endurance"
    stream_epoch = "runtime-resource-endurance-epoch-v1"

    def fetch_catalog_page(self, checkpoint):
        del checkpoint
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor="runtime-resource-endurance-catalog",
            position=1,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        del checkpoint, records, max_items
        return ()

    def resolve_event(self, delta):
        del delta
        raise AssertionError("idle resource qualification must not resolve market deltas")


def _expected_worker_failure() -> object:
    raise _ExpectedQualificationFailure("expected-resource-cycle-terminal")


def _finish_worker(worker: object, worker_name: str) -> None:
    started = getattr(worker, "start")(_expected_worker_failure)
    if not started:
        raise RuntimeError(f"{worker_name} did not acquire an idle single-flight slot")
    thread = getattr(worker, "_thread", None)
    if not isinstance(thread, threading.Thread):
        raise RuntimeError(f"{worker_name} did not expose its started owned thread")
    thread.join(_WORKER_JOIN_TIMEOUT_SECONDS)
    if thread.is_alive():
        raise RuntimeError(f"{worker_name} owned thread did not reach terminal quiescence")
    message = getattr(worker, "poll")()
    if message is None or getattr(message, "error", None) is None:
        raise RuntimeError(f"{worker_name} did not publish the expected terminal failure")
    if bool(getattr(worker, "busy")):
        raise RuntimeError(f"{worker_name} remained busy after terminal message consumption")


def _exercise_workers() -> None:
    _finish_worker(OneShotObservationWorker(), "live observation worker")
    _finish_worker(OneShotReplayWorker(), "paper replay worker")
    _finish_worker(OneShotDatasetValidationWorker(), "dataset validation worker")


def _seeded_leak_detector_check() -> tuple[bool, int]:
    baseline = capture_owned_thread_snapshot()
    if not baseline.complete:
        raise RuntimeError("seeded leak baseline census was incomplete")

    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        entered.set()
        release.wait()

    thread = threading.Thread(
        target=hold,
        name="autosport-resource-leak-sentinel",
        daemon=False,
    )
    thread.start()
    if not entered.wait(_BARRIER_TIMEOUT_SECONDS):
        release.set()
        thread.join(_WORKER_JOIN_TIMEOUT_SECONDS)
        raise RuntimeError("seeded leak sentinel did not reach its deterministic barrier")

    try:
        during = capture_owned_thread_snapshot()
        comparison = compare_owned_thread_snapshots(baseline, during)
        detected = (
            comparison.status is ThreadCensusStatus.FAIL
            and any(
                item.name == "autosport-resource-leak-sentinel"
                for item in comparison.growth
            )
        )
        detected_count = sum(item.delta for item in comparison.growth)
        if comparison.status is ThreadCensusStatus.INCONCLUSIVE:
            raise RuntimeError("seeded leak census became inconclusive")
    finally:
        release.set()
        thread.join(_WORKER_JOIN_TIMEOUT_SECONDS)

    if thread.is_alive():
        raise RuntimeError("seeded leak sentinel did not terminate after release")
    restored = capture_owned_thread_snapshot()
    restored_comparison = compare_owned_thread_snapshots(baseline, restored)
    if restored_comparison.status is not ThreadCensusStatus.PASS:
        raise RuntimeError("seeded leak negative control did not restore a complete baseline")
    return detected, detected_count


def _run_runtime_cycle(
    workspace: Path,
    *,
    source: _IdleProductSource,
    clock: Callable[[], str],
    reason: str,
) -> str:
    runtime = build_autonomous_product_runtime(
        workspace=workspace,
        source=source,
        clock=clock,
        sleep=lambda _seconds: None,
    )
    try:
        runtime.start()
        session_id = runtime.coordinator.session_id
        runtime.stop(reason)
        return session_id
    finally:
        runtime.close()


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify bounded Autosport-owned runtime resource lifecycle across "
            "worker and canonical product-runtime restart cycles."
        )
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("runtime-resource-endurance-report.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if isinstance(args.cycles, bool) or not 1 <= args.cycles <= 500:
        raise SystemExit("--cycles must be an integer in [1, 500]")
    if not _SHA_RE.fullmatch(args.source_sha):
        raise SystemExit("--source-sha must be one lowercase 40-hex commit SHA")

    root = args.workspace
    if root.exists():
        raise SystemExit("resource endurance workspace must be fresh")
    root.mkdir(parents=True)

    baseline = capture_owned_thread_snapshot()
    failures: list[str] = []
    if not baseline.complete:
        failures.append("baseline owned-thread census is incomplete")

    first_failing_cycle: int | None = None
    max_owned_threads = baseline.owned_thread_count
    shared_session_id: str | None = None
    shared_reopens = 0
    move_round_trip_passes = 0
    move_delete_passes = 0
    source = _IdleProductSource()
    clock = _DeterministicClock()

    seeded_leak_detected = False
    seeded_leak_residual_count = 0
    try:
        if not failures:
            seeded_leak_detected, seeded_leak_residual_count = _seeded_leak_detector_check()
            if not seeded_leak_detected:
                failures.append("seeded leak negative control was not detected")

        shared_workspace = root / "shared-runtime"
        for cycle in range(1, args.cycles + 1):
            if failures:
                first_failing_cycle = cycle
                break

            _exercise_workers()
            session_id = _run_runtime_cycle(
                shared_workspace,
                source=source,
                clock=clock,
                reason=f"resource_cycle_{cycle}",
            )
            if shared_session_id is None:
                shared_session_id = session_id
            elif session_id != shared_session_id:
                failures.append("same workspace restart changed continuous session identity")
            else:
                shared_reopens += 1

            round_trip = probe_workspace_move_round_trip(shared_workspace)
            if round_trip.status == "PASS":
                move_round_trip_passes += 1
            else:
                failures.append(
                    f"shared workspace move round-trip failed:{round_trip.error_type}"
                )

            disposable = root / f"disposable-runtime-{cycle:04d}"
            _run_runtime_cycle(
                disposable,
                source=source,
                clock=clock,
                reason=f"disposable_resource_cycle_{cycle}",
            )
            move_delete = probe_disposable_workspace_move_delete(disposable)
            if move_delete.status == "PASS":
                move_delete_passes += 1
            else:
                failures.append(
                    f"disposable workspace move/delete failed:{move_delete.error_type}"
                )

            current = capture_owned_thread_snapshot()
            max_owned_threads = max(max_owned_threads, current.owned_thread_count)
            comparison = compare_owned_thread_snapshots(baseline, current)
            if comparison.status is ThreadCensusStatus.FAIL:
                failures.append(
                    "owned Autosport threads remained after quiescence:"
                    + ",".join(item.name for item in comparison.growth)
                )
            elif comparison.status is ThreadCensusStatus.INCONCLUSIVE:
                failures.append(
                    "owned-thread census became inconclusive:"
                    + ",".join(comparison.issues)
                )

            if failures:
                first_failing_cycle = cycle
                break
    except Exception as exc:
        if first_failing_cycle is None:
            first_failing_cycle = 0
        failures.append(f"qualification exception:{type(exc).__name__}")

    final_census = capture_owned_thread_snapshot()
    final_comparison = compare_owned_thread_snapshots(baseline, final_census)
    if final_comparison.status is ThreadCensusStatus.FAIL:
        failures.append(
            "final owned thread residual:"
            + ",".join(item.name for item in final_comparison.growth)
        )
    elif final_comparison.status is ThreadCensusStatus.INCONCLUSIVE:
        failures.append(
            "final owned-thread census inconclusive:"
            + ",".join(final_comparison.issues)
        )

    platform_name = platform.system() or os.name
    windows_probe_status = (
        "PASS"
        if os.name == "nt"
        and move_round_trip_passes == args.cycles
        and move_delete_passes == args.cycles
        and not failures
        else "NOT_APPLICABLE"
        if os.name != "nt"
        else "FAIL"
    )

    report: dict[str, object] = {
        "schema_version": 2,
        "status": "PASS" if not failures else "FAIL",
        "source_sha": args.source_sha,
        "platform": platform_name,
        "os_name": os.name,
        "cycle_count_requested": args.cycles,
        "shared_runtime_cycles_completed": move_round_trip_passes,
        "disposable_runtime_cycles_completed": move_delete_passes,
        "shared_workspace_reopens": shared_reopens,
        "same_workspace_session_identity_stable": (
            shared_session_id is not None
            and shared_reopens == max(0, move_round_trip_passes - 1)
        ),
        "seeded_leak_detected": seeded_leak_detected,
        "seeded_leak_residual_count": seeded_leak_residual_count,
        "baseline_census": baseline.to_dict(),
        "final_census": final_census.to_dict(),
        "final_census_status": final_comparison.status.value,
        "final_residual_threads": [
            item.to_dict() for item in final_comparison.growth
        ],
        "max_owned_thread_count_at_quiescent_checkpoint": max_owned_threads,
        "workspace_move_round_trip_passes": move_round_trip_passes,
        "workspace_move_delete_passes": move_delete_passes,
        "windows_workspace_handle_semantics": windows_probe_status,
        "first_failing_cycle": first_failing_cycle,
        "failures": failures,
        "barrier_model": "thread_join_and_event_barriers_no_correctness_sleep",
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "whole_product_complete": False,
    }
    report["evidence_sha256"] = stable_evidence_sha256(report)
    _write_json_atomic(args.output, report)

    print(
        f"runtime_resource_endurance={report['status']} "
        f"source_sha={args.source_sha} cycles={args.cycles} "
        f"windows_handle_semantics={windows_probe_status}"
    )
    print(f"evidence={args.output}")
    return 0 if report["status"] == "PASS" else 5


if __name__ == "__main__":
    raise SystemExit(main())
