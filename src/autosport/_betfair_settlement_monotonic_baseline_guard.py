"""Fail closed on unanchored pre-existing Betfair settlement journals.

#1272 introduces a new settlement-revision journal, so there is no legitimate
legacy journal that may be promoted into authority merely because its local hash
chain is structurally valid. A non-empty journal must already be covered by the
independent monotonic authority. Clean empty stores remain bootstrap-able and
normal ingest still establishes PREPARE before publishing the first record.

This is a composition guard over the canonical #1272 store; it does not create a
second journal, settlement projection, or monotonic authority.
"""
from __future__ import annotations

from . import betfair_settlement_revisions as _settlement


_store_type = _settlement.BetfairSettlementRevisionStore
_original_ensure_monotonic_current = _store_type._ensure_monotonic_current
_error = _settlement.BetfairSettlementRevisionError
_authority_error = _settlement.MonotonicWorkspaceAuthorityError

_state_digest_descriptor = vars(_store_type).get("_monotonic_state_digest")
_authority_descriptor = vars(_store_type).get("_monotonic_authority")
if not callable(_state_digest_descriptor) or not callable(_authority_descriptor):
    raise RuntimeError("Betfair settlement monotonic authority dispatch is unavailable")
_state_digest_code = getattr(_state_digest_descriptor, "__code__", None)
_authority_code = getattr(_authority_descriptor, "__code__", None)
if _state_digest_code is None or _authority_code is None:
    raise RuntimeError("Betfair settlement monotonic authority code is unavailable")


def _require_canonical_monotonic_dispatch() -> None:
    current_digest = vars(_store_type).get("_monotonic_state_digest")
    current_authority = vars(_store_type).get("_monotonic_authority")
    if (
        current_digest is not _state_digest_descriptor
        or getattr(current_digest, "__code__", None) is not _state_digest_code
        or current_authority is not _authority_descriptor
        or getattr(current_authority, "__code__", None) is not _authority_code
    ):
        raise _error("settlement monotonic authority dispatch changed")


def _ensure_monotonic_current_without_unanchored_adoption(
    self: _settlement.BetfairSettlementRevisionStore,
    *,
    adopt_if_missing: bool,
) -> None:
    """Reject positive local history when no independent authority exists."""

    if type(self) is not _store_type:
        raise _error("settlement store must be the canonical concrete type")
    _require_canonical_monotonic_dispatch()
    observed = _state_digest_descriptor(self)
    _require_canonical_monotonic_dispatch()
    if observed is not None:
        try:
            history = _authority_descriptor(self).read_history()
        except _authority_error as exc:
            raise _error(
                "settlement monotonic authority cannot validate journal baseline"
            ) from exc
        _require_canonical_monotonic_dispatch()
        if not history:
            raise _error(
                "non-empty settlement journal lacks independent monotonic authority"
            )

    # This schema is new and has no trusted migration path. Missing monotonic
    # history is valid only for an empty store; the owning implementation already
    # returns cleanly for that state. First canonical ingest creates PREPARE before
    # appending, so subsequent reload has history and can recover/commit normally.
    _require_canonical_monotonic_dispatch()
    result = _original_ensure_monotonic_current(self, adopt_if_missing=False)
    _require_canonical_monotonic_dispatch()
    return result


if _store_type._ensure_monotonic_current is not _original_ensure_monotonic_current:
    raise RuntimeError("Betfair settlement monotonic baseline dispatch changed")

_store_type._ensure_monotonic_current = (
    _ensure_monotonic_current_without_unanchored_adoption
)
_ensure_monotonic_current_without_unanchored_adoption._autosport_unanchored_baseline_guard = True
