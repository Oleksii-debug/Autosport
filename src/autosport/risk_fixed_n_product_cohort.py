from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .paper_settlement_learning import PaperSettlementLearningBridge
from .risk_of_ruin_evaluator import RiskPathObservation
from .risk_path_observation_authority import (
    ProductRunCapitalPathError,
    ProductRunCapitalPathEvidence,
    resolve_product_run_capital_path_evidence,
)
from .risk_sampling_dependence import (
    ResolvedFixedNIidPrecommitAuthority,
    ResolvedFixedNIidSamplingStructure,
    RiskSamplingDependenceError,
    inspect_fixed_n_iid_sampling_structure,
    resolve_fixed_n_iid_precommit_authority,
)
from .risk_sampling_membership import ResolvedFixedNRiskMembership


_MAX_PRODUCT_FIXED_N_COHORT = 10_000
_HEX = frozenset("0123456789abcdef")

_MEMBERSHIP_TYPE = ResolvedFixedNRiskMembership
_STRUCTURE_TYPE = ResolvedFixedNIidSamplingStructure
_PRECOMMIT_TYPE = ResolvedFixedNIidPrecommitAuthority
_PATH_TYPE = ProductRunCapitalPathEvidence
_BRIDGE_TYPE = PaperSettlementLearningBridge
_OBSERVATION_TYPE = RiskPathObservation

_STRUCTURE = inspect_fixed_n_iid_sampling_structure
_STRUCTURE_CODE = getattr(_STRUCTURE, "__code__", None)
_PRECOMMIT = resolve_fixed_n_iid_precommit_authority
_PRECOMMIT_CODE = getattr(_PRECOMMIT, "__code__", None)
_PATH_RESOLVER = resolve_product_run_capital_path_evidence
_PATH_RESOLVER_CODE = getattr(_PATH_RESOLVER, "__code__", None)


class ProductFixedNIidCohortError(RuntimeError):
    """Product-owned fixed-N IID cohort cannot be resolved fail-closed."""


def _sha(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(ch not in _HEX for ch in value)
    ):
        raise ProductFixedNIidCohortError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return value


