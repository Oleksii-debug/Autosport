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
