from __future__ import annotations

from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from autosport.smarkets_execution_reconciliation import (
    SmarketsReconciliationError,
    SmarketsReconciliationJournal,
    _decimal_text,
)
from autosport.workspace_lock import WorkspaceEconomicLock


def test_decimal_text_preserves_provider_derived_value_across_ambient_precision() -> None:
    with localcontext() as context:
        context.prec = 50
        value = Decimal(10000) / Decimal(3500)

    expected = "2.8571428571428571428571428571428571428571428571429"
    assert _decimal_text(value) == expected

    for precision in (2, 5, 9, 28, 50):
        with localcontext() as context:
            context.prec = precision
            assert _decimal_text(value) == expected


def test_decimal_text_keeps_compact_fixed_point_projection() -> None:
    assert _decimal_text(Decimal("10.5000")) == "10.5"
    assert _decimal_text(Decimal("0.000")) == "0"
    assert _decimal_text(Decimal("-0.000")) == "-0"


def test_journal_append_is_composed_with_canonical_economic_writer_lock() -> None:
    assert getattr(
        SmarketsReconciliationJournal.append,
        "_autosport_economic_writer_locked",
        False,
    ) is True


def test_journal_append_fails_closed_during_competing_economic_writer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)

    # Holding the canonical workspace lock simulates another cooperating process
    # inside an economic mutation. The Smarkets journal must not reach its own
    # load/validate/append section and must not acknowledge a competing write.
    with WorkspaceEconomicLock(tmp_path):
        with pytest.raises(
            SmarketsReconciliationError,
            match="economic-writer lock",
        ):
            journal.append(object())  # type: ignore[arg-type]

    assert not path.exists()

    # Once contention is gone, the wrapper releases normally and the owning
    # journal's original input validation is reached. This proves a failed lock
    # acquisition does not poison the journal object for a later retry.
    with pytest.raises(
        SmarketsReconciliationError,
        match="journal accepts only VerifiedSmarketsOrderEffect",
    ):
        journal.append(object())  # type: ignore[arg-type]
