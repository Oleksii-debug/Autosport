from __future__ import annotations

import unittest

from autosport.incident_risk_register import (
    IncidentRiskEntry,
    RegisterEntryKind,
    RiskEvidenceState,
    RiskSeverity,
    RiskStatus,
    derive_occurrence_entry_id,
    operator_projection,
)


class IncidentRiskProjectionIdentifierRedactionTests(unittest.TestCase):
    def test_operator_projection_redacts_credentials_from_identifier_tuples(self) -> None:
        occurrence = "evidence://provider/gap?api_key=secret-value"
        evidence = tuple(sorted((
            occurrence,
            "https://provider.example/receipt?access_token=another-secret",
        )))
        components = ("market_mirror",)
        entry = IncidentRiskEntry(
            entry_id=derive_occurrence_entry_id(
                kind=RegisterEntryKind.INCIDENT,
                affected_components=components,
                occurrence_evidence_refs=(occurrence,),
            ),
            revision=1,
            kind=RegisterEntryKind.INCIDENT,
            severity=RiskSeverity.HIGH,
            status=RiskStatus.OPEN,
            evidence_state=RiskEvidenceState.PARTIAL,
            opened_at="2026-10-06T05:00:00+00:00",
            updated_at="2026-10-06T05:01:00+00:00",
            title="Provider gap",
            summary="Operator-visible incident.",
            affected_components=components,
            occurrence_evidence_refs=(occurrence,),
            evidence_refs=evidence,
            requires_operator_action=True,
        )

        projection = operator_projection(entry)

        self.assertEqual(entry.occurrence_evidence_refs, (occurrence,))
        self.assertEqual(entry.evidence_refs, evidence)
        self.assertEqual(
            projection.occurrence_evidence_refs,
            ("evidence://provider/gap?[REDACTED]",),
        )
        self.assertEqual(
            projection.evidence_refs,
            (
                "evidence://provider/gap?[REDACTED]",
                "https://provider.example/receipt?[REDACTED]",
            ),
        )
        self.assertEqual(projection.fingerprint_sha256, entry.fingerprint_sha256)
        self.assertNotIn("secret-value", repr(projection))
        self.assertNotIn("another-secret", repr(projection))


    def test_operator_projection_redacts_uri_userinfo_credentials(self) -> None:
        occurrence = "postgresql://risk_user:super-secret-password@db.example/risk"
        evidence = tuple(sorted((
            occurrence,
            "https://api-user:another-secret@provider.example/incident/42",
            "https://opaque-credential-material@mirror.example/evidence/7",
        )))
        components = ("risk_store",)
        entry = IncidentRiskEntry(
            entry_id=derive_occurrence_entry_id(
                kind=RegisterEntryKind.INCIDENT,
                affected_components=components,
                occurrence_evidence_refs=(occurrence,),
            ),
            revision=1,
            kind=RegisterEntryKind.INCIDENT,
            severity=RiskSeverity.HIGH,
            status=RiskStatus.OPEN,
            evidence_state=RiskEvidenceState.PARTIAL,
            opened_at="2026-10-06T05:00:00+00:00",
            updated_at="2026-10-06T05:01:00+00:00",
            title="Risk store connectivity",
            summary="Operator-visible incident.",
            affected_components=components,
            occurrence_evidence_refs=(occurrence,),
            evidence_refs=evidence,
            requires_operator_action=True,
        )

        projection = operator_projection(entry)

        self.assertEqual(entry.occurrence_evidence_refs, (occurrence,))
        self.assertEqual(entry.evidence_refs, evidence)
        self.assertEqual(projection.fingerprint_sha256, entry.fingerprint_sha256)
        self.assertNotIn("risk_user", repr(projection))
        self.assertNotIn("super-secret-password", repr(projection))
        self.assertNotIn("api-user", repr(projection))
        self.assertNotIn("another-secret", repr(projection))
        self.assertNotIn("opaque-credential-material", repr(projection))
        self.assertIn("db.example/risk", repr(projection))
        self.assertIn("mirror.example/evidence/7", repr(projection))
        self.assertIn("provider.example/incident/42", repr(projection))


if __name__ == "__main__":
    unittest.main()
