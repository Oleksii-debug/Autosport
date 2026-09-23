from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from autosport.runtime_resource_census import (
    AUTOSPORT_THREAD_PREFIX,
    OwnedThreadRecord,
    OwnedThreadSnapshot,
    ThreadCensusStatus,
    capture_owned_thread_snapshot,
    compare_owned_thread_snapshots,
    probe_disposable_workspace_move_delete,
    probe_workspace_move_round_trip,
    stable_evidence_sha256,
)


class RuntimeResourceThreadCensusTests(unittest.TestCase):
    def _blocking_thread(
        self,
        *,
        name: str,
        daemon: bool,
    ) -> tuple[threading.Event, threading.Thread]:
        release = threading.Event()
        started = threading.Event()

        def run() -> None:
            started.set()
            release.wait()

        thread = threading.Thread(target=run, name=name, daemon=daemon)
        thread.start()
        self.assertTrue(started.wait(5.0))
        self.assertTrue(thread.is_alive())
        return release, thread

    def test_seeded_owned_thread_growth_is_detected_and_clears_after_join(self) -> None:
        baseline = capture_owned_thread_snapshot()
        release, thread = self._blocking_thread(
            name="autosport-census-seeded-leak",
            daemon=False,
        )
        try:
            leaked = capture_owned_thread_snapshot()
            comparison = compare_owned_thread_snapshots(baseline, leaked)
            self.assertEqual(comparison.status, ThreadCensusStatus.FAIL)
            matching = [
                growth
                for growth in comparison.growth
                if growth.name == "autosport-census-seeded-leak"
                and growth.daemon is False
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].delta, 1)
        finally:
            release.set()
            thread.join(5.0)

        self.assertFalse(thread.is_alive())
        recovered = capture_owned_thread_snapshot()
        recovered_comparison = compare_owned_thread_snapshots(baseline, recovered)
        self.assertEqual(recovered_comparison.status, ThreadCensusStatus.PASS)

    def test_same_name_growth_counts_every_live_owned_thread(self) -> None:
        baseline = capture_owned_thread_snapshot()
        releases_and_threads = [
            self._blocking_thread(
                name="autosport-census-duplicate-owner",
                daemon=False,
            )
            for _ in range(2)
        ]
        try:
            current = capture_owned_thread_snapshot()
            comparison = compare_owned_thread_snapshots(baseline, current)
            matching = [
                growth
                for growth in comparison.growth
                if growth.name == "autosport-census-duplicate-owner"
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].delta, 2)
        finally:
            for release, _ in releases_and_threads:
                release.set()
            for _, thread in releases_and_threads:
                thread.join(5.0)

    def test_daemon_product_thread_is_visible_but_foreign_thread_is_not(self) -> None:
        owned_release, owned = self._blocking_thread(
            name="autosport-census-daemon",
            daemon=True,
        )
        foreign_release, foreign = self._blocking_thread(
            name="third-party-background-worker",
            daemon=False,
        )
        try:
            snapshot = capture_owned_thread_snapshot()
            records = {(record.name, record.daemon) for record in snapshot.records}
            self.assertIn(("autosport-census-daemon", True), records)
            self.assertNotIn(("third-party-background-worker", False), records)
        finally:
            owned_release.set()
            foreign_release.set()
            owned.join(5.0)
            foreign.join(5.0)

    def test_records_are_deterministically_sorted(self) -> None:
        first_release, first = self._blocking_thread(
            name="autosport-census-z",
            daemon=False,
        )
        second_release, second = self._blocking_thread(
            name="autosport-census-a",
            daemon=False,
        )
        try:
            records = [
                record
                for record in capture_owned_thread_snapshot().records
                if record.name in {"autosport-census-a", "autosport-census-z"}
            ]
            self.assertEqual(
                [record.name for record in records],
                ["autosport-census-a", "autosport-census-z"],
            )
        finally:
            first_release.set()
            second_release.set()
            first.join(5.0)
            second.join(5.0)

    def test_missing_live_thread_identity_is_inconclusive_not_pass(self) -> None:
        class MissingIdentityThread:
            name = f"{AUTOSPORT_THREAD_PREFIX}missing-ident"
            daemon = False
            ident = None
            native_id = None

            @staticmethod
            def is_alive() -> bool:
                return True

        with patch(
            "autosport.runtime_resource_census.threading.enumerate",
            return_value=[MissingIdentityThread()],
        ):
            current = capture_owned_thread_snapshot()

        self.assertFalse(current.complete)
        comparison = compare_owned_thread_snapshots(
            OwnedThreadSnapshot(records=()),
            current,
        )
        self.assertEqual(comparison.status, ThreadCensusStatus.INCONCLUSIVE)
        self.assertEqual(comparison.growth, ())
        self.assertEqual(
            comparison.issues,
            ("live_owned_thread_missing_ident:autosport-missing-ident",),
        )

    def test_missing_and_present_identity_same_name_remains_inconclusive(self) -> None:
        class FakeThread:
            def __init__(self, ident: int | None, native_id: int | None) -> None:
                self.name = f"{AUTOSPORT_THREAD_PREFIX}same-name"
                self.daemon = False
                self.ident = ident
                self.native_id = native_id

            @staticmethod
            def is_alive() -> bool:
                return True

        with patch(
            "autosport.runtime_resource_census.threading.enumerate",
            return_value=[FakeThread(None, None), FakeThread(17, 23)],
        ):
            snapshot = capture_owned_thread_snapshot()

        self.assertEqual(len(snapshot.records), 2)
        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.records[0].ident, 17)
        self.assertIsNone(snapshot.records[1].ident)
        comparison = compare_owned_thread_snapshots(
            OwnedThreadSnapshot(records=()),
            snapshot,
        )
        self.assertEqual(comparison.status, ThreadCensusStatus.INCONCLUSIVE)

    def test_constructed_missing_identity_cannot_suppress_inconclusive_issue(self) -> None:
        snapshot = OwnedThreadSnapshot(
            records=(
                OwnedThreadRecord(
                    name="autosport-manual-missing-ident",
                    daemon=False,
                    ident=None,
                    native_id=None,
                ),
            ),
            issues=(),
        )

        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.counts, ())
        self.assertEqual(
            snapshot.integrity_issues,
            ("live_owned_thread_missing_ident:autosport-manual-missing-ident",),
        )
        comparison = compare_owned_thread_snapshots(
            OwnedThreadSnapshot(records=()),
            snapshot,
        )
        self.assertEqual(comparison.status, ThreadCensusStatus.INCONCLUSIVE)

    def test_unknown_constructed_record_type_is_inconclusive_not_crash(self) -> None:
        snapshot = OwnedThreadSnapshot(records=(object(),))  # type: ignore[arg-type]

        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.counts, ())
        self.assertEqual(
            snapshot.integrity_issues,
            ("invalid_owned_thread_record_type",),
        )
        self.assertEqual(snapshot.to_dict()["owned_threads"], [])
        comparison = compare_owned_thread_snapshots(
            OwnedThreadSnapshot(records=()),
            snapshot,
        )
        self.assertEqual(comparison.status, ThreadCensusStatus.INCONCLUSIVE)

    def test_exact_snapshot_type_is_required(self) -> None:
        snapshot = OwnedThreadSnapshot(records=())
        with self.assertRaises(TypeError):
            compare_owned_thread_snapshots(snapshot, object())  # type: ignore[arg-type]


