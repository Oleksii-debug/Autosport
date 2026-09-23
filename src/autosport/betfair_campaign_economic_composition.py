from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .betfair_commission_cost_evidence import issue_betfair_commission_cost_evidence
from .betfair_market_commission_authority import BetfairMarketCommissionAuthority
from .campaign_cost_evidence import (
    CampaignEconomicEvidenceVersion,
    CostClass,
    CostEvidence,
    CostEvidenceError,
    derive_campaign_economics,
)
from .campaign_economic_authority import FinalizedCampaignAuthority
from .campaign_provider_scope_authority import CampaignProviderScopeProjection


_TARGET_CLASS = CostClass.EXECUTION_FEES_COMMISSION_TAX
_UNRESOLVED_REASON = f"UNRESOLVED_COST_AUTHORITY:{_TARGET_CLASS.value}"


def derive_campaign_economics_with_betfair_commission(
    *,
    source: BetfairMarketCommissionAuthority,
    campaign: FinalizedCampaignAuthority,
    provider_scope: CampaignProviderScopeProjection,
    receipt_id: str,
    record_sha256: str,
    as_of: datetime,
    previous: CampaignEconomicEvidenceVersion | None = None,
) -> CampaignEconomicEvidenceVersion:
    """Compose exact Betfair commission authority into campaign economics.

    This is deliberately a product-owned consumer seam rather than another
    monetary source. The exact receipt, account, campaign/provider scope,
    market, membership, amount, currency and causal timestamps are re-resolved
    by ``issue_betfair_commission_cost_evidence``. Callers cannot supply a
    ``CostEvidence`` object or a monetary amount to this function.

    Betfair MARKET commission currently remains shared ``INFORMATIONAL``
    evidence because no canonical allocation/accounting authority proves the
    campaign share or whether the rollup is already embedded in gross P&L.
    Therefore this composition may close only the *source-authority* reason for
    the exact verified commission evidence. It must not subtract that amount,
    infer campaign currency, or upgrade incomplete net economics.

    A supplied prior version is never returned as positive authority merely
    because it has the exact public dataclass type. Every call first re-runs the
    canonical derivation, so caller-authored completeness/totals/reasons cannot
    bypass the fail-closed economic rules.
    """

    if previous is not None and type(previous) is not CampaignEconomicEvidenceVersion:
        raise CostEvidenceError(
            "previous must be exact CampaignEconomicEvidenceVersion"
        )

    commission = issue_betfair_commission_cost_evidence(
        source=source,
        campaign=campaign,
        provider_scope=provider_scope,
        receipt_id=receipt_id,
        record_sha256=record_sha256,
        as_of=as_of,
    )
    costs = _merge_previous_costs(previous, commission)

    # Never shortcut through a caller-supplied CampaignEconomicEvidenceVersion.
    # It is a public immutable value, not an issued capability. Canonical
    # derivation must recompute gross/cost semantics before this source-specific
    # consumer is allowed to narrow one generic unresolved-source reason.
    derived = derive_campaign_economics(
        campaign=campaign,
        costs=costs,
        as_of=as_of,
        previous=previous,
    )

    # The generic derivation is intentionally hostile to caller-authored
    # CostEvidence and labels every populated class as unresolved authority.
    # Remove that class-level reason only when every effective item in this
    # class is the exact source-resolved Betfair evidence from this call. This
    # prevents one verified receipt from laundering an additional caller-owned
    # cost in the same class.
    effective = _effective_costs(derived.costs)
    target_costs = tuple(
        item for item in effective if item.cost_class is _TARGET_CLASS
    )
    trusted_ids = {commission.cost_evidence_id}
    if target_costs and all(
        item.cost_evidence_id in trusted_ids and item == commission
        for item in target_costs
    ):
        reasons = set(derived.incomplete_reasons)
        reasons.discard(_UNRESOLVED_REASON)
        derived = replace(derived, incomplete_reasons=tuple(sorted(reasons)))

    return derived


def _merge_previous_costs(
    previous: CampaignEconomicEvidenceVersion | None,
    commission: CostEvidence,
) -> tuple[CostEvidence, ...]:
    if previous is None:
        return (commission,)

    by_id = {item.cost_evidence_id: item for item in previous.costs}
    retained = by_id.get(commission.cost_evidence_id)
    if retained is not None:
        if retained != commission:
            raise CostEvidenceError(
                "same commission evidence identity changed across versions"
            )
        return previous.costs

    return tuple(
        sorted(
            (*previous.costs, commission),
            key=lambda item: item.cost_evidence_id,
        )
    )


def _effective_costs(costs: tuple[CostEvidence, ...]) -> tuple[CostEvidence, ...]:
    superseded = {
        evidence_id
        for item in costs
        for evidence_id in item.supersedes_cost_evidence_ids
    }
    return tuple(
        item for item in costs if item.cost_evidence_id not in superseded
    )
