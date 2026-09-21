from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .domain import MarketType
from .market_outcomes import (
    MarketOutcomeAuthorityAssessment,
    MarketOutcomeIdentity,
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    OutcomeRosterBasis,
    SettlementSemantics,
    _VERIFIED_AUTHORITY_TOKEN,
    _canonical_timestamp,
)


_BETFAIR_FOOTBALL_EVENT_TYPE_ID = "1"
_BETFAIR_MATCH_ODDS = "MATCH_ODDS"
_BETFAIR_ODDS = "ODDS"
_BETFAIR_LIVE_SOURCE_ID = "betfair_exchange_live"
_RULES_PROFILE_ID = "betfair-football-match-odds-rule-evidence-v1"
_EXCHANGE_RULES_URL = "https://support.betfair.com/app/answers/detail/a_id/10620"
_FOOTBALL_RULES_URL = "https://support.betfair.com/app/answers/detail/a_id/10642"
_VERIFIED_RULES_TOKEN = object()

_EXCHANGE_RULE_ANCHORS = (
    "the market information",
    "shall prevail",
    "markets will be settled as set out in the market information",
)
_FOOTBALL_RULE_ANCHORS = (
    "material event",
    "the match will be declared void",
    "all bets matched on the affected markets will be void",
)


def _canonical_text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    return value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_rule_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalized_official_url(name: str, value: object, expected: str) -> str:
    url = _canonical_text(name, value).rstrip("/")
    if url != expected:
        raise ValueError(f"{name} must use the canonical Betfair support authority URL")
    return url


def _require_rule_anchors(name: str, text: str, anchors: tuple[str, ...]) -> None:
    normalized = _normalized_rule_text(text)
    missing = tuple(anchor for anchor in anchors if anchor not in normalized)
    if missing:
        raise ValueError(f"{name} does not contain the reviewed settlement-rule anchors")


