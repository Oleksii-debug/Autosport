from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.candidate_optimizer import PortfolioAwareCandidateOptimizer
from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.portfolio import PortfolioEngine
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


def _candidate(leg: CandidateLeg) -> ParlayCandidate:
    return ParlayCandidate(
        (leg,),
        leg.decimal_odds,
        leg.probability,
        leg.probability * leg.decimal_odds - Decimal("1"),
    )


@pytest.mark.parametrize("event_id", ("", " event", "event "))
def test_ticket_identity_rejects_noncanonical_event_id(event_id: str) -> None:
    leg = CandidateLeg(
        f"{event_id}|market|selection",
        event_id,
        Decimal("2"),
        Decimal("0.5"),
        "market",
        "selection",
    )

    with pytest.raises(
        ValueError,
        match="event_id must be a non-empty canonical string",
    ):
        leg.ticket_identity()


def test_direct_optimizer_rejects_empty_event_before_candidate_scenario_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct evaluate_candidates callers must not depend on Beam input validation."""

    leg = CandidateLeg(
        "|market|selection",
        "",
        Decimal("2"),
        Decimal("0.5"),
        "market",
        "selection",
    )
    groups = [
        ScenarioGroup(
            "candidate",
            (
                ScenarioOutcome("|market|selection"),
                ScenarioOutcome("other-event|market|other"),
            ),
        )
    ]

    original = PortfolioEngine.scenario_profit
    profit_calls = 0

    def counted_profit(tickets, winning_quote_keys):
        nonlocal profit_calls
        profit_calls += 1
        return original(tickets, winning_quote_keys)

    monkeypatch.setattr(PortfolioEngine, "scenario_profit", staticmethod(counted_profit))

    optimizer = PortfolioAwareCandidateOptimizer()
    with pytest.raises(
        ValueError,
        match="event_id must be a non-empty canonical string",
    ):
        optimizer.evaluate_candidates(
            [],
            [_candidate(leg)],
            groups,
            stake=Decimal("10"),
        )

    # An empty base portfolio takes the exact zero fast path without P&L calls.
    # Any call here would therefore prove the invalid candidate entered scenario risk.
    assert profit_calls == 0
