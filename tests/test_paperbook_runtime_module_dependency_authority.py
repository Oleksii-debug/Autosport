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
