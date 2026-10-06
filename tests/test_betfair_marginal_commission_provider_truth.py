from dataclasses import replace
from decimal import Decimal

import pytest

import autosport.betfair_marginal_commission_ev as commission_ev

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

def test_projection_cannot_be_subclassed_to_override_false_authority():
    with pytest.raises(
        TypeError,
        match="BetfairMarginalCommissionEVProjection must not be subclassed",
    ):
        class ForgedProjection(type(projection())):
            @property
            def provider_applicability_proven(self):
                return True

            @property
            def execution_authorized(self):
                return True

            @property
            def decision_authorized(self):
                return True

def test_module_rebinding_cannot_mint_provider_or_execution_truth(monkeypatch):
    baseline = projection()

    monkeypatch.setattr(
        commission_ev,
        "COMMISSION_RATE_BASIS",
        "SETTLEMENT_AUTHORITATIVE",
    )
    monkeypatch.setattr(commission_ev, "PROVIDER_APPLICABILITY_PROVEN", True)
    monkeypatch.setattr(commission_ev, "PROVIDER_POSTED_EXACT", True)
    monkeypatch.setattr(commission_ev, "SETTLEMENT_RATE_AUTHORITATIVE", True)
    monkeypatch.setattr(commission_ev, "EXECUTION_AUTHORIZED", True)
    monkeypatch.setattr(commission_ev, "REAL_MONEY_EXECUTION", True)
    monkeypatch.setattr(commission_ev, "SCHEMA_VERSION", 999)
    monkeypatch.setattr(commission_ev, "SOURCE_FAMILY", "forged.source.family")
    monkeypatch.setattr(commission_ev, "COMMISSION_QUANTUM", Decimal("1"))
    monkeypatch.setattr(commission_ev, "COMMISSION_ROUNDING", "ROUND_DOWN")
    monkeypatch.setattr(commission_ev, "ROUND_HALF_UP", "ROUND_DOWN")
    monkeypatch.setattr(commission_ev, "MAX_OUTCOMES", 0)
    monkeypatch.setattr(commission_ev, "MAX_SIGNIFICANT_DIGITS", 1)
    monkeypatch.setattr(commission_ev, "MAX_ADJUSTED_EXPONENT", 1)

    rebound = projection()

    assert rebound.commission_rate_basis == "DECISION_SNAPSHOT_CONDITIONAL"
    assert rebound.provider_applicability_proven is False
    assert rebound.provider_posted_exact is False
    assert rebound.settlement_rate_authoritative is False
    assert rebound.execution_authorized is False
    assert rebound.real_money_execution is False
    assert rebound.decision_authorized is False
    assert rebound.commission_quantum == Decimal("0.01")
    assert rebound.commission_rounding == "ROUND_HALF_UP"
    assert rebound.calculation_sha256 == baseline.calculation_sha256

