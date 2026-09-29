from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import os

from .betfair_campaign_economic_composition import (
    derive_campaign_economics_with_betfair_commission,
)
from .betfair_commission_cost_evidence import (
    BetfairCommissionCostEvidenceError,
    issue_betfair_commission_cost_evidence,
)
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
        inherited = self._durably_source_qualified_target_costs(previous)
        try:
            commission = issue_betfair_commission_cost_evidence(
                source=self._betfair_source,
                campaign=self.campaign,
                provider_scope=self._provider_scope,
                receipt_id=receipt_id,
                record_sha256=record_sha256,
                as_of=as_of,
            )
        except BetfairCommissionCostEvidenceError as direct_error:
            # A corrected MARKET receipt names its predecessor at the provider
            # source boundary. The campaign store may already contain several
            # independent MARKET commission costs, so resolve the correction
            # against the exact durable predecessor rather than assuming the
            # whole campaign has only one active commission receipt.
            corrections: dict[str, CostEvidence] = {}
            for predecessor in inherited:
                try:
                    candidate = issue_betfair_commission_cost_evidence(
                        source=self._betfair_source,
                        campaign=self.campaign,
                        provider_scope=self._provider_scope,
                        receipt_id=receipt_id,
                        record_sha256=record_sha256,
                        as_of=as_of,
                        supersedes=predecessor,
                    )
                except BetfairCommissionCostEvidenceError:
                    continue
                corrections[candidate.cost_evidence_id] = candidate
            if not corrections:
                raise direct_error
            if len(corrections) != 1:
                raise CampaignEconomicStoreError(
                    "Betfair commission correction is ambiguous across active market predecessors"
                )
            commission = next(iter(corrections.values()))

        costs = _merge_source_cost(previous, commission)
        generic = super().derive(
            costs=costs,
            as_of=as_of,
            previous=previous,
        )
        trusted = {
            item.cost_evidence_id: item
            for item in inherited
            if item.cost_evidence_id not in commission.supersedes_cost_evidence_ids
        }
        trusted[commission.cost_evidence_id] = commission
        return _qualify_exact_source_variant(
            generic,
            tuple(sorted(trusted.values(), key=lambda item: item.cost_evidence_id)),
        )

    def _durably_source_qualified_target_costs(
        self,
        previous: CampaignEconomicEvidenceVersion | None,
    ) -> tuple[CostEvidence, ...]:
        if previous is None:
            return ()
        effective_target = _target_costs(previous.costs)
        if not effective_target:
            return ()
        if _UNRESOLVED_REASON in previous.incomplete_reasons:
            # A prior generic version may contain Betfair-shaped values without
            # source authority. Never use those values as correction capability.
            return ()
        if not self._has_durable_source_qualification(previous):
            raise CampaignEconomicStoreError(
                "resolved Betfair commission history lacks durable source qualification"
            )
        if not all(_is_betfair_source_cost(item) for item in effective_target):
            raise CampaignEconomicStoreError(
                "resolved Betfair commission history contains non-canonical target cost evidence"
            )
        return effective_target

    def _is_exact_live_retry(
        self,
        version: CampaignEconomicEvidenceVersion,
        *,
        receipt_id: str,
        record_sha256: str,
        as_of: datetime,
    ) -> bool:
        if _UNRESOLVED_REASON in version.incomplete_reasons:
            return False
        if as_of < version.as_of:
            raise CampaignEconomicStoreError(
                "Betfair commission retry as_of cannot predate latest durable economic version"
            )
        matches = tuple(
            item
            for item in _target_costs(version.costs)
            if item.source.family == BETFAIR_COMMISSION_SOURCE_FAMILY
            and item.source.evidence_id == receipt_id
            and item.source.sha256 == record_sha256
        )
        if len(matches) != 1:
            return False
        self._reverify_source_cost(version, matches[0], as_of=as_of)
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
        previous_target_ids = (
            set()
            if previous is None
            else {item.cost_evidence_id for item in _target_costs(previous.costs)}
        )
        introduced = tuple(
            item for item in target if item.cost_evidence_id not in previous_target_ids
        )
        if len(introduced) != 1:
            raise CampaignEconomicStoreError(
                "source-qualified Betfair append must introduce exactly one commission receipt"
            )
        self._reverify_source_cost(
            version,
            introduced[0],
            previous=previous,
        )

    def _reverify_source_cost(
        self,
        version: CampaignEconomicEvidenceVersion,
        observed: CostEvidence,
        *,
        previous: CampaignEconomicEvidenceVersion | None = None,
        as_of: datetime | None = None,
    ) -> None:
        if not _is_betfair_source_cost(observed):
            raise CampaignEconomicStoreError(
                "source-qualified Betfair version contains invalid commission evidence"
            )
        predecessor = self._find_correction_predecessor(
            version,
            observed,
            previous=previous,
        )
        try:
            expected = issue_betfair_commission_cost_evidence(
                source=self._betfair_source,
                campaign=self.campaign,
                provider_scope=self._provider_scope,
                receipt_id=observed.source.evidence_id,
                record_sha256=observed.source.sha256,
                as_of=version.as_of if as_of is None else as_of,
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

    def _find_correction_predecessor(
        self,
        version: CampaignEconomicEvidenceVersion,
        observed: CostEvidence,
        *,
        previous: CampaignEconomicEvidenceVersion | None = None,
    ) -> CostEvidence | None:
        supersedes = observed.supersedes_cost_evidence_ids
        if not supersedes:
            return None
        if len(supersedes) != 1:
            raise CampaignEconomicStoreError(
                "Betfair correction must name exactly one predecessor cost"
            )
        predecessor_id = supersedes[0]
        cursor = previous
        if cursor is None and version.previous_version_id is not None:
            cursor = self._load_raw(version.previous_version_id)
        seen: set[str] = set()
        while cursor is not None:
            if cursor.version_id in seen:
                raise CampaignEconomicStoreError(
                    "economic history cycle while resolving Betfair correction"
                )
            seen.add(cursor.version_id)
            matches = tuple(
                item
                for item in cursor.costs
                if item.cost_evidence_id == predecessor_id
            )
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise CampaignEconomicStoreError(
                    "Betfair correction predecessor is not unique"
                )
            if cursor.previous_version_id is None:
                break
            cursor = self._load_raw(cursor.previous_version_id)
        raise CampaignEconomicStoreError(
            "Betfair correction predecessor is absent from durable economic history"
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


def _merge_source_cost(
    previous: CampaignEconomicEvidenceVersion | None,
    commission: CostEvidence,
) -> tuple[CostEvidence, ...]:
    prior = () if previous is None else previous.costs
    superseded = set(commission.supersedes_cost_evidence_ids)
    retained = tuple(
        item for item in prior if item.cost_evidence_id not in superseded
    )
    duplicate = tuple(
        item
        for item in retained
        if item.cost_evidence_id == commission.cost_evidence_id
    )
    if duplicate:
        if len(duplicate) != 1 or duplicate[0] != commission:
            raise CampaignEconomicStoreError(
                "Betfair commission evidence identity conflicts with durable history"
            )
        raise CampaignEconomicStoreError(
            "existing Betfair commission receipt requires an exact live retry"
        )
    return tuple(
        sorted(
            (*retained, commission),
            key=lambda item: item.cost_evidence_id,
        )
    )


def _is_betfair_source_cost(cost: CostEvidence) -> bool:
    return (
        type(cost) is CostEvidence
        and cost.cost_class is _TARGET_CLASS
        and cost.source.family == BETFAIR_COMMISSION_SOURCE_FAMILY
        and cost.basis is CostBasis.OBSERVED_INCURRED
        and cost.treatment is CostTreatment.INFORMATIONAL
        and cost.truth in {CostTruth.KNOWN_ZERO, CostTruth.KNOWN_AMOUNT}
        and cost.unit is CostUnit.MONEY
        and cost.shared_source
        and cost.allocation_source is None
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
    source_costs: tuple[CostEvidence, ...],
) -> CampaignEconomicEvidenceVersion:
    target = _target_costs(generic.costs)
    trusted = tuple(sorted(source_costs, key=lambda item: item.cost_evidence_id))
    if not target or target != trusted:
        return generic
    if not all(_is_betfair_source_cost(item) for item in target):
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
    if not target or not all(_is_betfair_source_cost(cost) for cost in target):
        return False
    reasons = set(generic.incomplete_reasons)
    if _UNRESOLVED_REASON not in reasons:
        return False
    reasons.remove(_UNRESOLVED_REASON)
    expected = replace(generic, incomplete_reasons=tuple(sorted(reasons)))
    return expected == candidate