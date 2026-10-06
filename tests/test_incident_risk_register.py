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
    derive_occurrence_entry_id,
    operator_projection,
    operator_sort,
    validate_successor,
)


class IncidentRiskRegisterTests(unittest.TestCase):
    OPENED = "2026-09-21T07:00:00+00:00"
    UPDATED = "2026-09-21T07:05:00+00:00"

    @classmethod
    def _entry(cls, occurrence_tag: str = "001", **overrides) -> IncidentRiskEntry:
        occurrence_ref = f"evidence://provider-gap/{occurrence_tag}"
        values = {
            "revision": 1,
            "kind": RegisterEntryKind.INCIDENT,
            "severity": RiskSeverity.HIGH,
            "status": RiskStatus.OPEN,
            "evidence_state": RiskEvidenceState.PARTIAL,
            "opened_at": cls.OPENED,
            "updated_at": cls.UPDATED,
            "title": "Provider gap during PAPER campaign",
            "summary": "Observed quote ingestion gap requires operator review.",
            "mitigation": "",
            "residual_risk": "Decision inputs may remain incomplete until recovery.",
            "affected_components": ("live_ingestion", "market_mirror"),
            "occurrence_evidence_refs": (occurrence_ref,),
            "evidence_refs": (occurrence_ref,),
            "model_version_ids": (),
            "requires_operator_action": True,
        }
        values.update(overrides)
        if "entry_id" not in overrides:
            values["entry_id"] = derive_occurrence_entry_id(
                kind=values["kind"],
                affected_components=values["affected_components"],
                occurrence_evidence_refs=values["occurrence_evidence_refs"],
                model_version_ids=values["model_version_ids"],
            )
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
        self.assertEqual(projection.status_key, "ui.risk_register.status.open")
        self.assertEqual(projection.evidence_key, "ui.risk_register.evidence.partial")
        self.assertEqual(projection.opened_at, entry.opened_at)
        self.assertEqual(projection.updated_at, entry.updated_at)
        self.assertEqual(
            projection.next_action_key,
            "ui.risk_register.next_action.review",
        )
        self.assertEqual(projection.title, entry.title)
        self.assertEqual(
            projection.occurrence_evidence_refs,
            entry.occurrence_evidence_refs,
        )
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

        wrong_schema_version_bool = dict(payload)
        wrong_schema_version_bool["schema_version"] = True
        with self.assertRaisesRegex(IncidentRiskRegisterError, "unsupported"):
            IncidentRiskEntry.from_dict(wrong_schema_version_bool)

        wrong_schema_version_float = dict(payload)
        wrong_schema_version_float["schema_version"] = 1.0
        with self.assertRaisesRegex(IncidentRiskRegisterError, "unsupported"):
            IncidentRiskEntry.from_dict(wrong_schema_version_float)

        wrong_enum_type = dict(payload)
        wrong_enum_type["severity"] = 3
        with self.assertRaisesRegex(IncidentRiskRegisterError, "severity must be a JSON string"):
            IncidentRiskEntry.from_dict(wrong_enum_type)

        for noncanonical_severity in ("HIGH", "High"):
            with self.subTest(noncanonical_severity=noncanonical_severity):
                wrong_severity_case = dict(payload)
                wrong_severity_case["severity"] = noncanonical_severity
                with self.assertRaisesRegex(
                    IncidentRiskRegisterError,
                    "invalid enum/value data",
                ):
                    IncidentRiskEntry.from_dict(wrong_severity_case)

    def test_deserialization_rejects_subclass_before_constructor_dispatch(self) -> None:
        payload = self._entry().to_dict()

        class HostileEntry(IncidentRiskEntry):
            def __new__(cls, *args, **kwargs):
                raise AssertionError("hostile subclass constructor executed")

        with self.assertRaisesRegex(
            TypeError,
            "deserialization requires exact IncidentRiskEntry",
        ):
            HostileEntry.from_dict(payload)

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
        with self.assertRaisesRegex(IncidentRiskRegisterError, "sorted and unique"):
            self._entry(
                occurrence_evidence_refs=(
                    "evidence://z",
                    "evidence://a",
                ),
                evidence_refs=("evidence://a", "evidence://z"),
            )

    def test_occurrence_identity_is_product_derived_and_presentation_independent(self) -> None:
        first = self._entry()
        presentation_variant = self._entry(
            severity=RiskSeverity.MEDIUM,
            updated_at="2026-09-21T07:06:00+00:00",
            title="Same occurrence, different operator title",
            summary="Presentation changes do not mint a new occurrence.",
        )
        other_occurrence = self._entry(occurrence_tag="002")

        self.assertEqual(first.entry_id, presentation_variant.entry_id)
        self.assertNotEqual(first.entry_id, other_occurrence.entry_id)
        self.assertTrue(first.entry_id.startswith("incident:"))

        with self.assertRaisesRegex(
            IncidentRiskRegisterError,
            "product-derived occurrence identity",
        ):
            self._entry(entry_id="caller-chosen-id")

        with self.assertRaisesRegex(
            IncidentRiskRegisterError,
            "included in evidence_refs",
        ):
            self._entry(
                occurrence_evidence_refs=("evidence://provider-gap/other",),
            )

        changed_component = self._entry(
            affected_components=("market_mirror",),
        )
        self.assertNotEqual(first.entry_id, changed_component.entry_id)

    def test_model_risk_requires_exact_model_identity(self) -> None:
        with self.assertRaisesRegex(IncidentRiskRegisterError, "model_version_id"):
            self._entry(kind=RegisterEntryKind.MODEL_RISK, model_version_ids=())

        entry = self._entry(
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
                status=RiskStatus.RESOLVED,
                mitigation="Provider recovered and replay was reconciled.",
                evidence_state=RiskEvidenceState.PARTIAL,
                requires_operator_action=False,
            )
        with self.assertRaisesRegex(IncidentRiskRegisterError, "cannot require operator action"):
            self._entry(
                status=RiskStatus.RESOLVED,
                mitigation="Provider recovered and replay was reconciled.",
                evidence_state=RiskEvidenceState.VERIFIED,
                requires_operator_action=True,
            )

        closed = self._entry(
            revision=3,
            updated_at="2026-09-21T08:00:00+00:00",
            status=RiskStatus.RESOLVED,
            mitigation="Provider recovered and replay was reconciled.",
            residual_risk="No unresolved gap remains in the recorded interval.",
            evidence_state=RiskEvidenceState.VERIFIED,
            requires_operator_action=False,
        )
        self.assertEqual(closed.status, RiskStatus.RESOLVED)

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

        with self.assertRaisesRegex(
            IncidentRiskRegisterError,
            "product-derived occurrence identity",
        ):
            self._entry(
                entry_id="other-entry",
                revision=2,
                updated_at="2026-09-21T07:06:00+00:00",
            )

        with self.assertRaisesRegex(
            IncidentRiskRegisterError,
            "product-derived occurrence identity",
        ):
            self._entry(
                entry_id=previous.entry_id,
                revision=2,
                updated_at="2026-09-21T07:06:00+00:00",
                kind=RegisterEntryKind.MODEL_RISK,
                model_version_ids=("model-v1",),
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

    def test_successor_preserves_all_prior_evidence_refs(self) -> None:
        previous = self._entry(
            evidence_refs=(
                "evidence://analysis/001",
                "evidence://provider-gap/001",
            ),
        )

        with self.assertRaisesRegex(
            IncidentRiskRegisterError,
            "preserve all prior evidence_refs",
        ):
            validate_successor(
                previous,
                self._entry(
                    revision=2,
                    updated_at="2026-09-21T07:06:00+00:00",
                    status=RiskStatus.ACKNOWLEDGED,
                    evidence_refs=("evidence://provider-gap/001",),
                ),
            )

        with self.assertRaisesRegex(
            IncidentRiskRegisterError,
            "preserve all prior evidence_refs",
        ):
            validate_successor(
                previous,
                self._entry(
                    revision=2,
                    updated_at="2026-09-21T07:06:00+00:00",
                    status=RiskStatus.ACKNOWLEDGED,
                    evidence_refs=(
                        "evidence://provider-gap/001",
                        "evidence://replacement/001",
                    ),
                ),
            )

        appended = self._entry(
            revision=2,
            updated_at="2026-09-21T07:06:00+00:00",
            status=RiskStatus.ACKNOWLEDGED,
            evidence_refs=(
                "evidence://analysis/001",
                "evidence://followup/001",
                "evidence://provider-gap/001",
            ),
        )
        validate_successor(previous, appended)

    def test_lifecycle_transitions_are_explicit_and_evidence_bound(self) -> None:
        opened = self._entry()
        acknowledged = self._entry(
            revision=2,
            updated_at="2026-09-21T07:06:00+00:00",
            status=RiskStatus.ACKNOWLEDGED,
        )
        validate_successor(opened, acknowledged)
        self.assertEqual(
            operator_projection(acknowledged).status_key,
            "ui.risk_register.status.acknowledged",
        )

        mitigating = self._entry(
            revision=3,
            updated_at="2026-09-21T07:07:00+00:00",
            status=RiskStatus.MITIGATING,
            mitigation="Keep decisions fail-closed while recovery is verified.",
        )
        validate_successor(acknowledged, mitigating)

        with self.assertRaisesRegex(
            IncidentRiskRegisterError,
            "lifecycle transition",
        ):
            validate_successor(
                acknowledged,
                self._entry(
                    revision=3,
                    updated_at="2026-09-21T07:07:00+00:00",
                    status=RiskStatus.OPEN,
                ),
            )

        with self.assertRaisesRegex(
            IncidentRiskRegisterError,
            "new verified evidence",
        ):
            validate_successor(
                opened,
                self._entry(
                    revision=2,
                    updated_at="2026-09-21T07:06:00+00:00",
                    status=RiskStatus.RESOLVED,
                    evidence_state=RiskEvidenceState.VERIFIED,
                    mitigation="Recovery was reported complete.",
                    requires_operator_action=False,
                ),
            )

        resolved = self._entry(
            revision=4,
            updated_at="2026-09-21T07:08:00+00:00",
            status=RiskStatus.RESOLVED,
            evidence_state=RiskEvidenceState.VERIFIED,
            evidence_refs=(
                "evidence://provider-gap/001",
                "evidence://resolution/001",
            ),
            mitigation="Provider recovery and causal replay were verified.",
            requires_operator_action=False,
        )
        validate_successor(mitigating, resolved)

        with self.assertRaisesRegex(
            IncidentRiskRegisterError,
            "new verified evidence",
        ):
            validate_successor(
                resolved,
                self._entry(
                    revision=5,
                    updated_at="2026-09-21T07:09:00+00:00",
                    status=RiskStatus.OPEN,
                    evidence_state=RiskEvidenceState.VERIFIED,
                    evidence_refs=resolved.evidence_refs,
                    mitigation=resolved.mitigation,
                    requires_operator_action=True,
                ),
            )

        reopened = self._entry(
            revision=5,
            updated_at="2026-09-21T07:09:00+00:00",
            status=RiskStatus.OPEN,
            evidence_state=RiskEvidenceState.VERIFIED,
            evidence_refs=(
                "evidence://provider-gap/001",
                "evidence://reopen/001",
                "evidence://resolution/001",
            ),
            mitigation=resolved.mitigation,
            requires_operator_action=True,
        )
        validate_successor(resolved, reopened)

        superseded = self._entry(
            revision=6,
            updated_at="2026-09-21T07:10:00+00:00",
            status=RiskStatus.SUPERSEDED,
            evidence_state=RiskEvidenceState.VERIFIED,
            evidence_refs=(
                "evidence://provider-gap/001",
                "evidence://reopen/001",
                "evidence://resolution/001",
                "evidence://supersession/001",
            ),
            mitigation="A verified successor occurrence now carries the condition.",
            requires_operator_action=False,
        )
        validate_successor(reopened, superseded)

    def test_operator_sort_prioritizes_action_then_severity_then_recency(self) -> None:
        low_action = self._entry(
            occurrence_tag="a",
            severity=RiskSeverity.LOW,
            requires_operator_action=True,
            updated_at="2026-09-21T07:10:00+00:00",
        )
        critical_action = self._entry(
            occurrence_tag="b",
            severity=RiskSeverity.CRITICAL,
            requires_operator_action=True,
            updated_at="2026-09-21T07:06:00+00:00",
        )
        high_no_action = self._entry(
            occurrence_tag="c",
            severity=RiskSeverity.HIGH,
            requires_operator_action=False,
            updated_at="2026-09-21T07:20:00+00:00",
        )
        medium_no_action_newer = self._entry(
            occurrence_tag="d",
            severity=RiskSeverity.MEDIUM,
            requires_operator_action=False,
            updated_at="2026-09-21T07:30:00+00:00",
        )

        ordered = operator_sort(
            (medium_no_action_newer, high_no_action, low_action, critical_action)
        )
        self.assertEqual(
            ordered,
            (critical_action, low_action, high_no_action, medium_no_action_newer),
        )

    def test_operator_projection_next_action_key_is_safe_and_status_bounded(self) -> None:
        monitor = self._entry(
            occurrence_tag="monitor",
            requires_operator_action=False,
        )
        mitigating = self._entry(
            occurrence_tag="mitigating",
            revision=2,
            updated_at="2026-09-21T07:06:00+00:00",
            status=RiskStatus.MITIGATING,
            mitigation="Keep the canonical protection active.",
            requires_operator_action=False,
        )
        resolved = self._entry(
            occurrence_tag="resolved",
            revision=2,
            updated_at="2026-09-21T07:06:00+00:00",
            status=RiskStatus.RESOLVED,
            evidence_state=RiskEvidenceState.VERIFIED,
            evidence_refs=(
                "evidence://provider-gap/resolved",
                "evidence://resolution/resolved",
            ),
            mitigation="Canonical recovery evidence is verified.",
            requires_operator_action=False,
        )

        self.assertEqual(
            operator_projection(monitor).next_action_key,
            "ui.risk_register.next_action.monitor",
        )
        self.assertEqual(
            operator_projection(mitigating).next_action_key,
            "ui.risk_register.next_action.verify_mitigation",
        )
        self.assertEqual(
            operator_projection(resolved).next_action_key,
            "ui.risk_register.next_action.none",
        )

    def test_operator_projection_redacts_credential_bearing_text_deterministically(self) -> None:
        first = self._entry(
            title="Provider Authorization: Bearer FIRST_SECRET_VALUE_1234567890",
            summary="api_key=first-secret-value-1234567890",
            mitigation="-----BEGIN PRIVATE KEY-----FIRST_PRIVATE_MATERIAL-----END PRIVATE KEY-----",
        )
        second = self._entry(
            title="Provider Authorization: Bearer SECOND_SECRET_VALUE_ABCDEFGHIJ",
            summary="api_key=second-secret-value-ABCDEFGHIJ",
            mitigation="-----BEGIN PRIVATE KEY-----SECOND_PRIVATE_MATERIAL-----END PRIVATE KEY-----",
        )

        first_projection = operator_projection(first)
        second_projection = operator_projection(second)

        self.assertEqual(first_projection.title, "Provider [REDACTED]")
        self.assertEqual(first_projection.summary, "[REDACTED]")
        self.assertEqual(first_projection.mitigation, "[REDACTED]")
        self.assertEqual(
            (first_projection.title, first_projection.summary, first_projection.mitigation),
            (second_projection.title, second_projection.summary, second_projection.mitigation),
        )
        self.assertNotIn("FIRST_SECRET_VALUE", first_projection.title)
        self.assertNotIn("SECOND_SECRET_VALUE", second_projection.title)
        self.assertEqual(first.title, "Provider Authorization: Bearer FIRST_SECRET_VALUE_1234567890")
        self.assertEqual(first_projection.fingerprint_sha256, first.fingerprint_sha256)

    def test_operator_projection_redacts_assignment_and_bare_auth_credentials(self) -> None:
        entry = self._entry(
            title="Retry Authorization=Bearer short-secret",
            summary=(
                "Proxy-Authorization: Basic aGVsbG8="
                " and auth token tiny-token should not reach the operator."
            ),
            mitigation="authorization=token one-word-secret",
            residual_risk="bearer x",
        )

        projection = operator_projection(entry)

        self.assertEqual(projection.title, "Retry [REDACTED]")
        self.assertEqual(
            projection.summary,
            "[REDACTED] and auth [REDACTED] should not reach the operator.",
        )
        self.assertEqual(projection.mitigation, "[REDACTED]")
        self.assertEqual(projection.residual_risk, "[REDACTED]")

    def test_operator_projection_redacts_common_modern_provider_credential_ids(self) -> None:
        entry = self._entry(
            title="AWS key ASIA1234567890ABCD and GitHub github_pat_1234567890abcdefghij",
            summary="token=plain-secret auth_token='another-secret'",
        )

        projection = operator_projection(entry)

        self.assertEqual(
            projection.title,
            "AWS key [REDACTED] and GitHub [REDACTED]",
        )
        self.assertEqual(
            projection.summary,
            "[REDACTED] [REDACTED]",
        )

    def test_operator_projection_redacts_aws_credential_environment_variables(self) -> None:
        entry = self._entry(
            title="AWS_ACCESS_KEY_ID=AKIA1234567890ABCD",
            summary="AWS_SECRET_ACCESS_KEY=secret-value AWS_SESSION_TOKEN=session-value",
            mitigation="security_token: security-secret",
        )

        projection = operator_projection(entry)

        self.assertEqual(projection.title, "AWS_ACCESS_KEY_ID=[REDACTED]")
        self.assertEqual(projection.summary, "[REDACTED] [REDACTED]")
        self.assertEqual(projection.mitigation, "[REDACTED]")

    def test_operator_projection_preserves_noncredential_text(self) -> None:
        entry = self._entry(
            title="Provider session recovered",
            summary="Secret rotation completed; no credential value is present.",
            mitigation="Keep monitoring the canonical provider health signal.",
            residual_risk="No unresolved credential material is exposed to the operator.",
        )

        projection = operator_projection(entry)

        self.assertEqual(projection.title, entry.title)
        self.assertEqual(projection.summary, entry.summary)
        self.assertEqual(projection.mitigation, entry.mitigation)
        self.assertEqual(projection.residual_risk, entry.residual_risk)

    def test_operator_projection_revalidates_post_construction_entry_tamper(self) -> None:
        entry = self._entry()
        object.__setattr__(entry, "severity", object())

        with self.assertRaisesRegex(
            IncidentRiskRegisterError,
            "severity must be RiskSeverity",
        ):
            operator_projection(entry)

    def test_operator_sort_revalidates_post_construction_entry_tamper(self) -> None:
        entry = self._entry()
        object.__setattr__(entry, "updated_at", "not-a-timestamp")

        with self.assertRaisesRegex(
            IncidentRiskRegisterError,
            "updated_at must be ISO-8601",
        ):
            operator_sort((entry,))

    def test_successor_validation_rejects_hostile_subclass_before_attribute_access(self) -> None:
        previous = self._entry()

        class HostileEntry(IncidentRiskEntry):
            def __getattribute__(self, name: str):
                raise AssertionError("hostile IncidentRiskEntry subclass executed")

        candidate = object.__new__(HostileEntry)
        with self.assertRaisesRegex(
            TypeError,
            "register successor validation requires exact IncidentRiskEntry values",
        ):
            validate_successor(previous, candidate)

    def test_successor_validation_revalidates_post_construction_tamper(self) -> None:
        previous = self._entry()
        candidate = self._entry(
            revision=2,
            updated_at="2026-09-21T07:06:00+00:00",
            status=RiskStatus.ACKNOWLEDGED,
        )
        object.__setattr__(candidate, "requires_operator_action", 1)

        with self.assertRaisesRegex(
            IncidentRiskRegisterError,
            "requires_operator_action must be a bool",
        ):
            validate_successor(previous, candidate)

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
