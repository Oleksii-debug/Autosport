from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import os

from .betfair_campaign_economic_composition import (
    derive_campaign_economics_with_betfair_commission,
)
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
)
from .campaign_economic_authority import FinalizedCampaignAuthority
from .campaign_economic_store import (
    CampaignEconomicEvidenceStore,
    CampaignEconomicStoreError,
)
from .campaign_provider_scope_authority import CampaignProviderScopeProjection


_TARGET_CLASS = CostClass.EXECUTION_FEES_COMMISSION_TAX
_UNRESOLVED_REASON = f"UNRESOLVED_COST_AUTHORITY:{_TARGET_CLASS.value}"


class BetfairCampaignEconomicEvidenceStore(CampaignEconomicEvidenceStore):
    """Durably admit source-qualified Betfair commission campaign economics.

    Persistence and rollback fencing remain the existing
    ``CampaignEconomicEvidenceStore`` and its ``MonotonicWorkspaceAuthority``.
    This adapter only adds the product-owned source capability required to
    validate the Betfair-qualified semantic variant and its exact append-only
    provider correction lineage.

    First publication requires live source re-resolution. After publication,
    restart validation accepts that exact immutable version only when the same
    version and semantic binding have a durable COMMIT, or the live terminal
    PREPARE required for exact crash recovery. Public ``append`` therefore
    cannot self-attest source authority by copying Betfair source fields.
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
        if previous is not None and self._is_exact_live_retry(
            previous,
            receipt_id=receipt_id,
            record_sha256=record_sha256,
            as_of=as_of,
        ):
            return previous.version_id

        version = self._derive_for_durable_append(
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

    def _derive_for_durable_append(
        self,
        *,
        receipt_id: str,
        record_sha256: str,
        as_of: datetime,
        previous: CampaignEconomicEvidenceVersion | None,
    ) -> CampaignEconomicEvidenceVersion:
        predecessor = self._active_source_qualified_predecessor(previous)
        if predecessor is None:
            return self.derive_betfair_commission(
                receipt_id=receipt_id,
                record_sha256=record_sha256,
                as_of=as_of,
                previous=previous,
            )

        correction = issue_betfair_commission_cost_evidence(
            source=self._betfair_source,
            campaign=self.campaign,
            provider_scope=self._provider_scope,
            receipt_id=receipt_id,
            record_sha256=record_sha256,
            as_of=as_of,
            supersedes=predecessor,
        )
        costs = tuple(
            sorted(
                (
                    *(
                        item
                        for item in previous.costs
                        if item.cost_evidence_id != predecessor.cost_evidence_id
                    ),
                    correction,
                ),
                key=lambda item: item.cost_evidence_id,
            )
        )
        generic = super().derive(
            costs=costs,
            as_of=as_of,
            previous=previous,
        )
        return _qualify_exact_source_variant(generic, correction)

    def _active_source_qualified_predecessor(
        self,
        previous: CampaignEconomicEvidenceVersion | None,
    ) -> CostEvidence | None:
        if previous is None:
            return None
        effective_target = _target_costs(previous.costs)
        betfair = tuple(
            item
            for item in effective_target
            if item.source.family == BETFAIR_COMMISSION_SOURCE_FAMILY
        )
        if not betfair:
            return None
        if (
            len(effective_target) != 1
            or len(betfair) != 1
            or _UNRESOLVED_REASON in previous.incomplete_reasons
            or not self._has_durable_source_qualification(previous)
        ):
            raise CampaignEconomicStoreError(
                "Betfair commission correction requires exactly one durable active source-qualified predecessor"
            )
        return betfair[0]

    def _is_exact_live_retry(
        self,
        version: CampaignEconomicEvidenceVersion,
        *,
        receipt_id: str,
        record_sha256: str,
        as_of: datetime,
    ) -> bool:
        if version.as_of != as_of or _UNRESOLVED_REASON in version.incomplete_reasons:
            return False
        target = _target_costs(version.costs)
        if len(target) != 1:
            return False
        observed = target[0]
        if (
            observed.source.family != BETFAIR_COMMISSION_SOURCE_FAMILY
            or observed.source.evidence_id != receipt_id
            or observed.source.sha256 != record_sha256
        ):
            return False
        self._reverify_live_commission(version)
        return True

    def _validate_derived(
        self,
        version: CampaignEconomicEvidenceVersion,
        previous: CampaignEconomicEvidenceVersion | None,
    ) -> None:
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
                self._reverify_live_commission(version, previous=previous)
                return
            if self._has_durable_source_qualification(version):
                return
            raise CampaignEconomicStoreError(
                "source-qualified economic version lacks live verification or durable publication authority"
            ) from generic_error

    def _reverify_live_commission(
        self,
        version: CampaignEconomicEvidenceVersion,
        *,
        previous: CampaignEconomicEvidenceVersion | None = None,
    ) -> None:
        target = _target_costs(version.costs)
        if len(target) != 1:
            raise CampaignEconomicStoreError(
                "source-qualified Betfair version must contain one effective commission receipt"
            )
        observed = target[0]
        supersedes = observed.supersedes_cost_evidence_ids
        predecessor: CostEvidence | None = None
        if supersedes:
            if len(supersedes) != 1 or version.previous_version_id is None:
                raise CampaignEconomicStoreError(
                    "Betfair correction must name one predecessor cost in the prior economic version"
                )
            if previous is None:
                previous = self._load_raw(version.previous_version_id)
            if previous.version_id != version.previous_version_id:
                raise CampaignEconomicStoreError(
                    "Betfair correction predecessor economic version mismatch"
                )
            matches = tuple(
                item
                for item in previous.costs
                if item.cost_evidence_id == supersedes[0]
            )
            if len(matches) != 1:
                raise CampaignEconomicStoreError(
                    "Betfair correction target is not uniquely present in predecessor economics"
                )
            predecessor = matches[0]
        try:
            expected = issue_betfair_commission_cost_evidence(
                source=self._betfair_source,
                campaign=self.campaign,
                provider_scope=self._provider_scope,
                receipt_id=observed.source.evidence_id,
                record_sha256=observed.source.sha256,
                as_of=version.as_of,
                supersedes=predecessor,
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


def _qualify_exact_source_variant(
    generic: CampaignEconomicEvidenceVersion,
    source_cost: CostEvidence,
) -> CampaignEconomicEvidenceVersion:
    target = _target_costs(generic.costs)
    if len(target) != 1 or target[0] != source_cost:
        return generic
    reasons = set(generic.incomplete_reasons)
    if _UNRESOLVED_REASON not in reasons:
        return generic
    reasons.remove(_UNRESOLVED_REASON)
    return replace(generic, incomplete_reasons=tuple(sorted(reasons)))


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
