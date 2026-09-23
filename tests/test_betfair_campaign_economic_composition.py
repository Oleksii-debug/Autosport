from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

import autosport.betfair_commission_cost_evidence as commission_bridge
from autosport.betfair_campaign_economic_composition import (
    derive_campaign_economics_with_betfair_commission,
)
from autosport.campaign_cost_evidence import (
    CampaignEconomicEvidenceVersion,
    CostClass,
    CostEvidenceError,
    CostSourceRef,
    EconomicCompleteness,
    derive_campaign_economics,
)
from test_betfair_commission_cost_evidence import NOW, _authorities, _receipt, _scope


UNRESOLVED = "UNRESOLVED_COST_AUTHORITY:EXECUTION_FEES_COMMISSION_TAX"
INFORMATIONAL = "INFORMATIONAL_ONLY:EXECUTION_FEES_COMMISSION_TAX"


def _compose(monkeypatch: pytest.MonkeyPatch, *, previous=None):
    receipt = _receipt()
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)
    version = derive_campaign_economics_with_betfair_commission(
        source=source,
        campaign=campaign,
        provider_scope=_scope(),
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
        previous=previous,
    )
    return receipt, source, campaign, version


def test_verified_commission_closes_only_source_authority_not_net_economics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _, _, version = _compose(monkeypatch)

    assert len(version.costs) == 1
    cost = version.costs[0]
    assert cost.source.evidence_id == receipt.receipt_id
    assert cost.source.sha256 == receipt.record_sha256
    assert cost.amount == receipt.commission
    assert cost.currency == receipt.currency
    assert cost.shared_source is True
    assert cost.cost_class is CostClass.EXECUTION_FEES_COMMISSION_TAX

    assert UNRESOLVED not in version.incomplete_reasons
    assert INFORMATIONAL in version.incomplete_reasons
    assert "MISSING_CAMPAIGN_CURRENCY_AUTHORITY" in version.incomplete_reasons
    assert "MISSING_COST_CLASS:PROVIDER_DATA" in version.incomplete_reasons
    assert version.known_cost_total == Decimal("0")
    assert version.net_after_known_costs is None
    assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS


def test_source_verification_is_append_only_upgrade_of_generic_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)
    evidence = commission_bridge.issue_betfair_commission_cost_evidence(
        source=source,
        campaign=campaign,
        provider_scope=_scope(),
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
    )
    generic = derive_campaign_economics(
        campaign=campaign,
        costs=(evidence,),
        as_of=NOW,
    )
    assert UNRESOLVED in generic.incomplete_reasons

    verified = derive_campaign_economics_with_betfair_commission(
        source=source,
        campaign=campaign,
        provider_scope=_scope(),
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
        previous=generic,
    )

    assert verified.previous_version_id == generic.version_id
    assert verified.previous_version_sha256 == generic.record_sha256
    assert verified.costs == generic.costs
    assert UNRESOLVED not in verified.incomplete_reasons
    assert INFORMATIONAL in verified.incomplete_reasons
    assert verified.net_after_known_costs is None


def test_exact_retry_rederives_before_emitting_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, source, campaign, first = _compose(monkeypatch)

    retry = derive_campaign_economics_with_betfair_commission(
        source=source,
        campaign=campaign,
        provider_scope=_scope(),
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
        previous=first,
    )

    assert retry is not first
    assert retry.previous_version_id == first.version_id
    assert retry.previous_version_sha256 == first.record_sha256
    assert retry.costs == first.costs
    assert retry.known_cost_total == Decimal("0")
    assert retry.net_after_known_costs is None
    assert retry.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert UNRESOLVED not in retry.incomplete_reasons


def test_caller_constructed_complete_previous_is_never_returned_as_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, source, campaign, first = _compose(monkeypatch)
    forged = replace(
        first,
        known_cost_total=Decimal("999"),
        net_after_known_costs=Decimal("-939"),
        completeness=EconomicCompleteness.COMPLETE_NET_ECONOMICS,
        incomplete_reasons=(),
    )
    assert type(forged) is CampaignEconomicEvidenceVersion
    assert forged.completeness is EconomicCompleteness.COMPLETE_NET_ECONOMICS

    repaired = derive_campaign_economics_with_betfair_commission(
        source=source,
        campaign=campaign,
        provider_scope=_scope(),
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
        previous=forged,
    )

    assert repaired is not forged
    assert repaired.previous_version_id == forged.version_id
    assert repaired.known_cost_total == Decimal("0")
    assert repaired.net_after_known_costs is None
    assert repaired.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert INFORMATIONAL in repaired.incomplete_reasons
    assert "MISSING_CAMPAIGN_CURRENCY_AUTHORITY" in repaired.incomplete_reasons


def test_one_verified_receipt_cannot_launder_other_same_class_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)
    verified_cost = commission_bridge.issue_betfair_commission_cost_evidence(
        source=source,
        campaign=campaign,
        provider_scope=_scope(),
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
    )
    caller_cost = replace(
        verified_cost,
        source=CostSourceRef(
            family="caller.fake.fee",
            evidence_id="caller-fee",
            sha256="f" * 64,
        ),
        amount=Decimal("7"),
    )
    generic = derive_campaign_economics(
        campaign=campaign,
        costs=tuple(
            sorted(
                (verified_cost, caller_cost),
                key=lambda item: item.cost_evidence_id,
            )
        ),
        as_of=NOW,
    )

    product = derive_campaign_economics_with_betfair_commission(
        source=source,
        campaign=campaign,
        provider_scope=_scope(),
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
        previous=generic,
    )

    assert UNRESOLVED in product.incomplete_reasons
    assert product.net_after_known_costs is None
    assert product.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS


def test_previous_version_requires_exact_value_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)
    with pytest.raises(CostEvidenceError, match="exact CampaignEconomicEvidenceVersion"):
        derive_campaign_economics_with_betfair_commission(
            source=source,
            campaign=campaign,
            provider_scope=_scope(),
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
            previous=object(),
        )
