from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True, slots=True)
class ProductIidExpectedMemberDraw:
    member_id: str
    member_index: int
    stream_sha256: str
    draw_count: int
    draw_indices: tuple[int, ...]
    draw_unit_ids: tuple[str, ...]
    draw_payload_sha256: tuple[str, ...]
    draw_transcript_sha256: str

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ProductIidExpectedMemberDraw must not be subclassed")

    @property
    def execution_consumption_proven(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ProductIidExpectedDrawPlan:
    experiment_id: str
    sampling_manifest_sha256: str
    sampling_frame_sha256: str
    horizon_sha256: str
    frame_units: tuple[SamplingFrameUnit, ...]
    member_draws: tuple[ProductIidExpectedMemberDraw, ...]
    plan_sha256: str

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


def _parse_frame(
    sampling_frame_json: str,
    *,
    expected_sha256: str,
) -> tuple[SamplingFrameUnit, ...]:
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
        seen_ids.add(unit_id)
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
    ):
        raise ProductIidDrawPlanError(
            "IID draw-plan authority dispatch changed"
        )


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
            "member_id": member_id,
            "member_index": member_index,
            "sampling_frame_sha256": structure.sampling_frame_sha256,
            "stream_sha256": stream,
        }
        member_draws.append(
            ProductIidExpectedMemberDraw(
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
        "sampling_frame_sha256": structure.sampling_frame_sha256,
        "sampling_manifest_sha256": structure.manifest_sha256,
        "schema": _PLAN_SCHEMA,
    }
    result = ProductIidExpectedDrawPlan(
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


def verify_product_iid_expected_draw_plan(
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
    """Re-resolve and compare every field; the dataclass is not a bearer capability."""

    if type(candidate) is not ProductIidExpectedDrawPlan:
        raise TypeError("candidate must be an exact ProductIidExpectedDrawPlan")
    canonical = resolve_product_iid_expected_draw_plan(
        membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=sampling_manifest_json,
        sampling_frame_json=sampling_frame_json,
        horizon_json=horizon_json,
        authority_root=authority_root,
    )
    if candidate != canonical:
        raise ProductIidDrawPlanError(
            "IID expected draw plan differs from canonical frozen evidence"
        )
    return canonical


__all__ = [
    "ProductIidDrawPlanError",
    "ProductIidExpectedDrawPlan",
    "ProductIidExpectedMemberDraw",
    "SamplingFrameUnit",
    "resolve_product_iid_expected_draw_plan",
    "verify_product_iid_expected_draw_plan",
]
