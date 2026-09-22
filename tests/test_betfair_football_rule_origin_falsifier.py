from __future__ import annotations

from autosport.betfair_football_match_odds import (
    assess_betfair_football_prematch_match_odds_authority,
    verify_betfair_football_rules_evidence,
)
from autosport.market_outcomes import OutcomeAuthorityStatus


def test_caller_supplied_rule_text_cannot_mint_provider_rule_authority() -> None:
    """A canonical URL label plus anchor phrases is not provider-origin proof."""

    fabricated_exchange_rules = (
        "Caller-authored text says the market information shall prevail. "
        "Markets will be settled as set out in the market information."
    )
    fabricated_football_rules = (
        "Caller-authored material event text says the match will be declared void. "
        "All bets matched on the affected markets will be void."
    )

    try:
        rules_authority = verify_betfair_football_rules_evidence(
            exchange_rules_url="https://support.betfair.com/app/answers/detail/a_id/10620",
            exchange_rules_text=fabricated_exchange_rules,
            football_rules_url="https://support.betfair.com/app/answers/detail/a_id/10642",
            football_rules_text=fabricated_football_rules,
            retrieved_at="2026-09-21T17:40:00+00:00",
        )
    except (TypeError, ValueError):
        return

    market_description: dict[str, object] = {
        "marketType": "MATCH_ODDS",
        "bettingType": "ODDS",
        "rules": "Caller-authored market information.",
        "rulesHasDate": True,
        "clarifications": None,
    }
    market_book: dict[str, object] = {
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
            {"selectionId": 10, "status": "ACTIVE"},
            {"selectionId": 20, "status": "ACTIVE"},
            {"selectionId": 30, "status": "ACTIVE"},
        ],
    }

    try:
        assessment = assess_betfair_football_prematch_match_odds_authority(
            event_id="event-123",
            event_type_id="1",
            market_id="1.23456789",
            market_description=market_description,
            market_book=market_book,
            rules_authority=rules_authority,
            observed_at="2026-09-21T17:45:00+00:00",
        )
    except (TypeError, ValueError):
        return

    assert not (
        assessment.status is OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE
        and assessment.authority is not None
    ), (
        "caller-supplied canonical URL labels plus fabricated anchor-bearing rule text "
        "self-minted sealed provider-rule evidence and PROVEN_EXHAUSTIVE settlement "
        "authority without product-owned acquisition/origin proof"
    )
