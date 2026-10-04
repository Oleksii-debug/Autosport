from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .integrity import atomic_write_json, durable_path_lock
from .run_registry import RunRegistry
from .run_transaction import RunTransaction, RunTransactionError
from .risk_sampling_dependence import (
    ResolvedFixedNIidSamplingStructure,
    RiskSamplingDependenceError,
    inspect_fixed_n_iid_sampling_structure,
    resolve_fixed_n_iid_precommit_authority,
)
from .risk_sampling_membership import ResolvedFixedNRiskMembership


_FRAME_SCHEMA = "AUTOSPORT_RISK_IID_SAMPLING_FRAME_V1"
_HORIZON_SCHEMA = "AUTOSPORT_RISK_IID_FIXED_DRAW_HORIZON_V1"
_PLAN_SCHEMA = "AUTOSPORT_PRODUCT_IID_EXPECTED_DRAW_PLAN_V1"
_SUPPORTED_RNG_ALGORITHM = "AUTOSPORT_SHA256_REJECTION_V1"
_SUPPORTED_RNG_VERSION = "1"
_MAX_FRAME_UNITS = 65536
_MAX_DRAW_COUNT = 1000000
_MAX_TOTAL_DRAWS = 1000000
_MAX_FRAME_JSON_BYTES = 16 * 1024 * 1024
_MAX_HORIZON_JSON_BYTES = 4096
_HEX = frozenset("0123456789abcdef")

_PRECOMMIT = resolve_fixed_n_iid_precommit_authority
_PRECOMMIT_CODE = getattr(_PRECOMMIT, "__code__", None)
_STRUCTURE = inspect_fixed_n_iid_sampling_structure
_STRUCTURE_CODE = getattr(_STRUCTURE, "__code__", None)
_MEMBERSHIP_TYPE = ResolvedFixedNRiskMembership
_STRUCTURE_TYPE = ResolvedFixedNIidSamplingStructure


class ProductIidDrawPlanError(RuntimeError):
    """Expected IID draw plan cannot be re-resolved from frozen product evidence."""


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
        raise ProductIidDrawPlanError(
            "IID draw-plan material is outside canonical JSON"
        ) from exc


def _parse_canonical_json(
    raw: object,
    *,
    label: str,
) -> object:
    if type(raw) is not str or not raw or raw != raw.strip():
        raise ProductIidDrawPlanError(f"{label} must be non-empty canonical JSON text")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProductIidDrawPlanError(f"{label} is invalid JSON") from exc
    canonical = _canonical_json(payload).decode("utf-8")
    if raw != canonical:
        raise ProductIidDrawPlanError(
            f"{label} must use exact canonical JSON serialization"
        )
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON value {value!r}")


