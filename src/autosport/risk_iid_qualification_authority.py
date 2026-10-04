from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .paper_settlement_learning import PaperSettlementLearningBridge
from .risk_path_observation_authority import (
    ProductRunCapitalPathError,
    ProductRunCapitalPathEvidence,
    resolve_product_run_capital_path_evidence,
)
from .risk_sampling_dependence import (
    IidSamplingOccurrence,
    ResolvedFixedNIidOccurrenceSet,
    ResolvedFixedNIidSamplingStructure,
    RiskSamplingDependenceError,
    inspect_fixed_n_iid_occurrences,
    inspect_fixed_n_iid_sampling_structure,
)
from .risk_sampling_membership import ResolvedFixedNRiskMembership
from .risk_sampling_occurrence_authority import (
    ProductIidDrawPlanError,
    ProductIidRunExecutionReceipt,
    resolve_product_iid_run_execution,
)


_SCHEMA = "AUTOSPORT_PRODUCT_FIXED_N_IID_QUALIFICATION_V1"
_HEX = frozenset("0123456789abcdef")

_MEMBERSHIP_TYPE = ResolvedFixedNRiskMembership
_STRUCTURE_TYPE = ResolvedFixedNIidSamplingStructure
_STRUCTURE = inspect_fixed_n_iid_sampling_structure
_STRUCTURE_CODE = getattr(_STRUCTURE, "__code__", None)
_OCCURRENCE_TYPE = IidSamplingOccurrence
_OCCURRENCE_SET_TYPE = ResolvedFixedNIidOccurrenceSet
_OCCURRENCE_INSPECTOR = inspect_fixed_n_iid_occurrences
_OCCURRENCE_INSPECTOR_CODE = getattr(_OCCURRENCE_INSPECTOR, "__code__", None)
_EXECUTION_TYPE = ProductIidRunExecutionReceipt
_EXECUTION_RESOLVER = resolve_product_iid_run_execution
_EXECUTION_RESOLVER_CODE = getattr(_EXECUTION_RESOLVER, "__code__", None)
_PATH_TYPE = ProductRunCapitalPathEvidence
_PATH_RESOLVER = resolve_product_run_capital_path_evidence
_PATH_RESOLVER_CODE = getattr(_PATH_RESOLVER, "__code__", None)
_BRIDGE_TYPE = PaperSettlementLearningBridge


class ProductFixedNIidQualificationError(RuntimeError):
    """Fixed-N IID qualification cannot be re-resolved from product authorities."""


