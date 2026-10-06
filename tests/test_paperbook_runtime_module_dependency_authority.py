from __future__ import annotations

import pytest

import autosport.paper as paper_module
from autosport.paper import PaperBook


def test_committed_stake_rejects_rebound_runtime_module_dependency_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = PaperBook("100")

    def hostile(*_args, **_kwargs):
        raise AssertionError("rebound localcontext executed")

    monkeypatch.setattr(paper_module, "localcontext", hostile)

    with pytest.raises(
        ValueError,
        match=r"runtime module dependency changed: localcontext",
    ):
        _ = book.committed_stake


def test_committed_stake_rejects_in_place_runtime_module_dependency_code_mutation() -> None:
    book = PaperBook("100")
    authority = paper_module._paper_decimal_context
    original_code = authority.__code__

    def hostile():
        raise AssertionError("mutated paper decimal context executed")

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(
            ValueError,
            match=r"runtime module dependency authority changed: _paper_decimal_context",
        ):
            _ = book.committed_stake
    finally:
        authority.__code__ = original_code


def test_open_ticket_rejects_rebound_timestamp_parser_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = PaperBook("100")

    def hostile(*_args, **_kwargs):
        raise AssertionError("rebound parse_iso_timestamp executed")

    monkeypatch.setattr(paper_module, "parse_iso_timestamp", hostile)

    with pytest.raises(
        ValueError,
        match=r"runtime module dependency changed: parse_iso_timestamp",
    ):
        book.open_ticket([], "1")


def test_constructor_rejects_rebound_decimal_dependency_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound Decimal executed")

    monkeypatch.setattr(paper_module, "Decimal", hostile)

    with pytest.raises(
        ValueError,
        match=r"constructor module dependency changed: Decimal",
    ):
        PaperBook("100")

    assert attacker_calls == 0


def test_constructor_rejects_rebound_runtime_helper_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound finite validator executed")

    monkeypatch.setattr(PaperBook, "_require_finite", staticmethod(hostile))

    with pytest.raises(
        ValueError,
        match=r"runtime helper dispatch changed: _require_finite",
    ):
        PaperBook("100")

    assert attacker_calls == 0


def test_constructor_rejects_rebound_decimal_text_limit_before_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(paper_module, "_MAX_PAPER_DECIMAL_TEXT_CHARS", 10_000)

    with pytest.raises(
        ValueError,
        match=r"constructor module dependency changed: _MAX_PAPER_DECIMAL_TEXT_CHARS",
    ):
        PaperBook("100")


def test_raw_snapshot_rejects_rebound_paper_ticket_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound PaperTicket executed")

    monkeypatch.setattr(paper_module, "PaperTicket", hostile)

    raw = {
        "initial_bankroll": "100",
        "balance": "100",
        "tickets": [],
    }
    with pytest.raises(
        ValueError,
        match=r"snapshot module dependency changed: PaperTicket",
    ):
        PaperBook._from_raw_snapshot(raw)

    assert attacker_calls == 0


def test_load_bytes_rejects_rebound_snapshot_timestamp_parser_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound snapshot timestamp parser executed")

    monkeypatch.setattr(paper_module, "parse_iso_timestamp", hostile)

    payload = (
        b'{"schema_version":7,"initial_bankroll":"100","balance":"100",'
        b'"tickets":[],"lifecycle":[]}'
    )
    with pytest.raises(
        ValueError,
        match=r"snapshot module dependency changed: parse_iso_timestamp",
    ):
        PaperBook.load_bytes(payload)

    assert attacker_calls == 0


def test_save_rejects_rebound_named_temporary_file_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound NamedTemporaryFile executed")

    monkeypatch.setattr(paper_module.tempfile, "NamedTemporaryFile", hostile)

    with pytest.raises(
        ValueError,
        match=r"temporary-file authority changed",
    ):
        book.save(tmp_path / "paper.json")

    assert attacker_calls == 0


def test_save_rejects_rebound_atomic_replace_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound os.replace executed")

    monkeypatch.setattr(paper_module.os, "replace", hostile)

    with pytest.raises(
        ValueError,
        match=r"atomic replace authority changed",
    ):
        book.save(tmp_path / "paper.json")

    assert attacker_calls == 0


def test_save_rejects_rebound_path_unlink_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    original = paper_module.Path.unlink

    def hostile(*_args, **_kwargs):
        raise AssertionError("rebound Path.unlink executed")

    monkeypatch.setattr(paper_module.Path, "unlink", hostile)

    try:
        with pytest.raises(
            ValueError,
            match=r"snapshot unlink authority changed",
        ):
            book.save(tmp_path / "paper.json")
    finally:
        monkeypatch.setattr(paper_module.Path, "unlink", original)


def test_load_rejects_rebound_path_factory_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    snapshot = tmp_path / "paper.json"
    book.save(snapshot)
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound Path executed")

    monkeypatch.setattr(paper_module, "Path", hostile)

    with pytest.raises(
        ValueError,
        match=r"load Path authority changed",
    ):
        PaperBook.load(snapshot)

    assert attacker_calls == 0


def test_load_rejects_rebound_read_bytes_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    snapshot = tmp_path / "paper.json"
    book.save(snapshot)
    path_type = type(paper_module.Path("."))
    original = path_type.read_bytes
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound read_bytes executed")

    monkeypatch.setattr(path_type, "read_bytes", hostile)

    try:
        with pytest.raises(
            ValueError,
            match=r"snapshot read authority changed",
        ):
            PaperBook.load(snapshot)
    finally:
        monkeypatch.setattr(path_type, "read_bytes", original)

    assert attacker_calls == 0



def test_save_rejects_rebound_directory_open_before_parent_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound os.open executed")

    monkeypatch.setattr(paper_module.os, "open", hostile)
    destination = tmp_path / "nested" / "paper.json"

    with pytest.raises(
        ValueError,
        match=r"directory open authority changed",
    ):
        book.save(destination)

    assert attacker_calls == 0
    assert not destination.exists()
    assert not destination.parent.exists()


def test_save_rejects_rebound_directory_close_before_parent_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound os.close executed")

    monkeypatch.setattr(paper_module.os, "close", hostile)
    destination = tmp_path / "nested-close" / "paper.json"

    with pytest.raises(
        ValueError,
        match=r"directory close authority changed",
    ):
        book.save(destination)

    assert attacker_calls == 0
    assert not destination.exists()
    assert not destination.parent.exists()


def test_save_rejects_directory_flag_authority_drift_before_parent_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    original = getattr(paper_module.os, "O_DIRECTORY", None)
    replacement = 1 if original is None else original + 1
    monkeypatch.setattr(paper_module.os, "O_DIRECTORY", replacement, raising=False)
    destination = tmp_path / "nested-flag" / "paper.json"

    with pytest.raises(
        ValueError,
        match=r"directory flag authority changed",
    ):
        book.save(destination)

    assert not destination.exists()
    assert not destination.parent.exists()
