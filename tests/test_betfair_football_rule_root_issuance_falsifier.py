from __future__ import annotations

import hashlib

from autosport.betfair_football_match_odds import (
    BetfairFootballRulesTrustRoot,
    assess_betfair_football_prematch_match_odds_authority,
)
from autosport.market_outcomes import OutcomeAuthorityStatus


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_caller_minted_rule_root_cannot_issue_proven_exhaustive_authority() -> None:
    """Matching caller hashes are not independent product-governance authority."""

    market_rules = "caller-authored market rules"
    exchange_rules = "caller-authored exchange rules"
    football_rules = "caller-authored football rules"

    try:
        trust_root = BetfairFootballRulesTrustRoot(
            market_rules_sha256=_sha256(market_rules),
            exchange_rules_sha256=_sha256(exchange_rules),
            football_rules_sha256=_sha256(football_rules),
            authority_revision="caller-self-issued-review",
        )
    except (TypeError, ValueError):
        return

    description: dict[str, object] = {
        "marketType": "MATCH_ODDS",
        "bettingType": "ODDS",
        "rules": market_rules,
        "rulesHasDate": True,
        "clarifications": None,
    }
    book: dict[str, object] = {
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
            market_description=description,
            market_book=book,
            exchange_rules_text=exchange_rules,
            football_rules_text=football_rules,
            trust_root=trust_root,
            observed_at="2026-09-21T17:45:00+00:00",
        )
    except (TypeError, ValueError):
        return

    assert not (
        assessment.status is OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE
        and assessment.authority is not None
    ), (
        "caller-authored rule text plus caller-minted matching digest pins produced "
        "PROVEN_EXHAUSTIVE settlement-outcome authority without independent "
        "product-governance issuance"
    )
