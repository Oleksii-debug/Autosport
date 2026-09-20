from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from autosport.betfair_commission_cost_evidence import issue_betfair_commission_cost_evidence
from autosport.campaign_cost_evidence import (
    CostClass,
    CostEvidenceError,
    EconomicCompleteness,
    derive_campaign_economics,
)
from autosport.campaign_economic_cost_composition import (
    CampaignEconomicCostCompositionError,
    compose_betfair_commission_campaign_economics,
)
from test_betfair_commission_cost_evidence import NOW, _authorities, _receipt, _scope


def test_source_owned_commission_is_consumed_without_fake_net_economics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt(commission=Decimal("2.25"))
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)
    provider_scope = _scope()

    version = compose_betfair_commission_campaign_economics(
        source=source,
        campaign=campaign,
        provider_scope=provider_scope,
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
    )

    assert len(version.costs) == 1
    commission = version.costs[0]
    assert commission.cost_class is CostClass.EXECUTION_FEES_COMMISSION_TAX
    assert commission.amount == Decimal("2.25")
    assert commission.currency == "EUR"
    assert commission.shared_source is True
    assert (
        "UNRESOLVED_COST_AUTHORITY:EXECUTION_FEES_COMMISSION_TAX"
        not in version.incomplete_reasons
    )
    assert (
        "SHARED_UNALLOCATED_COST:EXECUTION_FEES_COMMISSION_TAX"
        in version.incomplete_reasons
    )
    assert "INFORMATIONAL_ONLY:EXECUTION_FEES_COMMISSION_TAX" in version.incomplete_reasons
    assert "MISSING_CAMPAIGN_CURRENCY_AUTHORITY" in version.incomplete_reasons
    assert version.known_cost_total == Decimal("0")
    assert version.net_after_known_costs is None
    assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS


def test_generic_derivation_does_not_self_attest_same_commission_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)
    provider_scope = _scope()
    evidence = issue_betfair_commission_cost_evidence(
        source=source,
        campaign=campaign,
        provider_scope=provider_scope,
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
    )

    generic = derive_campaign_economics(
        campaign=campaign,
        costs=(evidence,),
        as_of=NOW,
    )
    assert (
        "UNRESOLVED_COST_AUTHORITY:EXECUTION_FEES_COMMISSION_TAX"
        in generic.incomplete_reasons
    )

    composed = compose_betfair_commission_campaign_economics(
        source=source,
        campaign=campaign,
        provider_scope=provider_scope,
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
        additional_costs=(evidence,),
    )
    assert len(composed.costs) == 1
    assert (
        "UNRESOLVED_COST_AUTHORITY:EXECUTION_FEES_COMMISSION_TAX"
        not in composed.incomplete_reasons
    )


def test_caller_amount_forgery_cannot_replace_verified_commission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)
    provider_scope = _scope()
    evidence = issue_betfair_commission_cost_evidence(
        source=source,
        campaign=campaign,
        provider_scope=provider_scope,
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
    )
    forged = replace(evidence, amount=Decimal("99"))

    with pytest.raises(CostEvidenceError, match="same immutable cost source"):
        compose_betfair_commission_campaign_economics(
            source=source,
            campaign=campaign,
            provider_scope=provider_scope,
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
            additional_costs=(forged,),
        )


def test_future_applicability_cannot_backdate_campaign_economics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)

    with pytest.raises(
        CampaignEconomicCostCompositionError,
        match="cannot be resolved",
    ):
        compose_betfair_commission_campaign_economics(
            source=source,
            campaign=campaign,
            provider_scope=_scope(available_at=NOW + timedelta(seconds=1)),
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )
