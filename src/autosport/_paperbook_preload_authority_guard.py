"""Fail closed PaperBook path loads at the durable authority boundary.

A PaperBook snapshot is caller-editable persistence. Structural validation can prove
that its JSON is internally coherent, but it cannot prove that the persisted opening
economics or causal history are the product-issued values that existed before a
restart. Therefore a path load must not mint the private positive authorities used by
``committed_stake``, ``open_ticket`` or ``settle`` from those same bytes.

This guard deliberately keeps the existing parser/validator as the single structural
authority. It only removes the unsafe promotion performed by ``PaperBook.load``.
Callers that need positive restart admission must compose the parsed snapshot with an
independent canonical durable authority before economic use.
"""

from __future__ import annotations

from pathlib import Path

from . import paper as _paper


_PAPER_BOOK = _paper.PaperBook
_LOAD_BYTES_DESCRIPTOR = vars(_PAPER_BOOK)["load_bytes"]
_LOAD_BYTES = _LOAD_BYTES_DESCRIPTOR.__func__
_PATH = Path


def _structural_path_load(cls, path: str | Path):
    """Decode and structurally validate a snapshot without minting economic authority."""

    if cls is not _PAPER_BOOK:
        raise TypeError("PaperBook.load requires the canonical PaperBook class")
    payload = _PATH(path).read_bytes()
    # Invoke the captured canonical implementation directly instead of dispatching
    # through a caller-rebound ``cls.load_bytes`` slot.
    return _LOAD_BYTES(cls, payload)


_structural_path_load.__name__ = "load"
_structural_path_load.__qualname__ = "PaperBook.load"
_structural_path_load.__doc__ = (
    "Load and structurally validate a PaperBook snapshot. The returned object is "
    "not positive economic authority until independently re-authorized."
)

# Preserve exactly one PaperBook parser/validator implementation; only replace the
# unsafe promotion boundary after ``paper`` has finished defining the canonical type.
_PAPER_BOOK.load = classmethod(_structural_path_load)
