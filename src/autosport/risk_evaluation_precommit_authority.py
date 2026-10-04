from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .risk_sampling_dependence import (
    ResolvedFixedNIidPrecommitAuthority,
    ResolvedFixedNIidSamplingStructure,
    RiskSamplingDependenceError,
    inspect_fixed_n_iid_sampling_structure,
    resolve_fixed_n_iid_precommit_authority,
)
from .risk_sampling_membership import (
    ResolvedFixedNRiskEvaluationSpec,
    ResolvedFixedNRiskMembership,
    RiskSamplingMembershipError,
    inspect_fixed_n_risk_evaluation_spec,
)


_MEMBERSHIP_TYPE = ResolvedFixedNRiskMembership
_SPEC_TYPE = ResolvedFixedNRiskEvaluationSpec
_STRUCTURE_TYPE = ResolvedFixedNIidSamplingStructure
_PRECOMMIT_TYPE = ResolvedFixedNIidPrecommitAuthority
_SPEC_RESOLVER = inspect_fixed_n_risk_evaluation_spec
_SPEC_RESOLVER_CODE = getattr(_SPEC_RESOLVER, "__code__", None)
_STRUCTURE_RESOLVER = inspect_fixed_n_iid_sampling_structure
_STRUCTURE_RESOLVER_CODE = getattr(_STRUCTURE_RESOLVER, "__code__", None)
_PRECOMMIT_RESOLVER = resolve_fixed_n_iid_precommit_authority
_PRECOMMIT_RESOLVER_CODE = getattr(_PRECOMMIT_RESOLVER, "__code__", None)
_HEX = frozenset("0123456789abcdef")


class ProductRiskEvaluationPrecommitError(RuntimeError):
    """Risk-evaluation statistical policy cannot be product-resolved fail-closed."""


def _sha(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(ch not in _HEX for ch in value)
    ):
        raise ProductRiskEvaluationPrecommitError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return value


def _canonical_decimal(value: Decimal, name: str) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ProductRiskEvaluationPrecommitError(
            f"{name} must be a finite exact Decimal"
        )
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if len(text) > 128:
        raise ProductRiskEvaluationPrecommitError(
            f"{name} exceeds supported canonical size"
        )
    return text