def _empty_clarifications(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


@dataclass(frozen=True, slots=True)
class BetfairFootballRulesAuthority:
    """Sealed provider-rule evidence derived from raw official Betfair rule text."""

    exchange_rules_url: str
    football_rules_url: str
    exchange_rules_sha256: str
    football_rules_sha256: str
    retrieved_at: str
    profile_id: str
    _verification_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _VERIFIED_RULES_TOKEN:
            raise TypeError(
                "BetfairFootballRulesAuthority must come from verified raw rule evidence"
            )
        if self.profile_id != _RULES_PROFILE_ID:
            raise ValueError("unsupported Betfair football rule evidence profile")
        _normalized_official_url(
            "exchange_rules_url",
            self.exchange_rules_url,
            _EXCHANGE_RULES_URL,
        )
        _normalized_official_url(
            "football_rules_url",
            self.football_rules_url,
            _FOOTBALL_RULES_URL,
        )
        _canonical_timestamp("rules retrieved_at", self.retrieved_at)

    @property
    def authority_sha256(self) -> str:
        return _sha256_json(
            {
                "profile_id": self.profile_id,
                "exchange_rules_url": self.exchange_rules_url,
                "football_rules_url": self.football_rules_url,
                "exchange_rules_sha256": self.exchange_rules_sha256,
                "football_rules_sha256": self.football_rules_sha256,
                "retrieved_at": self.retrieved_at,
            }
        )


def verify_betfair_football_rules_evidence(
    *,
    exchange_rules_url: str,
    exchange_rules_text: str,
    football_rules_url: str,
    football_rules_text: str,
    retrieved_at: str,
) -> BetfairFootballRulesAuthority:
    """Mint sealed rule authority only from raw provider evidence, never caller hashes."""

    exchange_url = _normalized_official_url(
        "exchange_rules_url",
        exchange_rules_url,
        _EXCHANGE_RULES_URL,
    )
    football_url = _normalized_official_url(
        "football_rules_url",
        football_rules_url,
        _FOOTBALL_RULES_URL,
    )
    exchange_text = _canonical_text("exchange_rules_text", exchange_rules_text)
    football_text = _canonical_text("football_rules_text", football_rules_text)
    _require_rule_anchors(
        "exchange_rules_text",
        exchange_text,
        _EXCHANGE_RULE_ANCHORS,
    )
    _require_rule_anchors(
        "football_rules_text",
        football_text,
        _FOOTBALL_RULE_ANCHORS,
    )
    retrieved, _ = _canonical_timestamp("rules retrieved_at", retrieved_at)
    return BetfairFootballRulesAuthority(
        exchange_rules_url=exchange_url,
        football_rules_url=football_url,
        exchange_rules_sha256=_sha256_text(exchange_text),
        football_rules_sha256=_sha256_text(football_text),
        retrieved_at=retrieved,
        profile_id=_RULES_PROFILE_ID,
        _verification_token=_VERIFIED_RULES_TOKEN,
    )


def _identity(*, event_id: str, market_id: str) -> MarketOutcomeIdentity:
    return MarketOutcomeIdentity(
        sport="football",
        event_id=_canonical_text("event_id", event_id),
        market_id=_canonical_text("market_id", market_id),
        source_id=_BETFAIR_LIVE_SOURCE_ID,
        market_type=MarketType.WINNER,
    )


def _refused(
    identity: MarketOutcomeIdentity,
    reason: str,
) -> MarketOutcomeAuthorityAssessment:
    return MarketOutcomeAuthorityAssessment(
        identity=identity,
        status=OutcomeAuthorityStatus.REFUSED,
        authority=None,
        refusal_reason=reason,
    )


def assess_betfair_football_prematch_match_odds_authority(
    *,
    event_id: str,
    event_type_id: str,
    market_id: str,
    market_description: dict[str, object],
    market_book: dict[str, object],
    rules_authority: BetfairFootballRulesAuthority,
    observed_at: str,
) -> MarketOutcomeAuthorityAssessment:
    """Qualify one exact pre-match Football MATCH_ODDS terminal space.

    The terminal topology comes from provider market identity plus a complete live
    three-runner, one-winner, non-runner-voidable book. Raw official Exchange/Football
    rule evidence is independently verified and sealed before this function can mint an
    exact authority. Market Information is captured verbatim and any clarification or
    unsupported live shape fails closed.
    """

    identity = _identity(event_id=event_id, market_id=market_id)
    if not isinstance(rules_authority, BetfairFootballRulesAuthority):
        raise TypeError("rules_authority must be BetfairFootballRulesAuthority")
    if type(market_description) is not dict:
        raise ValueError("market_description must be a JSON object")
    if type(market_book) is not dict:
        raise ValueError("market_book must be a JSON object")

    event_type = _canonical_text("event_type_id", event_type_id)
    observed, observed_dt = _canonical_timestamp("observed_at", observed_at)
    _, rules_retrieved_dt = _canonical_timestamp(
        "rules retrieved_at",
        rules_authority.retrieved_at,
    )
    if rules_retrieved_dt > observed_dt:
        raise ValueError("rules authority must be causally available by observed_at")

    if event_type != _BETFAIR_FOOTBALL_EVENT_TYPE_ID:
        return _refused(identity, "betfair_event_type_is_not_football")
    if market_book.get("marketId") != identity.market_id:
        return _refused(identity, "betfair_market_id_mismatch")
    if market_description.get("marketType") != _BETFAIR_MATCH_ODDS:
        return _refused(identity, "betfair_market_type_is_not_match_odds")
    if market_description.get("bettingType") != _BETFAIR_ODDS:
        return _refused(identity, "betfair_betting_type_is_not_odds")

    market_rules = market_description.get("rules")
    if type(market_rules) is not str or not market_rules.strip():
        return _refused(identity, "betfair_market_information_rules_missing")
    if type(market_description.get("rulesHasDate")) is not bool:
        return _refused(identity, "betfair_market_information_rules_date_truth_missing")
    if not _empty_clarifications(market_description.get("clarifications")):
        return _refused(identity, "betfair_market_information_has_unreviewed_clarifications")

    required_book_truth = {
        "status": "OPEN",
        "inplay": False,
        "complete": True,
        "numberOfWinners": 1,
        "numberOfRunners": 3,
        "numberOfActiveRunners": 3,
        "runnersVoidable": False,
        "betDelay": 0,
    }
    for field_name, expected in required_book_truth.items():
        if market_book.get(field_name) != expected:
            return _refused(identity, f"betfair_live_shape_{field_name}_not_exact")
    version = market_book.get("version")
    if type(version) is not int or version <= 0:
        return _refused(identity, "betfair_market_version_missing")

    runners = market_book.get("runners")
    if type(runners) is not list or len(runners) != 3:
        return _refused(identity, "betfair_live_book_lacks_exact_three_runner_roster")
    selection_ids: list[str] = []
    runner_evidence: list[dict[str, object]] = []
    for index, runner in enumerate(runners):
        if type(runner) is not dict or runner.get("selectionId") is None:
            return _refused(identity, "betfair_live_runner_identity_missing")
        if runner.get("status") != "ACTIVE":
            return _refused(identity, "betfair_live_runner_is_not_active")
        selection_id = _canonical_text(
            f"market_book.runners[{index}].selectionId",
            str(runner["selectionId"]),
        )
        selection_ids.append(selection_id)
        runner_evidence.append(
            {"selectionId": selection_id, "status": runner.get("status")}
        )
    if len(set(selection_ids)) != 3:
        return _refused(identity, "betfair_live_runner_ids_are_not_unique")
    canonical_selections = tuple(sorted(selection_ids))

    market_rules_sha256 = _sha256_text(market_rules)
    roster_payload = {
        "provider": _BETFAIR_LIVE_SOURCE_ID,
        "event_type_id": event_type,
        "event_id": identity.event_id,
        "market_id": identity.market_id,
        "version": version,
        "status": market_book["status"],
        "betDelay": market_book["betDelay"],
        "inplay": market_book["inplay"],
        "complete": market_book["complete"],
        "numberOfWinners": market_book["numberOfWinners"],
        "numberOfRunners": market_book["numberOfRunners"],
        "numberOfActiveRunners": market_book["numberOfActiveRunners"],
        "runnersVoidable": market_book["runnersVoidable"],
        "runners": sorted(runner_evidence, key=lambda row: str(row["selectionId"])),
    }
    roster_provenance_sha256 = _sha256_json(roster_payload)
    settlement_rules_sha256 = _sha256_json(
        {
            "profile_id": rules_authority.profile_id,
            "official_rules_authority_sha256": rules_authority.authority_sha256,
            "market_rules_sha256": market_rules_sha256,
            "rulesHasDate": market_description["rulesHasDate"],
            "clarifications": market_description.get("clarifications"),
            "terminal_family": SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID.value,
        }
    )
    verification_protocol_sha256 = _sha256_json(
        {
            "protocol": "autosport.betfair_football.prematch_match_odds_exact.v2",
            "event_type_id": _BETFAIR_FOOTBALL_EVENT_TYPE_ID,
            "market_type": _BETFAIR_MATCH_ODDS,
            "betting_type": _BETFAIR_ODDS,
            "requires_open": True,
            "requires_bet_delay_zero": True,
            "requires_inplay_false": True,
            "requires_complete": True,
            "requires_runner_count": 3,
            "requires_active_runner_count": 3,
            "requires_winner_count": 1,
            "requires_runners_voidable_false": True,
            "requires_all_runner_status_active": True,
            "requires_empty_clarifications": True,
            "official_rules_authority_sha256": rules_authority.authority_sha256,
        }
    )
    source_revision = (
        f"betfair-live:{identity.market_id}:v{version}:"
        f"{roster_provenance_sha256[:16]}:{rules_authority.authority_sha256[:16]}:"
        f"{market_rules_sha256[:16]}"
    )
    authority = MarketSettlementOutcomeAuthority(
        identity=identity,
        selection_ids=canonical_selections,
        roster_basis=OutcomeRosterBasis.PROVIDER_MARKET_DEFINITION,
        settlement_semantics=SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID,
        source_revision=source_revision,
        causal_cutoff=observed,
        observed_at=observed,
        roster_provenance_sha256=roster_provenance_sha256,
        settlement_rules_sha256=settlement_rules_sha256,
        verification_protocol_sha256=verification_protocol_sha256,
        _verification_token=_VERIFIED_AUTHORITY_TOKEN,
    )
    return MarketOutcomeAuthorityAssessment(
        identity=identity,
        status=OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        authority=authority,
        refusal_reason=None,
    )
