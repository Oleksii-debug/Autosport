from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.betfair_marginal_commission_ev import (
    BetfairMarketOutcomeEconomicInput,
    calculate_betfair_marginal_commission_ev,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def projection():
    return calculate_betfair_marginal_commission_ev(
        account_id="betfair-account-evidence:" + SHA_A,
        market_id="1.23456789",
        currency="GBP",
        effective_commission_rate=Decimal("0.05"),
        probability_evidence_sha256=SHA_A,
        exposure_evidence_sha256=SHA_B,
        candidate_evidence_sha256=SHA_C,
        commission_rate_evidence_sha256=SHA_D,
        outcomes=(
            BetfairMarketOutcomeEconomicInput(
                "only",
                Decimal("1"),
                Decimal("1.00"),
                Decimal("0.25"),
            ),
        ),
    )


def test_projection_is_explicitly_conditional_not_provider_posted_truth():
    result = projection()

    assert result.commission_rate_basis == "DECISION_SNAPSHOT_CONDITIONAL"
    assert result.provider_applicability_proven is False
    assert result.provider_posted_exact is False
    assert result.settlement_rate_authoritative is False
    assert result.execution_authorized is False
    assert result.real_money_execution is False
    assert result.decision_authorized is False


def test_valid_caller_evidence_cannot_forge_provider_truth_fields():
    result = projection()

    for field_name in (
        "provider_applicability_proven",
        "provider_posted_exact",
        "settlement_rate_authoritative",
        "execution_authorized",
        "real_money_execution",
        "decision_authorized",
    ):
        with pytest.raises(TypeError):
            replace(result, **{field_name: True})

    with pytest.raises(TypeError):
        replace(result, commission_rate_basis="SETTLEMENT_AUTHORITATIVE")