@dataclass(frozen=True, slots=True, init=False)
class ProductFixedNIidQualificationAuthority:
    """Whole-experiment IID authority after every frozen member completes.

    Qualification requires product-issued randomization/execution ancestry plus
    exact runtime capital and stake-policy identities for every member. It remains
    research/PAPER evidence and never grants real-money execution authority.
    """

    experiment_id: str
    planned_member_ids: tuple[str, ...]
    member_execution_receipt_sha256: tuple[str, ...]
    member_path_evidence_sha256: tuple[str, ...]
    occurrence_root_sha256: str
    sampling_manifest_sha256: str
    initial_capital_state_sha256: str
    stake_policy_sha256: str
    qualification_sha256: str

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> "ProductFixedNIidQualificationAuthority":
        raise TypeError(
            "ProductFixedNIidQualificationAuthority is product-resolved; "
            "use resolve_product_fixed_n_iid_qualification"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError(
            "ProductFixedNIidQualificationAuthority must not be subclassed"
        )

    @property
    def product_precommit_bound(self) -> bool:
        return True

    @property
    def fixed_n_complete(self) -> bool:
        return True

    @property
    def occurrence_ancestry_proven(self) -> bool:
        return True

    @property
    def initial_capital_state_bound(self) -> bool:
        return True

    @property
    def stake_policy_bound(self) -> bool:
        return True

    @property
    def iid_qualified(self) -> bool:
        return True

    @property
    def grants_real_money_authority(self) -> bool:
        return False


_AUTHORITY_TYPE = ProductFixedNIidQualificationAuthority


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
        raise ProductFixedNIidQualificationError(
            "fixed-N IID qualification evidence is outside canonical JSON"
        ) from exc


def _sha(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(ch not in _HEX for ch in value)
    ):
        raise ProductFixedNIidQualificationError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return value


def _require_dispatch() -> None:
    if (
        ResolvedFixedNRiskMembership is not _MEMBERSHIP_TYPE
        or ResolvedFixedNIidSamplingStructure is not _STRUCTURE_TYPE
        or inspect_fixed_n_iid_sampling_structure is not _STRUCTURE
        or getattr(_STRUCTURE, "__code__", None) is not _STRUCTURE_CODE
        or IidSamplingOccurrence is not _OCCURRENCE_TYPE
        or ResolvedFixedNIidOccurrenceSet is not _OCCURRENCE_SET_TYPE
        or inspect_fixed_n_iid_occurrences is not _OCCURRENCE_INSPECTOR
        or getattr(_OCCURRENCE_INSPECTOR, "__code__", None)
        is not _OCCURRENCE_INSPECTOR_CODE
        or ProductIidRunExecutionReceipt is not _EXECUTION_TYPE
        or resolve_product_iid_run_execution is not _EXECUTION_RESOLVER
        or getattr(_EXECUTION_RESOLVER, "__code__", None)
        is not _EXECUTION_RESOLVER_CODE
        or ProductRunCapitalPathEvidence is not _PATH_TYPE
        or resolve_product_run_capital_path_evidence is not _PATH_RESOLVER
        or getattr(_PATH_RESOLVER, "__code__", None) is not _PATH_RESOLVER_CODE
        or PaperSettlementLearningBridge is not _BRIDGE_TYPE
        or ProductFixedNIidQualificationAuthority is not _AUTHORITY_TYPE
    ):
        raise ProductFixedNIidQualificationError(
            "fixed-N IID qualification authority dispatch changed"
        )


def resolve_product_fixed_n_iid_qualification(
    membership: ResolvedFixedNRiskMembership,
    *,
    registry_path: str | Path,
    workspace: str | Path,
    sampling_manifest_json: str,
    sampling_frame_json: str,
    horizon_json: str,
    settlement_bridges: tuple[PaperSettlementLearningBridge, ...],
    authority_root: str | Path | None = None,
) -> ProductFixedNIidQualificationAuthority:
    """Re-resolve all frozen members and promote only a complete product-owned set."""

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
    except (RiskSamplingDependenceError, OSError, ValueError) as exc:
        raise ProductFixedNIidQualificationError(
            "fixed-N IID sampling structure cannot be re-resolved"
        ) from exc
    _require_dispatch()
    if (
        type(structure) is not _STRUCTURE_TYPE
        or len(settlement_bridges) != structure.planned_n
    ):
        raise ProductFixedNIidQualificationError(
            "all frozen fixed-N members require one exact settlement bridge"
        )

    occurrences: list[IidSamplingOccurrence] = []
    execution_receipts: list[str] = []
    path_evidence: list[str] = []
    for member_index, member_id in enumerate(structure.planned_member_ids):
        try:
            execution = _EXECUTION_RESOLVER(
                membership,
                registry_path=registry_path,
                workspace=workspace,
                sampling_manifest_json=sampling_manifest_json,
                sampling_frame_json=sampling_frame_json,
                horizon_json=horizon_json,
                member_index=member_index,
                authority_root=authority_root,
            )
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
            ProductIidDrawPlanError,
            ProductRunCapitalPathError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            raise ProductFixedNIidQualificationError(
                f"fixed-N member {member_index} product evidence cannot be re-resolved"
            ) from exc
        _require_dispatch()

        if (
            type(execution) is not _EXECUTION_TYPE
            or type(path) is not _PATH_TYPE
            or execution.member_id != member_id
            or execution.member_index != member_index
            or execution.execution_consumption_proven is not True
            or execution.occurrence_ancestry_proven is not True
            or execution.iid_qualified is not False
            or execution.grants_real_money_authority is not False
            or path.member_id != member_id
            or path.member_index != member_index
            or path.execution_consumption_proven is not True
            or path.sampling_occurrence_ancestry_proven is not True
            or path.iid_qualified is not False
            or path.grants_real_money_authority is not False
            or path.run_execution_receipt_sha256 != execution.receipt_sha256
            or path.expected_draw_transcript_sha256
            != execution.expected_draw_transcript_sha256
        ):
            raise ProductFixedNIidQualificationError(
                f"fixed-N member {member_index} authority graph is inconsistent"
            )
        if (
            path.executed_initial_capital_state_sha256
            != structure.initial_capital_state_sha256
        ):
            raise ProductFixedNIidQualificationError(
                f"fixed-N member {member_index} initial capital state "
                "differs from the frozen IID design"
            )
        if path.executed_stake_policy_sha256 != structure.stake_policy_sha256:
            raise ProductFixedNIidQualificationError(
                f"fixed-N member {member_index} stake policy "
                "differs from the frozen IID design"
            )

        occurrence = _OCCURRENCE_TYPE(
            member_id=member_id,
            member_index=member_index,
            stream_sha256=structure.member_stream_sha256[member_index],
            draw_transcript_sha256=execution.expected_draw_transcript_sha256,
            initial_capital_state_sha256=path.executed_initial_capital_state_sha256,
            sampling_frame_sha256=structure.sampling_frame_sha256,
            protocol_sha256=structure.protocol_sha256,
            stake_policy_sha256=path.executed_stake_policy_sha256,
            horizon_sha256=structure.horizon_sha256,
            complete=True,
        )
        occurrences.append(occurrence)
        execution_receipts.append(
            _sha(execution.receipt_sha256, "execution receipt sha256")
        )
        path_evidence.append(
            _sha(path.source_evidence_sha256, "path evidence sha256")
        )

    try:
        occurrence_set = _OCCURRENCE_INSPECTOR(
            structure,
            tuple(occurrences),
        )
    except (RiskSamplingDependenceError, OSError, ValueError) as exc:
        raise ProductFixedNIidQualificationError(
            "product-owned fixed-N occurrence set failed structural validation"
        ) from exc
    _require_dispatch()
    if (
        type(occurrence_set) is not _OCCURRENCE_SET_TYPE
        or occurrence_set.complete is not True
        or occurrence_set.planned_member_ids != structure.planned_member_ids
        or occurrence_set.experiment_id != structure.experiment_id
        or occurrence_set.manifest_sha256 != structure.manifest_sha256
    ):
        raise ProductFixedNIidQualificationError(
            "fixed-N occurrence set differs from frozen design"
        )

    payload = {
        "schema": _SCHEMA,
        "experiment_id": structure.experiment_id,
        "planned_member_ids": list(structure.planned_member_ids),
        "member_execution_receipt_sha256": execution_receipts,
        "member_path_evidence_sha256": path_evidence,
        "occurrence_root_sha256": occurrence_set.occurrence_root_sha256,
        "sampling_manifest_sha256": structure.manifest_sha256,
        "initial_capital_state_sha256": structure.initial_capital_state_sha256,
        "stake_policy_sha256": structure.stake_policy_sha256,
        "fixed_n_complete": True,
        "occurrence_ancestry_proven": True,
        "iid_qualified": True,
        "grants_real_money_authority": False,
    }
    result = object.__new__(_AUTHORITY_TYPE)
    for field_name, value in (
        ("experiment_id", structure.experiment_id),
        ("planned_member_ids", structure.planned_member_ids),
        (
            "member_execution_receipt_sha256",
            tuple(execution_receipts),
        ),
        ("member_path_evidence_sha256", tuple(path_evidence)),
        (
            "occurrence_root_sha256",
            _sha(
                occurrence_set.occurrence_root_sha256,
                "occurrence_root_sha256",
            ),
        ),
        (
            "sampling_manifest_sha256",
            _sha(structure.manifest_sha256, "sampling_manifest_sha256"),
        ),
        (
            "initial_capital_state_sha256",
            _sha(
                structure.initial_capital_state_sha256,
                "initial_capital_state_sha256",
            ),
        ),
        (
            "stake_policy_sha256",
            _sha(structure.stake_policy_sha256, "stake_policy_sha256"),
        ),
        (
            "qualification_sha256",
            hashlib.sha256(_canonical_json(payload)).hexdigest(),
        ),
    ):
        object.__setattr__(result, field_name, value)
    _require_dispatch()
    return result


_QUALIFICATION_FIELDS = (
    "experiment_id",
    "planned_member_ids",
    "member_execution_receipt_sha256",
    "member_path_evidence_sha256",
    "occurrence_root_sha256",
    "sampling_manifest_sha256",
    "initial_capital_state_sha256",
    "stake_policy_sha256",
    "qualification_sha256",
)


def _build_qualification_verifier(
    resolver,
    authority_type: type[ProductFixedNIidQualificationAuthority],
):
    """Freeze verifier dispatch and compare immutable fields without dataclass __eq__."""

    module_globals = globals()
    resolver_code = getattr(resolver, "__code__", None)
    if resolver_code is None:
        raise RuntimeError("fixed-N IID qualification resolver is unavailable")
    fields = _QUALIFICATION_FIELDS

    def verifier(
        candidate: ProductFixedNIidQualificationAuthority,
        *,
        membership: ResolvedFixedNRiskMembership,
        registry_path: str | Path,
        workspace: str | Path,
        sampling_manifest_json: str,
        sampling_frame_json: str,
        horizon_json: str,
        settlement_bridges: tuple[PaperSettlementLearningBridge, ...],
        authority_root: str | Path | None = None,
    ) -> ProductFixedNIidQualificationAuthority:
        def require_verifier_dispatch() -> None:
            if (
                module_globals.get("resolve_product_fixed_n_iid_qualification")
                is not resolver
                or getattr(resolver, "__code__", None) is not resolver_code
                or module_globals.get("ProductFixedNIidQualificationAuthority")
                is not authority_type
            ):
                raise ProductFixedNIidQualificationError(
                    "fixed-N IID qualification verifier authority dispatch changed"
                )

        require_verifier_dispatch()
        _require_dispatch()
        if type(candidate) is not authority_type:
            raise TypeError(
                "candidate must be exact ProductFixedNIidQualificationAuthority"
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
        if type(canonical) is not authority_type:
            raise ProductFixedNIidQualificationError(
                "fixed-N IID qualification resolver returned invalid authority type"
            )
        try:
            differs = any(
                object.__getattribute__(candidate, field_name)
                != object.__getattribute__(canonical, field_name)
                for field_name in fields
            )
        except AttributeError as exc:
            raise ProductFixedNIidQualificationError(
                "fixed-N IID qualification differs from canonical durable evidence"
            ) from exc
        if differs:
            raise ProductFixedNIidQualificationError(
                "fixed-N IID qualification differs from canonical durable evidence"
            )
        require_verifier_dispatch()
        _require_dispatch()
        return canonical

    verifier.__name__ = "verify_product_fixed_n_iid_qualification"
    verifier.__qualname__ = "verify_product_fixed_n_iid_qualification"
    return verifier


verify_product_fixed_n_iid_qualification = _build_qualification_verifier(
    resolve_product_fixed_n_iid_qualification,
    ProductFixedNIidQualificationAuthority,
)
del _build_qualification_verifier


__all__ = [
    "ProductFixedNIidQualificationAuthority",
    "ProductFixedNIidQualificationError",
    "resolve_product_fixed_n_iid_qualification",
    "verify_product_fixed_n_iid_qualification",
]
