import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from autosport.incident_risk_register import (
    RiskEvidenceState,
    RiskSeverity,
    RiskStatus,
    validate_successor,
)
from autosport.incident_source_health_projection import (
    SourceHealthIncidentProjectionError,
    current_source_health_incident,
    project_source_health_incidents,
    resolve_source_health_evidence,
    validate_source_health_incident_evidence,
)
from autosport.ingestion_health import SourceHealthStore


class SourceHealthIncidentProjectionTests(unittest.TestCase):
    @staticmethod
    def store(directory: str) -> SourceHealthStore:
        return SourceHealthStore(Path(directory) / "source-health.json")

    @staticmethod
    def record_degraded(
        store: SourceHealthStore,
        *,
        source_id: str = "provider-a",
        now: str = "2026-09-23T01:00:00+00:00",
        quality_flags: tuple[str, ...] = ("PARTIAL_SNAPSHOT",),
    ) -> None:
        store.record_success(
            source_id,
            now=now,
            received=1,
            accepted=1,
            rejected=0,
            cursor="cursor-1",
            latest_source_ts="2026-09-23T00:59:59+00:00",
            quality_flags=quality_flags,
        )

    @staticmethod
    def record_healthy(
        store: SourceHealthStore,
        *,
        source_id: str = "provider-a",
        now: str = "2026-09-23T01:02:00+00:00",
    ) -> None:
        store.record_success(
            source_id,
            now=now,
            received=1,
            accepted=1,
            rejected=0,
            cursor="cursor-healthy",
            latest_source_ts="2026-09-23T01:01:59+00:00",
            quality_flags=(),
        )

    def test_degraded_health_opens_product_derived_verified_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            self.record_degraded(store)

            history = project_source_health_incidents(store, source_id="provider-a")

            self.assertEqual(len(history), 1)
            entry = history[0]
            self.assertEqual(entry.revision, 1)
            self.assertEqual(entry.status, RiskStatus.OPEN)
            self.assertEqual(entry.severity, RiskSeverity.MEDIUM)
            self.assertEqual(entry.evidence_state, RiskEvidenceState.VERIFIED)
            self.assertTrue(entry.requires_operator_action)
            self.assertEqual(len(entry.occurrence_evidence_refs), 1)
            self.assertEqual(
                entry.occurrence_evidence_refs,
                entry.evidence_refs,
            )
            self.assertNotIn("PARTIAL_SNAPSHOT", entry.summary)
            self.assertNotIn("provider-a", json.dumps(entry.to_dict(), ensure_ascii=False))

            resolved = resolve_source_health_evidence(
                store,
                source_id="provider-a",
                evidence_ref=entry.occurrence_evidence_refs[0],
            )
            self.assertEqual(resolved.status, "degraded")
            self.assertEqual(resolved.transition_order, 1)

    def test_failed_successor_escalates_without_caller_severity_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            self.record_degraded(
                store,
                quality_flags=("CRITICAL",),
            )
            store.record_failure(
                "provider-a",
                now="2026-09-23T01:01:00+00:00",
                error=ConnectionError("Bearer TOP-SECRET-TOKEN"),
            )

            history = project_source_health_incidents(store, source_id="provider-a")

            self.assertEqual(len(history), 2)
            first, second = history
            self.assertEqual(first.severity, RiskSeverity.MEDIUM)
            self.assertEqual(second.severity, RiskSeverity.HIGH)
            self.assertEqual(second.entry_id, first.entry_id)
            self.assertEqual(second.revision, 2)
            self.assertEqual(len(second.evidence_refs), 2)
            validate_successor(first, second)

            rendered = json.dumps(
                [entry.to_dict() for entry in history],
                ensure_ascii=False,
                sort_keys=True,
            )
            self.assertNotIn("TOP-SECRET-TOKEN", rendered)
            self.assertNotIn("Bearer", rendered)
            self.assertNotIn("CRITICAL", rendered)

    def test_only_later_canonical_healthy_transition_closes_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            store.record_failure(
                "provider-a",
                now="2026-09-23T01:00:00+00:00",
                error=TimeoutError("provider unavailable"),
            )

            open_entry = current_source_health_incident(
                store,
                source_id="provider-a",
            )
            self.assertIsNotNone(open_entry)
            assert open_entry is not None
            self.assertEqual(open_entry.status, RiskStatus.OPEN)

            unchanged = current_source_health_incident(
                SourceHealthStore(store.path),
                source_id="provider-a",
            )
            self.assertEqual(unchanged, open_entry)

            self.record_healthy(store, now="2026-09-23T01:02:00+00:00")
            history = project_source_health_incidents(store, source_id="provider-a")
            closed = history[-1]

            self.assertEqual(len(history), 2)
            self.assertEqual(closed.entry_id, open_entry.entry_id)
            self.assertEqual(closed.revision, 2)
            self.assertEqual(closed.status, RiskStatus.RESOLVED)
            self.assertEqual(closed.evidence_state, RiskEvidenceState.VERIFIED)
            self.assertFalse(closed.requires_operator_action)
            self.assertTrue(closed.mitigation)
            self.assertEqual(
                current_source_health_incident(store, source_id="provider-a"),
                None,
            )
            validate_successor(open_entry, closed)

            closure_ref = next(
                ref
                for ref in closed.evidence_refs
                if ref not in open_entry.evidence_refs
            )
            closure = resolve_source_health_evidence(
                store,
                source_id="provider-a",
                evidence_ref=closure_ref,
            )
            self.assertEqual(closure.status, "healthy")

    def test_use_time_validation_rejects_caller_severity_and_resolution_spoofs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            self.record_degraded(store)
            entry = project_source_health_incidents(
                store,
                source_id="provider-a",
            )[0]

            validate_source_health_incident_evidence(
                store,
                source_id="provider-a",
                entry=entry,
            )

            severity_spoof = replace(entry, severity=RiskSeverity.CRITICAL)
            with self.assertRaisesRegex(
                SourceHealthIncidentProjectionError,
                "severity does not match",
            ):
                validate_source_health_incident_evidence(
                    store,
                    source_id="provider-a",
                    entry=severity_spoof,
                )

            resolution_spoof = replace(
                entry,
                status=RiskStatus.RESOLVED,
                mitigation="Caller claims recovery without health evidence.",
                requires_operator_action=False,
            )
            with self.assertRaisesRegex(
                SourceHealthIncidentProjectionError,
                "requires canonical healthy closure",
            ):
                validate_source_health_incident_evidence(
                    store,
                    source_id="provider-a",
                    entry=resolution_spoof,
                )

    def test_use_time_validation_rejects_skipped_intermediate_health_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            self.record_degraded(store, now="2026-09-23T01:00:00+00:00")
            store.record_failure(
                "provider-a",
                now="2026-09-23T01:01:00+00:00",
                error=ConnectionError("down"),
            )
            self.record_healthy(store, now="2026-09-23T01:02:00+00:00")
            history = project_source_health_incidents(
                store,
                source_id="provider-a",
            )
            closed = history[-1]
            validate_source_health_incident_evidence(
                store,
                source_id="provider-a",
                entry=closed,
            )

            occurrence_ref = closed.occurrence_evidence_refs[0]
            closure_ref = next(
                ref
                for ref in closed.evidence_refs
                if resolve_source_health_evidence(
                    store,
                    source_id="provider-a",
                    evidence_ref=ref,
                ).status
                == "healthy"
            )
            missing_middle = replace(
                closed,
                evidence_refs=tuple(sorted((occurrence_ref, closure_ref))),
            )
            with self.assertRaisesRegex(
                SourceHealthIncidentProjectionError,
                "contiguous canonical transition segment",
            ):
                validate_source_health_incident_evidence(
                    store,
                    source_id="provider-a",
                    entry=missing_middle,
                )

    def test_restart_reprojects_identical_history_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            self.record_degraded(store)
            store.record_failure(
                "provider-a",
                now="2026-09-23T01:01:00+00:00",
                error=ConnectionError("down"),
            )
            self.record_healthy(store)

            before = project_source_health_incidents(store, source_id="provider-a")
            reopened = SourceHealthStore(store.path)
            after = project_source_health_incidents(
                reopened,
                source_id="provider-a",
            )

            self.assertEqual(after, before)
            self.assertEqual(
                [entry.fingerprint_sha256 for entry in after],
                [entry.fingerprint_sha256 for entry in before],
            )

    def test_equal_time_transition_order_fails_closed_instead_of_inventing_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            same_time = "2026-09-23T01:00:00+00:00"
            self.record_degraded(store, now=same_time)
            store.record_failure(
                "provider-a",
                now=same_time,
                error=ConnectionError("later durable transition, same evidence time"),
            )

            with self.assertRaisesRegex(
                SourceHealthIncidentProjectionError,
                "equal-time source-health successors",
            ):
                project_source_health_incidents(store, source_id="provider-a")

    def test_equal_time_recovery_fails_closed_instead_of_false_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            same_time = "2026-09-23T01:00:00+00:00"
            store.record_failure(
                "provider-a",
                now=same_time,
                error=ConnectionError("down"),
            )
            self.record_healthy(store, now=same_time)

            with self.assertRaisesRegex(
                SourceHealthIncidentProjectionError,
                "equal-time source-health closure",
            ):
                project_source_health_incidents(store, source_id="provider-a")

    def test_other_source_recovery_cannot_close_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            store.record_failure(
                "provider-a",
                now="2026-09-23T01:00:00+00:00",
                error=ConnectionError("a down"),
            )
            self.record_healthy(
                store,
                source_id="provider-b",
                now="2026-09-23T01:01:00+00:00",
            )

            incident = current_source_health_incident(
                store,
                source_id="provider-a",
            )
            self.assertIsNotNone(incident)
            assert incident is not None
            self.assertEqual(incident.status, RiskStatus.OPEN)
            self.assertIsNone(
                current_source_health_incident(store, source_id="provider-b")
            )

            with self.assertRaisesRegex(
                SourceHealthIncidentProjectionError,
                "absent from canonical history",
            ):
                resolve_source_health_evidence(
                    store,
                    source_id="provider-b",
                    evidence_ref=incident.occurrence_evidence_refs[0],
                )

    def test_healthy_only_source_does_not_mint_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            self.record_healthy(store)

            self.assertEqual(
                project_source_health_incidents(store, source_id="provider-a"),
                (),
            )
            self.assertIsNone(
                current_source_health_incident(store, source_id="provider-a")
            )

    def test_new_impairment_after_recovery_gets_new_occurrence_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            self.record_degraded(store, now="2026-09-23T01:00:00+00:00")
            self.record_healthy(store, now="2026-09-23T01:01:00+00:00")
            store.record_failure(
                "provider-a",
                now="2026-09-23T01:02:00+00:00",
                error=ConnectionError("new outage"),
            )

            history = project_source_health_incidents(store, source_id="provider-a")

            self.assertEqual(len(history), 3)
            first_open, first_closed, second_open = history
            self.assertEqual(first_closed.status, RiskStatus.RESOLVED)
            self.assertEqual(second_open.status, RiskStatus.OPEN)
            self.assertEqual(second_open.revision, 1)
            self.assertNotEqual(second_open.entry_id, first_open.entry_id)
            self.assertNotEqual(
                second_open.occurrence_evidence_refs,
                first_open.occurrence_evidence_refs,
            )

    def test_unknown_evidence_ref_and_invalid_ref_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            self.record_degraded(store)

            for evidence_ref in (
                "caller://verified",
                "source-health-evidence:" + "0" * 64,
            ):
                with self.subTest(evidence_ref=evidence_ref):
                    with self.assertRaises(SourceHealthIncidentProjectionError):
                        resolve_source_health_evidence(
                            store,
                            source_id="provider-a",
                            evidence_ref=evidence_ref,
                        )


if __name__ == "__main__":
    unittest.main()
