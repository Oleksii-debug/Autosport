from __future__ import annotations

from autosport.betfair_football_match_odds import (
    assess_betfair_football_prematch_match_odds_authority,
    verify_betfair_football_rules_evidence,
)
from autosport.market_outcomes import OutcomeAuthorityStatus


EXCHANGE_RULES_URL = "https://support.betfair.com/app/answers/detail/a_id/10620/"
FOOTBALL_RULES_URL = "https://support.betfair.com/app/answers/detail/a_id/10642/"
EXCHANGE_RULES = """
The Exchange Rules include the Market Information. If there is any inconsistency,
the Market Information shall prevail. Markets will be settled as set out in the
Market Information and/or the Specific Sports Rules.
""".strip()
FOOTBALL_RULES = """
For a Material Event Betfair may void affected in-play bets. If the venue is switched
to the opponent's ground, the match will be declared void. If official fixture team
details differ, all bets matched on the affected markets will be void.
""".strip()


def _rules_authority():
    return verify_betfair_football_rules_evidence(
        exchange_rules_url=EXCHANGE_RULES_URL,
        exchange_rules_text=EXCHANGE_RULES,
        football_rules_url=FOOTBALL_RULES_URL,
        football_rules_text=FOOTBALL_RULES,
        retrieved_at="2026-09-21T17:40:00+00:00",
    )


def _description() -> dict[str, object]:
    return {
        "marketType": "MATCH_ODDS",
        "bettingType": "ODDS",
        "rules": "Official market information for this reviewed MATCH_ODDS market.",
        "rulesHasDate": True,
        "clarifications": None,
    }


def _book() -> dict[str, object]:
    return {
        "marketId": "1.23456789",
        "status": "OPEN",
        "betDelay": 0,
        "complete": True,
        "inplay": False,
        "numberOfWinners": 1,
        "numberOfRunners": 3,
        "numberOfActiveRunners": 3,
        "runnersVoidable": False,
        "version": 42,
        "runners": [
            {"selectionId": 30, "status": "ACTIVE"},
            {"selectionId": 10, "status": "ACTIVE"},
            {"selectionId": 20, "status": "ACTIVE"},
        ],
    }


def _assess(book: dict[str, object]):
    return assess_betfair_football_prematch_match_odds_authority(
        event_id="event-123",
        event_type_id="1",
        market_id="1.23456789",
        market_description=_description(),
        market_book=book,
        rules_authority=_rules_authority(),
        observed_at="2026-09-21T17:45:00+00:00",
    )


def test_boolean_alias_cannot_impersonate_integer_winner_count() -> None:
    book = _book()
    book["numberOfWinners"] = True

    assessment = _assess(book)

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.authority is None


def test_string_selection_id_cannot_impersonate_provider_integer_identity() -> None:
    book = _book()
    runners = list(book["runners"])
    runners[0] = {"selectionId": "30", "status": "ACTIVE"}
    book["runners"] = runners

    assessment = _assess(book)

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.authority is None
