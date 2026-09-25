"""Seal Smarkets reconciliation runtime invariants at package composition.

The owning reconciliation module historically had two narrow ambient-process seams:

* ``Decimal.normalize()`` made durable Decimal text depend on the caller's active
  arithmetic context; and
* the append-only reconciliation journal performed ``load -> validate -> append``
  without the canonical cross-process economic-writer lock. Two cooperating writers
  could therefore derive the same chain head and both acknowledge appends whose
  combined journal was invalid after restart.

Keep both repairs compositional. Provider-native integer economics, hash-chain
validation and record semantics remain owned by ``smarkets_execution_reconciliation``;
writer serialization reuses Autosport's existing ``WorkspaceEconomicLock`` rather
than creating a second lock authority.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import smarkets_execution_reconciliation as _target
from .workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


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


_ORIGINAL_JOURNAL_APPEND = _target.SmarketsReconciliationJournal.append


def _locked_journal_append(
    self: _target.SmarketsReconciliationJournal,
    effect: _target.VerifiedSmarketsOrderEffect,
) -> None:
    """Serialize one complete hash-chain mutation under the canonical economic lock."""

    try:
        with WorkspaceEconomicLock(self.path.parent):
            _ORIGINAL_JOURNAL_APPEND(self, effect)
    except WorkspaceEconomicLockError as exc:
        raise _target.SmarketsReconciliationError(
            "cannot acquire Smarkets reconciliation economic-writer lock"
        ) from exc


def _install() -> None:
    current_text = _target._decimal_text
    if not getattr(current_text, "_autosport_context_independent_decimal_text", False):
        _exact_decimal_text._autosport_context_independent_decimal_text = True  # type: ignore[attr-defined]
        _target._decimal_text = _exact_decimal_text

    current_append: Any = _target.SmarketsReconciliationJournal.append
    if not getattr(current_append, "_autosport_economic_writer_locked", False):
        _locked_journal_append._autosport_economic_writer_locked = True  # type: ignore[attr-defined]
        _target.SmarketsReconciliationJournal.append = _locked_journal_append


_install()
del _install
