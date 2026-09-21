from __future__ import annotations

import hashlib

import pytest

from autosport.betfair_football_match_odds import (
    BetfairFootballRulesTrustRoot,
    assess_betfair_football_prematch_match_odds_authority,
)
from autosport.market_outcomes import OutcomeAuthorityStatus


MARKET_RULES = "Official market information for this reviewed MATCH_ODDS market."
EXCHANGE_RULES = "Pinned reviewed Betfair Exchange general rules snapshot."
FOOTBALL_RULES = "Pinned reviewed Betfair football rules snapshot."


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _trust_root() -> BetfairFootballRulesTrustRoot:
    return BetfairFootballRulesTrustRoot(
        market_rules_sha256=_sha256(MARKET_RULES),
        exchange_rules_sha256=_sha256(EXCHANGE_RULES),
        football_rules_sha256=_sha256(FOOTBALL_RULES),
        authority_revision="betfair-rules-review:2026-09-21",
    )


def _description() -> dict[str, object]:
    return {
        "marketType": "MATCH_ODDS",
        "bettingType": "ODDS",
        "rules": MARKET_RULES,
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
        exchange_rules_text=EXCHANGE_RULES,
        football_rules_text=FOOTBALL_RULES,
        trust_root=_trust_root(),
        observed_at="2026-09-21T17:45:00+00:00",
    )


@pytest.mark.parametrize(
    ("field", "alias"),
    [
        ("numberOfWinners", True),
        ("numberOfRunners", 3.0),
        ("numberOfActiveRunners", 3.0),
        ("complete", 1),
        ("inplay", 0),
        ("runnersVoidable", 0),
        ("betDelay", False),
    ],
)
def test_noncanonical_json_type_aliases_cannot_mint_exact_authority(
    field: str, alias: object
) -> None:
    book = _book()
    book[field] = alias

    assessment = _assess(book)

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.authority is None


@pytest.mark.parametrize("selection_id", ["30", 30.0, True])
def test_selection_id_must_remain_provider_integer(selection_id: object) -> None:
    book = _book()
    runners = list(book["runners"])
    runners[0] = {"selectionId": selection_id, "status": "ACTIVE"}
    book["runners"] = runners

    assessment = _assess(book)

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.authority is None
