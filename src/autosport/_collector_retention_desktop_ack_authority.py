from __future__ import annotations

"""Fail-closed desktop acknowledgement authority for collector retention.

Physical retention is allowed to delete historical collector rows, so acknowledgement
reads must come from the product-owned desktop checkpoint in the collector workspace.
Exact class checks alone are not enough: Python instance/class methods are mutable and
an arbitrary exact checkpoint can be pointed at a second file.

This installer therefore seals three things at the deletion boundary:
- pristine class method identity is anchored outside this reloadable module;
- authority reads use a guard-owned strict JSON decoder, not mutable checkpoint method
  dispatch;
- the checkpoint file is re-discovered from the collector workspace and must be the
  single durable DesktopDeltaCheckpointStore-shaped file there. Alternate exact stores,
  symlinks, path substitution, and ambiguous multiple checkpoint stores fail closed.

The same installer also closes the legacy public ACK write seam. A caller-authored
``DesktopApplicationReceipt`` is assertion data, never positive acknowledgement
authority. Positive ACK publication is admitted only while the canonical
``DesktopDeltaConsumer`` owns the handoff, and the exact delta is re-resolved from the
consumer's durable collector before the original checkpoint writer can run.

The canonical autonomous product composition already places its collector and
``desktop_acks.json`` under one deterministic workspace. Focused standalone tests use
that same one-workspace invariant without hard-coding a production filename.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import causal_collector_legacy as _legacy
from . import collector_retention as _retention
from .json_integrity import strict_json_loads


DesktopDeltaCheckpointStore = _legacy.DesktopDeltaCheckpointStore
_AUTHORITY_METHODS = ("has_ack", "application_receipt")
_PRISTINE_ANCHOR_NAME = "_collector_retention_pristine_desktop_ack_v1"
_BUILD_PLAN_ANCHOR_NAME = "_collector_retention_original_build_plan_v1"
_ACK_WRITE_ANCHOR_NAME = "_collector_desktop_ack_write_authority_v1"


def _load_pristine_authority_methods():
    existing = getattr(_legacy, _PRISTINE_ANCHOR_NAME, None)
    if existing is None:
        existing = (
            DesktopDeltaCheckpointStore,
            DesktopDeltaCheckpointStore.has_ack,
            DesktopDeltaCheckpointStore.application_receipt,
        )
        setattr(_legacy, _PRISTINE_ANCHOR_NAME, existing)
    if (
        not isinstance(existing, tuple)
        or len(existing) != 3
        or existing[0] is not DesktopDeltaCheckpointStore
        or not callable(existing[1])
        or not callable(existing[2])
    ):
        raise RuntimeError("desktop checkpoint acknowledgement authority anchor is invalid")
    return existing[1], existing[2]


def _load_original_build_plan():
    existing = getattr(_retention, _BUILD_PLAN_ANCHOR_NAME, None)
    if existing is None:
        existing = _retention.CollectorRetentionManager._build_plan
        setattr(_retention, _BUILD_PLAN_ANCHOR_NAME, existing)
    if not callable(existing):
        raise RuntimeError("collector retention build-plan authority anchor is invalid")
    return existing


def _load_ack_write_authority_methods():
    existing = getattr(_legacy, _ACK_WRITE_ANCHOR_NAME, None)
    if existing is None:
        existing = (
            DesktopDeltaCheckpointStore,
            DesktopDeltaCheckpointStore.ack,
            DesktopDeltaCheckpointStore._ack_locked,
            _legacy.DesktopDeltaConsumer,
            _legacy.DesktopDeltaConsumer.drain,
        )
        setattr(_legacy, _ACK_WRITE_ANCHOR_NAME, existing)
    if (
        not isinstance(existing, tuple)
        or len(existing) != 5
        or existing[0] is not DesktopDeltaCheckpointStore
        or existing[3] is not _legacy.DesktopDeltaConsumer
        or not callable(existing[1])
        or not callable(existing[2])
        or not callable(existing[4])
    ):
        raise RuntimeError("desktop checkpoint write authority anchor is invalid")
    return existing[1], existing[2], existing[4]


_PRISTINE_HAS_ACK, _PRISTINE_APPLICATION_RECEIPT = _load_pristine_authority_methods()
_ORIGINAL_BUILD_PLAN = _load_original_build_plan()
_ORIGINAL_PUBLIC_ACK, _ORIGINAL_ACK_LOCKED, _ORIGINAL_CONSUMER_DRAIN = (
    _load_ack_write_authority_methods()
)


def _strict_checkpoint_payload(path: Path) -> dict[str, object]:
    try:
        raw = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise TypeError("desktop checkpoint authority file is invalid") from exc
    if (
        type(raw) is not dict
        or type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != 1
        or type(raw.get("acks")) is not list
        or type(raw.get("streams")) is not dict
    ):
        raise TypeError("desktop checkpoint authority file is invalid")
    return raw


def _workspace_checkpoint_candidates(manager: _retention.CollectorRetentionManager) -> tuple[Path, ...]:
    collector_path = Path(manager.collector.path)
    try:
        workspace = collector_path.parent.resolve(strict=True)
        entries = tuple(workspace.iterdir())
    except OSError as exc:
        raise TypeError("collector workspace cannot be verified for desktop acknowledgement authority") from exc

    candidates: list[Path] = []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            raw = strict_json_loads(entry.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, TypeError, ValueError):
            continue
        if (
            type(raw) is dict
            and type(raw.get("schema_version")) is int
            and raw.get("schema_version") == 1
            and type(raw.get("acks")) is list
            and type(raw.get("streams")) is dict
        ):
            try:
                candidates.append(entry.resolve(strict=True))
            except OSError as exc:
                raise TypeError("desktop checkpoint authority path cannot be resolved") from exc
    return tuple(sorted(set(candidates), key=str))


def _verified_checkpoint_path(
    manager: _retention.CollectorRetentionManager,
    checkpoint: DesktopDeltaCheckpointStore,
) -> Path:
    if type(checkpoint) is not DesktopDeltaCheckpointStore:
        raise TypeError(
            "desktop_checkpoint must be the canonical DesktopDeltaCheckpointStore"
        )
    if (
        DesktopDeltaCheckpointStore.has_ack is not _PRISTINE_HAS_ACK
        or DesktopDeltaCheckpointStore.application_receipt
        is not _PRISTINE_APPLICATION_RECEIPT
    ):
        raise TypeError(
            "desktop checkpoint acknowledgement authority methods cannot be class-rebound"
        )
    namespace = getattr(checkpoint, "__dict__", None)
    if isinstance(namespace, dict) and any(
        method_name in namespace for method_name in _AUTHORITY_METHODS
    ):
        raise TypeError(
            "desktop_checkpoint acknowledgement authority methods cannot be instance-shadowed"
        )

    candidate_path = getattr(checkpoint, "path", None)
    if not isinstance(candidate_path, (str, Path)):
        raise TypeError("desktop checkpoint authority path is invalid")
    candidate_path = Path(candidate_path)
    try:
        if candidate_path.is_symlink():
            raise TypeError("desktop checkpoint authority path cannot be a symlink")
        resolved = candidate_path.resolve(strict=True)
    except OSError as exc:
        raise TypeError("desktop checkpoint authority path cannot be resolved") from exc

    candidates = _workspace_checkpoint_candidates(manager)
    if len(candidates) != 1:
        raise TypeError(
            "collector workspace must contain exactly one canonical desktop checkpoint authority"
        )
    if resolved != candidates[0]:
        raise TypeError(
            "desktop_checkpoint is not the product-owned canonical workspace checkpoint"
        )
    _strict_checkpoint_payload(resolved)
    return resolved


def _require_desktop_checkpoint(
    manager: _retention.CollectorRetentionManager,
    checkpoint: DesktopDeltaCheckpointStore,
) -> DesktopDeltaCheckpointStore:
    _verified_checkpoint_path(manager, checkpoint)
    return checkpoint


@dataclass(frozen=True, slots=True)
class _TrustedReceipt:
    delta_id: str
    canonical_event_digest: str
    receipt_id: str
    applied_at: str


class _TrustedDesktopCheckpointView:
    """Read only the verified workspace checkpoint without mutable method dispatch."""

    __slots__ = ("_manager", "_checkpoint")

    def __init__(
        self,
        manager: _retention.CollectorRetentionManager,
        checkpoint: DesktopDeltaCheckpointStore,
    ) -> None:
        self._manager = manager
        self._checkpoint = _require_desktop_checkpoint(manager, checkpoint)

    def _raw(self) -> dict[str, object]:
        path = _verified_checkpoint_path(self._manager, self._checkpoint)
        return _strict_checkpoint_payload(path)

    def has_ack(self, delta_id: str) -> bool:
        if type(delta_id) is not str or not delta_id.strip():
            raise ValueError("delta_id must be a non-empty string")
        raw = self._raw()
        return any(
            type(item) is dict and item.get("delta_id") == delta_id
            for item in raw["acks"]
        )

    def application_receipt(self, delta):
        raw = self._raw()
        for item in raw["acks"]:
            if type(item) is not dict or item.get("delta_id") != delta.delta_id:
                continue
            receipt_id = item.get("application_receipt_id")
            if type(receipt_id) is not str or not receipt_id.strip():
                continue
            digest = item.get("canonical_event_digest")
            applied_at = item.get("applied_at")
            if type(digest) is not str or len(digest) != 64:
                raise TypeError("desktop checkpoint receipt digest is invalid")
            try:
                int(digest, 16)
            except ValueError as exc:
                raise TypeError("desktop checkpoint receipt digest is invalid") from exc
            if type(applied_at) is not str or not applied_at.strip():
                raise TypeError("desktop checkpoint receipt applied_at is invalid")
            try:
                instant = datetime.fromisoformat(applied_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise TypeError("desktop checkpoint receipt applied_at is invalid") from exc
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise TypeError("desktop checkpoint receipt applied_at is invalid")
            return _TrustedReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=digest,
                receipt_id=receipt_id,
                applied_at=applied_at,
            )
        return None


def _build_plan(
    self: _retention.CollectorRetentionManager,
    connection,
    *,
    source_id: str,
    stream_epoch: str,
    desktop_checkpoint: DesktopDeltaCheckpointStore,
) -> _retention.CollectorRetentionPlan:
    checkpoint = _require_desktop_checkpoint(self, desktop_checkpoint)
    trusted_view = _TrustedDesktopCheckpointView(self, checkpoint)
    return _ORIGINAL_BUILD_PLAN(
        self,
        connection,
        source_id=source_id,
        stream_epoch=stream_epoch,
        desktop_checkpoint=trusted_view,
    )


# Install as an instance method so the authority check is bound to the exact collector
# workspace. preview()/compact() both re-run it, and the trusted view re-runs it before
# every acknowledgement read, closing path substitution between validation and use.
_retention.CollectorRetentionManager._require_desktop_checkpoint = _require_desktop_checkpoint
_retention.CollectorRetentionManager._build_plan = _build_plan


# ---------------------------------------------------------------------------
# Desktop ACK write authority
# ---------------------------------------------------------------------------

# The token contains exact object identities, not caller-authored DTO content. It is
# installed only around the already-established DesktopDeltaConsumer.drain handoff.
_ACK_CONTEXT: ContextVar[tuple[DesktopDeltaCheckpointStore, object] | None] = ContextVar(
    "autosport_desktop_ack_consumer_authority",
    default=None,
)


def _collector_call(collector: object, method_name: str, /, *args, **kwargs):
    namespace = getattr(collector, "__dict__", None)
    if isinstance(namespace, dict) and method_name in namespace:
        raise _legacy.ApplicationReceiptError(
            f"collector {method_name} authority cannot be instance-shadowed"
        )
    method = getattr(type(collector), method_name, None)
    if not callable(method):
        raise _legacy.ApplicationReceiptError(
            f"collector lacks canonical {method_name} authority"
        )
    return method(collector, *args, **kwargs)


def _canonical_commit_prefix(collector: object, delta: _legacy.CollectorDelta):
    after_delta_id: str | None = None
    prior: list[_legacy.CollectorDelta] = []
    while True:
        page = _collector_call(
            collector,
            "deltas_after_commit",
            source_id=delta.source_id,
            after_delta_id=after_delta_id,
            max_items=1000,
        )
        if not isinstance(page, tuple):
            raise _legacy.ApplicationReceiptError(
                "collector durable commit authority returned an invalid page"
            )
        if not page:
            break
        for item in page:
            if not isinstance(item, _legacy.CollectorDelta):
                raise _legacy.ApplicationReceiptError(
                    "collector durable commit authority returned a non-delta"
                )
            if item.delta_id == delta.delta_id:
                return tuple(prior)
            prior.append(item)
        after_delta_id = page[-1].delta_id
        if len(page) < 1000:
            break
    raise _legacy.ApplicationReceiptError(
        "desktop ACK delta is not present in canonical durable commit order"
    )


def _require_contiguous_predecessors(
    checkpoint: DesktopDeltaCheckpointStore,
    collector: object,
    delta: _legacy.CollectorDelta,
) -> None:
    prior = _canonical_commit_prefix(collector, delta)
    same_epoch = [
        item
        for item in prior
        if item.source_id == delta.source_id
        and item.stream_epoch == delta.stream_epoch
        and item.cursor_position < delta.cursor_position
    ]
    for item in same_epoch:
        if _PRISTINE_HAS_ACK(checkpoint, item.delta_id):
            continue
        if item.gap_state is _legacy.GapState.DETECTED:
            recovered = any(
                candidate.gap_state is _legacy.GapState.RECOVERED
                and candidate.revision_of == item.delta_id
                and candidate.source_id == item.source_id
                and candidate.stream_epoch == item.stream_epoch
                and candidate.cursor_position == item.cursor_position
                for candidate in prior
            )
            if recovered:
                continue
        raise _legacy.ApplicationReceiptError(
            "desktop ACK cannot skip an unacknowledged durable predecessor"
        )


def _guarded_public_ack(
    self: DesktopDeltaCheckpointStore,
    delta: _legacy.CollectorDelta,
    *,
    application_receipt: _legacy.DesktopApplicationReceipt,
    acknowledged_at: str,
) -> bool:
    del self, delta, application_receipt, acknowledged_at
    raise _legacy.ApplicationReceiptError(
        "public checkpoint.ack cannot mint positive authority; use DesktopDeltaConsumer"
    )


def _guarded_ack_locked(
    self: DesktopDeltaCheckpointStore,
    delta: _legacy.CollectorDelta,
    *,
    application_receipt: _legacy.DesktopApplicationReceipt,
    acknowledged_at: str,
) -> bool:
    authority = _ACK_CONTEXT.get()
    if authority is None or authority[0] is not self:
        raise _legacy.ApplicationReceiptError(
            "desktop ACK write requires the canonical consumer handoff"
        )
    collector = authority[1]
    canonical_delta = _collector_call(collector, "get", delta.delta_id)
    if canonical_delta is None or canonical_delta != delta:
        raise _legacy.ApplicationReceiptError(
            "desktop ACK assertion does not match canonical collector evidence"
        )
    _require_contiguous_predecessors(self, collector, canonical_delta)
    return _ORIGINAL_ACK_LOCKED(
        self,
        canonical_delta,
        application_receipt=application_receipt,
        acknowledged_at=acknowledged_at,
    )


def _guarded_consumer_drain(
    self: _legacy.DesktopDeltaConsumer,
    *,
    as_of: str,
    view: _legacy.CausalView = _legacy.CausalView.AS_KNOWN_AT_DECISION,
) -> tuple[str, ...]:
    token = _ACK_CONTEXT.set((self.checkpoint, self.collector))
    try:
        return _ORIGINAL_CONSUMER_DRAIN(self, as_of=as_of, view=view)
    finally:
        _ACK_CONTEXT.reset(token)


# Preserve exact class identities used throughout the collector/retention system and
# install only method guards, consistent with the repository's other authority fences.
DesktopDeltaCheckpointStore.ack = _guarded_public_ack
DesktopDeltaCheckpointStore._ack_locked = _guarded_ack_locked
_legacy.DesktopDeltaConsumer.drain = _guarded_consumer_drain
