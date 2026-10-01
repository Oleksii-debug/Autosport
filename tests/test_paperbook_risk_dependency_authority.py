from __future__ import annotations

from decimal import Decimal

import pytest

import autosport.risk as risk_module
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


_TS = "2026-09-27T20:00:00+00:00"


def _loaded_book(tmp_path, monkeypatch: pytest.MonkeyPatch) -> PaperBook:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-risk-dependency-authority"),
    )
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket(
        [
            TicketLeg(
                "risk-dependency-event",
                "risk-dependency-market",
                "risk-dependency-selection",
                Decimal("2"),
                sport="soccer",
                exchange_side="back",
            )
        ],
        "10",
        placed_at=_TS,
    )
    book.save(path)
    return PaperBook.load(path)


def test_portfolio_hash_cannot_be_retargeted_by_live_digest_global(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = _loaded_book(tmp_path, monkeypatch)
    canonical = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    assert canonical is not None
    hostile = "0" * 64
    assert canonical != hostile

    monkeypatch.setattr(risk_module, "_sha256_payload", lambda _payload: hostile)

    after = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    assert after is None or after == canonical
    assert after != hostile


def test_committed_exposure_cannot_be_retargeted_by_live_policy_helper(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = _loaded_book(tmp_path, monkeypatch)
    canonical = PaperRiskPolicy._book_state(book)
    assert canonical is not None
    assert canonical[2] == Decimal("10")

    monkeypatch.setattr(
        PaperRiskPolicy,
        "_exact_positive_sum",
        staticmethod(lambda _values: Decimal("0")),
    )

    after = PaperRiskPolicy._book_state(book)
    assert after is None or after[2] == Decimal("10")
