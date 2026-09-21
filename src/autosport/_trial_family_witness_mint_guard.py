"""Reserve trial-family cross-ledger witness publication to product authority.

The trial journal must recognize SEQUENTIAL_LOOK_REGISTERED during replay, but the
legacy generic _append_event seam must never let a caller mint that authority. The
cross-ledger publisher is the only supported writer because it first proves exact
durable native sequential evidence and captures the canonical registry prefix under
the shared workspace lock.
"""

from __future__ import annotations

from typing import Any, Callable

from . import trial_family_accounting as _tfa
from . import _trial_family_cross_ledger_witness as _witness

_ORIGINAL_APPEND_EVENT = _tfa.TrialFamilyAccountingStore._append_event


def _append_event_without_reserved_witness_mint(
    self: _tfa.TrialFamilyAccountingStore,
    kind: str,
    event_at: str,
    payload: dict[str, Any],
    *,
    locked_payload_factory: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> None:
    if kind == _witness._WITNESS_KIND:
        raise ValueError(
            "SEQUENTIAL_LOOK_REGISTERED is reserved for product-owned cross-ledger witness publication"
        )
    return _ORIGINAL_APPEND_EVENT(
        self,
        kind,
        event_at,
        payload,
        locked_payload_factory=locked_payload_factory,
    )


_tfa.TrialFamilyAccountingStore._append_event = _append_event_without_reserved_witness_mint

__all__: list[str] = []
