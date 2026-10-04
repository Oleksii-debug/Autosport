from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .paper_settlement_learning import PaperSettlementLearningBridge
from .risk_evaluation_precommit_authority import (
    ProductFixedNRiskEvaluationPrecommitAuthority,
    ProductRiskEvaluationPrecommitError,
    resolve_product_fixed_n_risk_evaluation_precommit,
)
from .risk_of_ruin_evaluator import (
    RiskOfRuinEvaluationError,
    RiskOfRuinIssuanceError,
    RiskPathObservation,
    clopper_pearson_upper_bound,
    evaluator_source_sha256,
)
from .risk_path_observation_set_authority import (
    ProductFixedNRiskObservationSet,
    ProductFixedNRiskObservationSetError,
    resolve_product_fixed_n_risk_observations,
)
from .risk_sampling_membership import ResolvedFixedNRiskMembership


_PRECOMMIT_TYPE = ProductFixedNRiskEvaluationPrecommitAuthority
_PRECOMMIT_RESOLVER = resolve_product_fixed_n_risk_evaluation_precommit
_PRECOMMIT_RESOLVER_CODE = getattr(_PRECOMMIT_RESOLVER, "__code__", None)
_OBSERVATION_SET_TYPE = ProductFixedNRiskObservationSet
_OBSERVATION_RESOLVER = resolve_product_fixed_n_risk_observations
_OBSERVATION_RESOLVER_CODE = getattr(_OBSERVATION_RESOLVER, "__code__", None)
_MEMBERSHIP_TYPE = ResolvedFixedNRiskMembership
_BRIDGE_TYPE = PaperSettlementLearningBridge
_OBSERVATION_TYPE = RiskPathObservation
_CP = clopper_pearson_upper_bound
_CP_CODE = getattr(_CP, "__code__", None)
_SOURCE_DIGEST = evaluator_source_sha256
_SOURCE_DIGEST_CODE = getattr(_SOURCE_DIGEST, "__code__", None)
_HEX = frozenset("0123456789abcdef")


class ProductFixedNRiskPolicyEstimateError(RuntimeError):
    """Product-owned frozen-policy ruin estimate cannot be re-resolved."""


def _sha(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(ch not in _HEX for ch in value)
    ):
        raise ProductFixedNRiskPolicyEstimateError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return value


