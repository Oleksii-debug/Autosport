from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .paper_settlement_learning import PaperSettlementLearningBridge
from .risk_iid_qualification_authority import (
    ProductFixedNIidQualificationAuthority,
    ProductFixedNIidQualificationError,
    resolve_product_fixed_n_iid_qualification,
)
from .risk_of_ruin_evaluator import RiskPathObservation
from .risk_path_observation_authority import (
    ProductRunCapitalPathError,
    ProductRunCapitalPathEvidence,
    resolve_product_run_capital_path_evidence,
)
from .risk_sampling_dependence import (
    ResolvedFixedNIidSamplingStructure,
    RiskSamplingDependenceError,
    inspect_fixed_n_iid_sampling_structure,
)
from .risk_sampling_membership import ResolvedFixedNRiskMembership


_SCHEMA = "AUTOSPORT_PRODUCT_FIXED_N_RISK_OBSERVATION_SET_V1"
_HEX = frozenset("0123456789abcdef")

_MEMBERSHIP_TYPE = ResolvedFixedNRiskMembership
_STRUCTURE_TYPE = ResolvedFixedNIidSamplingStructure
_STRUCTURE = inspect_fixed_n_iid_sampling_structure
_STRUCTURE_CODE = getattr(_STRUCTURE, "__code__", None)
_QUALIFICATION_TYPE = ProductFixedNIidQualificationAuthority
_QUALIFICATION_RESOLVER = resolve_product_fixed_n_iid_qualification
_QUALIFICATION_RESOLVER_CODE = getattr(
    _QUALIFICATION_RESOLVER,
    "__code__",
    None,
)
_PATH_TYPE = ProductRunCapitalPathEvidence
_PATH_RESOLVER = resolve_product_run_capital_path_evidence
_PATH_RESOLVER_CODE = getattr(_PATH_RESOLVER, "__code__", None)
_OBSERVATION_TYPE = RiskPathObservation
_OBSERVATION_INIT = RiskPathObservation.__init__
_OBSERVATION_INIT_CODE = getattr(_OBSERVATION_INIT, "__code__", None)
_OBSERVATION_POST_INIT = RiskPathObservation.__post_init__
_OBSERVATION_POST_INIT_CODE = getattr(_OBSERVATION_POST_INIT, "__code__", None)
_OBSERVATION_CANONICAL = RiskPathObservation.canonical_payload
_OBSERVATION_CANONICAL_CODE = getattr(_OBSERVATION_CANONICAL, "__code__", None)
_BRIDGE_TYPE = PaperSettlementLearningBridge


class ProductFixedNRiskObservationSetError(RuntimeError):
    """Qualified fixed-N risk observations cannot be product-resolved."""


