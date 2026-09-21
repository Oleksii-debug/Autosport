from __future__ import annotations

import copy
import unittest

from autosport.incident_risk_register import (
    IncidentRiskEntry,
    IncidentRiskRegisterError,
    RegisterEntryKind,
    RiskEvidenceState,
    RiskSeverity,
    RiskStatus,
    operator_projection,
    operator_sort,
    validate_successor,
)


class IncidentRiskRegisterTests(unittest.TestCase):
    OPENED = "2026-09-21T07:00:00+00:00"
    UPDATED = "2026-09-21T07:05:00+00:00"

    @classmethod
    def _entry(cls, **overrides) -> IncidentRiskEntry:
        values = {
            "entry_id": "incident-provider-gap-001",
            "revision": 1,
            "kind": RegisterEntryKind.INCIDENT,
            "severity": RiskSeverity.HIGH,
            "status": RiskStatus.INVESTIGATING,
            "evidence_state": RiskEvidenceState.PARTIAL,
            "opened_at": cls.OPENED,
            "updated_at": cls.UPDATED,
            "title": "Provider gap during PAPER campaign",
            "summary": "Observed quote ingestion gap requires operator review.",
            "mitigation": "",
            "residual_risk": "Decision inputs may remain incomplete until recovery.",
            "affected_components": ("live_ingestion", "market_mirror"),
            "evidence_refs": ("evidence://provider-gap/001",),
            "model_version_ids": (),
            "requires_operator_action": True,
        }
        values.update(overrides)
        return IncidentRiskEntry(**values)

    def test_round_trip_fingerprint_and_operator_projection_are_deterministic(self) -> None:
        entry = self._entry()
        payload = entry.to_dict()
        restored = IncidentRiskEntry.from_dict(copy.deepcopy(payload))

        self.assertEqual(restored, entry)
        self.assertEqual(restored.fingerprint_sha256, entry.fingerprint_sha256)
        self.assertEqual(len(entry.fingerprint_sha256), 64)

        projection = operator_projection(entry)
        self.assertEqual(projection.entry_id, entry.entry_id)
        self.assertEqual(projection.kind_key, "ui.risk_register.kind.incident")
        self.assertEqual(projection.severity_key, "ui.risk_register.severity.high")
        self.assertEqual(projection.status_key, "ui.risk_register.status.investigating")
        self.assertEqual(projection.evidence_key, "ui.risk_register.evidence.partial")
        self.assertEqual(projection.title, entry.title)
        self.assertEqual(projection.evidence_refs, entry.evidence_refs)
        self.assertTrue(projection.requires_operator_action)
        self.assertEqual(projection.fingerprint_sha256, entry.fingerprint_sha256)

    def test_strict_schema_rejects_unknown_missing_or_mistyped_fields(self) -> None:
        payload = self._entry().to_dict()

        unknown = dict(payload)
        unknown["execution_authorized"] = True
        with self.assertRaisesRegex(IncidentRiskRegisterError, "exactly canonical fields"):
            IncidentRiskEntry.from_dict(unknown)

        missing = dict(payload)
        missing.pop("summary")
        with self.assertRaisesRegex(IncidentRiskRegisterError, "exactly canonical fields"):
            IncidentRiskEntry.from_dict(missing)

        wrong_bool = dict(payload)
        wrong_bool["requires_operator_action"] = 1
        with self.assertRaisesRegex(IncidentRiskRegisterError, "must be a bool"):
            IncidentRiskEntry.from_dict(wrong_bool)

        wrong_revision = dict(payload)
        wrong_revision["revision"] = True
        with self.assertRaisesRegex(IncidentRiskRegisterError, "positive non-boolean integer"):
            IncidentRiskEntry.from_dict(wrong_revision)

    def test_canonical_text_timestamp_and_tuple_rules_fail_closed(self) -> None:
        with self.assertRaisesRegex(IncidentRiskRegisterError, "canonical trimmed text"):
            self._entry(title=" leading")
        with self.assertRaisesRegex(IncidentRiskRegisterError, "must not contain NUL"):
            self._entry(summary="bad\x00summary")
        with self.assertRaisesRegex(IncidentRiskRegisterError, "timezone-aware"):
            self._entry(updated_at="2026-09-21T07:05:00")
        with self.assertRaisesRegex(IncidentRiskRegisterError, "canonical UTC"):
            self._entry(updated_at="2026-09-21T09:05:00+02:00")
        with self.assertRaisesRegex(IncidentRiskRegisterError, "must not precede"):
            self._entry(updated_at="2026-09-21T06:59:59+00:00")
        with self.assertRaisesRegex(IncidentRiskRegisterError, "sorted and unique"):
            self._entry(affected_components=("market_mirror", "live_ingestion"))
        with self.assertRaisesRegex(IncidentRiskRegisterError, "sorted and unique"):
            self._entry(evidence_refs=("evidence://x", "evidence://x"))

    def test_model_risk_requires_exact_model_identity(self) -> None:
        with self.assertRaisesRegex(IncidentRiskRegisterError, "model_version_id"):
            self._entry(kind=RegisterEntryKind.MODEL_RISK, model_version_ids=())

        entry = self._entry(
            entry_id="model-risk-calibration-001",
            kind=RegisterEntryKind.MODEL_RISK,
            model_version_ids=("model-v17",),
        )
        self.assertEqual(entry.model_version_ids, ("model-v17",))

    def test_evidence_and_closure_cannot_claim_more_than_recorded(self) -> None:
        with self.assertRaisesRegex(IncidentRiskRegisterError, "evidence_ref"):
            self._entry(
                evidence_state=RiskEvidenceState.VERIFIED,
                evidence_refs=(),
            )
        with self.assertRaisesRegex(IncidentRiskRegisterError, "non-empty mitigation"):
            self._entry(
                status=RiskStatus.MITIGATING,
                mitigation="",
            )
        with self.assertRaisesRegex(IncidentRiskRegisterError, "verified evidence"):
            self._entry(
                status=RiskStatus.CLOSED,
                mitigation="Provider recovered and replay was reconciled.",
                evidence_state=RiskEvidenceState.PARTIAL,
                requires_operator_action=False,
            )
        with self.assertRaisesRegex(IncidentRiskRegisterError, "cannot require operator action"):
            self._entry(
                status=RiskStatus.CLOSED,
                mitigation="Provider recovered and replay was reconciled.",
                evidence_state=RiskEvidenceState.VERIFIED,
                requires_operator_action=True,
            )

        closed = self._entry(
            revision=3,
            updated_at="2026-09-21T08:00:00+00:00",
            status=RiskStatus.CLOSED,
            mitigation="Provider recovered and replay was reconciled.",
            residual_risk="No unresolved gap remains in the recorded interval.",
            evidence_state=RiskEvidenceState.VERIFIED,
            requires_operator_action=False,
        )
        self.assertEqual(closed.status, RiskStatus.CLOSED)

    def test_open_critical_risk_requires_operator_action(self) -> None:
        with self.assertRaisesRegex(IncidentRiskRegisterError, "must require operator action"):
            self._entry(
                severity=RiskSeverity.CRITICAL,
                requires_operator_action=False,
            )
        critical = self._entry(severity=RiskSeverity.CRITICAL)
        self.assertTrue(critical.requires_operator_action)

    def test_successor_requires_same_identity_contiguous_revision_and_forward_time(self) -> None:
        previous = self._entry()
        candidate = self._entry(
            revision=2,
            updated_at="2026-09-21T07:06:00+00:00",
            status=RiskStatus.MITIGATING,
            mitigation="Fail closed while provider recovery is verified.",
        )
        validate_successor(previous, candidate)

        with self.assertRaisesRegex(IncidentRiskRegisterError, "preserve entry_id"):
            validate_successor(
                previous,
                self._entry(
                    entry_id="other-entry",
                    revision=2,
                    updated_at="2026-09-21T07:06:00+00:00",
                ),
            )
        with self.assertRaisesRegex(IncidentRiskRegisterError, "preserve entry kind"):
            validate_successor(
                previous,
                self._entry(
                    revision=2,
                    updated_at="2026-09-21T07:06:00+00:00",
                    kind=RegisterEntryKind.MODEL_RISK,
                    model_version_ids=("model-v1",),
                ),
            )
        with self.assertRaisesRegex(IncidentRiskRegisterError, "contiguous"):
            validate_successor(
                previous,
                self._entry(
                    revision=3,
                    updated_at="2026-09-21T07:06:00+00:00",
                ),
            )
        with self.assertRaisesRegex(IncidentRiskRegisterError, "strictly forward"):
            validate_successor(previous, self._entry(revision=2))

    def test_operator_sort_prioritizes_action_then_severity_then_recency(self) -> None:
        low_action = self._entry(
            entry_id="a",
            severity=RiskSeverity.LOW,
            requires_operator_action=True,
            updated_at="2026-09-21T07:10:00+00:00",
        )
        critical_action = self._entry(
            entry_id="b",
            severity=RiskSeverity.CRITICAL,
            requires_operator_action=True,
            updated_at="2026-09-21T07:06:00+00:00",
        )
        high_no_action = self._entry(
            entry_id="c",
            severity=RiskSeverity.HIGH,
            requires_operator_action=False,
            updated_at="2026-09-21T07:20:00+00:00",
        )
        medium_no_action_newer = self._entry(
            entry_id="d",
            severity=RiskSeverity.MEDIUM,
            requires_operator_action=False,
            updated_at="2026-09-21T07:30:00+00:00",
        )

        ordered = operator_sort(
            (medium_no_action_newer, high_no_action, low_action, critical_action)
        )
        self.assertEqual(
            tuple(entry.entry_id for entry in ordered),
            ("b", "a", "c", "d"),
        )

    def test_register_contract_has_no_execution_or_release_truth_fields(self) -> None:
        keys = set(self._entry().to_dict())
        self.assertNotIn("real_money_execution", keys)
        self.assertNotIn("execution_authorized", keys)
        self.assertNotIn("human_tested", keys)
        self.assertNotIn("nvda_verified", keys)
        self.assertNotIn("v1_ready", keys)
        self.assertNotIn("whole_product_complete", keys)


if __name__ == "__main__":
    unittest.main()
