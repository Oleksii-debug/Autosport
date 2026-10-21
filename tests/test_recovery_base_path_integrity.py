from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from autosport.recovery import _require_recorded_base_state
from autosport.run_registry import ReconciliationError


_PAPER_BOOK_BYTES = b'{"schema_version":1}\n'
_DECISION_LEDGER_BYTES = b""


def _write_base_files(root: Path) -> tuple[Path, Path, dict[str, str]]:
    paper_book_path = root / "paper_book.json"
    decision_ledger_path = root / "decisions.jsonl"
    paper_book_path.write_bytes(_PAPER_BOOK_BYTES)
    decision_ledger_path.write_bytes(_DECISION_LEDGER_BYTES)
    item = {
        "base_paper_book_sha256": hashlib.sha256(_PAPER_BOOK_BYTES).hexdigest(),
        "base_decision_ledger_sha256": hashlib.sha256(_DECISION_LEDGER_BYTES).hexdigest(),
    }
    return paper_book_path, decision_ledger_path, item


def _replace_with_symlink(path: Path) -> None:
    backing = path.with_name(f"{path.name}.backing")
    path.replace(backing)
    try:
        path.symlink_to(backing.name)
    except (NotImplementedError, OSError) as exc:
        backing.replace(path)
        pytest.skip(f"file symlinks are unavailable on this test host: {exc}")


def _add_hardlink_alias(path: Path) -> None:
    alias = path.with_name(f"{path.name}.hardlink")
    try:
        os.link(path, alias)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file hardlinks are unavailable on this test host: {exc}")


def test_recorded_base_state_accepts_regular_single_link_files(tmp_path: Path) -> None:
    paper_book_path, decision_ledger_path, item = _write_base_files(tmp_path)

    assert _require_recorded_base_state(
        item,
        paper_book_path,
        decision_ledger_path,
    ) == (
        item["base_paper_book_sha256"],
        item["base_decision_ledger_sha256"],
    )


@pytest.mark.parametrize("target_name", ["paper_book.json", "decisions.jsonl"])
def test_recorded_base_state_rejects_final_path_symlink(
    tmp_path: Path,
    target_name: str,
) -> None:
    paper_book_path, decision_ledger_path, item = _write_base_files(tmp_path)
    _replace_with_symlink(tmp_path / target_name)

    with pytest.raises(
        ReconciliationError,
        match="canonical economic base files are missing, aliased, or unreadable",
    ):
        _require_recorded_base_state(item, paper_book_path, decision_ledger_path)


@pytest.mark.parametrize("target_name", ["paper_book.json", "decisions.jsonl"])
def test_recorded_base_state_rejects_multi_link_file(
    tmp_path: Path,
    target_name: str,
) -> None:
    paper_book_path, decision_ledger_path, item = _write_base_files(tmp_path)
    _add_hardlink_alias(tmp_path / target_name)

    with pytest.raises(
        ReconciliationError,
        match="canonical economic base files are missing, aliased, or unreadable",
    ):
        _require_recorded_base_state(item, paper_book_path, decision_ledger_path)
