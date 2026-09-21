from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .domain import MarketType
from .market_outcomes import (
    MarketOutcomeAuthorityAssessment,
    MarketOutcomeIdentity,
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    OutcomeRosterBasis,
    SettlementSemantics,
    _VERIFIED_AUTHORITY_TOKEN,
)


_BETFAIR_FOOTBALL_EVENT_TYPE_ID = "1"
_BETFAIR_MATCH_ODDS = "MATCH_ODDS"
_BETFAIR_ODDS = "ODDS"
_BETFAIR_LIVE_SOURCE_ID = "betfair_exchange_live"
_PROFILE_ID = "betfair-football-prematch-match-odds-v1"
_SHA256_HEX = frozenset("0123456789abcdef")


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


def _canonical_sha256(name: str, value: object) -> str:
    digest = _canonical_text(name, value)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in _SHA256_HEX for character in digest)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return digest


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


def _empty_clarifications(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


@dataclass(frozen=True, slots=True)
class BetfairFootballRulesTrustRoot:
    """Governed digest pins for one reviewed Betfair settlement-rule profile.

    This object is intentionally only a trust *input*. Callers must source these pins
    from product governance rather than deriving them from the same live payload being
    qualified. The assessor compares raw provider evidence against the independent pins
    and binds every digest into the resulting authority.
    """

    market_rules_sha256: str
    exchange_rules_sha256: str
    football_rules_sha256: str
    authority_revision: str
    profile_id: str = _PROFILE_ID

    def __post_init__(self) -> None:
        if self.profile_id != _PROFILE_ID:
            raise ValueError("unsupported Betfair football rule profile")
        _canonical_sha256("market_rules_sha256", self.market_rules_sha256)
        _canonical_sha256("exchange_rules_sha256", self.exchange_rules_sha256)
        _canonical_sha256("football_rules_sha256", self.football_rules_sha256)
        _canonical_text("authority_revision", self.authority_revision)

    @property
    def trust_root_sha256(self) -> str:
        return _sha256_json(
            {
                "profile_id": self.profile_id,
                "market_rules_sha256": self.market_rules_sha256,
                "exchange_rules_sha256": self.exchange_rules_sha256,
                "football_rules_sha256": self.football_rules_sha256,
                "authority_revision": self.authority_revision,
            }
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
    exchange_rules_text: str,
    football_rules_text: str,
    trust_root: BetfairFootballRulesTrustRoot,
    observed_at: str,
) -> MarketOutcomeAuthorityAssessment:
    """Qualify one exact pre-match Football MATCH_ODDS terminal space.

    Exactness is deliberately narrow. It requires independently governed rule digest
    pins, the market-specific Market Information, and a complete live three-runner book.
    Any unknown clarification, in-play state, removable runner, roster disagreement, or
    changed rule digest fails closed. The only admitted market-level terminal vectors are
    one of three runners winning (the other two losing) or the whole market being void.
    """

    identity = _identity(event_id=event_id, market_id=market_id)
    if not isinstance(trust_root, BetfairFootballRulesTrustRoot):
        raise TypeError("trust_root must be BetfairFootballRulesTrustRoot")
    if type(market_description) is not dict:
        raise ValueError("market_description must be a JSON object")
    if type(market_book) is not dict:
        raise ValueError("market_book must be a JSON object")

    event_type = _canonical_text("event_type_id", event_type_id)
    observed = _canonical_text("observed_at", observed_at)
    # Reuse the canonical authority timestamp validator instead of accepting naive time.
    from .market_outcomes import _canonical_timestamp

    _canonical_timestamp("observed_at", observed)

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

    exchange_rules = _canonical_text("exchange_rules_text", exchange_rules_text)
    football_rules = _canonical_text("football_rules_text", football_rules_text)
    actual_market_rules_sha256 = _sha256_text(market_rules)
    actual_exchange_rules_sha256 = _sha256_text(exchange_rules)
    actual_football_rules_sha256 = _sha256_text(football_rules)
    if actual_market_rules_sha256 != trust_root.market_rules_sha256:
        return _refused(identity, "betfair_market_information_rules_digest_changed")
    if actual_exchange_rules_sha256 != trust_root.exchange_rules_sha256:
        return _refused(identity, "betfair_exchange_rules_digest_changed")
    if actual_football_rules_sha256 != trust_root.football_rules_sha256:
        return _refused(identity, "betfair_football_rules_digest_changed")

    required_book_truth = {
        "status": "OPEN",
        "inplay": False,
        "complete": True,
        "numberOfWinners": 1,
        "numberOfRunners": 3,
        "numberOfActiveRunners": 3,
        "runnersVoidable": False,
    }
    for field_name, expected in required_book_truth.items():
        if market_book.get(field_name) != expected:
            return _refused(identity, f"betfair_live_shape_{field_name}_not_exact")
    if market_book.get("betDelay") not in (0, None):
        return _refused(identity, "betfair_market_has_nonzero_bet_delay")
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

    roster_payload = {
        "provider": _BETFAIR_LIVE_SOURCE_ID,
        "event_type_id": event_type,
        "event_id": identity.event_id,
        "market_id": identity.market_id,
        "version": version,
        "status": market_book["status"],
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
            "profile_id": trust_root.profile_id,
            "trust_root_sha256": trust_root.trust_root_sha256,
            "market_rules_sha256": actual_market_rules_sha256,
            "rulesHasDate": market_description["rulesHasDate"],
            "clarifications": market_description.get("clarifications"),
            "exchange_rules_sha256": actual_exchange_rules_sha256,
            "football_rules_sha256": actual_football_rules_sha256,
            "terminal_family": SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID.value,
        }
    )
    verification_protocol_sha256 = _sha256_json(
        {
            "protocol": "autosport.betfair_football.prematch_match_odds_exact.v1",
            "event_type_id": _BETFAIR_FOOTBALL_EVENT_TYPE_ID,
            "market_type": _BETFAIR_MATCH_ODDS,
            "betting_type": _BETFAIR_ODDS,
            "requires_open": True,
            "requires_inplay_false": True,
            "requires_complete": True,
            "requires_runner_count": 3,
            "requires_active_runner_count": 3,
            "requires_winner_count": 1,
            "requires_runners_voidable_false": True,
            "requires_all_runner_status_active": True,
            "requires_empty_clarifications": True,
            "trust_root_sha256": trust_root.trust_root_sha256,
        }
    )
    source_revision = (
        f"betfair-live:{identity.market_id}:v{version}:"
        f"{roster_provenance_sha256[:16]}:{trust_root.trust_root_sha256[:16]}"
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
