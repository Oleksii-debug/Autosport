from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    GapState,
    SyncState,
    canonical_event_digest,
    digest_source_payload,
)
from autosport.collector_service import CollectorServiceConfig, HeadlessCollectorService
from autosport.domain import MarketEvent
from autosport.event_lifecycle import (
    CatalogEvent,
    CatalogPage,
    ContinuousEventLifecycle,
    EventPhase,
)


_PRECOMMIT_CRASH_EXIT_CODE = 90
_POSTCOMMIT_CRASH_EXIT_CODE = 91
_SOURCE_ID = "source-process-kill"
_RUN_ID = "process-kill-run-1"


def _market_payload() -> dict[str, object]:
    return {
        "event_id": f"{_SOURCE_ID}:event-1",
        "market_id": f"{_SOURCE_ID}:winner",
        "selection_id": f"{_SOURCE_ID}:player-a",
        "decimal_odds": "1.80",
        "observed_ts": "2026-01-01T00:00:01+00:00",
        "source_id": _SOURCE_ID,
        "sequence": 1,
        "market_type": "winner",
        "status": "open",
        "source_ts": "2026-01-01T00:00:00+00:00",
        "ingest_ts": "2026-01-01T00:00:01+00:00",
        "metadata": {},
        "score_state": None,
        "sport": "table_tennis",
    }


def _delta() -> CollectorDelta:
    payload = _market_payload()
    event = MarketEvent.from_dict(payload)
    raw_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return CollectorDelta(
        schema_version=1,
        delta_id="process-kill-d1",
        source_id=_SOURCE_ID,
        lawful_terms_ref="terms:process-kill:v1",
        retention_ref="retention:process-kill:v1",
        stream_epoch="epoch-1",
        source_cursor="1",
        cursor_position=1,
        event_dedupe_key=event.dedupe_key,
        event_id=event.event_id,
        source_payload_digest=digest_source_payload(raw_payload),
        canonical_event_digest=canonical_event_digest(event),
        source_observed_at="2026-01-01T00:00:01+00:00",
        collector_received_at="2026-01-01T00:00:02+00:00",
        collector_committed_at="2026-01-01T00:00:03+00:00",
        desktop_available_at="2026-01-01T00:00:04+00:00",
        revision_of=None,
        revision_number=0,
        gap_state=GapState.NONE,
        sync_state=SyncState.READY,
        gap_from_cursor=None,
        gap_to_cursor=None,
    )


def _catalog_page() -> CatalogPage:
    return CatalogPage(
        source_id=_SOURCE_ID,
        stream_epoch="catalog-epoch-1",
        cursor="catalog-1",
        position=1,
        events=(
            CatalogEvent(
                source_id=_SOURCE_ID,
                sport="table_tennis",
                event_id="event-1",
                phase=EventPhase.PRE_MATCH,
                available_at="2026-01-01T00:00:01+00:00",
            ),
        ),
    )


class _ReplaySource:
    source_id = _SOURCE_ID
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, checkpoint):
        return _catalog_page()

    def fetch_deltas(self, checkpoint, records, max_items):
        return (_delta(),)


def _service(root: Path) -> HeadlessCollectorService:
    return HeadlessCollectorService(
        delta_store=CollectorDeltaStore(root / "collector.json"),
        lifecycle=ContinuousEventLifecycle(root / "catalog.json"),
        source=_ReplaySource(),
        state_path=root / "service.json",
        run_id=_RUN_ID,
        config=CollectorServiceConfig(
            poll_interval_seconds=1,
            initial_backoff_seconds=1,
            max_backoff_seconds=1,
            jitter_fraction=0,
        ),
        clock=lambda: "2026-01-01T00:00:10+00:00",
        sleep=lambda _: None,
        random_value=lambda: 0,
    )


def _crash_before_sqlite_commit(root: Path) -> None:
    service = _service(root)

    def die_with_transaction_open(_raw: dict[str, object]) -> None:
        # _append_with_runtime_stream_epoch calls _write after inserting the delta but
        # before connection.commit(). A real process exit here leaves SQLite rollback
        # recovery, not Python cleanup, responsible for removing the partial prefix.
        os._exit(_PRECOMMIT_CRASH_EXIT_CODE)

    service.delta_store._write = die_with_transaction_open
    service.run_cycle()
    raise AssertionError("collector worker did not terminate before SQLite commit")


def _crash_after_durable_commit(root: Path) -> None:
    service = _service(root)

    def die_before_success_bookkeeping(*, at: str, committed: int, duplicates: int) -> None:
        # The collector delta transaction has already committed when run_cycle reaches
        # record_success(). os._exit simulates abrupt process death: no finally blocks,
        # Python cleanup, or graceful service STOP bookkeeping can run.
        os._exit(_POSTCOMMIT_CRASH_EXIT_CODE)

    service._state.record_success = die_before_success_bookkeeping
    service.run_cycle()
    raise AssertionError("collector worker did not terminate after durable commit")


