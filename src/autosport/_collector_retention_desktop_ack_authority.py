from __future__ import annotations

"""Fail-closed desktop acknowledgement reads for collector retention.

Retention physically deletes acknowledged historical collector rows. Exact concrete
``DesktopDeltaCheckpointStore`` type checks are necessary but not sufficient because
ordinary Python instance attributes can shadow its non-data-descriptor methods. A
caller holding a real checkpoint instance must therefore not be able to replace
``has_ack`` / ``application_receipt`` and mint deletion authority.

This package-installed guard keeps the existing retention implementation and durable
store as the sole authorities. It rejects instance-shadowed acknowledgement methods
at every retention read boundary and routes the actual reads through the trusted exact
class implementations, so a later instance mutation cannot affect authority-bearing
dispatch between validation and use.
"""

from . import collector_retention as _retention
from .causal_collector_legacy import DesktopDeltaCheckpointStore


_ORIGINAL_BUILD_PLAN = _retention.CollectorRetentionManager._build_plan
_AUTHORITY_METHODS = ("has_ack", "application_receipt")


def _require_desktop_checkpoint(
    checkpoint: DesktopDeltaCheckpointStore,
) -> DesktopDeltaCheckpointStore:
    if type(checkpoint) is not DesktopDeltaCheckpointStore:
        raise TypeError(
            "desktop_checkpoint must be the canonical DesktopDeltaCheckpointStore"
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
    """Pin acknowledgement dispatch to exact canonical class implementations."""

    __slots__ = ("_checkpoint",)

    def __init__(self, checkpoint: DesktopDeltaCheckpointStore) -> None:
        self._checkpoint = _require_desktop_checkpoint(checkpoint)

    def has_ack(self, delta_id: str) -> bool:
        return DesktopDeltaCheckpointStore.has_ack(self._checkpoint, delta_id)

    def application_receipt(self, delta):
        return DesktopDeltaCheckpointStore.application_receipt(self._checkpoint, delta)


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
# points: preview/compact reject a shadow immediately, while _build_plan also
# revalidates and pins dispatch in case an instance is mutated after the outer check.
_retention.CollectorRetentionManager._require_desktop_checkpoint = staticmethod(
    _require_desktop_checkpoint
)
_retention.CollectorRetentionManager._build_plan = _build_plan
