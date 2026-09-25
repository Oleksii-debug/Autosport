"""Fail closed on unanchored pre-existing Betfair settlement journals.

#1272 introduces a new settlement-revision journal, so there is no legitimate
legacy journal that may be promoted into authority merely because its local hash
chain is structurally valid.  A non-empty journal must already be covered by the
independent monotonic authority.  Clean empty stores remain bootstrap-able and
normal ingest still establishes PREPARE before publishing the first record.

This is a composition guard over the canonical #1272 store; it does not create a
second journal, settlement projection, or monotonic authority.
"""
from __future__ import annotations

from . import betfair_settlement_revisions as _settlement


_original_ensure_monotonic_current = (
    _settlement.BetfairSettlementRevisionStore._ensure_monotonic_current
)
_error = _settlement.BetfairSettlementRevisionError
_authority_error = _settlement.MonotonicWorkspaceAuthorityError


def _ensure_monotonic_current_without_unanchored_adoption(
    self: _settlement.BetfairSettlementRevisionStore,
    *,
    adopt_if_missing: bool,
) -> None:
    """Reject positive local history when no independent authority exists."""

    observed = self._monotonic_state_digest()
    if observed is not None:
        try:
            history = self._monotonic_authority().read_history()
        except _authority_error as exc:
            raise _error(
                "settlement monotonic authority cannot validate journal baseline"
            ) from exc
        if not history:
            raise _error(
                "non-empty settlement journal lacks independent monotonic authority"
            )

    # This schema is new and has no trusted migration path.  Missing monotonic
    # history is valid only for an empty store; the owning implementation already
    # returns cleanly for that state.  First canonical ingest creates PREPARE before
    # appending, so subsequent reload has history and can recover/commit normally.
    return _original_ensure_monotonic_current(self, adopt_if_missing=False)


if (
    _settlement.BetfairSettlementRevisionStore._ensure_monotonic_current
    is not _original_ensure_monotonic_current
):  # pragma: no cover - import-order corruption guard
    raise RuntimeError("Betfair settlement monotonic baseline dispatch changed")

_settlement.BetfairSettlementRevisionStore._ensure_monotonic_current = (
    _ensure_monotonic_current_without_unanchored_adoption
)
_ensure_monotonic_current_without_unanchored_adoption._autosport_unanchored_baseline_guard = True
