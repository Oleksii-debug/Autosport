import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autosport.ingestion_health import SourceHealthStore


class SourceHealthHistoryTests(unittest.TestCase):
    @staticmethod
    def at(second: int) -> datetime:
        return datetime(2026, 9, 17, 12, 0, second, tzinfo=timezone.utc)

    @staticmethod
    def success(store: SourceHealthStore, now: str, *, flags: tuple[str, ...] = ()) -> None:
        store.record_success(
            "provider-a",
            now=now,
            received=1,
            accepted=1,
            rejected=0,
            cursor=now,
            latest_source_ts="2026-09-17T12:00:00+00:00",
            quality_flags=flags,
        )

    def test_history_survives_restart_and_replays_each_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            store = SourceHealthStore(path)
            self.success(store, "2026-09-17T12:00:03+00:00")
            store.record_failure(
                "provider-a",
                now="2026-09-17T12:00:08+00:00",
                error=TimeoutError("timeout"),
            )
            self.success(
                store,
                "2026-09-17T12:00:12+00:00",
                flags=("PARTIAL_SNAPSHOT",),
            )

            reopened = SourceHealthStore(path)
            self.assertEqual(reopened.get_as_of("provider-a", as_of=self.at(2)).status, "unknown")
            self.assertEqual(reopened.get_as_of("provider-a", as_of=self.at(5)).status, "healthy")
            self.assertEqual(reopened.get_as_of("provider-a", as_of=self.at(10)).status, "failed")
            degraded = reopened.get_as_of("provider-a", as_of=self.at(13))
            self.assertEqual(degraded.status, "degraded")
            self.assertEqual(degraded.quality_flags, ("PARTIAL_SNAPSHOT",))

    def test_legacy_v1_projection_migrates_without_backdating_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            legacy_state = {
                "source_id": "provider-a",
                "status": "healthy",
                "poll_count": 1,
                "total_received": 1,
                "total_accepted": 1,
                "total_rejected": 0,
                "total_failures": 0,
                "consecutive_failures": 0,
                "last_success_at": "2026-09-17T12:00:03+00:00",
                "last_error_at": None,
                "last_error": None,
                "last_cursor": "legacy",
                "latest_source_ts": "2026-09-17T12:00:00+00:00",
                "quality_flags": [],
            }
            path.write_text(
                json.dumps({"schema_version": 1, "sources": {"provider-a": legacy_state}}),
                encoding="utf-8",
            )

            store = SourceHealthStore(path)
            self.assertEqual(store.get_as_of("provider-a", as_of=self.at(2)).status, "unknown")
            self.assertEqual(store.get_as_of("provider-a", as_of=self.at(5)).status, "healthy")

            store.record_failure(
                "provider-a",
                now="2026-09-17T12:00:08+00:00",
                error=ConnectionError("down"),
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 2)
            self.assertEqual(len(persisted["history"]["provider-a"]), 2)
            self.assertEqual(store.get_as_of("provider-a", as_of=self.at(5)).status, "healthy")
            self.assertEqual(store.get_as_of("provider-a", as_of=self.at(9)).status, "failed")

    def test_out_of_order_transition_is_rejected_without_mutating_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            store = SourceHealthStore(path)
            self.success(store, "2026-09-17T12:00:08+00:00")
            baseline = path.read_bytes()

            with self.assertRaisesRegex(ValueError, "nondecreasing"):
                store.record_failure(
                    "provider-a",
                    now="2026-09-17T12:00:07+00:00",
                    error=RuntimeError("late writer"),
                )

            self.assertEqual(path.read_bytes(), baseline)
            self.assertEqual(store.get("provider-a").status, "healthy")

    def test_naive_as_of_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SourceHealthStore(Path(directory) / "health.json")
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                store.get_as_of("provider-a", as_of=datetime(2026, 9, 17, 12, 0, 0))


if __name__ == "__main__":
    unittest.main()
