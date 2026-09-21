from __future__ import annotations

import hashlib

import pytest

from autosport.betfair_football_match_odds import (
    BetfairFootballRulesTrustRoot,
    assess_betfair_football_prematch_match_odds_authority,
)
from autosport.market_outcomes import OutcomeAuthorityStatus, SettlementSemantics


MARKET_RULES = "Official market information for this reviewed MATCH_ODDS market."
EXCHANGE_RULES = "Pinned reviewed Betfair Exchange general rules snapshot."
FOOTBALL_RULES = "Pinned reviewed Betfair football rules snapshot."
OBSERVED_AT = "2026-09-21T17:45:00+00:00"


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


def _assess(
    *,
    description: dict[str, object] | None = None,
    book: dict[str, object] | None = None,
    event_type_id: str = "1",
    market_id: str = "1.23456789",
    exchange_rules_text: str = EXCHANGE_RULES,
    football_rules_text: str = FOOTBALL_RULES,
    trust_root: BetfairFootballRulesTrustRoot | None = None,
):
    return assess_betfair_football_prematch_match_odds_authority(
        event_id="event-123",
        event_type_id=event_type_id,
        market_id=market_id,
        market_description=_description() if description is None else description,
        market_book=_book() if book is None else book,
        exchange_rules_text=exchange_rules_text,
        football_rules_text=football_rules_text,
        trust_root=_trust_root() if trust_root is None else trust_root,
        observed_at=OBSERVED_AT,
    )


def test_exact_prematch_match_odds_produces_only_three_winners_plus_all_void() -> None:
    assessment = _assess()

    assert assessment.status is OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE
    assert assessment.refusal_reason is None
    authority = assessment.authority
    assert authority is not None
    assert authority.terminal_space_exact is True
    assert (
        authority.settlement_semantics
        is SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID
    )
    assert authority.selection_ids == ("10", "20", "30")
    assert authority.terminal_state_count == 4
    assert tuple(state.state_id for state in authority.terminal_states) == (
        "winner:10",
        "winner:20",
        "winner:30",
        "all_void",
    )
    for state in authority.terminal_states[:3]:
        results = [result.value for _, result in state.settlements]
        assert results.count("win") == 1
        assert results.count("loss") == 2
    assert {
        result.value for _, result in authority.terminal_states[-1].settlements
    } == {"void"}


def test_authority_binds_market_version_and_independent_rule_trust_root() -> None:
    first = _assess().authority
    assert first is not None

    changed_book = _book()
    changed_book["version"] = 43
    second = _assess(book=changed_book).authority
    assert second is not None
    assert first.source_revision != second.source_revision
    assert first.authority_sha256 != second.authority_sha256

    changed_revision = BetfairFootballRulesTrustRoot(
        market_rules_sha256=_sha256(MARKET_RULES),
        exchange_rules_sha256=_sha256(EXCHANGE_RULES),
        football_rules_sha256=_sha256(FOOTBALL_RULES),
        authority_revision="betfair-rules-review:2026-09-21-rechecked",
    )
    third = _assess(trust_root=changed_revision).authority
    assert third is not None
    assert first.settlement_rules_sha256 != third.settlement_rules_sha256
    assert first.authority_sha256 != third.authority_sha256


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("status", "SUSPENDED", "betfair_live_shape_status_not_exact"),
        ("inplay", True, "betfair_live_shape_inplay_not_exact"),
        ("complete", False, "betfair_live_shape_complete_not_exact"),
        ("numberOfWinners", 2, "betfair_live_shape_numberOfWinners_not_exact"),
        ("numberOfRunners", 4, "betfair_live_shape_numberOfRunners_not_exact"),
        (
            "numberOfActiveRunners",
            2,
            "betfair_live_shape_numberOfActiveRunners_not_exact",
        ),
        ("runnersVoidable", True, "betfair_live_shape_runnersVoidable_not_exact"),
        ("betDelay", 5, "betfair_live_shape_betDelay_not_exact"),
        ("betDelay", None, "betfair_live_shape_betDelay_not_exact"),
    ],
)
def test_live_shape_mutations_fail_closed(field: str, value: object, reason: str) -> None:
    book = _book()
    book[field] = value

    assessment = _assess(book=book)

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.authority is None
    assert assessment.refusal_reason == reason


def test_missing_bet_delay_is_not_admitted_as_prematch_exact() -> None:
    book = _book()
    del book["betDelay"]

    assessment = _assess(book=book)

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.refusal_reason == "betfair_live_shape_betDelay_not_exact"


def test_inactive_runner_fails_closed() -> None:
    book = _book()
    runners = list(book["runners"])
    runners[0] = {"selectionId": 30, "status": "REMOVED"}
    book["runners"] = runners

    assessment = _assess(book=book)

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.refusal_reason == "betfair_live_runner_is_not_active"


def test_duplicate_runner_fails_closed() -> None:
    book = _book()
    book["runners"] = [
        {"selectionId": 10, "status": "ACTIVE"},
        {"selectionId": 10, "status": "ACTIVE"},
        {"selectionId": 20, "status": "ACTIVE"},
    ]

    assessment = _assess(book=book)

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.refusal_reason == "betfair_live_runner_ids_are_not_unique"


def test_market_specific_rules_are_required_and_digest_pinned() -> None:
    missing = _description()
    missing["rules"] = ""
    assert (
        _assess(description=missing).refusal_reason
        == "betfair_market_information_rules_missing"
    )

    changed = _description()
    changed["rules"] = MARKET_RULES + " changed"
    assert (
        _assess(description=changed).refusal_reason
        == "betfair_market_information_rules_digest_changed"
    )


def test_nonempty_clarification_fails_closed_even_when_base_rules_match() -> None:
    description = _description()
    description["clarifications"] = "Settlement wording changed for this market."

    assessment = _assess(description=description)

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert (
        assessment.refusal_reason
        == "betfair_market_information_has_unreviewed_clarifications"
    )


def test_global_rule_change_fails_closed() -> None:
    exchange = _assess(exchange_rules_text=EXCHANGE_RULES + " changed")
    football = _assess(football_rules_text=FOOTBALL_RULES + " changed")

    assert exchange.refusal_reason == "betfair_exchange_rules_digest_changed"
    assert football.refusal_reason == "betfair_football_rules_digest_changed"


def test_nonfootball_or_wrong_market_family_cannot_mint_exactness() -> None:
    assert (
        _assess(event_type_id="2593174").refusal_reason
        == "betfair_event_type_is_not_football"
    )

    description = _description()
    description["marketType"] = "OVER_UNDER_25"
    assert (
        _assess(description=description).refusal_reason
        == "betfair_market_type_is_not_match_odds"
    )

    description = _description()
    description["bettingType"] = "LINE"
    assert (
        _assess(description=description).refusal_reason
        == "betfair_betting_type_is_not_odds"
    )


def test_market_identity_and_version_are_mandatory() -> None:
    mismatched = _book()
    mismatched["marketId"] = "1.other"
    assert _assess(book=mismatched).refusal_reason == "betfair_market_id_mismatch"

    no_version = _book()
    no_version["version"] = None
    assert _assess(book=no_version).refusal_reason == "betfair_market_version_missing"


def test_observed_at_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        assess_betfair_football_prematch_match_odds_authority(
            event_id="event-123",
            event_type_id="1",
            market_id="1.23456789",
            market_description=_description(),
            market_book=_book(),
            exchange_rules_text=EXCHANGE_RULES,
            football_rules_text=FOOTBALL_RULES,
            trust_root=_trust_root(),
            observed_at="2026-09-21T17:45:00",
        )
