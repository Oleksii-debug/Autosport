from __future__ import annotations

import hashlib
import inspect

import pytest

from autosport.betfair_football_match_odds import (
    BetfairFootballRulesAuthority,
    assess_betfair_football_prematch_match_odds_authority,
    capture_betfair_football_rules_evidence,
    validate_betfair_football_rules_authority,
    verify_betfair_football_rules_evidence,
)
from autosport.market_outcomes import OutcomeAuthorityStatus, SettlementSemantics


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
MARKET_RULES = "Official market information for this reviewed MATCH_ODDS market."
RULES_RETRIEVED_AT = "2026-09-21T17:40:00+00:00"
OBSERVED_AT = "2026-09-21T17:45:00+00:00"


def _register_test_rules_authority(
    *,
    retrieved_at: str = RULES_RETRIEVED_AT,
) -> BetfairFootballRulesAuthority:
    """Create a deterministic fixture without exposing a production injection API.

    Production capture accepts no URL/text/transport/clock injection.  Tests use the
    same private-closure pattern as the repository's provider billing authority tests:
    private reflection is outside the supported caller surface and only registers a
    structurally exact fixture for deterministic downstream qualification.
    """

    source = BetfairFootballRulesAuthority(
        exchange_rules_url=EXCHANGE_RULES_URL.rstrip("/"),
        football_rules_url=FOOTBALL_RULES_URL.rstrip("/"),
        exchange_rules_sha256=hashlib.sha256(EXCHANGE_RULES.encode("utf-8")).hexdigest(),
        football_rules_sha256=hashlib.sha256(FOOTBALL_RULES.encode("utf-8")).hexdigest(),
        retrieved_at=retrieved_at,
        profile_id="betfair-football-match-odds-rule-evidence-v1",
    )
    closure = capture_betfair_football_rules_evidence.__closure__
    assert closure is not None
    cells = {
        name: cell.cell_contents
        for name, cell in zip(
            capture_betfair_football_rules_evidence.__code__.co_freevars,
            closure,
            strict=True,
        )
    }
    register = cells["register"]
    return register(source)


