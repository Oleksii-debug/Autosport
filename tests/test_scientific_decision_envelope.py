import unittest
from dataclasses import replace

from autosport.scientific_decision_envelope import (
    CausalEvidenceRef,
    DecisionDisposition,
    DecisionEnvelopeError,
    DecisionOutcomeAppend,
    EvidenceKind,
    SealedDecisionEnvelope,
    evidence_snapshot_sha256,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
T0 = "2026-09-21T10:00:00Z"
T1 = "2026-09-21T10:00:01Z"
T2 = "2026-09-21T10:00:02Z"
T3 = "2026-09-21T10:00:03Z"
T4 = "2026-09-21T10:00:04Z"


def feature(**changes):
    values = dict(
        evidence_id="feature-1",
        kind=EvidenceKind.FEATURE,
        evidence_sha256=SHA_A,
        event_at=T0,
        ingested_at=T1,
        available_at=T1,
    )
    values.update(changes)
    return CausalEvidenceRef(**values)


def market(**changes):
    values = dict(
        evidence_id="market-1",
        kind=EvidenceKind.MARKET,
        evidence_sha256=SHA_B,
        event_at=T1,
        ingested_at=T2,
        available_at=T2,
    )
    values.update(changes)
    return CausalEvidenceRef(**values)


def envelope(**changes):
    values = dict(
        protocol_id="forward-economic-v1",
        decision_id="decision-1",
        decision_at=T3,
        event_watermark=T1,
        ingest_watermark=T2,
        disposition=DecisionDisposition.ACTION,
        evidence=(feature(), market()),
        action_identity="paper-proposal-1",
    )
    values.update(changes)
    return SealedDecisionEnvelope.seal(**values)


class ScientificDecisionEnvelopeTests(unittest.TestCase):
    def test_deterministic_hash_is_independent_of_input_evidence_order(self):
        first = envelope(evidence=(feature(), market()))
        second = envelope(evidence=(market(), feature()))
        self.assertEqual(first.canonical_payload(), second.canonical_payload())
        self.assertEqual(first.envelope_sha256, second.envelope_sha256)

    def test_equivalent_timezone_spelling_has_same_identity(self):
        first = envelope()
        second = envelope(
            decision_at="2026-09-21T12:00:03+02:00",
            event_watermark="2026-09-21T12:00:01+02:00",
            ingest_watermark="2026-09-21T12:00:02+02:00",
            evidence=(
                feature(
                    event_at="2026-09-21T12:00:00+02:00",
                    ingested_at="2026-09-21T12:00:01+02:00",
                    available_at="2026-09-21T12:00:01+02:00",
                ),
                market(
                    event_at="2026-09-21T12:00:01+02:00",
                    ingested_at="2026-09-21T12:00:02+02:00",
                    available_at="2026-09-21T12:00:02+02:00",
                ),
            ),
        )
        self.assertEqual(first.envelope_sha256, second.envelope_sha256)

    def test_future_feature_is_rejected(self):
        with self.assertRaisesRegex(DecisionEnvelopeError, "future evidence"):
            envelope(evidence=(feature(available_at=T4), market()))

    def test_post_decision_market_quote_is_rejected_even_with_old_event_time(self):
        late_quote = market(ingested_at=T4, available_at=T4)
        with self.assertRaisesRegex(DecisionEnvelopeError, "ingested after ingest_watermark"):
            envelope(evidence=(feature(), late_quote))

    def test_event_beyond_event_watermark_is_rejected(self):
        with self.assertRaisesRegex(DecisionEnvelopeError, "event is after event_watermark"):
            envelope(evidence=(feature(), market(event_at=T2)))

    def test_ingest_watermark_after_decision_is_rejected(self):
        with self.assertRaisesRegex(DecisionEnvelopeError, "ingest_watermark"):
            envelope(ingest_watermark=T4)

    def test_snapshot_hashes_are_derived_from_exact_evidence(self):
        sealed = envelope()
        changed = replace(feature(), evidence_sha256=SHA_C)
        self.assertNotEqual(
            sealed.feature_snapshot_sha256,
            evidence_snapshot_sha256((changed, market()), EvidenceKind.FEATURE),
        )
        with self.assertRaisesRegex(DecisionEnvelopeError, "feature snapshot hash"):
            replace(sealed, evidence=(changed, market()))

    def test_missing_feature_or_market_surface_fails_closed(self):
        with self.assertRaisesRegex(DecisionEnvelopeError, "MARKET evidence"):
            SealedDecisionEnvelope.seal(
                protocol_id="p",
                decision_id="d",
                decision_at=T3,
                event_watermark=T1,
                ingest_watermark=T2,
                disposition=DecisionDisposition.ABSTAIN,
                evidence=(feature(),),
                abstention_reason="market unavailable",
            )

    def test_duplicate_evidence_identity_is_rejected(self):
        duplicate = replace(feature(), evidence_sha256=SHA_C)
        with self.assertRaisesRegex(DecisionEnvelopeError, "duplicated"):
            envelope(evidence=(feature(), duplicate, market()))

    def test_action_and_abstention_are_explicit_and_disjoint(self):
        with self.assertRaisesRegex(DecisionEnvelopeError, "ACTION"):
            envelope(abstention_reason="no edge")
        abstained = envelope(
            disposition=DecisionDisposition.ABSTAIN,
            action_identity=None,
            abstention_reason="no edge after costs",
        )
        self.assertEqual(abstained.disposition, DecisionDisposition.ABSTAIN)
        self.assertEqual(abstained.abstention_reason, "no edge after costs")
        self.assertFalse(abstained.canonical_payload()["authority_grant"])
        with self.assertRaisesRegex(DecisionEnvelopeError, "ABSTAIN"):
            envelope(
                disposition=DecisionDisposition.ABSTAIN,
                abstention_reason="no edge",
                action_identity="forbidden-action",
            )

    def test_outcome_is_separate_and_must_be_later_than_decision(self):
        sealed = envelope()
        self.assertNotIn("outcome", sealed.canonical_payload())
        with self.assertRaisesRegex(DecisionEnvelopeError, "after decision_at"):
            DecisionOutcomeAppend.attach(
                sealed,
                outcome_id="outcome-1",
                outcome_sha256=SHA_C,
                revealed_at=T3,
            )
        outcome = DecisionOutcomeAppend.attach(
            sealed,
            outcome_id="outcome-1",
            outcome_sha256=SHA_C,
            revealed_at=T4,
        )
        outcome.verify_envelope(sealed)
        self.assertEqual(outcome.envelope_sha256, sealed.envelope_sha256)

    def test_outcome_cannot_be_rebound_to_a_modified_envelope(self):
        sealed = envelope()
        outcome = DecisionOutcomeAppend.attach(
            sealed,
            outcome_id="outcome-1",
            outcome_sha256=SHA_C,
            revealed_at=T4,
        )
        changed = envelope(decision_id="decision-2")
        with self.assertRaisesRegex(DecisionEnvelopeError, "decision_id"):
            outcome.verify_envelope(changed)


    def test_polymorphic_evidence_cannot_override_hash_material(self):
        class ForgedEvidence(CausalEvidenceRef):
            def canonical_payload(self):
                payload = super().canonical_payload()
                payload["evidence_sha256"] = SHA_C
                return payload

        forged = ForgedEvidence(
            evidence_id="feature-forged",
            kind=EvidenceKind.FEATURE,
            evidence_sha256=SHA_A,
            event_at=T0,
            ingested_at=T1,
            available_at=T1,
        )
        with self.assertRaisesRegex(DecisionEnvelopeError, "exact CausalEvidenceRef"):
            evidence_snapshot_sha256((forged,), EvidenceKind.FEATURE)
        with self.assertRaisesRegex(DecisionEnvelopeError, "exact CausalEvidenceRef"):
            envelope(evidence=(forged, market()))

    def test_polymorphic_envelope_cannot_mint_outcome_binding(self):
        class ForgedEnvelope(SealedDecisionEnvelope):
            @property
            def envelope_sha256(self):
                return SHA_C

        sealed = envelope()
        forged = ForgedEnvelope(**{
            field: getattr(sealed, field)
            for field in SealedDecisionEnvelope.__dataclass_fields__
        })
        with self.assertRaisesRegex(DecisionEnvelopeError, "exact SealedDecisionEnvelope"):
            DecisionOutcomeAppend.attach(
                forged,
                outcome_id="outcome-forged",
                outcome_sha256=SHA_C,
                revealed_at=T4,
            )

    def test_evidence_internal_clock_order_is_fail_closed(self):
        with self.assertRaisesRegex(DecisionEnvelopeError, "event_at"):
            market(event_at=T2, ingested_at=T1)
        with self.assertRaisesRegex(DecisionEnvelopeError, "ingested_at"):
            market(ingested_at=T2, available_at=T1)


if __name__ == "__main__":
    unittest.main()
