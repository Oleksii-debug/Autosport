from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from autosport.forward_economic_evidence import (
    AlphaAllocation,
    FamilywiseAlphaRegistry,
    ForwardEconomicEvidenceAccumulator,
    ForwardEconomicEvidenceError,
    ForwardEconomicProtocol,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
SHA_E = "e" * 64
SHA_F = "f" * 64


def _protocol() -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="protocol-integrity-family",
        total_alpha=Decimal("0.2"),
        allocations=(AlphaAllocation("challenger", Decimal("0.2")),),
        sealed_at=T0,
    )
    return ForwardEconomicProtocol(
        protocol_id="protocol-integrity",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="protocol-integrity-universe",
        universe_sha256=SHA_E,
        authority_binding_sha256=SHA_F,
        alpha_registry=registry,
        minimum_events=10,
        risk_unit_currency=Decimal("10"),
        maximum_accepted_odds=Decimal("10"),
        maximum_drawdown_currency=Decimal("100"),
        absolute_lambda=Decimal("0.5"),
        paired_lambda=Decimal("0.5"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
    )


def test_accumulator_rejects_or_ignores_postfreeze_protocol_mutation() -> None:
    protocol = _protocol()
    accumulator = ForwardEconomicEvidenceAccumulator(protocol)
    before = accumulator.summary()

    # frozen=True is not an authority boundary in Python. Simulate hostile or
    # accidental trusted-process mutation of a policy field after the
    # accumulator accepted the prospective protocol.
    object.__setattr__(protocol, "challenger_alpha", Decimal("0.9"))
    object.__setattr__(protocol, "minimum_events", 1)

    try:
        after = accumulator.summary()
    except ForwardEconomicEvidenceError:
        return

    # A safe implementation may retain an immutable internal snapshot instead
    # of raising, but post-freeze caller mutation must not rewrite the
    # scientific protocol/evidence identity or threshold.
    assert after.protocol_sha256 == before.protocol_sha256
    assert after.log_threshold == before.log_threshold
    assert after.evidence_sha256 == before.evidence_sha256

    public_protocol = accumulator.protocol
    object.__setattr__(public_protocol, "challenger_alpha", Decimal("0.8"))
    object.__setattr__(public_protocol, "minimum_events", 2)
    after_public_view_mutation = accumulator.summary()
    assert after_public_view_mutation.protocol_sha256 == before.protocol_sha256
    assert after_public_view_mutation.log_threshold == before.log_threshold
    assert after_public_view_mutation.evidence_sha256 == before.evidence_sha256
