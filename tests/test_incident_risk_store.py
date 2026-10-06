from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from autosport.incident_risk_register import (
    IncidentRiskEntry,
    RegisterEntryKind,
    RiskEvidenceState,
    RiskSeverity,
    RiskStatus,
    derive_occurrence_entry_id,
)
from autosport.incident_risk_store import (
    IncidentRiskStore,
    IncidentRiskStoreError,
    STORE_SCHEMA,
    STORE_SCHEMA_VERSION,
)


class IncidentRiskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.authority_temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.authority_root = Path(self.authority_temp.name)
        self.store = self._store(self.workspace)

    def tearDown(self) -> None:
        self.temp.cleanup()
        self.authority_temp.cleanup()

    def _store(self, workspace: Path) -> IncidentRiskStore:
        return IncidentRiskStore(
            workspace,
            authority_root=self.authority_root,
        )

    @staticmethod
    def _entry(
        entry_id: str = "incident-a",
        revision: int = 1,
        **overrides,
    ) -> IncidentRiskEntry:
        occurrence_ref = f"evidence://gap/{entry_id}/occurrence"
        revision_refs = tuple(
            f"evidence://gap/{entry_id}/revision/{number:06d}"
            for number in range(1, revision + 1)
        )
        values = {
            "revision": revision,
            "kind": RegisterEntryKind.INCIDENT,
            "severity": RiskSeverity.HIGH,
            "status": RiskStatus.OPEN,
            "evidence_state": RiskEvidenceState.PARTIAL,
            "opened_at": "2026-09-21T10:00:00+00:00",
            "updated_at": f"2026-09-21T10:{revision:02d}:00+00:00",
            "title": "Provider gap",
            "summary": "Durable operator evidence.",
            "mitigation": "",
            "residual_risk": "Inputs may be incomplete.",
            "affected_components": ("market_mirror",),
            "occurrence_evidence_refs": (occurrence_ref,),
            "evidence_refs": (occurrence_ref,) + revision_refs,
            "model_version_ids": (),
            "requires_operator_action": True,
        }
        values.update(overrides)
        values["entry_id"] = derive_occurrence_entry_id(
            kind=values["kind"],
            affected_components=values["affected_components"],
            occurrence_evidence_refs=values["occurrence_evidence_refs"],
            model_version_ids=values["model_version_ids"],
        )
        return IncidentRiskEntry(**values)

    @staticmethod
    def _core_hash(payload: dict[str, object]) -> str:
        core = {
            "schema": payload["schema"],
            "schema_version": payload["schema_version"],
            "histories": payload["histories"],
            "availability": payload["availability"],
        }
        return hashlib.sha256(
            json.dumps(
                core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def test_absence_is_empty_without_creating_state(self) -> None:
        snapshot = self.store.load()
        self.assertEqual(snapshot.histories, ())
        self.assertEqual(snapshot.current_entries, ())
        self.assertFalse(self.store.path.exists())
        self.assertEqual(len(snapshot.content_sha256), 64)

    def test_restart_round_trip_and_contiguous_append(self) -> None:
        first = self._entry()
        second = self._entry(
            revision=2,
            status=RiskStatus.MITIGATING,
            mitigation="Keep decisions fail-closed until replay completes.",
        )

        after_first = self.store.append(first)
        after_second = self.store.append(second)
        reopened = self._store(self.workspace).load()

        self.assertEqual(after_first.history(first.entry_id), (first,))
        self.assertEqual(
            after_second.history(first.entry_id),
            (first, second),
        )
        self.assertEqual(reopened, after_second)
        self.assertEqual(reopened.current_entries, (second,))
        self.assertEqual(
            reopened.content_sha256,
            after_second.content_sha256,
        )

    def test_causal_as_of_hides_future_incident_and_revision(self) -> None:
        first = self._entry()
        second = self._entry(
            revision=2,
            status=RiskStatus.MITIGATING,
            mitigation="Keep decisions fail-closed until replay completes.",
        )
        later_incident = self._entry(
            entry_id="incident-b",
            updated_at="2026-09-21T10:03:00+00:00",
        )

        with mock.patch(
            "autosport.incident_risk_store._availability_now",
            side_effect=(
                "2026-09-21T10:01:00+00:00",
                "2026-09-21T10:02:00+00:00",
                "2026-09-21T10:03:00+00:00",
            ),
        ):
            self.store.append(first)
            self.store.append(second)
            final = self.store.append(later_incident)

        self.assertEqual(
            final.current_entries_as_of("2026-09-21T10:00:59+00:00"),
            (),
        )
        self.assertEqual(
            final.current_entries_as_of("2026-09-21T10:01:00+00:00"),
            (first,),
        )
        self.assertEqual(
            final.current_entries_as_of("2026-09-21T10:01:59+00:00"),
            (first,),
        )
        self.assertEqual(
            final.current_entries_as_of("2026-09-21T10:02:00+00:00"),
            (second,),
        )
        self.assertEqual(
            self._store(self.workspace).load_as_of(
                "2026-09-21T10:03:00+00:00"
            ),
            (second, later_incident),
        )

    def test_causal_as_of_uses_product_availability_not_caller_updated_at(self) -> None:
        backdated = self._entry(
            updated_at="2026-09-21T10:01:00+00:00",
        )
        with mock.patch(
            "autosport.incident_risk_store._availability_now",
            return_value="2026-09-21T12:00:00+00:00",
        ):
            snapshot = self.store.append(backdated)

        self.assertEqual(
            snapshot.current_entries_as_of(
                "2026-09-21T11:59:59+00:00"
            ),
            (),
        )
        self.assertEqual(
            snapshot.current_entries_as_of(
                "2026-09-21T12:00:00+00:00"
            ),
            (backdated,),
        )
        self.assertEqual(
            self._store(self.workspace).load_as_of(
                "2026-09-21T11:59:59+00:00"
            ),
            (),
        )

    def test_availability_clock_regression_fails_closed(self) -> None:
        first = self._entry()
        second = self._entry(
            revision=2,
            status=RiskStatus.MITIGATING,
            mitigation="Mitigate.",
        )
        with mock.patch(
            "autosport.incident_risk_store._availability_now",
            return_value="2026-09-21T10:05:00+00:00",
        ):
            before = self.store.append(first)

        with mock.patch(
            "autosport.incident_risk_store._availability_now",
            return_value="2026-09-21T10:04:00+00:00",
        ):
            with self.assertRaisesRegex(
                IncidentRiskStoreError,
                "availability clock moved backwards",
            ):
                self.store.append(second)

        self.assertEqual(self.store.load(), before)

    def test_entry_updated_at_cannot_be_future_of_product_availability(self) -> None:
        future_dated = self._entry(
            updated_at="2026-09-21T10:10:00+00:00",
        )
        with mock.patch(
            "autosport.incident_risk_store._availability_now",
            return_value="2026-09-21T10:05:00+00:00",
        ):
            with self.assertRaisesRegex(
                IncidentRiskStoreError,
                "availability cannot precede entry.updated_at",
            ):
                self.store.append(future_dated)
        self.assertFalse(self.store.path.exists())

    def test_causal_as_of_requires_exact_canonical_utc_cutoff(self) -> None:
        snapshot = self.store.append(self._entry())

        for invalid in (
            "2026-09-21T10:01:00",
            "2026-09-21T12:01:00+02:00",
            " 2026-09-21T10:01:00+00:00",
            "2026-09-21T10:01:00Z",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    IncidentRiskStoreError,
                    "as_of",
                ):
                    snapshot.current_entries_as_of(invalid)

        with self.assertRaisesRegex(IncidentRiskStoreError, "as_of"):
            snapshot.current_entries_as_of(None)  # type: ignore[arg-type]

    def test_established_store_file_deletion_fails_closed(self) -> None:
        self.store.append(self._entry())
        self.store.path.unlink()

        with self.assertRaisesRegex(
            IncidentRiskStoreError,
            "continuity authority rejects",
        ):
            self._store(self.workspace).load()

    def test_stale_store_rollback_fails_closed(self) -> None:
        self.store.append(self._entry())
        old_bytes = self.store.path.read_bytes()
        self.store.append(
            self._entry(
                revision=2,
                status=RiskStatus.MITIGATING,
                mitigation="Mitigate.",
            )
        )
        self.store.path.write_bytes(old_bytes)

        with self.assertRaisesRegex(
            IncidentRiskStoreError,
            "continuity authority rejects",
        ):
            self._store(self.workspace).load()

    def test_restart_commits_exact_published_prepare_after_commit_crash(self) -> None:
        entry = self._entry()
        with mock.patch.object(
            self.store._authority,
            "commit",
            side_effect=RuntimeError("simulated crash before authority commit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.store.append(entry)

        self.assertTrue(self.store.path.exists())
        recovered = self._store(self.workspace).load()
        self.assertEqual(recovered.history(entry.entry_id), (entry,))
        self.assertEqual(self._store(self.workspace).load(), recovered)

    def test_restart_aborts_prepare_when_local_publish_never_happened(self) -> None:
        entry = self._entry()
        with mock.patch(
            "autosport.incident_risk_store.atomic_write_json",
            side_effect=OSError("simulated publish failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated publish failure"):
                self.store.append(entry)

        self.assertFalse(self.store.path.exists())
        recovered = self._store(self.workspace).load()
        self.assertEqual(recovered.histories, ())
        self.assertEqual(self._store(self.workspace).load(), recovered)

    def test_exact_latest_reappend_is_idempotent_but_conflict_fails(self) -> None:
        entry = self._entry()
        first = self.store.append(entry)
        before_bytes = self.store.path.read_bytes()

        repeated = self.store.append(entry)
        self.assertEqual(repeated, first)
        self.assertEqual(self.store.path.read_bytes(), before_bytes)

        conflicting = self._entry(title="Different same revision")
        with self.assertRaisesRegex(
            IncidentRiskStoreError,
            "cannot be rebound",
        ):
            self.store.append(conflicting)
        self.assertEqual(self.store.load(), first)

    def test_revision_sequence_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            IncidentRiskStoreError,
            "begin at revision 1",
        ):
            self.store.append(
                self._entry(entry_id="new", revision=2)
            )

        first = self._entry()
        self.store.append(first)
        with self.assertRaisesRegex(
            IncidentRiskStoreError,
            "contiguous successor",
        ):
            self.store.append(self._entry(revision=3))
        self.assertEqual(
            self.store.load().history(first.entry_id),
            (first,),
        )

    def test_input_order_has_deterministic_store_identity(self) -> None:
        a = self._entry(entry_id="a")
        b = self._entry(entry_id="b")
        left = self._store(self.workspace / "left")
        right = self._store(self.workspace / "right")

        with mock.patch(
            "autosport.incident_risk_store._availability_now",
            return_value="2026-09-21T10:05:00+00:00",
        ):
            left.append(b)
            left_final = left.append(a)
            right.append(a)
            right_final = right.append(b)

        self.assertEqual(left_final.histories, right_final.histories)
        self.assertEqual(
            left_final.availability,
            right_final.availability,
        )
        self.assertEqual(
            left_final.content_sha256,
            right_final.content_sha256,
        )
        self.assertEqual(
            left.path.read_text("utf-8"),
            right.path.read_text("utf-8"),
        )

    def test_concurrent_independent_appends_do_not_lose_update(self) -> None:
        entries = (
            self._entry(entry_id="a"),
            self._entry(entry_id="b"),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            tuple(executor.map(self.store.append, entries))

        loaded = self.store.load()
        self.assertEqual(
            tuple(
                entry.entry_id
                for entry in loaded.current_entries
            ),
            tuple(sorted(entry.entry_id for entry in entries)),
        )

    def test_duplicate_json_keys_fail_closed(self) -> None:
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(
            '{"schema":"autosport.incident_model_risk_store",'
            '"schema":"autosport.incident_model_risk_store",'
            '"schema_version":1,"histories":[],'
            '"content_sha256":"' + ("0" * 64) + '"}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            IncidentRiskStoreError,
            "invalid .* JSON",
        ):
            self.store.load()

    def test_content_hash_tamper_fails_closed(self) -> None:
        self.store.append(self._entry())
        payload = json.loads(
            self.store.path.read_text("utf-8")
        )
        payload["content_sha256"] = "0" * 64
        self.store.path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            IncidentRiskStoreError,
            "content hash mismatch",
        ):
            self.store.load()

    def test_self_consistent_noncanonical_order_fails(self) -> None:
        self.store.append(self._entry(entry_id="a"))
        self.store.append(self._entry(entry_id="b"))
        payload = json.loads(
            self.store.path.read_text("utf-8")
        )
        payload["histories"].reverse()
        payload["content_sha256"] = self._core_hash(payload)
        self.store.path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            IncidentRiskStoreError,
            "sorted by unique entry_id",
        ):
            self.store.load()

    def test_self_consistent_availability_backdating_fails_continuity(self) -> None:
        entry = self._entry()
        with mock.patch(
            "autosport.incident_risk_store._availability_now",
            return_value="2026-09-21T12:00:00+00:00",
        ):
            self.store.append(entry)

        payload = json.loads(
            self.store.path.read_text("utf-8")
        )
        payload["availability"][0]["available_at"] = (
            "2026-09-21T10:02:00+00:00"
        )
        payload["content_sha256"] = self._core_hash(payload)
        self.store.path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            IncidentRiskStoreError,
            "continuity authority rejects",
        ):
            self._store(self.workspace).load()

    def test_self_consistent_revision_gap_fails(self) -> None:
        first = self._entry()
        second = self._entry(
            revision=2,
            status=RiskStatus.MITIGATING,
            mitigation="Mitigate.",
        )
        self.store.append(first)
        self.store.append(second)
        payload = json.loads(
            self.store.path.read_text("utf-8")
        )
        payload["histories"][0]["revisions"][1]["revision"] = 3
        payload["content_sha256"] = self._core_hash(payload)
        self.store.path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            IncidentRiskStoreError,
            "revision chain is invalid",
        ):
            self.store.load()

    def test_wrapper_entry_id_mismatch_fails(self) -> None:
        self.store.append(self._entry())
        payload = json.loads(
            self.store.path.read_text("utf-8")
        )
        payload["histories"][0]["entry_id"] = "forged-wrapper"
        payload["content_sha256"] = self._core_hash(payload)
        self.store.path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            IncidentRiskStoreError,
            "wrapper entry_id",
        ):
            self.store.load()

    def test_root_schema_and_truth_boundary_are_exact(self) -> None:
        snapshot = self.store.append(self._entry())
        payload = json.loads(
            self.store.path.read_text("utf-8")
        )
        self.assertEqual(payload["schema"], STORE_SCHEMA)
        self.assertEqual(
            payload["schema_version"],
            STORE_SCHEMA_VERSION,
        )
        self.assertEqual(
            payload["content_sha256"],
            snapshot.content_sha256,
        )
        self.assertEqual(
            len(payload["availability"]),
            sum(len(history) for history in snapshot.histories),
        )
        self.assertEqual(
            {
                (record["entry_id"], record["revision"])
                for record in payload["availability"]
            },
            {
                (entry.entry_id, entry.revision)
                for history in snapshot.histories
                for entry in history
            },
        )
        serialized = json.dumps(payload)
        for forbidden in (
            "real_money_execution",
            "execution_authorized",
            "human_tested",
            "nvda_verified",
            "v1_ready",
            "whole_product_complete",
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