def _digest(payload: object) -> str:
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductRiskEvaluationPrecommitError(
            "risk-evaluation precommit is outside canonical JSON"
        ) from exc
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class ProductFixedNRiskEvaluationPrecommitAuthority:
    """Product-owned statistical policy frozen before fixed-N outcomes.

    This authority proves only that the exact statistical policy, capital root,
    stake-policy root, membership and product randomization manifest were bound
    before outcome reveal.  It does not prove completed observations, IID
    occurrence ancestry, an estimated upper bound, proposal-specific candidate
    identity, or any real-money permission.
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
    confidence_level: Decimal
    ruin_threshold: Decimal
    risk_target_scope: str
    initial_capital_state_sha256: str
    stake_policy_sha256: str
    authority_sha256: str

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> "ProductFixedNRiskEvaluationPrecommitAuthority":
        raise TypeError(
            "ProductFixedNRiskEvaluationPrecommitAuthority is product-issued; "
            "use resolve_product_fixed_n_risk_evaluation_precommit"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError(
            "ProductFixedNRiskEvaluationPrecommitAuthority must not be subclassed"
        )

    @property
    def product_preoutcome_chronology_proven(self) -> bool:
        return True

    @property
    def statistical_policy_precommitted(self) -> bool:
        return True

    @property
    def target_execution_proven(self) -> bool:
        return False

    @property
    def iid_qualified(self) -> bool:
        return False

    @property
    def risk_upper_bound_issued(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


_AUTHORITY_TYPE = ProductFixedNRiskEvaluationPrecommitAuthority


def _require_dispatch() -> None:
    if (
        ResolvedFixedNRiskMembership is not _MEMBERSHIP_TYPE
        or ResolvedFixedNRiskEvaluationSpec is not _SPEC_TYPE
        or ResolvedFixedNIidSamplingStructure is not _STRUCTURE_TYPE
        or ResolvedFixedNIidPrecommitAuthority is not _PRECOMMIT_TYPE
        or ProductFixedNRiskEvaluationPrecommitAuthority is not _AUTHORITY_TYPE
        or inspect_fixed_n_risk_evaluation_spec is not _SPEC_RESOLVER
        or getattr(_SPEC_RESOLVER, "__code__", None) is not _SPEC_RESOLVER_CODE
        or inspect_fixed_n_iid_sampling_structure is not _STRUCTURE_RESOLVER
        or getattr(_STRUCTURE_RESOLVER, "__code__", None)
        is not _STRUCTURE_RESOLVER_CODE
        or resolve_fixed_n_iid_precommit_authority is not _PRECOMMIT_RESOLVER
        or getattr(_PRECOMMIT_RESOLVER, "__code__", None)
        is not _PRECOMMIT_RESOLVER_CODE
    ):
        raise ProductRiskEvaluationPrecommitError(
            "risk-evaluation precommit authority dispatch changed"
        )


def resolve_product_fixed_n_risk_evaluation_precommit(
    membership: ResolvedFixedNRiskMembership,
    *,
    registry_path: str | Path,
    workspace: str | Path,
    sampling_manifest_json: str,
    authority_root: str | Path | None = None,
) -> ProductFixedNRiskEvaluationPrecommitAuthority:
    """Compose v2 preregistration with exact product pre-outcome chronology."""

    _require_dispatch()
    if type(membership) is not _MEMBERSHIP_TYPE:
        raise TypeError("membership must be exact ResolvedFixedNRiskMembership")
    try:
        spec = _SPEC_RESOLVER(
            registry_path,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
        )
        structure = _STRUCTURE_RESOLVER(
            membership,
            sampling_manifest_json=sampling_manifest_json,
        )
        precommit = _PRECOMMIT_RESOLVER(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            authority_root=authority_root,
        )
    except (
        RiskSamplingMembershipError,
        RiskSamplingDependenceError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise ProductRiskEvaluationPrecommitError(
            "risk-evaluation product precommit cannot be re-resolved"
        ) from exc
    _require_dispatch()
    if type(spec) is not _SPEC_TYPE:
        raise ProductRiskEvaluationPrecommitError(
            "risk-evaluation specification resolver returned unsupported type"
        )
    if type(structure) is not _STRUCTURE_TYPE:
        raise ProductRiskEvaluationPrecommitError(
            "IID structure resolver returned unsupported type"
        )
    if type(precommit) is not _PRECOMMIT_TYPE:
        raise ProductRiskEvaluationPrecommitError(
            "IID precommit resolver returned unsupported type"
        )
    if (
        precommit.product_membership_preoutcome_chronology_proven is not True
        or precommit.product_randomization_root_issued is not True
        or precommit.iid_qualified is not False
        or precommit.grants_real_money_authority is not False
    ):
        raise ProductRiskEvaluationPrecommitError(
            "IID precommit truth flags are inconsistent"
        )
    if (
        spec.product_preoutcome_chronology_proven is not False
        or spec.iid_qualified is not False
        or spec.grants_real_money_authority is not False
    ):
        raise ProductRiskEvaluationPrecommitError(
            "structural risk specification improperly claims product authority"
        )
    if (
        spec.research_protocol_id != membership.research_protocol_id
        or spec.protocol_sha256 != membership.protocol_sha256
        or spec.design_sha256 != membership.design_sha256
        or spec.dataset_snapshot_id != membership.dataset_snapshot_id
        or spec.dataset_manifest_sha256 != membership.dataset_manifest_sha256
        or spec.planned_run_ids != membership.planned_run_ids
        or structure.research_protocol_id != spec.research_protocol_id
        or structure.protocol_sha256 != spec.protocol_sha256
        or structure.dataset_snapshot_id != spec.dataset_snapshot_id
        or structure.dataset_manifest_sha256 != spec.dataset_manifest_sha256
        or structure.membership_design_sha256 != spec.design_sha256
        or structure.planned_member_ids != spec.planned_run_ids
        or structure.initial_capital_state_sha256
        != spec.initial_capital_state_sha256
        or structure.stake_policy_sha256 != spec.stake_policy_sha256
        or precommit.membership_design_sha256 != spec.design_sha256
        or precommit.sampling_manifest_sha256 != structure.manifest_sha256
        or precommit.planned_member_ids != spec.planned_run_ids
    ):
        raise ProductRiskEvaluationPrecommitError(
            "risk-evaluation preregistration differs from product IID precommit"
        )

    payload = {
        "schema": "AUTOSPORT_PRODUCT_FIXED_N_RISK_EVALUATION_PRECOMMIT_V1",
        "workspace_instance_id": precommit.workspace_instance_id,
        "experiment_id": structure.experiment_id,
        "research_protocol_id": spec.research_protocol_id,
        "protocol_sha256": _sha(spec.protocol_sha256, "protocol_sha256"),
        "dataset_snapshot_id": spec.dataset_snapshot_id,
        "dataset_manifest_sha256": _sha(
            spec.dataset_manifest_sha256,
            "dataset_manifest_sha256",
        ),
        "membership_design_sha256": _sha(
            spec.design_sha256,
            "membership_design_sha256",
        ),
        "sampling_manifest_sha256": _sha(
            structure.manifest_sha256,
            "sampling_manifest_sha256",
        ),
        "planned_member_ids": list(spec.planned_run_ids),
        "confidence_level": _canonical_decimal(
            spec.confidence_level,
            "confidence_level",
        ),
        "ruin_threshold": _canonical_decimal(
            spec.ruin_threshold,
            "ruin_threshold",
        ),
        "risk_target_scope": spec.risk_target_scope,
        "initial_capital_state_sha256": _sha(
            spec.initial_capital_state_sha256,
            "initial_capital_state_sha256",
        ),
        "stake_policy_sha256": _sha(
            spec.stake_policy_sha256,
            "stake_policy_sha256",
        ),
        "product_preoutcome_chronology_proven": True,
        "statistical_policy_precommitted": True,
        "target_execution_proven": False,
        "iid_qualified": False,
        "risk_upper_bound_issued": False,
        "grants_real_money_authority": False,
    }
    result = object.__new__(_AUTHORITY_TYPE)
    for field_name, value in (
        ("workspace_instance_id", precommit.workspace_instance_id),
        ("experiment_id", structure.experiment_id),
        ("research_protocol_id", spec.research_protocol_id),
        ("protocol_sha256", spec.protocol_sha256),
        ("dataset_snapshot_id", spec.dataset_snapshot_id),
        ("dataset_manifest_sha256", spec.dataset_manifest_sha256),
        ("membership_design_sha256", spec.design_sha256),
        ("sampling_manifest_sha256", structure.manifest_sha256),
        ("planned_member_ids", spec.planned_run_ids),
        ("confidence_level", spec.confidence_level),
        ("ruin_threshold", spec.ruin_threshold),
        ("risk_target_scope", spec.risk_target_scope),
        ("initial_capital_state_sha256", spec.initial_capital_state_sha256),
        ("stake_policy_sha256", spec.stake_policy_sha256),
        ("authority_sha256", _digest(payload)),
    ):
        object.__setattr__(result, field_name, value)
    _require_dispatch()
    return result


def _build_verifier(resolver, authority_type):
    module_globals = globals()
    resolver_code = getattr(resolver, "__code__", None)
    field_names = tuple(authority_type.__dataclass_fields__)

    def verifier(
        candidate: ProductFixedNRiskEvaluationPrecommitAuthority,
        membership: ResolvedFixedNRiskMembership,
        *,
        registry_path: str | Path,
        workspace: str | Path,
        sampling_manifest_json: str,
        authority_root: str | Path | None = None,
    ) -> ProductFixedNRiskEvaluationPrecommitAuthority:
        if (
            module_globals.get(
                "resolve_product_fixed_n_risk_evaluation_precommit"
            )
            is not resolver
            or getattr(resolver, "__code__", None) is not resolver_code
            or module_globals.get(
                "ProductFixedNRiskEvaluationPrecommitAuthority"
            )
            is not authority_type
        ):
            raise ProductRiskEvaluationPrecommitError(
                "risk-evaluation precommit verifier dispatch changed"
            )
        if type(candidate) is not authority_type:
            raise TypeError(
                "candidate must be exact ProductFixedNRiskEvaluationPrecommitAuthority"
            )
        canonical = resolver(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            authority_root=authority_root,
        )
        for field_name in field_names:
            supplied = getattr(candidate, field_name)
            expected = getattr(canonical, field_name)
            if type(supplied) is not type(expected) or supplied != expected:
                raise ProductRiskEvaluationPrecommitError(
                    "risk-evaluation precommit does not match canonical durable roots"
                )
        return canonical

    return verifier


verify_product_fixed_n_risk_evaluation_precommit = _build_verifier(
    resolve_product_fixed_n_risk_evaluation_precommit,
    ProductFixedNRiskEvaluationPrecommitAuthority,
)
del _build_verifier


__all__ = [
    "ProductFixedNRiskEvaluationPrecommitAuthority",
    "ProductRiskEvaluationPrecommitError",
    "resolve_product_fixed_n_risk_evaluation_precommit",
    "verify_product_fixed_n_risk_evaluation_precommit",
]
