from __future__ import annotations

import os
from pathlib import Path

import pytest

import autosport.betfair_settlement_revisions as settlement


def _stub_record_unsigned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settlement.BetfairSettlementRevisionStore,
        "_record_unsigned",
        lambda self, revision: {"schema": "path-guard-falsifier"},
    )


def test_settlement_store_rejects_symlink_journal_on_reload(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_bytes(b"")
    path = tmp_path / "settlement.jsonl"
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable on this platform: {exc}")

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="must not be a symlink",
    ):
        settlement.BetfairSettlementRevisionStore(path)

    assert target.read_bytes() == b""


def test_settlement_store_rejects_hard_link_alias_on_reload(tmp_path: Path) -> None:
    path = tmp_path / "settlement.jsonl"
    path.write_bytes(b"")
    alias = tmp_path / "settlement-alias.jsonl"
    try:
        os.link(path, alias)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hard-link creation unavailable on this platform: {exc}")

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="must not have hard-link aliases",
    ):
        settlement.BetfairSettlementRevisionStore(path)

    assert path.read_bytes() == b""
    assert alias.read_bytes() == b""


def test_append_rejects_hard_link_added_after_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "settlement.jsonl"
    path.write_bytes(b"")
    store = settlement.BetfairSettlementRevisionStore(path)
    alias = tmp_path / "late-alias.jsonl"
    try:
        os.link(path, alias)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hard-link creation unavailable on this platform: {exc}")
    _stub_record_unsigned(monkeypatch)

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="must not have hard-link aliases",
    ):
        store._append(object())

    assert path.read_bytes() == b""
    assert alias.read_bytes() == b""


def test_first_append_rejects_path_that_appeared_after_absent_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "settlement.jsonl"
    store = settlement.BetfairSettlementRevisionStore(path)
    assert not path.exists()

    foreign = b"foreign-data-must-not-be-appended\n"
    path.write_bytes(foreign)
    _stub_record_unsigned(monkeypatch)

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="path appeared after reload",
    ):
        store._append(object())

    assert path.read_bytes() == foreign


def test_append_rejects_regular_file_identity_replacement_before_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "settlement.jsonl"
    path.write_bytes(b"")
    store = settlement.BetfairSettlementRevisionStore(path)

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(b"")
    os.replace(replacement, path)
    _stub_record_unsigned(monkeypatch)

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="changed after reload",
    ):
        store._append(object())

    assert path.read_bytes() == b""
