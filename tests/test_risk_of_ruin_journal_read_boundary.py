from __future__ import annotations

from pathlib import Path

import pytest

import autosport.risk_of_ruin_evaluator as risk_module
from autosport.risk_of_ruin_evaluator import (
    ProductRiskOfRuinEvaluator,
    RiskOfRuinIssuanceError,
)


def _evaluator(tmp_path: Path) -> ProductRiskOfRuinEvaluator:
    return ProductRiskOfRuinEvaluator(
        workspace=(tmp_path / "workspace").resolve(),
        authority_root=(tmp_path / "authority").resolve(),
    )


def test_stable_journal_reader_returns_exact_regular_file_bytes(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    payload = b'{"records":[]}\n'
    journal.write_bytes(payload)

    assert risk_module._read_stable_journal_bytes(journal) == payload


def test_duplicate_json_keys_fail_before_journal_authority_recovery(
    tmp_path: Path,
) -> None:
    evaluator = _evaluator(tmp_path)
    workspace_id = evaluator.authority.workspace_instance_id
    evaluator.journal_path.write_text(
        "{"
        '"schema":"autosport.risk-of-ruin-product-evaluator-journal.v1",'
        '"schema":"autosport.risk-of-ruin-product-evaluator-journal.v1",'
        '"schema_version":1,'
        f'"workspace_instance_id":"{workspace_id}",'
        '"records":[]'
        "}",
        encoding="utf-8",
    )

    with pytest.raises(RiskOfRuinIssuanceError, match="journal is unreadable"):
        evaluator._read_state_under_lock()


def test_oversized_journal_fails_before_materialization(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    with journal.open("wb") as handle:
        handle.truncate(risk_module._MAX_JOURNAL_BYTES + 1)

    with pytest.raises(
        RiskOfRuinIssuanceError,
        match="journal exceeds supported size",
    ):
        risk_module._read_stable_journal_bytes(journal)
