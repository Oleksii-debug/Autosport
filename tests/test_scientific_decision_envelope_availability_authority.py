import pytest

from autosport.scientific_decision_envelope import (
    CausalEvidenceRef,
    DecisionDisposition,
    DecisionEnvelopeError,
    EvidenceKind,
    SealedDecisionEnvelope,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
T0 = "2026-09-25T00:00:00Z"
T1 = "2026-09-25T00:00:01Z"
T2 = "2026-09-25T00:00:02Z"
T3 = "2026-09-25T00:00:03Z"


def _ref(kind: EvidenceKind, evidence_id: str, digest: str) -> CausalEvidenceRef:
    # These values are intentionally only caller assertions.  Nothing in this
    # test creates or resolves a product-owned availability receipt.
    return CausalEvidenceRef(
        evidence_id=evidence_id,
        kind=kind,
        evidence_sha256=digest,
        event_at=T0,
        ingested_at=T1,
        available_at=T2,
    )


def test_caller_asserted_timestamps_cannot_mint_causal_availability_authority() -> None:
    feature = _ref(EvidenceKind.FEATURE, "caller-feature", SHA_A)
    market = _ref(EvidenceKind.MARKET, "caller-market", SHA_B)

    sealed = SealedDecisionEnvelope.seal(
        protocol_id="forward-economic-v1",
        decision_id="caller-asserted-decision",
        decision_at=T3,
        event_watermark=T1,
        ingest_watermark=T1,
        disposition=DecisionDisposition.ACTION,
        evidence=(feature, market),
        action_identity="paper-only-action",
    )

    assert feature.availability_authority_proven is False
    assert market.availability_authority_proven is False
    assert sealed.availability_authority_proven is False
    payload = sealed.canonical_payload()
    assert payload["authority_grant"] is False
    assert payload["availability_authority_proven"] is False
    assert payload["availability_semantics"] == "caller_asserted_ordering_only"
    assert all(
        item["availability_authority_proven"] is False
        for item in payload["evidence"]
    )

    with pytest.raises(
        DecisionEnvelopeError,
        match="product-owned causal availability authority is unproven",
    ):
        sealed.require_product_proven_causal_availability()