class RuntimeResourceWorkspaceProbeTests(unittest.TestCase):
    def test_round_trip_probe_restores_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            marker = workspace / "marker.txt"
            marker.write_text("ok", encoding="utf-8")

            result = probe_workspace_move_round_trip(workspace)

            self.assertEqual(result.status, "PASS")
            self.assertTrue(workspace.is_dir())
            self.assertEqual(marker.read_text(encoding="utf-8"), "ok")
            self.assertEqual(
                result.windows_handle_semantics,
                __import__("sys").platform == "win32",
            )

    def test_disposable_probe_moves_then_deletes_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "disposable"
            workspace.mkdir()
            (workspace / "marker.txt").write_text("ok", encoding="utf-8")

            result = probe_disposable_workspace_move_delete(workspace)

            self.assertEqual(result.status, "PASS")
            self.assertFalse(workspace.exists())

    def test_missing_workspace_probe_fails_without_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            result = probe_workspace_move_round_trip(missing)

        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.error_type, "FileNotFoundError")

    def test_evidence_digest_is_canonical_and_order_independent(self) -> None:
        first = {"b": 2, "a": ["x", 1]}
        second = {"a": ["x", 1], "b": 2}

        self.assertEqual(stable_evidence_sha256(first), stable_evidence_sha256(second))
        self.assertEqual(len(stable_evidence_sha256(first)), 64)
        json.dumps(first, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