@dataclass(frozen=True, slots=True, init=False)
class ProductFixedNRiskObservationSet:
    """Product-owned fixed-N observation cohort for risk-of-ruin evaluation."""

    experiment_id: str
    planned_member_ids: tuple[str, ...]
    qualification_sha256: str
    occurrence_root_sha256: str
    observations: tuple[RiskPathObservation, ...]
    observation_manifest_sha256: str
    source_evidence_sha256: str

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> "ProductFixedNRiskObservationSet":
        raise TypeError(
            "ProductFixedNRiskObservationSet is product-resolved; "
            "use resolve_product_fixed_n_risk_observations"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ProductFixedNRiskObservationSet must not be subclassed")

    @property
    def fixed_n_complete(self) -> bool:
        return True

    @property
    def product_observation_provenance(self) -> bool:
        return True

    @property
    def iid_qualified(self) -> bool:
        return True

    @property
    def grants_real_money_authority(self) -> bool:
        return False


_SET_TYPE = ProductFixedNRiskObservationSet


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
        raise ProductFixedNRiskObservationSetError(
            "risk observation cohort is outside canonical JSON"
        ) from exc


def _sha(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(ch not in _HEX for ch in value)
    ):
        raise ProductFixedNRiskObservationSetError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return value


def _require_dispatch() -> None:
    if (
        ResolvedFixedNRiskMembership is not _MEMBERSHIP_TYPE
        or ResolvedFixedNIidSamplingStructure is not _STRUCTURE_TYPE
        or inspect_fixed_n_iid_sampling_structure is not _STRUCTURE
        or getattr(_STRUCTURE, "__code__", None) is not _STRUCTURE_CODE
        or ProductFixedNIidQualificationAuthority is not _QUALIFICATION_TYPE
        or resolve_product_fixed_n_iid_qualification
        is not _QUALIFICATION_RESOLVER
        or getattr(_QUALIFICATION_RESOLVER, "__code__", None)
        is not _QUALIFICATION_RESOLVER_CODE
        or ProductRunCapitalPathEvidence is not _PATH_TYPE
        or resolve_product_run_capital_path_evidence is not _PATH_RESOLVER
        or getattr(_PATH_RESOLVER, "__code__", None) is not _PATH_RESOLVER_CODE
        or RiskPathObservation is not _OBSERVATION_TYPE
        or _OBSERVATION_TYPE.__init__ is not _OBSERVATION_INIT
        or getattr(_OBSERVATION_INIT, "__code__", None)
        is not _OBSERVATION_INIT_CODE
        or _OBSERVATION_TYPE.__post_init__ is not _OBSERVATION_POST_INIT
        or getattr(_OBSERVATION_POST_INIT, "__code__", None)
        is not _OBSERVATION_POST_INIT_CODE
        or _OBSERVATION_TYPE.canonical_payload is not _OBSERVATION_CANONICAL
        or getattr(_OBSERVATION_CANONICAL, "__code__", None)
        is not _OBSERVATION_CANONICAL_CODE
        or PaperSettlementLearningBridge is not _BRIDGE_TYPE
        or ProductFixedNRiskObservationSet is not _SET_TYPE
    ):
        raise ProductFixedNRiskObservationSetError(
            "risk observation cohort authority dispatch changed"
        )


def resolve_product_fixed_n_risk_observations(
    membership: ResolvedFixedNRiskMembership,
    *,
    registry_path: str | Path,
    workspace: str | Path,
    sampling_manifest_json: str,
    sampling_frame_json: str,
    horizon_json: str,
    settlement_bridges: tuple[PaperSettlementLearningBridge, ...],
    authority_root: str | Path | None = None,
) -> ProductFixedNRiskObservationSet:
    """Project a fully qualified IID experiment into exact evaluator observations."""

    _require_dispatch()
    if type(membership) is not _MEMBERSHIP_TYPE:
        raise TypeError(
            "membership must be an exact ResolvedFixedNRiskMembership"
        )
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
        qualification = _QUALIFICATION_RESOLVER(
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
        ProductFixedNIidQualificationError,
        RiskSamplingDependenceError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise ProductFixedNRiskObservationSetError(
            "qualified fixed-N experiment cannot be re-resolved"
        ) from exc
    _require_dispatch()
    if (
        type(structure) is not _STRUCTURE_TYPE
        or type(qualification) is not _QUALIFICATION_TYPE
        or qualification.fixed_n_complete is not True
        or qualification.occurrence_ancestry_proven is not True
        or qualification.iid_qualified is not True
        or qualification.grants_real_money_authority is not False
        or qualification.planned_member_ids != structure.planned_member_ids
        or qualification.sampling_manifest_sha256 != structure.manifest_sha256
        or len(settlement_bridges) != structure.planned_n
    ):
        raise ProductFixedNRiskObservationSetError(
            "qualified fixed-N authority graph is inconsistent"
        )

    observations: list[RiskPathObservation] = []
    path_roots: list[str] = []
    seen_sources: set[str] = set()
    for member_index, member_id in enumerate(structure.planned_member_ids):
        try:
            path = _PATH_RESOLVER(
                workspace=workspace,
                run_id=member_id,
                member_index=member_index,
                membership=membership,
                registry_path=registry_path,
                sampling_manifest_json=sampling_manifest_json,
                sampling_frame_json=sampling_frame_json,
                horizon_json=horizon_json,
                settlement_bridge=settlement_bridges[member_index],
                authority_root=authority_root,
            )
        except (
            ProductRunCapitalPathError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            raise ProductFixedNRiskObservationSetError(
                f"fixed-N member {member_index} path evidence cannot be re-resolved"
            ) from exc
        _require_dispatch()
        if (
            type(path) is not _PATH_TYPE
            or path.member_id != member_id
            or path.member_index != member_index
            or path.complete is not True
            or path.execution_consumption_proven is not True
            or path.sampling_occurrence_ancestry_proven is not True
            or path.iid_qualified is not False
            or path.grants_real_money_authority is not False
            or path.executed_initial_capital_state_sha256
            != qualification.initial_capital_state_sha256
            or path.executed_stake_policy_sha256
            != qualification.stake_policy_sha256
        ):
            raise ProductFixedNRiskObservationSetError(
                f"fixed-N member {member_index} path/qualification identity mismatch"
            )
        source_evidence_sha256 = _sha(
            path.source_evidence_sha256,
            "path source evidence sha256",
        )
        if source_evidence_sha256 in seen_sources:
            raise ProductFixedNRiskObservationSetError(
                "fixed-N member source evidence identities must be unique"
            )
        seen_sources.add(source_evidence_sha256)
        try:
            observation = _OBSERVATION_TYPE(
                independent_unit_id=member_id,
                dependence_group_id=(
                    f"iid-stream:{structure.member_stream_sha256[member_index]}"
                ),
                minimum_equity=path.minimum_equity,
                outcome_available_at=path.outcome_available_at,
                source_evidence_sha256=source_evidence_sha256,
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise ProductFixedNRiskObservationSetError(
                f"fixed-N member {member_index} risk observation is invalid"
            ) from exc
        observations.append(observation)
        path_roots.append(source_evidence_sha256)

    if len(observations) != structure.planned_n:
        raise ProductFixedNRiskObservationSetError(
            "all frozen fixed-N members must produce risk observations"
        )
    canonical_observations = [
        _OBSERVATION_CANONICAL(observation)
        for observation in sorted(
            observations,
            key=lambda item: item.independent_unit_id,
        )
    ]
    observation_manifest_sha256 = hashlib.sha256(
        _canonical_json({"observations": canonical_observations})
    ).hexdigest()
    payload = {
        "schema": _SCHEMA,
        "experiment_id": structure.experiment_id,
        "planned_member_ids": list(structure.planned_member_ids),
        "qualification_sha256": qualification.qualification_sha256,
        "occurrence_root_sha256": qualification.occurrence_root_sha256,
        "observation_manifest_sha256": observation_manifest_sha256,
        "path_source_evidence_sha256": path_roots,
        "observations": canonical_observations,
        "iid_qualified": True,
        "grants_real_money_authority": False,
    }
    result = object.__new__(_SET_TYPE)
    for field_name, value in (
        ("experiment_id", structure.experiment_id),
        ("planned_member_ids", structure.planned_member_ids),
        (
            "qualification_sha256",
            _sha(qualification.qualification_sha256, "qualification_sha256"),
        ),
        (
            "occurrence_root_sha256",
            _sha(qualification.occurrence_root_sha256, "occurrence_root_sha256"),
        ),
        ("observations", tuple(observations)),
        (
            "observation_manifest_sha256",
            observation_manifest_sha256,
        ),
        (
            "source_evidence_sha256",
            hashlib.sha256(_canonical_json(payload)).hexdigest(),
        ),
    ):
        object.__setattr__(result, field_name, value)
    _require_dispatch()
    return result


_OBSERVATION_SET_FIELDS = (
    "experiment_id",
    "planned_member_ids",
    "qualification_sha256",
    "occurrence_root_sha256",
    "observation_manifest_sha256",
    "source_evidence_sha256",
)
_OBSERVATION_FIELDS = (
    "independent_unit_id",
    "dependence_group_id",
    "minimum_equity",
    "outcome_available_at",
    "source_evidence_sha256",
)


def _build_observation_set_verifier(
    resolver,
    set_type: type[ProductFixedNRiskObservationSet],
):
    module_globals = globals()
    resolver_code = getattr(resolver, "__code__", None)
    if resolver_code is None:
        raise RuntimeError("risk observation cohort resolver is unavailable")
    fields = _OBSERVATION_SET_FIELDS

    def verifier(
        candidate: ProductFixedNRiskObservationSet,
        *,
        membership: ResolvedFixedNRiskMembership,
        registry_path: str | Path,
        workspace: str | Path,
        sampling_manifest_json: str,
        sampling_frame_json: str,
        horizon_json: str,
        settlement_bridges: tuple[PaperSettlementLearningBridge, ...],
        authority_root: str | Path | None = None,
    ) -> ProductFixedNRiskObservationSet:
        def require_verifier_dispatch() -> None:
            if (
                module_globals.get("resolve_product_fixed_n_risk_observations")
                is not resolver
                or getattr(resolver, "__code__", None) is not resolver_code
                or module_globals.get("ProductFixedNRiskObservationSet")
                is not set_type
            ):
                raise ProductFixedNRiskObservationSetError(
                    "risk observation cohort verifier authority dispatch changed"
                )

        require_verifier_dispatch()
        _require_dispatch()
        if type(candidate) is not set_type:
            raise TypeError(
                "candidate must be an exact ProductFixedNRiskObservationSet"
            )
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
        if type(canonical) is not set_type:
            raise ProductFixedNRiskObservationSetError(
                "risk observation cohort resolver returned invalid type"
            )
        try:
            differs = any(
                object.__getattribute__(candidate, field_name)
                != object.__getattribute__(canonical, field_name)
                for field_name in fields
            )
            candidate_observations = object.__getattribute__(
                candidate,
                "observations",
            )
            canonical_observations = object.__getattribute__(
                canonical,
                "observations",
            )
            if (
                type(candidate_observations) is not tuple
                or type(canonical_observations) is not tuple
                or len(candidate_observations) != len(canonical_observations)
            ):
                differs = True
            else:
                for candidate_observation, canonical_observation in zip(
                    candidate_observations,
                    canonical_observations,
                    strict=True,
                ):
                    if (
                        type(candidate_observation) is not _OBSERVATION_TYPE
                        or type(canonical_observation) is not _OBSERVATION_TYPE
                        or any(
                            object.__getattribute__(
                                candidate_observation,
                                field_name,
                            )
                            != object.__getattribute__(
                                canonical_observation,
                                field_name,
                            )
                            for field_name in _OBSERVATION_FIELDS
                        )
                    ):
                        differs = True
                        break
        except (AttributeError, TypeError) as exc:
            raise ProductFixedNRiskObservationSetError(
                "risk observation cohort differs from canonical durable evidence"
            ) from exc
        if differs:
            raise ProductFixedNRiskObservationSetError(
                "risk observation cohort differs from canonical durable evidence"
            )
        require_verifier_dispatch()
        _require_dispatch()
        return canonical

    verifier.__name__ = "verify_product_fixed_n_risk_observations"
    verifier.__qualname__ = "verify_product_fixed_n_risk_observations"
    return verifier


verify_product_fixed_n_risk_observations = _build_observation_set_verifier(
    resolve_product_fixed_n_risk_observations,
    ProductFixedNRiskObservationSet,
)
del _build_observation_set_verifier


__all__ = [
    "ProductFixedNRiskObservationSet",
    "ProductFixedNRiskObservationSetError",
    "resolve_product_fixed_n_risk_observations",
    "verify_product_fixed_n_risk_observations",
]