def _instant(value: object, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise ProductFixedNIidCohortError(
            f"{name} must be canonical ISO-8601 text"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductFixedNIidCohortError(
            f"{name} must be valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductFixedNIidCohortError(
            f"{name} must include a timezone offset"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductFixedNIidCohortError(
            "fixed-N IID cohort material is outside canonical JSON"
        ) from exc


@dataclass(frozen=True, slots=True, init=False)
class ProductFixedNIidCohort:
    """Product-resolved complete fixed-N IID run cohort.

    The receipt proves that every precommitted member reached exact replay
    execution, the frozen initial capital state, the frozen stake policy, and
    product-owned run-capital observation ancestry under the with-replacement design.  The IID claim is deliberately scoped to that frozen
    simulator distribution and does not itself authorize staking or real money.

    Consumers must re-resolve this receipt through
    resolve_product_fixed_n_iid_cohort rather than treating an in-memory Python
    object as a bearer capability.
    """

    experiment_id: str
    sampling_manifest_sha256: str
    planned_member_ids: tuple[str, ...]
    member_stream_sha256: tuple[str, ...]
    run_source_evidence_sha256: tuple[str, ...]
    run_execution_receipt_sha256: tuple[str, ...]
    initial_capital_state_sha256: str
    stake_policy_sha256: str
    observations: tuple[RiskPathObservation, ...]
    outcomes_available_at: str
    cohort_sha256: str

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> "ProductFixedNIidCohort":
        raise TypeError(
            "ProductFixedNIidCohort is product-resolved; "
            "use resolve_product_fixed_n_iid_cohort"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ProductFixedNIidCohort must not be subclassed")

    @property
    def planned_n(self) -> int:
        return len(self.planned_member_ids)

    @property
    def product_precommit_bound(self) -> bool:
        return True

    @property
    def execution_consumption_proven(self) -> bool:
        return True

    @property
    def occurrence_ancestry_proven(self) -> bool:
        return True

    @property
    def run_path_ancestry_proven(self) -> bool:
        return True

    @property
    def fixed_n_complete(self) -> bool:
        return True

    @property
    def iid_qualified(self) -> bool:
        return True

    @property
    def risk_scope(self) -> str:
        return "SIMULATOR_DISTRIBUTION_ONLY"

    @property
    def grants_real_money_authority(self) -> bool:
        return False


_COHORT_TYPE = ProductFixedNIidCohort


def _require_dispatch() -> None:
    if (
        ResolvedFixedNRiskMembership is not _MEMBERSHIP_TYPE
        or ResolvedFixedNIidSamplingStructure is not _STRUCTURE_TYPE
        or ResolvedFixedNIidPrecommitAuthority is not _PRECOMMIT_TYPE
        or ProductRunCapitalPathEvidence is not _PATH_TYPE
        or PaperSettlementLearningBridge is not _BRIDGE_TYPE
        or RiskPathObservation is not _OBSERVATION_TYPE
        or ProductFixedNIidCohort is not _COHORT_TYPE
        or inspect_fixed_n_iid_sampling_structure is not _STRUCTURE
        or getattr(_STRUCTURE, "__code__", None) is not _STRUCTURE_CODE
        or resolve_fixed_n_iid_precommit_authority is not _PRECOMMIT
        or getattr(_PRECOMMIT, "__code__", None) is not _PRECOMMIT_CODE
        or resolve_product_run_capital_path_evidence is not _PATH_RESOLVER
        or getattr(_PATH_RESOLVER, "__code__", None) is not _PATH_RESOLVER_CODE
    ):
        raise ProductFixedNIidCohortError(
            "fixed-N IID cohort authority dispatch changed"
        )


def _validate_product_fixed_n_iid_cohort_inputs(
    structure: ResolvedFixedNIidSamplingStructure,
    precommit: ResolvedFixedNIidPrecommitAuthority,
    evidence: tuple[ProductRunCapitalPathEvidence, ...],
) -> dict[str, object]:
    """Validate already re-resolved exact receipts without issuing authority.

    This helper deliberately returns neutral material only.  The positive receipt
    is constructed solely by resolve_product_fixed_n_iid_cohort after every member
    has been re-resolved from durable product state.
    """

    if type(structure) is not _STRUCTURE_TYPE:
        raise TypeError("structure must be exact ResolvedFixedNIidSamplingStructure")
    if type(precommit) is not _PRECOMMIT_TYPE:
        raise TypeError("precommit must be exact ResolvedFixedNIidPrecommitAuthority")
    if type(evidence) is not tuple:
        raise TypeError("evidence must be an exact tuple")
    planned_n = structure.planned_n
    if planned_n <= 0:
        raise ProductFixedNIidCohortError("fixed-N IID cohort must be non-empty")
    if planned_n > _MAX_PRODUCT_FIXED_N_COHORT:
        raise ProductFixedNIidCohortError(
            "UNSUPPORTED_RESOURCE_DOMAIN: fixed-N IID cohort exceeds the "
            "current evaluator work budget; this is not a statistical "
            "sample-adequacy judgment"
        )
    if len(evidence) != planned_n:
        raise ProductFixedNIidCohortError(
            "fixed-N IID cohort requires every precommitted member exactly once"
        )
    if (
        precommit.product_membership_preoutcome_chronology_proven is not True
        or precommit.product_randomization_root_issued is not True
        or precommit.occurrence_ancestry_proven is not False
        or precommit.iid_qualified is not False
        or precommit.experiment_id != structure.experiment_id
        or precommit.sampling_manifest_sha256 != structure.manifest_sha256
        or precommit.planned_member_ids != structure.planned_member_ids
    ):
        raise ProductFixedNIidCohortError(
            "fixed-N IID precommit authority does not bind the exact cohort"
        )
    if len(structure.member_stream_sha256) != planned_n:
        raise ProductFixedNIidCohortError(
            "fixed-N IID stream cardinality differs from planned membership"
        )
    expected_initial_capital = _sha(
        structure.initial_capital_state_sha256,
        "initial_capital_state_sha256",
    )
    expected_stake_policy = _sha(
        structure.stake_policy_sha256,
        "stake_policy_sha256",
    )
    streams = tuple(
        _sha(value, f"member_stream_sha256[{index}]")
        for index, value in enumerate(structure.member_stream_sha256)
    )
    if len(set(streams)) != planned_n:
        raise ProductFixedNIidCohortError(
            "fixed-N IID members must use distinct product randomization streams"
        )

    observations: list[RiskPathObservation] = []
    source_ids: list[str] = []
    execution_ids: list[str] = []
    available: list[datetime] = []
    for index, item in enumerate(evidence):
        if type(item) is not _PATH_TYPE:
            raise TypeError(
                "fixed-N IID evidence must contain exact ProductRunCapitalPathEvidence"
            )
        if (
            item.complete is not True
            or item.product_precommit_bound is not True
            or item.run_path_ancestry_proven is not True
            or item.sampling_frame_materialized is not True
            or item.expected_draw_product_derived is not True
            or item.run_admission_bound is not True
            or item.replay_input_binding_proven is not True
            or item.execution_consumption_proven is not True
            or item.sampling_occurrence_ancestry_proven is not True
            or item.iid_qualified is not False
            or item.grants_real_money_authority is not False
            or type(item.member_index) is not int
            or item.member_index != index
            or item.member_id != structure.planned_member_ids[index]
            or item.expected_stream_sha256 != streams[index]
            or item.executed_initial_capital_state_sha256
            != expected_initial_capital
            or item.executed_stake_policy_sha256 != expected_stake_policy
        ):
            raise ProductFixedNIidCohortError(
                "run-capital evidence does not bind the exact precommitted IID member"
            )
        if type(item.minimum_equity) is not Decimal or not item.minimum_equity.is_finite():
            raise ProductFixedNIidCohortError(
                "run-capital minimum equity must be a finite exact Decimal"
            )
        source_id = _sha(
            item.source_evidence_sha256,
            f"source_evidence_sha256[{index}]",
        )
        execution_id = _sha(
            item.run_execution_receipt_sha256,
            f"run_execution_receipt_sha256[{index}]",
        )
        when = _instant(item.outcome_available_at, f"outcome_available_at[{index}]")
        source_ids.append(source_id)
        execution_ids.append(execution_id)
        available.append(when)
        observations.append(
            _OBSERVATION_TYPE(
                independent_unit_id=item.member_id,
                dependence_group_id=streams[index],
                minimum_equity=item.minimum_equity,
                outcome_available_at=when.isoformat(),
                source_evidence_sha256=source_id,
            )
        )

    if len(set(source_ids)) != planned_n:
        raise ProductFixedNIidCohortError(
            "fixed-N IID cohort requires unique product run evidence per member"
        )
    if len(set(execution_ids)) != planned_n:
        raise ProductFixedNIidCohortError(
            "fixed-N IID cohort requires unique replay execution receipt per member"
        )

    outcomes_available_at = max(available).isoformat()
    payload = {
        "schema": "AUTOSPORT_PRODUCT_FIXED_N_IID_COHORT_V1",
        "experiment_id": structure.experiment_id,
        "sampling_manifest_sha256": _sha(
            structure.manifest_sha256,
            "sampling_manifest_sha256",
        ),
        "planned_member_ids": list(structure.planned_member_ids),
        "member_stream_sha256": list(streams),
        "run_source_evidence_sha256": source_ids,
        "run_execution_receipt_sha256": execution_ids,
        "initial_capital_state_sha256": expected_initial_capital,
        "stake_policy_sha256": expected_stake_policy,
        "observations": [item.canonical_payload() for item in observations],
        "outcomes_available_at": outcomes_available_at,
        "fixed_n_complete": True,
        "iid_qualified": True,
        "risk_scope": "SIMULATOR_DISTRIBUTION_ONLY",
        "grants_real_money_authority": False,
    }
    return {
        "experiment_id": structure.experiment_id,
        "sampling_manifest_sha256": _sha(
            structure.manifest_sha256,
            "sampling_manifest_sha256",
        ),
        "planned_member_ids": tuple(structure.planned_member_ids),
        "member_stream_sha256": streams,
        "run_source_evidence_sha256": tuple(source_ids),
        "run_execution_receipt_sha256": tuple(execution_ids),
        "initial_capital_state_sha256": expected_initial_capital,
        "stake_policy_sha256": expected_stake_policy,
        "observations": tuple(observations),
        "outcomes_available_at": outcomes_available_at,
        "cohort_sha256": hashlib.sha256(_canonical_json(payload)).hexdigest(),
    }


def resolve_product_fixed_n_iid_cohort(
    membership: ResolvedFixedNRiskMembership,
    *,
    workspace: str | Path,
    registry_path: str | Path,
    sampling_manifest_json: str,
    sampling_frame_json: str,
    horizon_json: str,
    settlement_bridges: tuple[PaperSettlementLearningBridge, ...],
    authority_root: str | Path | None = None,
) -> ProductFixedNIidCohort:
    """Re-resolve every fixed-N member and issue simulator-scoped IID authority."""

    _require_dispatch()
    if type(membership) is not _MEMBERSHIP_TYPE:
        raise TypeError("membership must be exact ResolvedFixedNRiskMembership")
    if type(settlement_bridges) is not tuple:
        raise TypeError("settlement_bridges must be an exact tuple")
    if any(type(item) is not _BRIDGE_TYPE for item in settlement_bridges):
        raise TypeError(
            "settlement_bridges must contain exact PaperSettlementLearningBridge values"
        )
    try:
        structure = _STRUCTURE(
            membership,
            sampling_manifest_json=sampling_manifest_json,
        )
        precommit = _PRECOMMIT(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            authority_root=authority_root,
        )
    except (RiskSamplingDependenceError, OSError, RuntimeError, ValueError) as exc:
        raise ProductFixedNIidCohortError(
            "fixed-N IID product precommit cannot be re-resolved"
        ) from exc
    _require_dispatch()
    if len(settlement_bridges) != structure.planned_n:
        raise ProductFixedNIidCohortError(
            "fixed-N IID cohort requires one settlement bridge per planned member"
        )

    resolved: list[ProductRunCapitalPathEvidence] = []
    for member_index, run_id in enumerate(structure.planned_member_ids):
        try:
            item = _PATH_RESOLVER(
                workspace=workspace,
                run_id=run_id,
                member_index=member_index,
                membership=membership,
                registry_path=registry_path,
                sampling_manifest_json=sampling_manifest_json,
                sampling_frame_json=sampling_frame_json,
                horizon_json=horizon_json,
                settlement_bridge=settlement_bridges[member_index],
                authority_root=authority_root,
            )
        except (ProductRunCapitalPathError, OSError, RuntimeError, ValueError) as exc:
            raise ProductFixedNIidCohortError(
                f"fixed-N IID member {member_index} run-capital evidence "
                "cannot be re-resolved"
            ) from exc
        _require_dispatch()
        resolved.append(item)

    material = _validate_product_fixed_n_iid_cohort_inputs(
        structure,
        precommit,
        tuple(resolved),
    )
    _require_dispatch()
    result = object.__new__(_COHORT_TYPE)
    for field_name in (
        "experiment_id",
        "sampling_manifest_sha256",
        "planned_member_ids",
        "member_stream_sha256",
        "run_source_evidence_sha256",
        "run_execution_receipt_sha256",
        "initial_capital_state_sha256",
        "stake_policy_sha256",
        "observations",
        "outcomes_available_at",
        "cohort_sha256",
    ):
        object.__setattr__(result, field_name, material[field_name])
    _require_dispatch()
    return result


def _build_product_fixed_n_iid_cohort_verifier(
    resolver,
    cohort_type: type[ProductFixedNIidCohort],
):
    """Capture the only resolver/type graph trusted for downstream re-verification."""

    module_globals = globals()
    resolver_code = getattr(resolver, "__code__", None)
    field_names = (
        "experiment_id",
        "sampling_manifest_sha256",
        "planned_member_ids",
        "member_stream_sha256",
        "run_source_evidence_sha256",
        "run_execution_receipt_sha256",
        "initial_capital_state_sha256",
        "stake_policy_sha256",
        "observations",
        "outcomes_available_at",
        "cohort_sha256",
    )

    def verifier(
        candidate: ProductFixedNIidCohort,
        membership: ResolvedFixedNRiskMembership,
        *,
        workspace: str | Path,
        registry_path: str | Path,
        sampling_manifest_json: str,
        sampling_frame_json: str,
        horizon_json: str,
        settlement_bridges: tuple[PaperSettlementLearningBridge, ...],
        authority_root: str | Path | None = None,
    ) -> ProductFixedNIidCohort:
        if (
            module_globals.get("resolve_product_fixed_n_iid_cohort") is not resolver
            or getattr(resolver, "__code__", None) is not resolver_code
            or module_globals.get("ProductFixedNIidCohort") is not cohort_type
        ):
            raise ProductFixedNIidCohortError(
                "fixed-N IID cohort verifier authority dispatch changed"
            )
        if type(candidate) is not cohort_type:
            raise TypeError("candidate must be an exact ProductFixedNIidCohort")
        canonical = resolver(
            membership,
            workspace=workspace,
            registry_path=registry_path,
            sampling_manifest_json=sampling_manifest_json,
            sampling_frame_json=sampling_frame_json,
            horizon_json=horizon_json,
            settlement_bridges=settlement_bridges,
            authority_root=authority_root,
        )
        if (
            module_globals.get("resolve_product_fixed_n_iid_cohort") is not resolver
            or getattr(resolver, "__code__", None) is not resolver_code
            or module_globals.get("ProductFixedNIidCohort") is not cohort_type
        ):
            raise ProductFixedNIidCohortError(
                "fixed-N IID cohort verifier authority dispatch changed"
            )
        for field_name in field_names:
            supplied = getattr(candidate, field_name)
            expected = getattr(canonical, field_name)
            if type(supplied) is not type(expected) or supplied != expected:
                raise ProductFixedNIidCohortError(
                    "fixed-N IID cohort does not match canonical durable roots"
                )
        if (
            canonical.product_precommit_bound is not True
            or canonical.execution_consumption_proven is not True
            or canonical.occurrence_ancestry_proven is not True
            or canonical.run_path_ancestry_proven is not True
            or canonical.fixed_n_complete is not True
            or canonical.iid_qualified is not True
            or canonical.risk_scope != "SIMULATOR_DISTRIBUTION_ONLY"
            or canonical.grants_real_money_authority is not False
        ):
            raise ProductFixedNIidCohortError(
                "fixed-N IID cohort truth flags are inconsistent"
            )
        return canonical

    verifier.__name__ = "verify_product_fixed_n_iid_cohort"
    verifier.__qualname__ = "verify_product_fixed_n_iid_cohort"
    return verifier


verify_product_fixed_n_iid_cohort = _build_product_fixed_n_iid_cohort_verifier(
    resolve_product_fixed_n_iid_cohort,
    ProductFixedNIidCohort,
)
del _build_product_fixed_n_iid_cohort_verifier


__all__ = [
    "ProductFixedNIidCohort",
    "ProductFixedNIidCohortError",
    "resolve_product_fixed_n_iid_cohort",
    "verify_product_fixed_n_iid_cohort",
]
