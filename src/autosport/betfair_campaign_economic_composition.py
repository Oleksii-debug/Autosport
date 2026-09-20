from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import os

from .betfair_commission_cost_evidence import issue_betfair_commission_cost_evidence
from .betfair_market_commission_authority import (
    SOURCE_FAMILY as BETFAIR_COMMISSION_SOURCE_FAMILY,
    BetfairMarketCommissionAuthority,
)
from .campaign_cost_evidence import (
    CampaignEconomicEvidenceVersion,
    CostBasis,
    CostClass,
    CostEvidence,
    CostEvidenceError,
    CostTreatment,
    CostTruth,
    CostUnit,
    derive_campaign_economics,
)
from .campaign_economic_authority import FinalizedCampaignAuthority
from .campaign_economic_store import (
    CampaignEconomicEvidenceStore,
    CampaignEconomicStoreError,
)
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

    # Exact retries of an already source-qualified immutable version are
    # idempotent. Revalidate the live finalized campaign projection first so a
    # caller cannot use retry behavior to carry a version across campaign truth.
    if (
        previous is not None
        and previous.costs == costs
        and previous.as_of == as_of
        and _UNRESOLVED_REASON not in previous.incomplete_reasons
        and campaign.projection() == previous.campaign_authority
    ):
        return previous

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


class BetfairCampaignEconomicEvidenceStore(CampaignEconomicEvidenceStore):
    """Canonical campaign store adapter for source-qualified Betfair commission.

    Persistence remains the existing ``CampaignEconomicEvidenceStore`` and its
    external monotonic authority. This adapter only adds the source capability
    needed to admit one source-qualified semantic variant safely.

    A first append is accepted only while this instance has just re-resolved
    the exact Betfair receipt through the product-owned composition path. After
    publication, restart validation may trust only an exact matching durable
    COMMIT (or the live terminal PREPARE needed for crash recovery). A caller
    therefore cannot manufacture a source-qualified version and feed it to
    public ``append`` merely by copying the Betfair source-family string.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        campaign: FinalizedCampaignAuthority,
        source: BetfairMarketCommissionAuthority,
        provider_scope: CampaignProviderScopeProjection,
        authority_root: str | os.PathLike[str] | None = None,
    ) -> None:
        super().__init__(
            root,
            campaign=campaign,
            authority_root=authority_root,
        )
        if type(source) is not BetfairMarketCommissionAuthority:
            raise CampaignEconomicStoreError(
                "Betfair economic store requires exact commission source authority"
            )
        if type(provider_scope) is not CampaignProviderScopeProjection:
            raise CampaignEconomicStoreError(
                "Betfair economic store requires exact provider scope projection"
            )
        self._betfair_source = source
        self._provider_scope = provider_scope
        self._source_qualified_append_id: str | None = None

    def derive_betfair_commission(
        self,
        *,
        receipt_id: str,
        record_sha256: str,
        as_of: datetime,
        previous: CampaignEconomicEvidenceVersion | None = None,
    ) -> CampaignEconomicEvidenceVersion:
        return derive_campaign_economics_with_betfair_commission(
            source=self._betfair_source,
            campaign=self.campaign,
            provider_scope=self._provider_scope,
            receipt_id=receipt_id,
            record_sha256=record_sha256,
            as_of=as_of,
            previous=previous,
        )

    def append_betfair_commission(
        self,
        *,
        receipt_id: str,
        record_sha256: str,
        as_of: datetime,
    ) -> str:
        previous = self.latest()
        version = self.derive_betfair_commission(
            receipt_id=receipt_id,
            record_sha256=record_sha256,
            as_of=as_of,
            previous=previous,
        )
        self._source_qualified_append_id = version.version_id
        try:
            return super().append(version)
        finally:
            self._source_qualified_append_id = None

    def _validate_derived(
        self,
        version: CampaignEconomicEvidenceVersion,
        previous: CampaignEconomicEvidenceVersion | None,
    ) -> None:
        # Preserve the canonical generic path byte-for-byte. Only the one
        # source-qualified variant below receives special handling.
        try:
            super()._validate_derived(version, previous)
            return
        except CampaignEconomicStoreError as generic_error:
            try:
                generic = super().derive(
                    costs=version.costs,
                    as_of=version.as_of,
                    previous=previous,
                )
            except CostEvidenceError:
                raise generic_error

            if not _is_exact_betfair_source_qualified_variant(generic, version):
                raise generic_error

            if self._source_qualified_append_id == version.version_id:
                self._reverify_live_commission(version)
                return
            if self._has_durable_source_qualification(version):
                return
            raise CampaignEconomicStoreError(
                "source-qualified economic version lacks live verification or durable publication authority"
            ) from generic_error

    def _reverify_live_commission(
        self,
        version: CampaignEconomicEvidenceVersion,
    ) -> None:
        target = _target_costs(version.costs)
        if len(target) != 1:
            raise CampaignEconomicStoreError(
                "source-qualified Betfair version must contain one effective commission receipt"
            )
        observed = target[0]
        try:
            expected = issue_betfair_commission_cost_evidence(
                source=self._betfair_source,
                campaign=self.campaign,
                provider_scope=self._provider_scope,
                receipt_id=observed.source.evidence_id,
                record_sha256=observed.source.sha256,
                as_of=version.as_of,
            )
        except Exception as exc:
            raise CampaignEconomicStoreError(
                "Betfair commission failed live source re-verification"
            ) from exc
        if observed != expected:
            raise CampaignEconomicStoreError(
                "Betfair commission changed during durable economic admission"
            )

    def _has_durable_source_qualification(
        self,
        version: CampaignEconomicEvidenceVersion,
    ) -> bool:
        history = self._authority.read_history()
        if not history:
            return False
        binding = self._semantic_binding(version)
        for record in history:
            if (
                record.phase.value == "COMMIT"
                and record.intended_state_sha256 == version.version_id
                and record.semantic_binding_sha256 == binding
            ):
                return True
        latest = history[-1]
        return (
            latest.phase.value == "PREPARE"
            and latest.intended_state_sha256 == version.version_id
            and latest.semantic_binding_sha256 == binding
        )


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


def _target_costs(costs: tuple[CostEvidence, ...]) -> tuple[CostEvidence, ...]:
    return tuple(
        item
        for item in _effective_costs(costs)
        if item.cost_class is _TARGET_CLASS
    )


def _is_exact_betfair_source_qualified_variant(
    generic: CampaignEconomicEvidenceVersion,
    candidate: CampaignEconomicEvidenceVersion,
) -> bool:
    target = _target_costs(candidate.costs)
    if len(target) != 1:
        return False
    cost = target[0]
    if (
        cost.source.family != BETFAIR_COMMISSION_SOURCE_FAMILY
        or cost.basis is not CostBasis.OBSERVED_INCURRED
        or cost.treatment is not CostTreatment.INFORMATIONAL
        or cost.truth not in {CostTruth.KNOWN_ZERO, CostTruth.KNOWN_AMOUNT}
        or cost.unit is not CostUnit.MONEY
        or not cost.shared_source
        or cost.allocation_source is not None
    ):
        return False
    reasons = set(generic.incomplete_reasons)
    if _UNRESOLVED_REASON not in reasons:
        return False
    reasons.remove(_UNRESOLVED_REASON)
    expected = replace(generic, incomplete_reasons=tuple(sorted(reasons)))
    return expected == candidate
