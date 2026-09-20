from __future__ import annotations

"""Fail-closed desktop acknowledgement reads for collector retention.

Retention physically deletes acknowledged historical collector rows. Exact concrete
``DesktopDeltaCheckpointStore`` type checks are necessary but not sufficient because
ordinary Python instance attributes can shadow its non-data-descriptor methods and
Python classes themselves are mutable at runtime. A caller holding acknowledgement
state must therefore not be able to replace ``has_ack`` / ``application_receipt`` at
either the instance or class level and mint deletion authority.

This package-installed guard keeps the existing retention implementation and durable
store as the sole authorities. It captures the pristine exact class implementations
once in the defining legacy module, rejects later class/instance rebinding, and routes
authority-bearing reads through those captured callables. The anchor lives outside
this guard module so reloading this installer or ``collector_retention`` cannot
silently recapture a forged implementation as pristine.
"""

from . import causal_collector_legacy as _legacy
from . import collector_retention as _retention


DesktopDeltaCheckpointStore = _legacy.DesktopDeltaCheckpointStore
_ORIGINAL_BUILD_PLAN = _retention.CollectorRetentionManager._build_plan
_AUTHORITY_METHODS = ("has_ack", "application_receipt")
_PRISTINE_ANCHOR_NAME = "_collector_retention_pristine_desktop_ack_v1"


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


_PRISTINE_HAS_ACK, _PRISTINE_APPLICATION_RECEIPT = _load_pristine_authority_methods()


def _require_desktop_checkpoint(
    checkpoint: DesktopDeltaCheckpointStore,
) -> DesktopDeltaCheckpointStore:
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
    return checkpoint


class _TrustedDesktopCheckpointView:
    """Pin acknowledgement dispatch to captured pristine class implementations."""

    __slots__ = ("_checkpoint",)

    def __init__(self, checkpoint: DesktopDeltaCheckpointStore) -> None:
        self._checkpoint = _require_desktop_checkpoint(checkpoint)

    def has_ack(self, delta_id: str) -> bool:
        _require_desktop_checkpoint(self._checkpoint)
        return _PRISTINE_HAS_ACK(self._checkpoint, delta_id)

    def application_receipt(self, delta):
        _require_desktop_checkpoint(self._checkpoint)
        return _PRISTINE_APPLICATION_RECEIPT(self._checkpoint, delta)


def _build_plan(
    self: _retention.CollectorRetentionManager,
    connection,
    *,
    source_id: str,
    stream_epoch: str,
    desktop_checkpoint: DesktopDeltaCheckpointStore,
) -> _retention.CollectorRetentionPlan:
    checkpoint = _require_desktop_checkpoint(desktop_checkpoint)
    trusted_view = _TrustedDesktopCheckpointView(checkpoint)
    return _ORIGINAL_BUILD_PLAN(
        self,
        connection,
        source_id=source_id,
        stream_epoch=stream_epoch,
        desktop_checkpoint=trusted_view,
    )


# The package imports this guard before public consumers run. Patch both entry
# points: preview/compact reject a substitution immediately, while _build_plan also
# revalidates and pins dispatch in case authority methods are mutated after the
# outer check.
_retention.CollectorRetentionManager._require_desktop_checkpoint = staticmethod(
    _require_desktop_checkpoint
)
_retention.CollectorRetentionManager._build_plan = _build_plan