def _rules_authority() -> BetfairFootballRulesAuthority:
    return _register_test_rules_authority()


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
    rules_authority: BetfairFootballRulesAuthority | None = None,
):
    return assess_betfair_football_prematch_match_odds_authority(
        event_id="event-123",
        event_type_id=event_type_id,
        market_id=market_id,
        market_description=_description() if description is None else description,
        market_book=_book() if book is None else book,
        rules_authority=(
            _rules_authority() if rules_authority is None else rules_authority
        ),
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


def test_caller_constructed_rule_dto_is_not_positive_provider_authority() -> None:
    caller_built = BetfairFootballRulesAuthority(
        exchange_rules_url=EXCHANGE_RULES_URL.rstrip("/"),
        football_rules_url=FOOTBALL_RULES_URL.rstrip("/"),
        exchange_rules_sha256="0" * 64,
        football_rules_sha256="1" * 64,
        retrieved_at=RULES_RETRIEVED_AT,
        profile_id="betfair-football-match-odds-rule-evidence-v1",
    )

    with pytest.raises(ValueError, match="issued by canonical provider capture"):
        validate_betfair_football_rules_authority(caller_built)
    with pytest.raises(ValueError, match="issued by canonical provider capture"):
        _assess(rules_authority=caller_built)


def test_rule_authority_dto_requires_canonical_official_urls() -> None:
    with pytest.raises(ValueError, match="canonical Betfair support authority URL"):
        BetfairFootballRulesAuthority(
            exchange_rules_url="https://example.invalid/rules",
            football_rules_url=FOOTBALL_RULES_URL.rstrip("/"),
            exchange_rules_sha256="0" * 64,
            football_rules_sha256="1" * 64,
            retrieved_at=RULES_RETRIEVED_AT,
            profile_id="betfair-football-match-odds-rule-evidence-v1",
        )


def test_caller_supplied_rule_text_cannot_mint_provider_rule_authority() -> None:
    fabricated_exchange_rules = (
        "Caller-authored text says the market information shall prevail. "
        "Markets will be settled as set out in the market information."
    )
    fabricated_football_rules = (
        "Caller-authored material event text says the match will be declared void. "
        "All bets matched on the affected markets will be void."
    )

    with pytest.raises(TypeError, match="caller-supplied rule text cannot mint"):
        verify_betfair_football_rules_evidence(
            exchange_rules_url=EXCHANGE_RULES_URL,
            exchange_rules_text=fabricated_exchange_rules,
            football_rules_url=FOOTBALL_RULES_URL,
            football_rules_text=fabricated_football_rules,
            retrieved_at=RULES_RETRIEVED_AT,
        )


def test_product_rule_capture_exposes_no_url_text_transport_or_clock_injection() -> None:
    signature = inspect.signature(capture_betfair_football_rules_evidence)
    assert tuple(signature.parameters) == ("timeout_seconds",)
    parameter = signature.parameters["timeout_seconds"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_market_version_and_raw_market_rules_change_authority_identity() -> None:
    first = _assess().authority
    assert first is not None

    changed_book = _book()
    changed_book["version"] = 43
    second = _assess(book=changed_book).authority
    assert second is not None
    assert first.source_revision != second.source_revision
    assert first.authority_sha256 != second.authority_sha256

    changed_description = _description()
    changed_description["rules"] = MARKET_RULES + " Clarified before observation."
    third = _assess(description=changed_description).authority
    assert third is not None
    assert first.settlement_rules_sha256 != third.settlement_rules_sha256
    assert first.source_revision != third.source_revision


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




@pytest.mark.parametrize(
    ("field", "alias"),
    [
        ("inplay", 0),
        ("complete", 1),
        ("numberOfWinners", True),
        ("numberOfRunners", 3.0),
        ("numberOfActiveRunners", 3.0),
        ("runnersVoidable", 0),
        ("betDelay", False),
    ],
)
def test_json_scalar_type_aliases_cannot_impersonate_provider_shape(
    field: str,
    alias: object,
) -> None:
    book = _book()
    book[field] = alias

    assessment = _assess(book=book)

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.authority is None
    assert assessment.refusal_reason == f"betfair_live_shape_{field}_not_exact"


@pytest.mark.parametrize("selection_id", ["30", 30.0, True, False, 0, -1])
def test_runner_selection_id_requires_positive_exact_provider_integer(
    selection_id: object,
) -> None:
    book = _book()
    runners = list(book["runners"])
    runners[0] = {"selectionId": selection_id, "status": "ACTIVE"}
    book["runners"] = runners

    assessment = _assess(book=book)

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.authority is None
    assert assessment.refusal_reason == "betfair_live_runner_identity_not_exact"


def test_missing_bet_delay_is_not_admitted_as_prematch_exact() -> None:
    book = _book()
    del book["betDelay"]

    assessment = _assess(book=book)

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.refusal_reason == "betfair_live_shape_betDelay_not_exact"


def test_inactive_and_duplicate_runner_evidence_fail_closed() -> None:
    inactive = _book()
    inactive["runners"] = [
        {"selectionId": 30, "status": "REMOVED"},
        {"selectionId": 10, "status": "ACTIVE"},
        {"selectionId": 20, "status": "ACTIVE"},
    ]
    assert _assess(book=inactive).refusal_reason == "betfair_live_runner_is_not_active"

    duplicate = _book()
    duplicate["runners"] = [
        {"selectionId": 10, "status": "ACTIVE"},
        {"selectionId": 10, "status": "ACTIVE"},
        {"selectionId": 20, "status": "ACTIVE"},
    ]
    assert (
        _assess(book=duplicate).refusal_reason
        == "betfair_live_runner_ids_are_not_unique"
    )


def test_market_information_is_required_and_unknown_clarification_fails_closed() -> None:
    missing = _description()
    missing["rules"] = ""
    assert (
        _assess(description=missing).refusal_reason
        == "betfair_market_information_rules_missing"
    )

    clarification = _description()
    clarification["clarifications"] = "Settlement wording changed for this market."
    assert (
        _assess(description=clarification).refusal_reason
        == "betfair_market_information_has_unreviewed_clarifications"
    )


def test_nonfootball_wrong_family_market_mismatch_and_missing_version_fail_closed() -> None:
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

    mismatched = _book()
    mismatched["marketId"] = "1.other"
    assert _assess(book=mismatched).refusal_reason == "betfair_market_id_mismatch"

    no_version = _book()
    no_version["version"] = None
    assert _assess(book=no_version).refusal_reason == "betfair_market_version_missing"


def test_rules_authority_must_precede_market_observation() -> None:
    future_rules = _register_test_rules_authority(
        retrieved_at="2026-09-21T17:46:00+00:00"
    )

    with pytest.raises(ValueError, match="causally available"):
        _assess(rules_authority=future_rules)


def test_observed_at_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        assess_betfair_football_prematch_match_odds_authority(
            event_id="event-123",
            event_type_id="1",
            market_id="1.23456789",
            market_description=_description(),
            market_book=_book(),
            rules_authority=_rules_authority(),
            observed_at="2026-09-21T17:45:00",
        )