def _text(value: object, name: str, *, max_length: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > max_length
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        raise ProductIidDrawPlanError(f"{name} must be canonical bounded text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductIidDrawPlanError(f"{name} must be valid UTF-8") from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name, max_length=64)
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise ProductIidDrawPlanError(f"{name} must be lowercase SHA-256 hex")
    return text


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class SamplingFrameUnit:
    unit_id: str
    payload_sha256: str

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("SamplingFrameUnit must not be subclassed")


@dataclass(frozen=True, slots=True, init=False)
class ProductIidExpectedMemberDraw:
    member_id: str
    member_index: int
    stream_sha256: str
    draw_count: int
    draw_indices: tuple[int, ...]
    draw_unit_ids: tuple[str, ...]
    draw_payload_sha256: tuple[str, ...]
    draw_transcript_sha256: str

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> "ProductIidExpectedMemberDraw":
        raise TypeError(
            "ProductIidExpectedMemberDraw is product-issued; "
            "use resolve_product_iid_expected_draw_plan"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ProductIidExpectedMemberDraw must not be subclassed")

    @property
    def execution_consumption_proven(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


@dataclass(frozen=True, slots=True, init=False)
class ProductIidExpectedDrawPlan:
    experiment_id: str
    sampling_manifest_sha256: str
    sampling_frame_sha256: str
    horizon_sha256: str
    frame_units: tuple[SamplingFrameUnit, ...]
    member_draws: tuple[ProductIidExpectedMemberDraw, ...]
    plan_sha256: str

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> "ProductIidExpectedDrawPlan":
        raise TypeError(
            "ProductIidExpectedDrawPlan is product-issued; "
            "use resolve_product_iid_expected_draw_plan"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ProductIidExpectedDrawPlan must not be subclassed")

    @property
    def product_precommit_bound(self) -> bool:
        return True

    @property
    def sampling_frame_materialized(self) -> bool:
        return True

    @property
    def expected_draws_product_derived(self) -> bool:
        return True

    @property
    def occurrence_ancestry_proven(self) -> bool:
        return False

    @property
    def iid_qualified(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


_FRAME_UNIT_TYPE = SamplingFrameUnit
_MEMBER_DRAW_TYPE = ProductIidExpectedMemberDraw
_PLAN_TYPE = ProductIidExpectedDrawPlan


def _parse_frame(
    sampling_frame_json: str,
    *,
    expected_sha256: str,
) -> tuple[SamplingFrameUnit, ...]:
    if (
        type(sampling_frame_json) is not str
        or len(sampling_frame_json.encode("utf-8")) > _MAX_FRAME_JSON_BYTES
    ):
        raise ProductIidDrawPlanError(
            "sampling frame exceeds supported serialized size"
        )
    payload = _parse_canonical_json(
        sampling_frame_json,
        label="sampling_frame_json",
    )
    if type(payload) is not dict or set(payload) != {"schema", "units"}:
        raise ProductIidDrawPlanError(
            "sampling frame fields do not match the supported schema"
        )
    if payload.get("schema") != _FRAME_SCHEMA:
        raise ProductIidDrawPlanError("sampling frame schema is unsupported")
    raw_units = payload.get("units")
    if type(raw_units) is not list or not raw_units:
        raise ProductIidDrawPlanError("sampling frame must contain at least one unit")
    if len(raw_units) > _MAX_FRAME_UNITS:
        raise ProductIidDrawPlanError("sampling frame exceeds supported unit bound")
    if _sha_bytes(sampling_frame_json.encode("utf-8")) != expected_sha256:
        raise ProductIidDrawPlanError(
            "materialized sampling frame differs from the frozen frame digest"
        )

    units: list[SamplingFrameUnit] = []
    seen_ids: set[str] = set()
    seen_payloads: set[str] = set()
    for index, raw in enumerate(raw_units):
        if type(raw) is not dict or set(raw) != {"payload_sha256", "unit_id"}:
            raise ProductIidDrawPlanError(
                f"sampling frame unit {index} fields are invalid"
            )
        unit_id = _text(raw.get("unit_id"), f"units[{index}].unit_id")
        payload_sha256 = _sha(
            raw.get("payload_sha256"),
            f"units[{index}].payload_sha256",
        )
        if unit_id in seen_ids:
            raise ProductIidDrawPlanError(
                "sampling frame unit ids must be unique; implicit weighting is forbidden"
            )
        if payload_sha256 in seen_payloads:
            raise ProductIidDrawPlanError(
                "sampling frame payloads must be unique; implicit weighting is forbidden"
            )
        seen_ids.add(unit_id)
        seen_payloads.add(payload_sha256)
        units.append(
            SamplingFrameUnit(
                unit_id=unit_id,
                payload_sha256=payload_sha256,
            )
        )
    return tuple(units)


def _parse_horizon(
    horizon_json: str,
    *,
    expected_sha256: str,
) -> int:
    if (
        type(horizon_json) is not str
        or len(horizon_json.encode("utf-8")) > _MAX_HORIZON_JSON_BYTES
    ):
        raise ProductIidDrawPlanError(
            "horizon exceeds supported serialized size"
        )
    payload = _parse_canonical_json(
        horizon_json,
        label="horizon_json",
    )
    if type(payload) is not dict or set(payload) != {"draw_count", "schema"}:
        raise ProductIidDrawPlanError(
            "horizon fields do not match the supported schema"
        )
    if payload.get("schema") != _HORIZON_SCHEMA:
        raise ProductIidDrawPlanError("horizon schema is unsupported")
    draw_count = payload.get("draw_count")
    if type(draw_count) is not int or draw_count <= 0:
        raise ProductIidDrawPlanError("draw_count must be a positive exact integer")
    if draw_count > _MAX_DRAW_COUNT:
        raise ProductIidDrawPlanError("draw_count exceeds supported bound")
    if _sha_bytes(horizon_json.encode("utf-8")) != expected_sha256:
        raise ProductIidDrawPlanError(
            "materialized horizon differs from the frozen horizon digest"
        )
    return draw_count


def _draw_index(
    *,
    stream_sha256: str,
    draw_ordinal: int,
    frame_size: int,
) -> int:
    if frame_size <= 0:
        raise ProductIidDrawPlanError("sampling frame is empty")
    modulus = 1 << 256
    rejection_limit = modulus - (modulus % frame_size)
    attempt = 0
    while True:
        material = (
            "autosport-risk-iid-draw-v1\n"
            f"{stream_sha256}\n"
            f"{draw_ordinal}\n"
            f"{attempt}"
        ).encode("utf-8")
        candidate = int.from_bytes(hashlib.sha256(material).digest(), "big")
        if candidate < rejection_limit:
            return candidate % frame_size
        attempt += 1
        if attempt > 1024:
            raise ProductIidDrawPlanError(
                "deterministic rejection sampler exceeded safety bound"
            )


def _require_dispatch() -> None:
    if (
        resolve_fixed_n_iid_precommit_authority is not _PRECOMMIT
        or getattr(_PRECOMMIT, "__code__", None) is not _PRECOMMIT_CODE
        or inspect_fixed_n_iid_sampling_structure is not _STRUCTURE
        or getattr(_STRUCTURE, "__code__", None) is not _STRUCTURE_CODE
        or ResolvedFixedNRiskMembership is not _MEMBERSHIP_TYPE
        or ResolvedFixedNIidSamplingStructure is not _STRUCTURE_TYPE
        or SamplingFrameUnit is not _FRAME_UNIT_TYPE
        or ProductIidExpectedMemberDraw is not _MEMBER_DRAW_TYPE
        or ProductIidExpectedDrawPlan is not _PLAN_TYPE
    ):
        raise ProductIidDrawPlanError(
            "IID draw-plan authority dispatch changed"
        )


def _issue_member_draw(
    *,
    member_id: str,
    member_index: int,
    stream_sha256: str,
    draw_count: int,
    draw_indices: tuple[int, ...],
    draw_unit_ids: tuple[str, ...],
    draw_payload_sha256: tuple[str, ...],
    draw_transcript_sha256: str,
) -> ProductIidExpectedMemberDraw:
    result = object.__new__(_MEMBER_DRAW_TYPE)
    for field_name, value in (
        ("member_id", member_id),
        ("member_index", member_index),
        ("stream_sha256", stream_sha256),
        ("draw_count", draw_count),
        ("draw_indices", draw_indices),
        ("draw_unit_ids", draw_unit_ids),
        ("draw_payload_sha256", draw_payload_sha256),
        ("draw_transcript_sha256", draw_transcript_sha256),
    ):
        object.__setattr__(result, field_name, value)
    return result


def _issue_plan(
    *,
    experiment_id: str,
    sampling_manifest_sha256: str,
    sampling_frame_sha256: str,
    horizon_sha256: str,
    frame_units: tuple[SamplingFrameUnit, ...],
    member_draws: tuple[ProductIidExpectedMemberDraw, ...],
    plan_sha256: str,
) -> ProductIidExpectedDrawPlan:
    result = object.__new__(_PLAN_TYPE)
    for field_name, value in (
        ("experiment_id", experiment_id),
        ("sampling_manifest_sha256", sampling_manifest_sha256),
        ("sampling_frame_sha256", sampling_frame_sha256),
        ("horizon_sha256", horizon_sha256),
        ("frame_units", frame_units),
        ("member_draws", member_draws),
        ("plan_sha256", plan_sha256),
    ):
        object.__setattr__(result, field_name, value)
    return result


def resolve_product_iid_expected_draw_plan(
    membership: ResolvedFixedNRiskMembership,
    *,
    registry_path: str | Path,
    workspace: str | Path,
    sampling_manifest_json: str,
    sampling_frame_json: str,
    horizon_json: str,
    authority_root: str | Path | None = None,
) -> ProductIidExpectedDrawPlan:
    """Resolve the immutable expected draws for one product-precommitted IID design.

    This closes the materialized-frame/expected-draw gap only. It deliberately does
    not claim that any completed run consumed these draws. Positive occurrence
    ancestry still requires a runtime/transaction boundary that binds the exact
    draw transcript to the executed simulation path.
    """

    _require_dispatch()
    if type(membership) is not _MEMBERSHIP_TYPE:
        raise TypeError("membership must be an exact ResolvedFixedNRiskMembership")
    try:
        precommit = _PRECOMMIT(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            authority_root=authority_root,
        )
        structure = _STRUCTURE(
            membership,
            sampling_manifest_json=sampling_manifest_json,
        )
    except (RiskSamplingDependenceError, OSError, RuntimeError, ValueError) as exc:
        raise ProductIidDrawPlanError(
            "product IID precommit cannot be re-resolved"
        ) from exc
    _require_dispatch()

    if type(structure) is not _STRUCTURE_TYPE:
        raise ProductIidDrawPlanError("IID structure resolver returned wrong type")
    if (
        precommit.product_membership_preoutcome_chronology_proven is not True
        or precommit.product_randomization_root_issued is not True
        or precommit.occurrence_ancestry_proven is not False
        or precommit.iid_qualified is not False
        or precommit.sampling_manifest_sha256 != structure.manifest_sha256
        or precommit.planned_member_ids != structure.planned_member_ids
    ):
        raise ProductIidDrawPlanError(
            "product IID precommit truth boundary is inconsistent"
        )
    if (
        structure.rng_algorithm != _SUPPORTED_RNG_ALGORITHM
        or structure.rng_version != _SUPPORTED_RNG_VERSION
    ):
        raise ProductIidDrawPlanError(
            "IID draw-plan RNG algorithm/version is not product-supported"
        )

    frame = _parse_frame(
        sampling_frame_json,
        expected_sha256=structure.sampling_frame_sha256,
    )
    draw_count = _parse_horizon(
        horizon_json,
        expected_sha256=structure.horizon_sha256,
    )
    if draw_count * structure.planned_n > _MAX_TOTAL_DRAWS:
        raise ProductIidDrawPlanError(
            "fixed-N IID draw plan exceeds supported total-work bound"
        )

    member_draws: list[ProductIidExpectedMemberDraw] = []
    for member_index, member_id in enumerate(structure.planned_member_ids):
        stream = _sha(
            structure.member_stream_sha256[member_index],
            "member_stream_sha256",
        )
        indices = tuple(
            _draw_index(
                stream_sha256=stream,
                draw_ordinal=ordinal,
                frame_size=len(frame),
            )
            for ordinal in range(draw_count)
        )
        ids = tuple(frame[index].unit_id for index in indices)
        payloads = tuple(frame[index].payload_sha256 for index in indices)
        transcript_payload = {
            "draws": [
                {
                    "draw_ordinal": ordinal,
                    "frame_index": frame_index,
                    "payload_sha256": payloads[ordinal],
                    "unit_id": ids[ordinal],
                }
                for ordinal, frame_index in enumerate(indices)
            ],
            "experiment_id": structure.experiment_id,
            "horizon_sha256": structure.horizon_sha256,
            "member_id": member_id,
            "member_index": member_index,
            "rng_algorithm": structure.rng_algorithm,
            "rng_version": structure.rng_version,
            "sampling_frame_sha256": structure.sampling_frame_sha256,
            "stream_sha256": stream,
        }
        member_draws.append(
            _issue_member_draw(
                member_id=member_id,
                member_index=member_index,
                stream_sha256=stream,
                draw_count=draw_count,
                draw_indices=indices,
                draw_unit_ids=ids,
                draw_payload_sha256=payloads,
                draw_transcript_sha256=_sha_bytes(
                    _canonical_json(transcript_payload)
                ),
            )
        )

    plan_payload = {
        "experiment_id": structure.experiment_id,
        "horizon_sha256": structure.horizon_sha256,
        "member_draws": [
            {
                "draw_count": draw.draw_count,
                "draw_transcript_sha256": draw.draw_transcript_sha256,
                "member_id": draw.member_id,
                "member_index": draw.member_index,
                "stream_sha256": draw.stream_sha256,
            }
            for draw in member_draws
        ],
        "rng_algorithm": structure.rng_algorithm,
        "rng_version": structure.rng_version,
        "sampling_frame_sha256": structure.sampling_frame_sha256,
        "sampling_manifest_sha256": structure.manifest_sha256,
        "schema": _PLAN_SCHEMA,
    }
    result = _issue_plan(
        experiment_id=structure.experiment_id,
        sampling_manifest_sha256=structure.manifest_sha256,
        sampling_frame_sha256=structure.sampling_frame_sha256,
        horizon_sha256=structure.horizon_sha256,
        frame_units=frame,
        member_draws=tuple(member_draws),
        plan_sha256=_sha_bytes(_canonical_json(plan_payload)),
    )
    _require_dispatch()
    return result


def _build_draw_plan_verifier(
    resolver,
    plan_type: type[ProductIidExpectedDrawPlan],
):
    module_globals = globals()
    resolver_code = getattr(resolver, "__code__", None)

    def verifier(
        candidate: ProductIidExpectedDrawPlan,
        *,
        membership: ResolvedFixedNRiskMembership,
        registry_path: str | Path,
        workspace: str | Path,
        sampling_manifest_json: str,
        sampling_frame_json: str,
        horizon_json: str,
        authority_root: str | Path | None = None,
    ) -> ProductIidExpectedDrawPlan:
        if (
            module_globals.get("resolve_product_iid_expected_draw_plan")
            is not resolver
            or getattr(resolver, "__code__", None) is not resolver_code
            or module_globals.get("ProductIidExpectedDrawPlan") is not plan_type
        ):
            raise ProductIidDrawPlanError(
                "IID draw-plan verifier authority dispatch changed"
            )
        if type(candidate) is not plan_type:
            raise TypeError("candidate must be an exact ProductIidExpectedDrawPlan")
        canonical = resolver(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            sampling_frame_json=sampling_frame_json,
            horizon_json=horizon_json,
            authority_root=authority_root,
        )
        if (
            module_globals.get("resolve_product_iid_expected_draw_plan")
            is not resolver
            or getattr(resolver, "__code__", None) is not resolver_code
            or module_globals.get("ProductIidExpectedDrawPlan") is not plan_type
        ):
            raise ProductIidDrawPlanError(
                "IID draw-plan verifier authority dispatch changed"
            )
        if candidate != canonical:
            raise ProductIidDrawPlanError(
                "IID expected draw plan differs from canonical frozen evidence"
            )
        return canonical

    verifier.__name__ = "verify_product_iid_expected_draw_plan"
    verifier.__qualname__ = "verify_product_iid_expected_draw_plan"
    return verifier


verify_product_iid_expected_draw_plan = _build_draw_plan_verifier(
    resolve_product_iid_expected_draw_plan,
    ProductIidExpectedDrawPlan,
)
del _build_draw_plan_verifier


__all__ = [
    "ProductIidDrawPlanError",
    "ProductIidExpectedDrawPlan",
    "ProductIidExpectedMemberDraw",
    "SamplingFrameUnit",
    "resolve_product_iid_expected_draw_plan",
    "verify_product_iid_expected_draw_plan",
]


_RUN_ADMISSION_SCHEMA = "AUTOSPORT_PRODUCT_IID_RUN_ADMISSION_V1"
_RUN_ADMISSION_PLAN = resolve_product_iid_expected_draw_plan
_RUN_ADMISSION_PLAN_CODE = getattr(_RUN_ADMISSION_PLAN, "__code__", None)
_RUN_ADMISSION_REGISTRY_TYPE = RunRegistry
_RUN_ADMISSION_REGISTRY_READ = RunRegistry._read
_RUN_ADMISSION_REGISTRY_READ_CODE = getattr(
    _RUN_ADMISSION_REGISTRY_READ,
    "__code__",
    None,
)
_RUN_ADMISSION_TX_TYPE = RunTransaction
_RUN_ADMISSION_TX_BASE = RunTransaction.verified_base_paper_book_snapshot
_RUN_ADMISSION_TX_TERMINAL = RunTransaction.verified_terminal_paper_book_snapshot
_RUN_ADMISSION_TX_BASE_CODE = getattr(_RUN_ADMISSION_TX_BASE, "__code__", None)
_RUN_ADMISSION_TX_TERMINAL_CODE = getattr(
    _RUN_ADMISSION_TX_TERMINAL,
    "__code__",
    None,
)


@dataclass(frozen=True, slots=True, init=False)
class ProductIidRunAdmissionReceipt:
    """Product-issued pre-run binding to one exact expected draw transcript.

    This is stronger than an expected draw plan because the receipt must exist before
    RunRegistry.begin and must survive in RunRegistry, RunTransaction, and the
    canonical run summary. It still does not claim that simulator code consumed every
    draw; execution consumption remains a separate authority boundary.
    """

    experiment_id: str
    member_id: str
    member_index: int
    stream_sha256: str
    expected_draw_plan_sha256: str
    expected_draw_transcript_sha256: str
    sampling_manifest_sha256: str
    sampling_frame_sha256: str
    horizon_sha256: str
    state_sha256: str
    receipt_sha256: str
    run_admission_bound: bool

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> "ProductIidRunAdmissionReceipt":
        raise TypeError(
            "ProductIidRunAdmissionReceipt is product-issued; "
            "use issue_product_iid_run_admission"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ProductIidRunAdmissionReceipt must not be subclassed")

    @property
    def product_precommit_bound(self) -> bool:
        return True

    @property
    def execution_consumption_proven(self) -> bool:
        return False

    @property
    def occurrence_ancestry_proven(self) -> bool:
        return False

    @property
    def iid_qualified(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


_RUN_ADMISSION_TYPE = ProductIidRunAdmissionReceipt


def _require_run_admission_dispatch() -> None:
    if (
        resolve_product_iid_expected_draw_plan is not _RUN_ADMISSION_PLAN
        or getattr(_RUN_ADMISSION_PLAN, "__code__", None)
        is not _RUN_ADMISSION_PLAN_CODE
        or RunRegistry is not _RUN_ADMISSION_REGISTRY_TYPE
        or _RUN_ADMISSION_REGISTRY_TYPE._read is not _RUN_ADMISSION_REGISTRY_READ
        or getattr(_RUN_ADMISSION_REGISTRY_READ, "__code__", None)
        is not _RUN_ADMISSION_REGISTRY_READ_CODE
        or RunTransaction is not _RUN_ADMISSION_TX_TYPE
        or _RUN_ADMISSION_TX_TYPE.verified_base_paper_book_snapshot
        is not _RUN_ADMISSION_TX_BASE
        or _RUN_ADMISSION_TX_TYPE.verified_terminal_paper_book_snapshot
        is not _RUN_ADMISSION_TX_TERMINAL
        or getattr(_RUN_ADMISSION_TX_BASE, "__code__", None)
        is not _RUN_ADMISSION_TX_BASE_CODE
        or getattr(_RUN_ADMISSION_TX_TERMINAL, "__code__", None)
        is not _RUN_ADMISSION_TX_TERMINAL_CODE
        or ProductIidRunAdmissionReceipt is not _RUN_ADMISSION_TYPE
    ):
        raise ProductIidDrawPlanError(
            "IID run-admission authority dispatch changed"
        )


def _run_admission_state_path(
    workspace: Path,
    *,
    experiment_id: str,
    member_index: int,
) -> Path:
    key = _sha_bytes(
        (
            "autosport-product-iid-run-admission-v1\n"
            f"{experiment_id}\n{member_index}"
        ).encode("utf-8")
    )[:24]
    return workspace / f".risk-iid-run-admission-{key}.json"


def _run_admission_registry_item(
    workspace: Path,
    member_id: str,
) -> dict[str, Any] | None:
    registry = _RUN_ADMISSION_REGISTRY_TYPE(
        workspace / "run_registry.json"
    )
    try:
        state = _RUN_ADMISSION_REGISTRY_READ(registry)
    except (OSError, ValueError) as exc:
        raise ProductIidDrawPlanError(
            "IID run-admission cannot read RunRegistry"
        ) from exc
    _require_run_admission_dispatch()
    runs = state.get("runs") if type(state) is dict else None
    if type(runs) is not dict:
        raise ProductIidDrawPlanError(
            "IID run-admission RunRegistry state is invalid"
        )
    matches = [
        dict(item)
        for item in runs.values()
        if type(item) is dict and item.get("run_id") == member_id
    ]
    if len(matches) > 1:
        raise ProductIidDrawPlanError(
            "IID run-admission member run identity is ambiguous"
        )
    return matches[0] if matches else None


def _run_admission_core(
    plan: ProductIidExpectedDrawPlan,
    *,
    member_index: int,
) -> dict[str, object]:
    if type(plan) is not _PLAN_TYPE:
        raise ProductIidDrawPlanError(
            "IID run-admission expected draw plan type is invalid"
        )
    if type(member_index) is not int or member_index < 0:
        raise ProductIidDrawPlanError(
            "member_index must be a non-negative exact integer"
        )
    if member_index >= len(plan.member_draws):
        raise ProductIidDrawPlanError(
            "member_index is outside the expected draw plan"
        )
    draw = plan.member_draws[member_index]
    return {
        "schema": _RUN_ADMISSION_SCHEMA,
        "experiment_id": plan.experiment_id,
        "member_id": draw.member_id,
        "member_index": draw.member_index,
        "stream_sha256": draw.stream_sha256,
        "expected_draw_plan_sha256": plan.plan_sha256,
        "expected_draw_transcript_sha256": draw.draw_transcript_sha256,
        "sampling_manifest_sha256": plan.sampling_manifest_sha256,
        "sampling_frame_sha256": plan.sampling_frame_sha256,
        "horizon_sha256": plan.horizon_sha256,
    }


def _issue_run_admission_receipt(
    state: dict[str, object],
    *,
    run_admission_bound: bool,
) -> ProductIidRunAdmissionReceipt:
    state_sha256 = _sha_bytes(_canonical_json(state))
    receipt_payload = {
        "schema": _RUN_ADMISSION_SCHEMA,
        "state_sha256": state_sha256,
        "experiment_id": state["experiment_id"],
        "member_id": state["member_id"],
        "member_index": state["member_index"],
        "stream_sha256": state["stream_sha256"],
        "expected_draw_plan_sha256": state["expected_draw_plan_sha256"],
        "expected_draw_transcript_sha256": (
            state["expected_draw_transcript_sha256"]
        ),
        "sampling_manifest_sha256": state["sampling_manifest_sha256"],
        "sampling_frame_sha256": state["sampling_frame_sha256"],
        "horizon_sha256": state["horizon_sha256"],
    }
    result = object.__new__(_RUN_ADMISSION_TYPE)
    for field_name, value in (
        ("experiment_id", state["experiment_id"]),
        ("member_id", state["member_id"]),
        ("member_index", state["member_index"]),
        ("stream_sha256", state["stream_sha256"]),
        ("expected_draw_plan_sha256", state["expected_draw_plan_sha256"]),
        (
            "expected_draw_transcript_sha256",
            state["expected_draw_transcript_sha256"],
        ),
        ("sampling_manifest_sha256", state["sampling_manifest_sha256"]),
        ("sampling_frame_sha256", state["sampling_frame_sha256"]),
        ("horizon_sha256", state["horizon_sha256"]),
        ("state_sha256", state_sha256),
        ("receipt_sha256", _sha_bytes(_canonical_json(receipt_payload))),
        ("run_admission_bound", run_admission_bound),
    ):
        object.__setattr__(result, field_name, value)
    return result


def _resolve_run_admission_state(
    *,
    membership: ResolvedFixedNRiskMembership,
    registry_path: str | Path,
    workspace: Path,
    sampling_manifest_json: str,
    sampling_frame_json: str,
    horizon_json: str,
    member_index: int,
    authority_root: str | Path | None,
    require_completed_run: bool,
) -> ProductIidRunAdmissionReceipt:
    _require_run_admission_dispatch()
    plan = _RUN_ADMISSION_PLAN(
        membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=sampling_manifest_json,
        sampling_frame_json=sampling_frame_json,
        horizon_json=horizon_json,
        authority_root=authority_root,
    )
    _require_run_admission_dispatch()
    state_expected = _run_admission_core(
        plan,
        member_index=member_index,
    )
    path = _run_admission_state_path(
        workspace,
        experiment_id=plan.experiment_id,
        member_index=member_index,
    )
    try:
        raw = path.read_text(encoding="utf-8")
        state = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProductIidDrawPlanError(
            "IID run-admission state cannot be re-resolved"
        ) from exc
    if type(state) is not dict or state != state_expected:
        raise ProductIidDrawPlanError(
            "IID run-admission state differs from frozen expected draws"
        )
    prepared = _issue_run_admission_receipt(
        state,
        run_admission_bound=False,
    )
    if not require_completed_run:
        return prepared

    item = _run_admission_registry_item(
        workspace,
        prepared.member_id,
    )
    if item is None or item.get("status") != "completed":
        raise ProductIidDrawPlanError(
            "IID run-admission requires the exact completed member run"
        )
    if (
        item.get("sampling_draw_admission_receipt_sha256")
        != prepared.receipt_sha256
    ):
        raise ProductIidDrawPlanError(
            "completed run does not bind the exact IID draw-admission receipt"
        )
    tx = _RUN_ADMISSION_TX_TYPE(workspace, prepared.member_id)
    try:
        _RUN_ADMISSION_TX_BASE(tx)
        _RUN_ADMISSION_TX_TERMINAL(tx)
        _RUN_ADMISSION_REGISTRY_TYPE(
            workspace / "run_registry.json"
        ).verified_completed_summary_for_run(prepared.member_id)
    except (RunTransactionError, OSError, ValueError, KeyError) as exc:
        raise ProductIidDrawPlanError(
            "IID draw-admission completed transaction cannot be re-resolved"
        ) from exc
    _require_run_admission_dispatch()
    return _issue_run_admission_receipt(
        state,
        run_admission_bound=True,
    )


def issue_product_iid_run_admission(
    membership: ResolvedFixedNRiskMembership,
    *,
    registry_path: str | Path,
    workspace: str | Path,
    sampling_manifest_json: str,
    sampling_frame_json: str,
    horizon_json: str,
    member_index: int,
    authority_root: str | Path | None = None,
) -> ProductIidRunAdmissionReceipt:
    """Persist the exact expected draw transcript before the member run begins."""

    if type(membership) is not _MEMBERSHIP_TYPE:
        raise TypeError(
            "membership must be an exact ResolvedFixedNRiskMembership"
        )
    root = Path(workspace).expanduser().resolve(strict=True)
    plan = _RUN_ADMISSION_PLAN(
        membership,
        registry_path=registry_path,
        workspace=root,
        sampling_manifest_json=sampling_manifest_json,
        sampling_frame_json=sampling_frame_json,
        horizon_json=horizon_json,
        authority_root=authority_root,
    )
    _require_run_admission_dispatch()
    state = _run_admission_core(
        plan,
        member_index=member_index,
    )
    member_id = str(state["member_id"])
    path = _run_admission_state_path(
        root,
        experiment_id=plan.experiment_id,
        member_index=member_index,
    )
    with durable_path_lock(path):
        if path.exists():
            return _resolve_run_admission_state(
                membership=membership,
                registry_path=registry_path,
                workspace=root,
                sampling_manifest_json=sampling_manifest_json,
                sampling_frame_json=sampling_frame_json,
                horizon_json=horizon_json,
                member_index=member_index,
                authority_root=authority_root,
                require_completed_run=False,
            )
        if _run_admission_registry_item(root, member_id) is not None:
            raise ProductIidDrawPlanError(
                "IID run-admission must be issued before RunRegistry.begin"
            )
        if _RUN_ADMISSION_TX_TYPE(root, member_id).root.exists():
            raise ProductIidDrawPlanError(
                "IID run-admission must be issued before RunTransaction.start"
            )
        atomic_write_json(path, state)
    return _issue_run_admission_receipt(
        state,
        run_admission_bound=False,
    )


def resolve_product_iid_run_admission(
    membership: ResolvedFixedNRiskMembership,
    *,
    registry_path: str | Path,
    workspace: str | Path,
    sampling_manifest_json: str,
    sampling_frame_json: str,
    horizon_json: str,
    member_index: int,
    authority_root: str | Path | None = None,
) -> ProductIidRunAdmissionReceipt:
    """Prove a completed run durably retained its pre-run expected-draw admission."""

    if type(membership) is not _MEMBERSHIP_TYPE:
        raise TypeError(
            "membership must be an exact ResolvedFixedNRiskMembership"
        )
    root = Path(workspace).expanduser().resolve(strict=True)
    return _resolve_run_admission_state(
        membership=membership,
        registry_path=registry_path,
        workspace=root,
        sampling_manifest_json=sampling_manifest_json,
        sampling_frame_json=sampling_frame_json,
        horizon_json=horizon_json,
        member_index=member_index,
        authority_root=authority_root,
        require_completed_run=True,
    )


__all__.extend(
    [
        "ProductIidRunAdmissionReceipt",
        "issue_product_iid_run_admission",
        "resolve_product_iid_run_admission",
    ]
)