def _decimal_text(value: object, name: str) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ProductFixedNRiskPolicyEstimateError(
            f"{name} must be a finite exact Decimal"
        )
    if value.is_zero():
        return "0"
    parts = value.as_tuple()
    exponent = int(parts.exponent)
    digits = len(parts.digits)
    sign = 1 if parts.sign else 0
    if exponent >= 0:
        length = sign + digits + exponent
    elif digits + exponent > 0:
        length = sign + digits + 1
    else:
        length = sign + 2 - exponent
    if length > 256:
        raise ProductFixedNRiskPolicyEstimateError(
            f"{name} exceeds supported canonical size"
        )
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _instant(value: object, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise ProductFixedNRiskPolicyEstimateError(
            f"{name} must be canonical ISO-8601 text"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductFixedNRiskPolicyEstimateError(
            f"{name} must be valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductFixedNRiskPolicyEstimateError(
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
        raise ProductFixedNRiskPolicyEstimateError(
            "risk policy estimate is outside canonical JSON"
        ) from exc


@dataclass(frozen=True, slots=True, init=False)
class ProductFixedNRiskPolicyEstimate:
    """Product-derived ruin bound for the frozen simulator stake policy.

    This result is scientific/PAPER evidence for the exact precommitted policy
    distribution only. It is not proposal-specific candidate/stake evidence and
    cannot authorize a ticket or real-money execution.
    """

    workspace_instance_id: str
    experiment_id: str
    research_protocol_id: str
    protocol_sha256: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    membership_design_sha256: str
    sampling_manifest_sha256: str
    planned_member_ids: tuple[str, ...]
    observation_manifest_sha256: str
    observation_source_evidence_sha256: str
    qualification_sha256: str
    occurrence_root_sha256: str
    confidence_level: Decimal
    ruin_threshold: Decimal
    risk_target_scope: str
    initial_capital_state_sha256: str
    stake_policy_sha256: str
    observed_through: str
    evaluator_source_sha256: str
    independent_units: int
    ruin_count: int
    upper_bound: Decimal
    estimate_sha256: str

    def __new__(cls, *args: object, **kwargs: object) -> "ProductFixedNRiskPolicyEstimate":
        raise TypeError(
            "ProductFixedNRiskPolicyEstimate is product-resolved; "
            "use resolve_product_fixed_n_risk_policy_estimate"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ProductFixedNRiskPolicyEstimate must not be subclassed")

    @property
    def product_preoutcome_policy_proven(self) -> bool:
        return True

    @property
    def frozen_policy_execution_proven(self) -> bool:
        return True

    @property
    def iid_qualified(self) -> bool:
        return True

    @property
    def proposal_target_execution_proven(self) -> bool:
        return False

    @property
    def risk_upper_bound_computed(self) -> bool:
        return True

    @property
    def grants_ticket_authority(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


_ESTIMATE_TYPE = ProductFixedNRiskPolicyEstimate
_SHA_HELPER = _sha
_SHA_HELPER_CODE = getattr(_SHA_HELPER, "__code__", None)
_DECIMAL_TEXT_HELPER = _decimal_text
_DECIMAL_TEXT_HELPER_CODE = getattr(_DECIMAL_TEXT_HELPER, "__code__", None)
_INSTANT_HELPER = _instant
_INSTANT_HELPER_CODE = getattr(_INSTANT_HELPER, "__code__", None)
_CANONICAL_JSON_HELPER = _canonical_json
_CANONICAL_JSON_HELPER_CODE = getattr(_CANONICAL_JSON_HELPER, "__code__", None)


def _require_dispatch() -> None:
    if (
        ProductFixedNRiskEvaluationPrecommitAuthority is not _PRECOMMIT_TYPE
        or resolve_product_fixed_n_risk_evaluation_precommit is not _PRECOMMIT_RESOLVER
        or getattr(_PRECOMMIT_RESOLVER, "__code__", None)
        is not _PRECOMMIT_RESOLVER_CODE
        or ProductFixedNRiskObservationSet is not _OBSERVATION_SET_TYPE
        or resolve_product_fixed_n_risk_observations is not _OBSERVATION_RESOLVER
        or getattr(_OBSERVATION_RESOLVER, "__code__", None)
        is not _OBSERVATION_RESOLVER_CODE
        or ResolvedFixedNRiskMembership is not _MEMBERSHIP_TYPE
        or PaperSettlementLearningBridge is not _BRIDGE_TYPE
        or RiskPathObservation is not _OBSERVATION_TYPE
        or clopper_pearson_upper_bound is not _CP
        or getattr(_CP, "__code__", None) is not _CP_CODE
        or evaluator_source_sha256 is not _SOURCE_DIGEST
        or getattr(_SOURCE_DIGEST, "__code__", None) is not _SOURCE_DIGEST_CODE
        or ProductFixedNRiskPolicyEstimate is not _ESTIMATE_TYPE
        or _sha is not _SHA_HELPER
        or getattr(_SHA_HELPER, "__code__", None) is not _SHA_HELPER_CODE
        or _decimal_text is not _DECIMAL_TEXT_HELPER
        or getattr(_DECIMAL_TEXT_HELPER, "__code__", None)
        is not _DECIMAL_TEXT_HELPER_CODE
        or _instant is not _INSTANT_HELPER
        or getattr(_INSTANT_HELPER, "__code__", None) is not _INSTANT_HELPER_CODE
        or _canonical_json is not _CANONICAL_JSON_HELPER
        or getattr(_CANONICAL_JSON_HELPER, "__code__", None)
        is not _CANONICAL_JSON_HELPER_CODE
        or _derive_policy_estimate_material
        is not _DERIVE_POLICY_ESTIMATE_MATERIAL
        or getattr(_DERIVE_POLICY_ESTIMATE_MATERIAL, "__code__", None)
        is not _DERIVE_POLICY_ESTIMATE_MATERIAL_CODE
    ):
        raise ProductFixedNRiskPolicyEstimateError(
            "risk policy estimate authority dispatch changed"
        )


def _derive_policy_estimate_material(
    precommit: ProductFixedNRiskEvaluationPrecommitAuthority,
    observations: ProductFixedNRiskObservationSet,
) -> dict[str, object]:
    """Derive neutral deterministic material from exact already-resolved authorities."""

    _require_dispatch()
    if type(precommit) is not _PRECOMMIT_TYPE:
        raise TypeError(
            "precommit must be exact ProductFixedNRiskEvaluationPrecommitAuthority"
        )
    if type(observations) is not _OBSERVATION_SET_TYPE:
        raise TypeError("observations must be exact ProductFixedNRiskObservationSet")
    if (
        precommit.product_preoutcome_chronology_proven is not True
        or precommit.statistical_policy_precommitted is not True
        or precommit.target_execution_proven is not False
        or precommit.iid_qualified is not False
        or precommit.risk_upper_bound_issued is not False
        or precommit.grants_real_money_authority is not False
        or observations.fixed_n_complete is not True
        or observations.product_observation_provenance is not True
        or observations.iid_qualified is not True
        or observations.grants_real_money_authority is not False
        or precommit.experiment_id != observations.experiment_id
        or precommit.protocol_sha256 != observations.research_protocol_sha256
        or precommit.dataset_snapshot_id != observations.dataset_snapshot_id
        or precommit.dataset_manifest_sha256 != observations.dataset_manifest_sha256
        or precommit.sampling_manifest_sha256 != observations.sampling_manifest_sha256
        or precommit.initial_capital_state_sha256
        != observations.initial_capital_state_sha256
        or precommit.stake_policy_sha256 != observations.stake_policy_sha256
        or precommit.planned_member_ids != observations.planned_member_ids
        or precommit.risk_target_scope != "FROZEN_STAKE_POLICY"
    ):
        raise ProductFixedNRiskPolicyEstimateError(
            "risk policy precommit and observation authority are inconsistent"
        )

    raw_observations = observations.observations
    if type(raw_observations) is not tuple or len(raw_observations) != len(
        precommit.planned_member_ids
    ):
        raise ProductFixedNRiskPolicyEstimateError(
            "risk observation cohort does not match fixed-N membership"
        )
    by_id: dict[str, RiskPathObservation] = {}
    for item in raw_observations:
        if type(item) is not _OBSERVATION_TYPE:
            raise ProductFixedNRiskPolicyEstimateError(
                "risk observation cohort contains unsupported observation type"
            )
        if item.independent_unit_id in by_id:
            raise ProductFixedNRiskPolicyEstimateError(
                "risk observation cohort contains duplicate member identity"
            )
        by_id[item.independent_unit_id] = item
    if tuple(sorted(by_id)) != tuple(sorted(precommit.planned_member_ids)):
        raise ProductFixedNRiskPolicyEstimateError(
            "risk observation cohort member identity differs from precommit"
        )

    ordered = tuple(by_id[member_id] for member_id in precommit.planned_member_ids)
    available = tuple(
        _instant(item.outcome_available_at, "outcome_available_at")
        for item in ordered
    )
    ruin_count = sum(
        item.minimum_equity <= precommit.ruin_threshold for item in ordered
    )
    try:
        upper_bound = _CP(
            ruin_count=ruin_count,
            independent_units=len(ordered),
            confidence_level=precommit.confidence_level,
        )
        source_sha = _SOURCE_DIGEST()
    except (
        RiskOfRuinEvaluationError,
        RiskOfRuinIssuanceError,
        OSError,
        ArithmeticError,
        ValueError,
    ) as exc:
        raise ProductFixedNRiskPolicyEstimateError(
            "risk policy estimator could not derive canonical result"
        ) from exc
    _require_dispatch()
    source_sha = _sha(source_sha, "evaluator_source_sha256")
    observed_through = max(available).isoformat()

    payload = {
        "schema": "AUTOSPORT_PRODUCT_FIXED_N_RISK_POLICY_ESTIMATE_V1",
        "workspace_instance_id": precommit.workspace_instance_id,
        "experiment_id": precommit.experiment_id,
        "research_protocol_id": precommit.research_protocol_id,
        "protocol_sha256": _sha(precommit.protocol_sha256, "protocol_sha256"),
        "dataset_snapshot_id": precommit.dataset_snapshot_id,
        "dataset_manifest_sha256": _sha(
            precommit.dataset_manifest_sha256,
            "dataset_manifest_sha256",
        ),
        "membership_design_sha256": _sha(
            precommit.membership_design_sha256,
            "membership_design_sha256",
        ),
        "sampling_manifest_sha256": _sha(
            precommit.sampling_manifest_sha256,
            "sampling_manifest_sha256",
        ),
        "planned_member_ids": list(precommit.planned_member_ids),
        "observation_manifest_sha256": _sha(
            observations.observation_manifest_sha256,
            "observation_manifest_sha256",
        ),
        "observation_source_evidence_sha256": _sha(
            observations.source_evidence_sha256,
            "observation_source_evidence_sha256",
        ),
        "qualification_sha256": _sha(
            observations.qualification_sha256,
            "qualification_sha256",
        ),
        "occurrence_root_sha256": _sha(
            observations.occurrence_root_sha256,
            "occurrence_root_sha256",
        ),
        "confidence_level": _decimal_text(
            precommit.confidence_level,
            "confidence_level",
        ),
        "ruin_threshold": _decimal_text(
            precommit.ruin_threshold,
            "ruin_threshold",
        ),
        "risk_target_scope": precommit.risk_target_scope,
        "initial_capital_state_sha256": _sha(
            precommit.initial_capital_state_sha256,
            "initial_capital_state_sha256",
        ),
        "stake_policy_sha256": _sha(
            precommit.stake_policy_sha256,
            "stake_policy_sha256",
        ),
        "observed_through": observed_through,
        "evaluator_source_sha256": source_sha,
        "independent_units": len(ordered),
        "ruin_count": ruin_count,
        "upper_bound": _decimal_text(upper_bound, "upper_bound"),
        "product_preoutcome_policy_proven": True,
        "frozen_policy_execution_proven": True,
        "iid_qualified": True,
        "proposal_target_execution_proven": False,
        "grants_ticket_authority": False,
        "grants_real_money_authority": False,
    }
    material: dict[str, object] = {
        "workspace_instance_id": precommit.workspace_instance_id,
        "experiment_id": precommit.experiment_id,
        "research_protocol_id": precommit.research_protocol_id,
        "protocol_sha256": precommit.protocol_sha256,
        "dataset_snapshot_id": precommit.dataset_snapshot_id,
        "dataset_manifest_sha256": precommit.dataset_manifest_sha256,
        "membership_design_sha256": precommit.membership_design_sha256,
        "sampling_manifest_sha256": precommit.sampling_manifest_sha256,
        "planned_member_ids": precommit.planned_member_ids,
        "observation_manifest_sha256": observations.observation_manifest_sha256,
        "observation_source_evidence_sha256": observations.source_evidence_sha256,
        "qualification_sha256": observations.qualification_sha256,
        "occurrence_root_sha256": observations.occurrence_root_sha256,
        "confidence_level": precommit.confidence_level,
        "ruin_threshold": precommit.ruin_threshold,
        "risk_target_scope": precommit.risk_target_scope,
        "initial_capital_state_sha256": precommit.initial_capital_state_sha256,
        "stake_policy_sha256": precommit.stake_policy_sha256,
        "observed_through": observed_through,
        "evaluator_source_sha256": source_sha,
        "independent_units": len(ordered),
        "ruin_count": ruin_count,
        "upper_bound": upper_bound,
        "estimate_sha256": hashlib.sha256(_canonical_json(payload)).hexdigest(),
    }
    _require_dispatch()
    return material


_DERIVE_POLICY_ESTIMATE_MATERIAL = _derive_policy_estimate_material
_DERIVE_POLICY_ESTIMATE_MATERIAL_CODE = getattr(
    _DERIVE_POLICY_ESTIMATE_MATERIAL,
    "__code__",
    None,
)


def resolve_product_fixed_n_risk_policy_estimate(
    membership: ResolvedFixedNRiskMembership,
    *,
    registry_path: str | Path,
    workspace: str | Path,
    sampling_manifest_json: str,
    sampling_frame_json: str,
    horizon_json: str,
    settlement_bridges: tuple[PaperSettlementLearningBridge, ...],
    authority_root: str | Path | None = None,
) -> ProductFixedNRiskPolicyEstimate:
    """Re-resolve precommit + IID observations and derive frozen-policy ruin bound."""

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
        precommit = _PRECOMMIT_RESOLVER(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            authority_root=authority_root,
        )
        observation_set = _OBSERVATION_RESOLVER(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            sampling_frame_json=sampling_frame_json,
            horizon_json=horizon_json,
            settlement_bridges=settlement_bridges,
            authority_root=authority_root,
        )
    except (
        ProductRiskEvaluationPrecommitError,
        ProductFixedNRiskObservationSetError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise ProductFixedNRiskPolicyEstimateError(
            "product risk policy authorities cannot be re-resolved"
        ) from exc
    _require_dispatch()
    material = _DERIVE_POLICY_ESTIMATE_MATERIAL(precommit, observation_set)
    _require_dispatch()
    result = object.__new__(_ESTIMATE_TYPE)
    for field_name in _ESTIMATE_FIELDS:
        object.__setattr__(result, field_name, material[field_name])
    _require_dispatch()
    return result


_ESTIMATE_FIELDS = (
    "workspace_instance_id",
    "experiment_id",
    "research_protocol_id",
    "protocol_sha256",
    "dataset_snapshot_id",
    "dataset_manifest_sha256",
    "membership_design_sha256",
    "sampling_manifest_sha256",
    "planned_member_ids",
    "observation_manifest_sha256",
    "observation_source_evidence_sha256",
    "qualification_sha256",
    "occurrence_root_sha256",
    "confidence_level",
    "ruin_threshold",
    "risk_target_scope",
    "initial_capital_state_sha256",
    "stake_policy_sha256",
    "observed_through",
    "evaluator_source_sha256",
    "independent_units",
    "ruin_count",
    "upper_bound",
    "estimate_sha256",
)


def _build_verifier(resolver, estimate_type):
    module_globals = globals()
    resolver_code = getattr(resolver, "__code__", None)
    if resolver_code is None:
        raise RuntimeError("risk policy estimate resolver is unavailable")

    def verifier(
        candidate: ProductFixedNRiskPolicyEstimate,
        membership: ResolvedFixedNRiskMembership,
        *,
        registry_path: str | Path,
        workspace: str | Path,
        sampling_manifest_json: str,
        sampling_frame_json: str,
        horizon_json: str,
        settlement_bridges: tuple[PaperSettlementLearningBridge, ...],
        authority_root: str | Path | None = None,
    ) -> ProductFixedNRiskPolicyEstimate:
        def require_verifier_dispatch() -> None:
            if (
                module_globals.get("resolve_product_fixed_n_risk_policy_estimate")
                is not resolver
                or getattr(resolver, "__code__", None) is not resolver_code
                or module_globals.get("ProductFixedNRiskPolicyEstimate")
                is not estimate_type
            ):
                raise ProductFixedNRiskPolicyEstimateError(
                    "risk policy estimate verifier dispatch changed"
                )

        require_verifier_dispatch()
        _require_dispatch()
        if type(candidate) is not estimate_type:
            raise TypeError("candidate must be exact ProductFixedNRiskPolicyEstimate")
        canonical = resolver(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            sampling_frame_json=sampling_frame_json,
            horizon_json=horizon_json,
            settlement_bridges=settlement_bridges,
            authority_root=authority_root,
        )
        require_verifier_dispatch()
        _require_dispatch()
        if type(canonical) is not estimate_type:
            raise ProductFixedNRiskPolicyEstimateError(
                "risk policy estimate resolver returned invalid result type"
            )
        try:
            differs = any(
                type(object.__getattribute__(candidate, field_name))
                is not type(object.__getattribute__(canonical, field_name))
                or object.__getattribute__(candidate, field_name)
                != object.__getattribute__(canonical, field_name)
                for field_name in _ESTIMATE_FIELDS
            )
        except AttributeError as exc:
            raise ProductFixedNRiskPolicyEstimateError(
                "risk policy estimate differs from canonical durable roots"
            ) from exc
        if differs:
            raise ProductFixedNRiskPolicyEstimateError(
                "risk policy estimate differs from canonical durable roots"
            )
        return canonical

    return verifier


verify_product_fixed_n_risk_policy_estimate = _build_verifier(
    resolve_product_fixed_n_risk_policy_estimate,
    ProductFixedNRiskPolicyEstimate,
)
del _build_verifier


__all__ = [
    "ProductFixedNRiskPolicyEstimate",
    "ProductFixedNRiskPolicyEstimateError",
    "resolve_product_fixed_n_risk_policy_estimate",
    "verify_product_fixed_n_risk_policy_estimate",
]
