from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


_TS = "2026-09-27T19:48:00+00:00"


def _leg(selection_id: str) -> TicketLeg:
    return TicketLeg(
        "risk-decision-generation-event",
        "risk-decision-generation-market",
        selection_id,
        Decimal("2"),
        sport="soccer",
        exchange_side="back",
    )


def _authority_root(tmp_path) -> str:
    return str(tmp_path.parent / f"{tmp_path.name}-risk-decision-generation-authority")


def test_evaluate_holds_publication_lock_through_final_decision(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer durable generation cannot land after validation but before ALLOW."""

    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.save(path)
    evaluated = PaperBook.load(path)
    writer = PaperBook.load(path)

    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    original_derived = PaperRiskPolicy._derived_risk_values
    writer_blocked = False

    def interleave_publication(
        self,
        initial_bankroll,
        balance,
        committed_stake,
        amount,
    ):
        nonlocal writer_blocked
        try:
            writer.open_ticket(
                [_leg("concurrent-writer")],
                "95",
                placed_at=_TS,
            )
            writer.save(path)
        except ValueError as exc:
            assert "publication lock" in str(exc)
            writer_blocked = True
        return original_derived(
            self,
            initial_bankroll,
            balance,
            committed_stake,
            amount,
        )

    monkeypatch.setattr(
        PaperRiskPolicy,
        "_derived_risk_values",
        interleave_publication,
    )

    decision = policy.evaluate(evaluated, Decimal("10"))

    assert decision.allowed is True
    assert writer_blocked is True
    assert PaperBook.load(path).balance == Decimal("100")
