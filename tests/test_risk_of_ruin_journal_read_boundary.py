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


def test_stable_journal_reader_returns_exact_regular_file_bytes(
    tmp_path: Path,
) -> None:
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


def test_swap_to_symlink_at_open_is_rejected_by_no_follow_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = tmp_path / "journal.json"
    journal.write_text('{"original":true}', encoding="utf-8")
    original_file = tmp_path / "journal.original.json"
    target = tmp_path / "replacement-target.json"
    target.write_text('{"replacement":true}', encoding="utf-8")

    canonical_open = risk_module._open_read_only_descriptor
    swap_attempted = False

    def swap_then_open(path: Path) -> int:
        nonlocal swap_attempted
        swap_attempted = True
        journal.replace(original_file)
        try:
            journal.symlink_to(target.name)
        except (OSError, NotImplementedError):
            original_file.replace(journal)
            pytest.skip("symlink replacement is unavailable on this host")
        return canonical_open(path)

    monkeypatch.setattr(
        risk_module,
        "_open_read_only_descriptor",
        swap_then_open,
    )

    with pytest.raises(
        RiskOfRuinIssuanceError,
        match="journal (?:is unreadable|changed during open)",
    ):
        risk_module._read_stable_journal_bytes(journal)

    assert swap_attempted
    assert target.read_text(encoding="utf-8") == '{"replacement":true}'


def test_symlink_journal_is_not_accepted_as_durable_state(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    journal = tmp_path / "journal.json"
    try:
        journal.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this host")

    with pytest.raises(
        RiskOfRuinIssuanceError,
        match="journal must be one regular file",
    ):
        risk_module._read_stable_journal_bytes(journal)
