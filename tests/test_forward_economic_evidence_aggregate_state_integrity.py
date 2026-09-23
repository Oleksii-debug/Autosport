from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.forward_economic_evidence import (
    AlphaAllocation,
    FamilywiseAlphaRegistry,
    ForwardEconomicEvidenceAccumulator,
    ForwardEconomicEvidenceError,
    ForwardEconomicProtocol,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 23, 1, 0, tzinfo=UTC)
SHA_UNIVERSE = "e" * 64
SHA_AUTHORITY = "f" * 64


def _protocol() -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="aggregate-state-integrity",
        total_alpha=Decimal("0.05"),
        allocations=(
            AlphaAllocation(
                challenger_id="challenger",
                alpha=Decimal("0.05"),
            ),
        ),
        sealed_at=T0,
    )
    return ForwardEconomicProtocol(
        protocol_id="aggregate-state-integrity-v1",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="aggregate-state-universe",
        universe_sha256=SHA_UNIVERSE,
        authority_binding_sha256=SHA_AUTHORITY,
        alpha_registry=registry,
        minimum_events=1,
        risk_unit_currency=Decimal("10"),
        maximum_accepted_odds=Decimal("10"),
        maximum_drawdown_currency=Decimal("100"),
        maximum_economic_cost_currency=Decimal("0"),
        absolute_lambda=Decimal("0.5"),
        paired_lambda=Decimal("0.5"),
        start_sequence=0,
        frozen_at=T0 + timedelta(seconds=1),
    )


@pytest.mark.parametrize(
    ("cache_name", "summary_field", "forged_value"),
    (
        ("_absolute_log_e", "absolute_log_e", Decimal("999")),
        ("_paired_log_e", "paired_log_e", Decimal("999")),
        (
            "_champion_total",
            "champion_total_pnl_currency",
            Decimal("123.45"),
        ),
    ),
)
def test_cached_aggregate_mutation_cannot_change_summary_under_same_evidence(
    cache_name: str,
    summary_field: str,
    forged_value: Decimal,
) -> None:
    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    before = accumulator.summary()

    setattr(accumulator, cache_name, forged_value)

    try:
        after = accumulator.summary()
    except ForwardEconomicEvidenceError:
        return

    assert after.evidence_sha256 == before.evidence_sha256
    assert getattr(after, summary_field) == getattr(before, summary_field)
    assert after.positive_authority_verified is False
    assert after.conditional_eprocess_verified is False
    assert after.scientific_promotion_gate_passed is False
    assert after.promotion_authority is False
