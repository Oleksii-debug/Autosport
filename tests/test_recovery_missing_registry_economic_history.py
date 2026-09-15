from __future__ import annotations

from pathlib import Path

import pytest

from autosport.recovery import RecoveryReport, reconcile_late_crashes
from autosport.run_registry import ReconciliationError
from autosport.run_transaction import RunTransaction


def test_missing_registry_with_paper_book_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "paper_book.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        ReconciliationError,
        match="run registry is missing while durable run history exists",
    ):
        reconcile_late_crashes(tmp_path)

    assert not (tmp_path / "run_registry.json").exists()


def test_missing_registry_with_nonempty_decision_ledger_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        ReconciliationError,
        match="run registry is missing while durable run history exists",
    ):
        reconcile_late_crashes(tmp_path)

    assert not (tmp_path / "run_registry.json").exists()


def test_missing_registry_with_nonregular_decision_ledger_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "decisions.jsonl").mkdir()

    with pytest.raises(
        ReconciliationError,
        match="run registry is missing while durable run history exists",
    ):
        reconcile_late_crashes(tmp_path)

    assert not (tmp_path / "run_registry.json").exists()


def test_pristine_zero_byte_ledger_and_empty_transaction_root_are_recovery_compatible(
    tmp_path: Path,
) -> None:
    (tmp_path / "decisions.jsonl").write_bytes(b"")
    (tmp_path / RunTransaction.ROOT_NAME).mkdir()

    assert reconcile_late_crashes(tmp_path) == RecoveryReport((), (), ())
    assert not (tmp_path / "run_registry.json").exists()
