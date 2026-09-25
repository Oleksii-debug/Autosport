"""Seal exact Smarkets Decimal journal serialization against ambient context.

The owning reconciliation module used ``Decimal.normalize()`` before formatting.
``normalize()`` applies the current Decimal context, so a provider-derived value
computed under the module's precision-50 conversion could be silently rounded when
serialized under the process-default precision (or a caller-mutated lower precision).
Durable replay then recomputed the exact value and rejected its own journal record.

Keep this repair narrow: change only the text projection helper; provider native
integer units, arithmetic precision, record hashes and replay verification remain
owned by ``smarkets_execution_reconciliation``.
"""
from __future__ import annotations

from decimal import Decimal

from . import smarkets_execution_reconciliation as _target


def _exact_decimal_text(value: Decimal) -> str:
    """Return exact fixed-point Decimal text without context-sensitive arithmetic."""

    if type(value) is not Decimal:
        raise TypeError("Smarkets durable decimal value must be an exact Decimal")
    # Decimal.__format__('f') projects the stored coefficient/exponent exactly and
    # does not round through the active arithmetic Context. Strip only fractional
    # trailing zeroes so canonical text retains the owning helper's compact form.
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _install() -> None:
    current = _target._decimal_text
    if getattr(current, "_autosport_context_independent_decimal_text", False):
        return
    _exact_decimal_text._autosport_context_independent_decimal_text = True  # type: ignore[attr-defined]
    _target._decimal_text = _exact_decimal_text


_install()
del _install
