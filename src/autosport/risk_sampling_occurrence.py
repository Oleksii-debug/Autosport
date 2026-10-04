"""Product-owned fixed-N sampling occurrence issuance and completed-run binding.

This module turns one frozen member stream into one exact frame selection before the
member run begins. The durable receipt is not an IID verdict: it proves only that
Autosport selected the exact replay input from the exact frozen frame and that a
completed RunRegistry/RunTransaction later used that selected input.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads
from .risk_sampling_dependence import (
    ResolvedFixedNIidPrecommitAuthority,
    ResolvedFixedNIidSamplingStructure,
    inspect_fixed_n_iid_sampling_structure,
    resolve_fixed_n_iid_precommit_authority,
)
from .risk_sampling_membership import ResolvedFixedNRiskMembership
from .run_registry import RunRegistry
from .run_transaction import RunTransaction, RunTransactionError


_SCHEMA: Final = "autosport.risk.iid-sampling-occurrence"
_SCHEMA_VERSION: Final = 1
_FRAME_SCHEMA: Final = "autosport-risk-iid-sampling-frame-v1"
_RNG_ALGORITHM: Final = "SHA256_COUNTER_V1"
_RNG_VERSION: Final = "autosport-risk-v1"
_HEX: Final = frozenset("0123456789abcdef")
_MAX_FRAME_ITEMS: Final = 100_000
_MAX_FRAME_BYTES: Final = 16 * 1024 * 1024

_PRECOMMIT = resolve_fixed_n_iid_precommit_authority
_PRECOMMIT_CODE = getattr(_PRECOMMIT, "__code__", None)
_STRUCTURE = inspect_fixed_n_iid_sampling_structure
_STRUCTURE_CODE = getattr(_STRUCTURE, "__code__", None)
_RUN_REGISTRY_TYPE = RunRegistry
_RUN_REGISTRY_READ = RunRegistry._read
_RUN_REGISTRY_READ_CODE = getattr(_RUN_REGISTRY_READ, "__code__", None)
_TX_TYPE = RunTransaction
_TX_BASE = RunTransaction.verified_base_paper_book_snapshot
_TX_TERMINAL = RunTransaction.verified_terminal_paper_book_snapshot
_TX_BASE_CODE = getattr(_TX_BASE, "__code__", None)
_TX_TERMINAL_CODE = getattr(_TX_TERMINAL, "__code__", None)


class RiskSamplingOccurrenceError(RuntimeError):
    """One fixed-N member occurrence cannot be issued or re-resolved safely."""


@dataclass(frozen=True, slots=True)
class ProductIidSamplingOccurrenceReceipt:
    experiment_id: str
    member_id: str
    member_index: int
    stream_sha256: str
    sampling_frame_sha256: str
    frame_item_id: str
    market_sha256: str
    sealed_results_sha256: str
    draw_counter: int
    draw_word_sha256: str
    draw_transcript_sha256: str
    randomization_precommit_receipt_sha256: str
    sampling_manifest_sha256: str
    state_sha256: str
    receipt_sha256: str
    completed_run_bound: bool

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ProductIidSamplingOccurrenceReceipt must not be subclassed")

    @property
    def occurrence_ancestry_proven(self) -> bool:
        return self.completed_run_bound is True

    @property
    def iid_qualified(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


def _require_dispatch() -> None:
    if (
        resolve_fixed_n_iid_precommit_authority is not _PRECOMMIT
        or getattr(_PRECOMMIT, "__code__", None) is not _PRECOMMIT_CODE
        or inspect_fixed_n_iid_sampling_structure is not _STRUCTURE
        or getattr(_STRUCTURE, "__code__", None) is not _STRUCTURE_CODE
        or RunRegistry is not _RUN_REGISTRY_TYPE
        or _RUN_REGISTRY_TYPE._read is not _RUN_REGISTRY_READ
        or getattr(_RUN_REGISTRY_READ, "__code__", None) is not _RUN_REGISTRY_READ_CODE
        or RunTransaction is not _TX_TYPE
        or _TX_TYPE.verified_base_paper_book_snapshot is not _TX_BASE
        or _TX_TYPE.verified_terminal_paper_book_snapshot is not _TX_TERMINAL
        or getattr(_TX_BASE, "__code__", None) is not _TX_BASE_CODE
        or getattr(_TX_TERMINAL, "__code__", None) is not _TX_TERMINAL_CODE
    ):
        raise RiskSamplingOccurrenceError("sampling occurrence authority dispatch changed")


def _text(value: object, name: str, *, max_length: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RiskSamplingOccurrenceError(f"{name} must be canonical non-empty text")
    if len(value) > max_length or "\x00" in value or "\r" in value or "\n" in value:
        raise RiskSamplingOccurrenceError(f"{name} contains unsupported characters")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RiskSamplingOccurrenceError(f"{name} must be valid UTF-8") from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise RiskSamplingOccurrenceError(f"{name} must be lowercase SHA-256 hex")
    return text


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RiskSamplingOccurrenceError(
            "sampling occurrence evidence is outside canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _workspace(workspace: str | Path) -> Path:
    try:
        root = Path(workspace).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RiskSamplingOccurrenceError("workspace cannot be resolved") from exc
    if not root.is_dir():
        raise RiskSamplingOccurrenceError("workspace must be an existing directory")
    return root


def _experiment_key(experiment_id: str) -> str:
    return hashlib.sha256(
        ("autosport-risk-iid-occurrence-experiment-v1\n" + experiment_id).encode("utf-8")
    ).hexdigest()[:24]


def _state_path(root: Path, experiment_id: str, member_index: int) -> Path:
    return root / (
        f".risk-iid-occurrence-{_experiment_key(experiment_id)}-{member_index}.json"
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RiskSamplingOccurrenceError(
                f"sampling frame contains duplicate JSON key {key!r}"
            )
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise RiskSamplingOccurrenceError(
        f"sampling frame contains non-finite JSON value {value!r}"
    )


def canonical_sampling_frame_json(
    items: tuple[tuple[str, str, str], ...],
) -> str:
    """Build the exact supported frame representation.

    Each item is (frame_item_id, market_sha256, sealed_results_sha256).
    """

    if type(items) is not tuple or not items or len(items) > _MAX_FRAME_ITEMS:
        raise RiskSamplingOccurrenceError(
            "sampling frame items must be a bounded non-empty exact tuple"
        )
    payload_items: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if type(item) is not tuple or len(item) != 3:
            raise RiskSamplingOccurrenceError(
                f"sampling frame item {index} must be an exact three-tuple"
            )
        item_id = _text(item[0], f"sampling frame item {index} id")
        if item_id in seen:
            raise RiskSamplingOccurrenceError("sampling frame item ids must be unique")
        seen.add(item_id)
        payload_items.append(
            {
                "frame_item_id": item_id,
                "market_sha256": _sha(
                    item[1],
                    f"sampling frame item {index} market_sha256",
                ),
                "sealed_results_sha256": _sha(
                    item[2],
                    f"sampling frame item {index} sealed_results_sha256",
                ),
            }
        )
    return _canonical_bytes(
        {"schema": _FRAME_SCHEMA, "items": payload_items}
    ).decode("utf-8")


def _parse_frame(raw: object) -> tuple[dict[str, str], ...]:
    text = _text(raw, "sampling_frame_json", max_length=_MAX_FRAME_BYTES)
    if len(text.encode("utf-8")) > _MAX_FRAME_BYTES:
        raise RiskSamplingOccurrenceError("sampling frame exceeds supported evidence size")
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RiskSamplingOccurrenceError("sampling frame is invalid JSON") from exc
    if type(payload) is not dict or set(payload) != {"schema", "items"}:
        raise RiskSamplingOccurrenceError("sampling frame fields do not match schema")
    if payload.get("schema") != _FRAME_SCHEMA:
        raise RiskSamplingOccurrenceError("sampling frame schema is unsupported")
    raw_items = payload.get("items")
    if (
        type(raw_items) is not list
        or not raw_items
        or len(raw_items) > _MAX_FRAME_ITEMS
    ):
        raise RiskSamplingOccurrenceError("sampling frame item count is unsupported")
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_items):
        if type(item) is not dict or set(item) != {
            "frame_item_id",
            "market_sha256",
            "sealed_results_sha256",
        }:
            raise RiskSamplingOccurrenceError(
                f"sampling frame item {index} fields are invalid"
            )
        item_id = _text(item.get("frame_item_id"), f"sampling frame item {index} id")
        if item_id in seen:
            raise RiskSamplingOccurrenceError("sampling frame item ids must be unique")
        seen.add(item_id)
        items.append(
            {
                "frame_item_id": item_id,
                "market_sha256": _sha(
                    item.get("market_sha256"),
                    f"sampling frame item {index} market_sha256",
                ),
                "sealed_results_sha256": _sha(
                    item.get("sealed_results_sha256"),
                    f"sampling frame item {index} sealed_results_sha256",
                ),
            }
        )
    canonical = _canonical_bytes(
        {"schema": _FRAME_SCHEMA, "items": items}
    ).decode("utf-8")
    if canonical != text:
        raise RiskSamplingOccurrenceError(
            "sampling frame must use canonical JSON serialization"
        )
    return tuple(items)


def _uniform_draw(stream_sha256: str, frame_size: int) -> tuple[int, int, str]:
    if type(frame_size) is not int or frame_size <= 0:
        raise RiskSamplingOccurrenceError("sampling frame size is invalid")
    modulus = 1 << 256
    limit = modulus - (modulus % frame_size)
    for counter in range(1_000_000):
        material = (
            "autosport-risk-iid-draw-v1\n"
            f"{stream_sha256}\n{counter}"
        ).encode("utf-8")
        word = hashlib.sha256(material).digest()
        value = int.from_bytes(word, "big")
        if value < limit:
            return value % frame_size, counter, word.hex()
    raise RiskSamplingOccurrenceError("sampling draw rejection bound exhausted")


def _authorities(
    membership: ResolvedFixedNRiskMembership,
    *,
    registry_path: str | Path,
    workspace: Path,
    sampling_manifest_json: str,
    authority_root: str | Path | None,
) -> tuple[ResolvedFixedNIidPrecommitAuthority, ResolvedFixedNIidSamplingStructure]:
    _require_dispatch()
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
    except (OSError, RuntimeError, ValueError) as exc:
        raise RiskSamplingOccurrenceError(
            "sampling occurrence precommit authority cannot be re-resolved"
        ) from exc
    _require_dispatch()
    if (
        type(precommit) is not ResolvedFixedNIidPrecommitAuthority
        or type(structure) is not ResolvedFixedNIidSamplingStructure
        or precommit.product_membership_preoutcome_chronology_proven is not True
        or precommit.product_randomization_root_issued is not True
        or precommit.occurrence_ancestry_proven is not False
        or precommit.iid_qualified is not False
        or precommit.sampling_manifest_sha256 != structure.manifest_sha256
        or precommit.planned_member_ids != structure.planned_member_ids
    ):
        raise RiskSamplingOccurrenceError(
            "sampling occurrence precommit boundary is invalid"
        )
    if (
        structure.rng_algorithm != _RNG_ALGORITHM
        or structure.rng_version != _RNG_VERSION
    ):
        raise RiskSamplingOccurrenceError(
            "sampling manifest RNG is not implemented by product occurrence authority"
        )
    return precommit, structure


def _state_core(
    *,
    precommit: ResolvedFixedNIidPrecommitAuthority,
    structure: ResolvedFixedNIidSamplingStructure,
    member_index: int,
    frame_json: str,
    selected: dict[str, str],
    draw_counter: int,
    draw_word_sha256: str,
    draw_transcript_sha256: str,
) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "experiment_id": structure.experiment_id,
        "member_id": structure.planned_member_ids[member_index],
        "member_index": member_index,
        "stream_sha256": structure.member_stream_sha256[member_index],
        "sampling_frame_sha256": structure.sampling_frame_sha256,
        "sampling_frame_json": frame_json,
        "frame_item_id": selected["frame_item_id"],
        "market_sha256": selected["market_sha256"],
        "sealed_results_sha256": selected["sealed_results_sha256"],
        "draw_counter": draw_counter,
        "draw_word_sha256": draw_word_sha256,
        "draw_transcript_sha256": draw_transcript_sha256,
        "membership_receipt_sha256": precommit.membership_receipt_sha256,
        "randomization_precommit_receipt_sha256": (
            precommit.randomization_precommit_receipt_sha256
        ),
        "sampling_manifest_sha256": structure.manifest_sha256,
    }


def _run_registry_item(root: Path, member_id: str) -> dict[str, object] | None:
    registry = _RUN_REGISTRY_TYPE(root / "run_registry.json")
    try:
        state = _RUN_REGISTRY_READ(registry)
    except (OSError, ValueError) as exc:
        raise RiskSamplingOccurrenceError(
            "run registry cannot be re-resolved"
        ) from exc
    _require_dispatch()
    runs = state.get("runs") if type(state) is dict else None
    if type(runs) is not dict:
        raise RiskSamplingOccurrenceError("run registry state is invalid")
    matches = [
        item
        for item in runs.values()
        if type(item) is dict and item.get("run_id") == member_id
    ]
    if len(matches) > 1:
        raise RiskSamplingOccurrenceError("member run identity is ambiguous")
    return dict(matches[0]) if matches else None


def _receipt_from_state(
    state: dict[str, object],
    *,
    state_sha256: str,
    completed_run_bound: bool,
) -> ProductIidSamplingOccurrenceReceipt:
    receipt_core = {
        "state_sha256": state_sha256,
        "experiment_id": state["experiment_id"],
        "member_id": state["member_id"],
        "member_index": state["member_index"],
        "stream_sha256": state["stream_sha256"],
        "sampling_frame_sha256": state["sampling_frame_sha256"],
        "frame_item_id": state["frame_item_id"],
        "market_sha256": state["market_sha256"],
        "sealed_results_sha256": state["sealed_results_sha256"],
        "draw_counter": state["draw_counter"],
        "draw_word_sha256": state["draw_word_sha256"],
        "draw_transcript_sha256": state["draw_transcript_sha256"],
        "randomization_precommit_receipt_sha256": (
            state["randomization_precommit_receipt_sha256"]
        ),
        "sampling_manifest_sha256": state["sampling_manifest_sha256"],
        "completed_run_bound": completed_run_bound,
    }
    return ProductIidSamplingOccurrenceReceipt(
        experiment_id=str(state["experiment_id"]),
        member_id=str(state["member_id"]),
        member_index=int(state["member_index"]),
        stream_sha256=str(state["stream_sha256"]),
        sampling_frame_sha256=str(state["sampling_frame_sha256"]),
        frame_item_id=str(state["frame_item_id"]),
        market_sha256=str(state["market_sha256"]),
        sealed_results_sha256=str(state["sealed_results_sha256"]),
        draw_counter=int(state["draw_counter"]),
        draw_word_sha256=str(state["draw_word_sha256"]),
        draw_transcript_sha256=str(state["draw_transcript_sha256"]),
        randomization_precommit_receipt_sha256=str(
            state["randomization_precommit_receipt_sha256"]
        ),
        sampling_manifest_sha256=str(state["sampling_manifest_sha256"]),
        state_sha256=state_sha256,
        receipt_sha256=_digest(receipt_core),
        completed_run_bound=completed_run_bound,
    )


def issue_fixed_n_iid_member_occurrence(
    membership: ResolvedFixedNRiskMembership,
    *,
    registry_path: str | Path,
    workspace: str | Path,
    sampling_manifest_json: str,
    member_index: int,
    sampling_frame_json: str,
    authority_root: str | Path | None = None,
) -> ProductIidSamplingOccurrenceReceipt:
    """Create-or-recover one product-selected frame member before its run begins."""

    if type(membership) is not ResolvedFixedNRiskMembership:
        raise TypeError("membership must be exact ResolvedFixedNRiskMembership")
    if type(member_index) is not int or member_index < 0:
        raise RiskSamplingOccurrenceError(
            "member_index must be a non-negative exact int"
        )
    root = _workspace(workspace)
    precommit, structure = _authorities(
        membership,
        registry_path=registry_path,
        workspace=root,
        sampling_manifest_json=sampling_manifest_json,
        authority_root=authority_root,
    )
    if member_index >= structure.planned_n:
        raise RiskSamplingOccurrenceError(
            "member_index is outside frozen membership"
        )

    frame = _parse_frame(sampling_frame_json)
    frame_sha256 = hashlib.sha256(
        sampling_frame_json.encode("utf-8")
    ).hexdigest()
    if frame_sha256 != structure.sampling_frame_sha256:
        raise RiskSamplingOccurrenceError(
            "materialized sampling frame does not match frozen frame digest"
        )
    stream = _sha(
        structure.member_stream_sha256[member_index],
        "member_stream_sha256",
    )
    selected_index, counter, word_sha256 = _uniform_draw(stream, len(frame))
    selected = frame[selected_index]
    transcript = {
        "schema": "autosport-risk-iid-draw-transcript-v1",
        "experiment_id": structure.experiment_id,
        "member_id": structure.planned_member_ids[member_index],
        "member_index": member_index,
        "stream_sha256": stream,
        "sampling_frame_sha256": structure.sampling_frame_sha256,
        "frame_size": len(frame),
        "draw_counter": counter,
        "draw_word_sha256": word_sha256,
        "selected_index": selected_index,
        "frame_item_id": selected["frame_item_id"],
        "market_sha256": selected["market_sha256"],
        "sealed_results_sha256": selected["sealed_results_sha256"],
    }
    transcript_sha256 = _digest(transcript)
    state = _state_core(
        precommit=precommit,
        structure=structure,
        member_index=member_index,
        frame_json=sampling_frame_json,
        selected=selected,
        draw_counter=counter,
        draw_word_sha256=word_sha256,
        draw_transcript_sha256=transcript_sha256,
    )
    state_sha256 = _digest(state)
    path = _state_path(root, structure.experiment_id, member_index)

    with durable_path_lock(path):
        if path.exists():
            return _resolve_state(
                membership,
                registry_path=registry_path,
                root=root,
                sampling_manifest_json=sampling_manifest_json,
                member_index=member_index,
                authority_root=authority_root,
                require_completed_run=False,
            )
        member_id = structure.planned_member_ids[member_index]
        if _run_registry_item(root, member_id) is not None:
            raise RiskSamplingOccurrenceError(
                "sampling occurrence must be issued before RunRegistry.begin"
            )
        if _TX_TYPE(root, member_id).root.exists():
            raise RiskSamplingOccurrenceError(
                "sampling occurrence must be issued before RunTransaction.start"
            )
        atomic_write_json(path, state)

    return _receipt_from_state(
        state,
        state_sha256=state_sha256,
        completed_run_bound=False,
    )


def _resolve_state(
    membership: ResolvedFixedNRiskMembership,
    *,
    registry_path: str | Path,
    root: Path,
    sampling_manifest_json: str,
    member_index: int,
    authority_root: str | Path | None,
    require_completed_run: bool,
) -> ProductIidSamplingOccurrenceReceipt:
    precommit, structure = _authorities(
        membership,
        registry_path=registry_path,
        workspace=root,
        sampling_manifest_json=sampling_manifest_json,
        authority_root=authority_root,
    )
    if member_index >= structure.planned_n:
        raise RiskSamplingOccurrenceError(
            "member_index is outside frozen membership"
        )
    path = _state_path(root, structure.experiment_id, member_index)
    try:
        raw = path.read_text(encoding="utf-8")
        state = strict_json_loads(
            raw,
            context="sampling occurrence state",
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise RiskSamplingOccurrenceError(
            "sampling occurrence state cannot be read"
        ) from exc
    if type(state) is not dict:
        raise RiskSamplingOccurrenceError(
            "sampling occurrence state must be an object"
        )
    expected_fields = {
        "schema",
        "schema_version",
        "experiment_id",
        "member_id",
        "member_index",
        "stream_sha256",
        "sampling_frame_sha256",
        "sampling_frame_json",
        "frame_item_id",
        "market_sha256",
        "sealed_results_sha256",
        "draw_counter",
        "draw_word_sha256",
        "draw_transcript_sha256",
        "membership_receipt_sha256",
        "randomization_precommit_receipt_sha256",
        "sampling_manifest_sha256",
    }
    if set(state) != expected_fields:
        raise RiskSamplingOccurrenceError(
            "sampling occurrence state fields mismatch"
        )
    frame_json = _text(
        state.get("sampling_frame_json"),
        "sampling_frame_json",
        max_length=_MAX_FRAME_BYTES,
    )
    frame = _parse_frame(frame_json)
    frame_sha256 = hashlib.sha256(frame_json.encode("utf-8")).hexdigest()
    stream = _sha(
        structure.member_stream_sha256[member_index],
        "member_stream_sha256",
    )
    selected_index, counter, word_sha256 = _uniform_draw(stream, len(frame))
    selected = frame[selected_index]
    transcript = {
        "schema": "autosport-risk-iid-draw-transcript-v1",
        "experiment_id": structure.experiment_id,
        "member_id": structure.planned_member_ids[member_index],
        "member_index": member_index,
        "stream_sha256": stream,
        "sampling_frame_sha256": structure.sampling_frame_sha256,
        "frame_size": len(frame),
        "draw_counter": counter,
        "draw_word_sha256": word_sha256,
        "selected_index": selected_index,
        "frame_item_id": selected["frame_item_id"],
        "market_sha256": selected["market_sha256"],
        "sealed_results_sha256": selected["sealed_results_sha256"],
    }
    expected = _state_core(
        precommit=precommit,
        structure=structure,
        member_index=member_index,
        frame_json=frame_json,
        selected=selected,
        draw_counter=counter,
        draw_word_sha256=word_sha256,
        draw_transcript_sha256=_digest(transcript),
    )
    if state != expected or frame_sha256 != structure.sampling_frame_sha256:
        raise RiskSamplingOccurrenceError(
            "sampling occurrence state does not match canonical frozen authorities"
        )
    state_sha256 = _digest(state)

    completed_run_bound = False
    item = _run_registry_item(
        root,
        structure.planned_member_ids[member_index],
    )
    if require_completed_run:
        if item is None or item.get("status") != "completed":
            raise RiskSamplingOccurrenceError(
                "sampling occurrence requires the exact completed member run"
            )
        prepared_receipt = _receipt_from_state(
            state,
            state_sha256=state_sha256,
            completed_run_bound=False,
        )
        if (
            item.get("market_sha256") != selected["market_sha256"]
            or item.get("results_sha256") != selected["sealed_results_sha256"]
            or item.get("sampling_occurrence_receipt_sha256")
            != prepared_receipt.receipt_sha256
        ):
            raise RiskSamplingOccurrenceError(
                "completed member run does not bind the selected sampling occurrence"
            )
        tx = _TX_TYPE(
            root,
            structure.planned_member_ids[member_index],
        )
        try:
            _TX_BASE(tx)
            _TX_TERMINAL(tx)
        except (RunTransactionError, OSError, ValueError) as exc:
            raise RiskSamplingOccurrenceError(
                "sampling occurrence completed transaction cannot be re-resolved"
            ) from exc
        _require_dispatch()
        completed_run_bound = True

    return _receipt_from_state(
        state,
        state_sha256=state_sha256,
        completed_run_bound=completed_run_bound,
    )


def resolve_fixed_n_iid_member_occurrence(
    membership: ResolvedFixedNRiskMembership,
    *,
    registry_path: str | Path,
    workspace: str | Path,
    sampling_manifest_json: str,
    member_index: int,
    authority_root: str | Path | None = None,
) -> ProductIidSamplingOccurrenceReceipt:
    """Re-resolve one occurrence and prove the completed run used its exact draw."""

    if type(membership) is not ResolvedFixedNRiskMembership:
        raise TypeError(
            "membership must be exact ResolvedFixedNRiskMembership"
        )
    if type(member_index) is not int or member_index < 0:
        raise RiskSamplingOccurrenceError(
            "member_index must be a non-negative exact int"
        )
    return _resolve_state(
        membership,
        registry_path=registry_path,
        root=_workspace(workspace),
        sampling_manifest_json=sampling_manifest_json,
        member_index=member_index,
        authority_root=authority_root,
        require_completed_run=True,
    )
