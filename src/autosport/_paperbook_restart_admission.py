"""Compose an in-memory product PaperBook with a verified durable lineage.

The durable snapshot witness authenticates the loaded copy. Some existing product
runtimes intentionally keep a separately constructed in-memory PaperBook and compare
it with that copy before continuing. This helper transfers only the *path binding* to
such an exact, already-authoritative equivalent object; it never derives opening or
causal authority from snapshot bytes.
"""

from __future__ import annotations

from pathlib import Path

from . import paper as _paper
from . import _paperbook_preload_authority_guard as _guard


_PAPER_BOOK = _paper.PaperBook
_REQUIRE_OPENING = _paper._require_ticket_opening_authority
_REQUIRE_CAUSAL = _paper._require_paperbook_causal_history_authority
_VALIDATE = _paper.PaperBook._validate_loaded_state


def bind_equivalent_product_book(
    candidate: _paper.PaperBook,
    verified: _paper.PaperBook,
    path: str | Path,
) -> None:
    """Bind ``candidate`` only when it exactly equals witnessed ``verified`` state."""

    if type(candidate) is not _PAPER_BOOK or type(verified) is not _PAPER_BOOK:
        raise TypeError("restart admission requires canonical PaperBook values")
    destination = Path(path)

    # The source copy must itself have come through exact witnessed path admission.
    _guard._require_bound_book(verified, destination)

    # The candidate must already own product-issued in-process opening/causal
    # authority. A structural load_bytes object cannot be upgraded through this seam.
    _REQUIRE_OPENING(candidate)
    _REQUIRE_CAUSAL(candidate)
    _VALIDATE(candidate)

    if (
        candidate.initial_bankroll != verified.initial_bankroll
        or candidate.balance != verified.balance
        or candidate.tickets != verified.tickets
        or candidate._lifecycle != verified._lifecycle
        or candidate._settlement_times != verified._settlement_times
    ):
        raise ValueError(
            "configured PaperBook does not exactly match witnessed durable state"
        )

    _guard._bind_book(candidate, destination)