def _worker_environment() -> tuple[Path, dict[str, str]]:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python_path = str(repo_root / "src")
    existing = env.get("PYTHONPATH")
    if existing:
        python_path = os.pathsep.join((python_path, existing))
    env["PYTHONPATH"] = python_path
    return repo_root, env


def _run_crash_worker(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    repo_root, env = _worker_environment()
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), mode, str(root)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


class CollectorProcessDeathRecoveryTests(unittest.TestCase):
    def test_uncommitted_delta_rolls_back_after_process_death(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = _run_crash_worker(root, "--crash-before-commit")
            self.assertEqual(
                worker.returncode,
                _PRECOMMIT_CRASH_EXIT_CODE,
                msg=(
                    "crash worker did not reach the open-transaction boundary; "
                    f"stdout={worker.stdout!r} stderr={worker.stderr!r}"
                ),
            )

            reopened_store = CollectorDeltaStore(root / "collector.json")
            self.assertEqual(
                reopened_store.deltas_after_commit(source_id=_SOURCE_ID),
                (),
            )
            self.assertIsNone(
                reopened_store.stream_checkpoint(_SOURCE_ID, "epoch-1")
            )
            self.assertEqual(
                reopened_store.runtime_stream_epoch(_SOURCE_ID),
                ("epoch-1", 1),
            )

            crashed_state = json.loads(
                (root / "service.json").read_text(encoding="utf-8")
            )
            self.assertEqual(crashed_state["cycles_attempted"], 1)
            self.assertEqual(crashed_state["cycles_succeeded"], 0)
            self.assertEqual(crashed_state["deltas_committed"], 0)
            self.assertIsNone(crashed_state["last_error_code"])

            resumed = _service(root)
            replay = resumed.run_cycle()
            self.assertEqual(replay.committed_delta_ids, ("process-kill-d1",))
            self.assertEqual(replay.duplicate_delta_ids, ())

            recovered_store = CollectorDeltaStore(root / "collector.json")
            recovered = recovered_store.deltas_after_commit(source_id=_SOURCE_ID)
            self.assertEqual(
                tuple(item.delta_id for item in recovered),
                ("process-kill-d1",),
            )
            recovered_state = json.loads(
                (root / "service.json").read_text(encoding="utf-8")
            )
            self.assertEqual(recovered_state["cycles_attempted"], 2)
            self.assertEqual(recovered_state["cycles_succeeded"], 1)
            self.assertEqual(recovered_state["deltas_committed"], 1)
            self.assertEqual(recovered_state["duplicate_deltas"], 0)

    def test_durable_commit_survives_process_death_and_duplicate_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = _run_crash_worker(root, "--crash-after-commit")
            self.assertEqual(
                worker.returncode,
                _POSTCOMMIT_CRASH_EXIT_CODE,
                msg=(
                    "crash worker did not reach the post-commit boundary; "
                    f"stdout={worker.stdout!r} stderr={worker.stderr!r}"
                ),
            )

            reopened_store = CollectorDeltaStore(root / "collector.json")
            durable = reopened_store.deltas_after_commit(source_id=_SOURCE_ID)
            self.assertEqual(
                tuple(item.delta_id for item in durable),
                ("process-kill-d1",),
            )
            self.assertEqual(
                reopened_store.runtime_stream_epoch(_SOURCE_ID),
                ("epoch-1", 1),
            )

            crashed_state = json.loads(
                (root / "service.json").read_text(encoding="utf-8")
            )
            self.assertEqual(crashed_state["cycles_attempted"], 1)
            self.assertEqual(crashed_state["cycles_succeeded"], 0)
            self.assertEqual(crashed_state["deltas_committed"], 0)
            self.assertIsNone(crashed_state["stopped_at"])
            self.assertIsNone(crashed_state["stop_reason"])

            resumed = _service(root)
            replay = resumed.run_cycle()
            self.assertEqual(replay.committed_delta_ids, ())
            self.assertEqual(replay.duplicate_delta_ids, ("process-kill-d1",))

            final_store = CollectorDeltaStore(root / "collector.json")
            final_durable = final_store.deltas_after_commit(source_id=_SOURCE_ID)
            self.assertEqual(
                tuple(item.delta_id for item in final_durable),
                ("process-kill-d1",),
            )
            self.assertEqual(
                final_store.runtime_stream_epoch(_SOURCE_ID),
                ("epoch-1", 1),
            )

            recovered_state = json.loads(
                (root / "service.json").read_text(encoding="utf-8")
            )
            self.assertEqual(recovered_state["cycles_attempted"], 2)
            self.assertEqual(recovered_state["cycles_succeeded"], 1)
            self.assertEqual(recovered_state["deltas_committed"], 0)
            self.assertEqual(recovered_state["duplicate_deltas"], 1)
            self.assertIsNone(recovered_state["last_error_code"])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--crash-before-commit":
        _crash_before_sqlite_commit(Path(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--crash-after-commit":
        _crash_after_durable_commit(Path(sys.argv[2]))
    else:
        unittest.main()
