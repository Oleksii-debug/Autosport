from __future__ import annotations

from autosport.betfair_football_match_odds import (
    assess_betfair_football_prematch_match_odds_authority,
    verify_betfair_football_rules_evidence,
)
from autosport.market_outcomes import OutcomeAuthorityStatus


EXCHANGE_RULES_URL = "https://support.betfair.com/app/answers/detail/a_id/10620"
FOOTBALL_RULES_URL = "https://support.betfair.com/app/answers/detail/a_id/10642"
RULES_RETRIEVED_AT = "2026-09-21T17:40:00+00:00"
OBSERVED_AT = "2026-09-21T17:45:00+00:00"

# Deliberately caller-authored prose. It was not acquired from either URL.
# It contains only the semantic substrings the current verifier searches for.
FORGED_EXCHANGE_RULES = """
The market information shall prevail.
Markets will be settled as set out in the market information.
This sentence was invented by an ordinary caller and was never fetched from Betfair.
""".strip()

FORGED_FOOTBALL_RULES = """
A material event exists.
The match will be declared void.
All bets matched on the affected markets will be void.
This sentence was invented by an ordinary caller and was never fetched from Betfair.
""".strip()


def _caller_minted_rules_authority():
    try:
        return verify_betfair_football_rules_evidence(
            exchange_rules_url=EXCHANGE_RULES_URL,
            exchange_rules_text=FORGED_EXCHANGE_RULES,
            football_rules_url=FOOTBALL_RULES_URL,
            football_rules_text=FORGED_FOOTBALL_RULES,
            retrieved_at=RULES_RETRIEVED_AT,
        )
    except (TypeError, ValueError):
        # A repaired implementation may reject this legacy caller-only API shape,
        # require a product-owned acquisition receipt, or pin independently
        # governed official bytes/digests.
        return None


def test_caller_authored_anchor_text_cannot_mint_official_rule_authority() -> None:
    authority = _caller_minted_rules_authority()

    assert authority is None, (
        "caller-authored prose containing reviewed substrings must not become "
        "official Betfair rule authority without independent acquisition/digest authority"
    )


def test_caller_only_rule_and_market_payloads_cannot_mint_exhaustive_outcomes() -> None:
    rules_authority = _caller_minted_rules_authority()
    if rules_authority is None:
        return

    # These provider-shaped mappings are also ordinary caller values. This test
    # freezes the stronger trust invariant: a sealed output token cannot turn
    # caller-created input bytes into provider-origin settlement truth.
    assessment = assess_betfair_football_prematch_match_odds_authority(
        event_id="caller-event",
        event_type_id="1",
        market_id="1.caller",
        market_description={
            "marketType": "MATCH_ODDS",
            "bettingType": "ODDS",
            "rules": "Caller-authored market information.",
            "rulesHasDate": True,
            "clarifications": None,
        },
        market_book={
            "marketId": "1.caller",
            "status": "OPEN",
            "betDelay": 0,
            "complete": True,
            "inplay": False,
            "numberOfWinners": 1,
            "numberOfRunners": 3,
            "numberOfActiveRunners": 3,
            "runnersVoidable": False,
            "version": 1,
            "runners": [
                {"selectionId": 10, "status": "ACTIVE"},
                {"selectionId": 20, "status": "ACTIVE"},
                {"selectionId": 30, "status": "ACTIVE"},
            ],
        },
        rules_authority=rules_authority,
        observed_at=OBSERVED_AT,
    )

    assert assessment.status is not OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE
    assert assessment.authority is None
