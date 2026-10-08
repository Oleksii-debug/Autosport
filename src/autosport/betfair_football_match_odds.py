from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
_RULES_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_RULES_ORIGIN_TRUST_SCOPE = "trusted-autosport-process-v1"

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


class _VisibleRuleText(HTMLParser):
    """Extract visible support-page text without treating script/style bytes as rules."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data:
            self.parts.append(data)

    @property
    def text(self) -> str:
        return " ".join(self.parts)


class _RejectRuleRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        del req, fp, code, msg, headers, newurl
        return None


@dataclass(frozen=True, slots=True)
class BetfairFootballRulesAuthority:
    """Provider-rule DTO; positive authority requires product-owned capture issuance."""

    exchange_rules_url: str
    football_rules_url: str
    exchange_rules_sha256: str
    football_rules_sha256: str
    retrieved_at: str
    profile_id: str
    origin_trust_scope: str = _RULES_ORIGIN_TRUST_SCOPE

    def __post_init__(self) -> None:
        if self.profile_id != _RULES_PROFILE_ID:
            raise ValueError("unsupported Betfair football rule evidence profile")
        if self.origin_trust_scope != _RULES_ORIGIN_TRUST_SCOPE:
            raise ValueError("unsupported Betfair football rule origin trust scope")
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
        for field_name, digest in (
            ("exchange_rules_sha256", self.exchange_rules_sha256),
            ("football_rules_sha256", self.football_rules_sha256),
        ):
            if (
                type(digest) is not str
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{field_name} must be canonical lowercase SHA-256")
        _canonical_timestamp("rules retrieved_at", self.retrieved_at)

    @property
    def authority_sha256(self) -> str:
        return _sha256_json(
            {
                "profile_id": self.profile_id,
                "origin_trust_scope": self.origin_trust_scope,
                "exchange_rules_url": self.exchange_rules_url,
                "football_rules_url": self.football_rules_url,
                "exchange_rules_sha256": self.exchange_rules_sha256,
                "football_rules_sha256": self.football_rules_sha256,
                "retrieved_at": self.retrieved_at,
            }
        )


def _build_rules_authority():
    """Build the product-owned fixed-origin rule acquisition/issuance boundary.

    This mirrors the repository's canonical trusted-process provider authority model:
    supported callers can request a bounded production read, but cannot inject URLs,
    response text, transport, clock, or a pre-built authority.  Positive authority is
    process-local and must be reacquired after restart.  It is not an OS sandbox
    against arbitrary introspection or monkeypatching inside the trusted process.
    """

    authority_type = BetfairFootballRulesAuthority
    authority_post_init = authority_type.__dict__["__post_init__"]
    request_type = Request
    opener = build_opener(_RejectRuleRedirects())
    opener_type = type(opener)
    opener_open = opener_type.__dict__["open"]
    now = datetime.now
    utc = timezone.utc
    issued: dict[int, tuple[BetfairFootballRulesAuthority, tuple[object, ...]]] = {}

    def projection(source: BetfairFootballRulesAuthority) -> tuple[object, ...]:
        return (
            source.exchange_rules_url,
            source.football_rules_url,
            source.exchange_rules_sha256,
            source.football_rules_sha256,
            source.retrieved_at,
            source.profile_id,
            source.origin_trust_scope,
            source.authority_sha256,
        )

    def register(source: BetfairFootballRulesAuthority) -> BetfairFootballRulesAuthority:
        if type(source) is not authority_type:
            raise TypeError("rule authority must be exact BetfairFootballRulesAuthority")
        authority_post_init(source)
        issued[id(source)] = (source, projection(source))
        return source

    def validate(source: BetfairFootballRulesAuthority) -> BetfairFootballRulesAuthority:
        if type(source) is not authority_type:
            raise TypeError("rules_authority must be exact BetfairFootballRulesAuthority")
        authority_post_init(source)
        registered = issued.get(id(source))
        if registered is None or registered[0] is not source:
            raise ValueError(
                "Betfair football rule authority must be issued by canonical provider capture"
            )
        if registered[1] != projection(source):
            raise ValueError("Betfair football rule authority no longer matches issued identity")
        return source

    def read_page(expected_url: str, *, timeout: float) -> tuple[str, str]:
        if type(opener) is not opener_type or opener_type.__dict__.get("open") is not opener_open:
            raise ValueError("Betfair rule network opener executable drifted")
        request_url = expected_url + "/"
        request = request_type(
            request_url,
            headers={
                "Accept": "text/html,text/plain;q=0.9",
                "User-Agent": "Autosport/1 provider-rule-evidence",
            },
            method="GET",
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                final_url = str(response.geturl()).rstrip("/")
                if final_url != expected_url:
                    raise ValueError("Betfair rule response final URL is not canonical")
                content_type = str(response.headers.get("Content-Type", "")).casefold()
                if not (
                    content_type.startswith("text/html")
                    or content_type.startswith("text/plain")
                ):
                    raise ValueError("Betfair rule response content type is not text")
                raw = response.read(_RULES_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise ValueError(f"Betfair rule acquisition HTTP_{int(exc.code)}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ValueError("Betfair rule acquisition unavailable") from exc
        if len(raw) > _RULES_MAX_RESPONSE_BYTES:
            raise ValueError("Betfair rule response exceeds maximum size")
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Betfair rule response is not UTF-8") from exc
        parser = _VisibleRuleText()
        parser.feed(html)
        parser.close()
        visible_text = parser.text
        if not visible_text.strip():
            raise ValueError("Betfair rule response has no visible text")
        return hashlib.sha256(raw).hexdigest(), visible_text

    def capture(*, timeout_seconds: float = 10.0) -> BetfairFootballRulesAuthority:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be a positive finite number")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        exchange_sha256, exchange_text = read_page(_EXCHANGE_RULES_URL, timeout=timeout)
        football_sha256, football_text = read_page(_FOOTBALL_RULES_URL, timeout=timeout)
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
        retrieved, _ = _canonical_timestamp(
            "rules retrieved_at",
            now(utc).isoformat(),
        )
        return register(
            authority_type(
                exchange_rules_url=_EXCHANGE_RULES_URL,
                football_rules_url=_FOOTBALL_RULES_URL,
                exchange_rules_sha256=exchange_sha256,
                football_rules_sha256=football_sha256,
                retrieved_at=retrieved,
                profile_id=_RULES_PROFILE_ID,
            )
        )

    return capture, validate


(
    capture_betfair_football_rules_evidence,
    validate_betfair_football_rules_authority,
) = _build_rules_authority()


def verify_betfair_football_rules_evidence(
    *,
    exchange_rules_url: str,
    exchange_rules_text: str,
    football_rules_url: str,
    football_rules_text: str,
    retrieved_at: str,
) -> BetfairFootballRulesAuthority:
    """Fail closed: caller-supplied URL/text is descriptive input, not provider origin."""

    del (
        exchange_rules_url,
        exchange_rules_text,
        football_rules_url,
        football_rules_text,
        retrieved_at,
    )
    raise TypeError(
        "caller-supplied rule text cannot mint provider authority; "
        "use capture_betfair_football_rules_evidence"
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
    validate_betfair_football_rules_authority(rules_authority)
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
    if (
        type(market_book.get("marketId")) is not str
        or market_book.get("marketId") != identity.market_id
    ):
        return _refused(identity, "betfair_market_id_mismatch")
    if (
        type(market_description.get("marketType")) is not str
        or market_description.get("marketType") != _BETFAIR_MATCH_ODDS
    ):
        return _refused(identity, "betfair_market_type_is_not_match_odds")
    if (
        type(market_description.get("bettingType")) is not str
        or market_description.get("bettingType") != _BETFAIR_ODDS
    ):
        return _refused(identity, "betfair_betting_type_is_not_odds")

    market_rules = market_description.get("rules")
    if type(market_rules) is not str or not market_rules.strip():
        return _refused(identity, "betfair_market_information_rules_missing")
    if type(market_description.get("rulesHasDate")) is not bool:
        return _refused(identity, "betfair_market_information_rules_date_truth_missing")
    if not _empty_clarifications(market_description.get("clarifications")):
        return _refused(identity, "betfair_market_information_has_unreviewed_clarifications")

    required_book_truth = (
        ("status", str, "OPEN"),
        ("inplay", bool, False),
        ("complete", bool, True),
        ("numberOfWinners", int, 1),
        ("numberOfRunners", int, 3),
        ("numberOfActiveRunners", int, 3),
        ("runnersVoidable", bool, False),
        ("betDelay", int, 0),
    )
    for field_name, expected_type, expected in required_book_truth:
        value = market_book.get(field_name)
        if type(value) is not expected_type or value != expected:
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
        if type(runner) is not dict:
            return _refused(identity, "betfair_live_runner_identity_missing")
        raw_selection_id = runner.get("selectionId")
        if type(raw_selection_id) is not int or raw_selection_id <= 0:
            return _refused(identity, "betfair_live_runner_identity_not_exact")
        if type(runner.get("status")) is not str or runner.get("status") != "ACTIVE":
            return _refused(identity, "betfair_live_runner_is_not_active")
        selection_id = str(raw_selection_id)
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
