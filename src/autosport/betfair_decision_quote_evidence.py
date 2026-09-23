"""Fail-closed decision-time executable quote evidence for Betfair MarketBook."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from time import monotonic_ns
from weakref import ref

from . import betfair_account_readonly as _base
from .betfair_marketbook_freshness import (
    BetfairMarketBookFreshnessError,
    _authenticated_application_key_context,
    _canonical_network_transport,
)

_METHOD = "SportsAPING/v1.0/listMarketBook"
_SCHEMA = "autosport.betfair_decision_quote_evidence.v1"
_SIDES = frozenset({"BACK", "LAY"})
_MAX_DEPTH = 10
_PRODUCT_MAX_DECISION_AGE_SECONDS = Decimal("5")


class BetfairDecisionQuoteError(BetfairMarketBookFreshnessError):
    """The exact decision-time quote authority could not be established."""


def _decimal(value: object, name: str, *, odds: bool = False) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise BetfairDecisionQuoteError(f"{name} must be a positive finite Decimal")
    if odds and value <= 1:
        raise BetfairDecisionQuoteError(f"{name} must be decimal odds greater than 1")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise BetfairDecisionQuoteError(f"{name} must be an exact integer >= {minimum}")
    return value


def _canonical_sha(value: object) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class BetfairPriceLevel:
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        _decimal(self.price, "price", odds=True)
        _decimal(self.size, "size")

    def payload(self) -> dict[str, str]:
        return {"price": str(self.price), "size": str(self.size)}


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairDecisionQuoteAssessment:
    evidence_id: str
    decision_eligible: bool
    reasons: tuple[str, ...]
    executable_size_at_or_better: Decimal
    requested_size: Decimal
    monotonic_age_seconds: Decimal
    utc_age_seconds: Decimal
    assessed_at: str
    assessed_monotonic_ns: int
    max_age_seconds: Decimal
    execution_delay_present: bool
    market_version_material_change_guard_available: bool = True
    market_version_price_lock_proven: bool = False
    execution_fill_proven: bool = False
    realized_price_proven: bool = False
    real_money_authorized: bool = False

    @property
    def assessment_id(self) -> str:
        return _canonical_sha(
            {
                "evidence_id": self.evidence_id,
                "eligible": self.decision_eligible,
                "reasons": list(self.reasons),
                "executable_size": str(self.executable_size_at_or_better),
                "requested_size": str(self.requested_size),
                "monotonic_age": str(self.monotonic_age_seconds),
                "utc_age": str(self.utc_age_seconds),
                "assessed_at": self.assessed_at,
                "assessed_monotonic_ns": self.assessed_monotonic_ns,
                "max_age": str(self.max_age_seconds),
                "execution_delay_present": self.execution_delay_present,
                "material_change_guard": (
                    self.market_version_material_change_guard_available
                ),
                "price_lock": self.market_version_price_lock_proven,
                "fill_proven": self.execution_fill_proven,
                "realized_price_proven": self.realized_price_proven,
                "real_money_authorized": self.real_money_authorized,
            }
        )

    def assert_authoritative(self) -> None:
        raise BetfairDecisionQuoteError("assessment is not product-issued")


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairDecisionQuoteEvidence:
    venue_id: str
    configured_account_ref: str
    adapter_id: str
    adapter_version: str
    market_id: str
    selection_id: int
    handicap: Decimal
    side: str
    decision_price: Decimal
    requested_size: Decimal
    best_prices_depth: int
    market_status: str
    runner_status: str
    market_version: int
    inplay: bool
    bet_delay_seconds: int
    is_market_data_delayed: bool
    levels: tuple[BetfairPriceLevel, ...]
    observed_at: str
    received_monotonic_ns: int
    source_payload_sha256: str
    authenticated_context_sha256: str | None = None
    developer_app_id: int | None = None
    application_version_id: int | None = None
    application_key_delay_data: bool | None = None
    application_key_active: bool | None = None
    application_key_owner_managed: bool | None = None

    def __post_init__(self) -> None:
        _base._required_text(self.venue_id, "venue_id")
        _base._required_text(self.configured_account_ref, "configured_account_ref")
        if self.adapter_id != _base.ADAPTER_ID or self.adapter_version != _base.ADAPTER_VERSION:
            raise BetfairDecisionQuoteError("Betfair adapter identity mismatch")
        _base._required_text(self.market_id, "market_id")
        _integer(self.selection_id, "selection_id", minimum=1)
        if type(self.handicap) is not Decimal or not self.handicap.is_finite():
            raise BetfairDecisionQuoteError("handicap must be a finite Decimal")
        if self.side not in _SIDES:
            raise BetfairDecisionQuoteError("side must be BACK or LAY")
        _decimal(self.decision_price, "decision_price", odds=True)
        _decimal(self.requested_size, "requested_size")
        depth = _integer(self.best_prices_depth, "best_prices_depth", minimum=1)
        if depth > _MAX_DEPTH:
            raise BetfairDecisionQuoteError("best_prices_depth must be <= 10")
        _base._required_text(self.market_status, "market_status")
        _base._required_text(self.runner_status, "runner_status")
        _integer(self.market_version, "market_version")
        if type(self.inplay) is not bool or type(self.is_market_data_delayed) is not bool:
            raise BetfairDecisionQuoteError("inplay/delay flags must be exact bool")
        _integer(self.bet_delay_seconds, "bet_delay_seconds")
        if type(self.levels) is not tuple or not all(
            type(level) is BetfairPriceLevel for level in self.levels
        ):
            raise BetfairDecisionQuoteError("levels must be exact BetfairPriceLevel tuple")
        if len(self.levels) > depth:
            raise BetfairDecisionQuoteError("provider returned more levels than requested")
        if len({level.price for level in self.levels}) != len(self.levels):
            raise BetfairDecisionQuoteError("provider depth contains duplicate prices")
        _base._iso_timestamp(self.observed_at, "observed_at")
        _integer(self.received_monotonic_ns, "received_monotonic_ns")
        _base._sha256_hex(self.source_payload_sha256, "source_payload_sha256")
        context = (
            self.authenticated_context_sha256,
            self.developer_app_id,
            self.application_version_id,
            self.application_key_delay_data,
            self.application_key_active,
            self.application_key_owner_managed,
        )
        if any(value is not None for value in context):
            if any(value is None for value in context):
                raise BetfairDecisionQuoteError("authenticated App Key context is partial")
            _base._sha256_hex(self.authenticated_context_sha256, "authenticated_context")
            _integer(self.developer_app_id, "developer_app_id", minimum=1)
            _integer(self.application_version_id, "application_version_id", minimum=1)
            if any(type(value) is not bool for value in context[3:]):
                raise BetfairDecisionQuoteError("authenticated App Key flags must be bool")

    @property
    def application_key_class(self) -> str:
        if self.application_key_delay_data is None:
            return "unknown"
        return "delayed" if self.application_key_delay_data else "live"

    @property
    def executable_size_at_or_better(self) -> Decimal:
        if self.side == "BACK":
            valid = (level.size for level in self.levels if level.price >= self.decision_price)
        else:
            valid = (level.size for level in self.levels if level.price <= self.decision_price)
        return sum(valid, Decimal("0"))

    @property
    def provider_publish_time(self) -> None:
        return None  # REST MarketBook is not Stream publish-time authority.

    @property
    def stream_continuity_proven(self) -> bool:
        return False

    @property
    def market_version_price_lock_proven(self) -> bool:
        return False

    @property
    def execution_fill_proven(self) -> bool:
        return False

    @property
    def realized_price_proven(self) -> bool:
        return False

    @property
    def real_money_authorized(self) -> bool:
        return False

    @property
    def evidence_id(self) -> str:
        return _canonical_sha(
            {
                "schema": _SCHEMA,
                "venue_id": self.venue_id,
                "configured_account_ref": self.configured_account_ref,
                "adapter_id": self.adapter_id,
                "adapter_version": self.adapter_version,
                "market_id": self.market_id,
                "selection_id": self.selection_id,
                "handicap": str(self.handicap),
                "side": self.side,
                "decision_price": str(self.decision_price),
                "requested_size": str(self.requested_size),
                "best_prices_depth": self.best_prices_depth,
                "market_status": self.market_status,
                "runner_status": self.runner_status,
                "market_version": self.market_version,
                "inplay": self.inplay,
                "bet_delay_seconds": self.bet_delay_seconds,
                "is_market_data_delayed": self.is_market_data_delayed,
                "levels": [level.payload() for level in self.levels],
                "observed_at": self.observed_at,
                "received_monotonic_ns": self.received_monotonic_ns,
                "source_payload_sha256": self.source_payload_sha256,
                "authenticated_context_sha256": self.authenticated_context_sha256,
                "developer_app_id": self.developer_app_id,
                "application_version_id": self.application_version_id,
                "application_key_delay_data": self.application_key_delay_data,
                "application_key_active": self.application_key_active,
                "application_key_owner_managed": self.application_key_owner_managed,
                "stream_continuity_proven": False,
                "market_version_price_lock_proven": False,
                "execution_fill_proven": False,
                "realized_price_proven": False,
                "real_money_authorized": False,
            }
        )

    def assert_provider_authoritative(self) -> None:
        raise BetfairDecisionQuoteError("decision quote is not product-issued")

    def assess(
        self,
        *,
        now_utc: datetime,
        now_monotonic_ns: int,
        max_age_seconds: Decimal,
    ) -> BetfairDecisionQuoteAssessment:
        raise BetfairDecisionQuoteError("decision quote is not product-issued")


def _levels(runner, side: str, depth: int) -> tuple[BetfairPriceLevel, ...]:
    exchange = _base._mapping(runner.get("ex"), "runner.ex")
    key = "availableToBack" if side == "BACK" else "availableToLay"
    raw_levels = _base._sequence(exchange.get(key), f"runner.ex.{key}")
    if len(raw_levels) > depth:
        raise BetfairDecisionQuoteError("provider returned more levels than requested")
    result = []
    for index, raw in enumerate(raw_levels):
        level = _base._mapping(raw, f"runner.ex.{key}[{index}]")
        result.append(
            BetfairPriceLevel(
                _base._number(level, "price", "price"),
                _base._number(level, "size", "size"),
            )
        )
    return tuple(result)


def _runner(row, selection_id: int, handicap: Decimal):
    matches = []
    for index, raw in enumerate(_base._sequence(row.get("runners"), "runners")):
        candidate = _base._mapping(raw, f"runners[{index}]")
        if (
            _base._provider_int(candidate, "selectionId", "selection_id") == selection_id
            and _base._number(candidate, "handicap", "handicap") == handicap
        ):
            matches.append(candidate)
    if len(matches) != 1:
        raise BetfairDecisionQuoteError("exact selection_id/handicap runner is ambiguous")
    return matches[0]


def _read(
    client: _base.BetfairReadOnlyClient,
    *,
    market_id: str,
    selection_id: int,
    handicap: Decimal,
    side: str,
    decision_price: Decimal,
    requested_size: Decimal,
    best_prices_depth: int,
    production_utc_now,
    production_monotonic_ns,
) -> BetfairDecisionQuoteEvidence:
    if type(client) is not _base.BetfairReadOnlyClient:
        raise TypeError("client must be an exact BetfairReadOnlyClient")
    market = _base._required_text(market_id, "market_id")
    selection = _integer(selection_id, "selection_id", minimum=1)
    if type(handicap) is not Decimal or not handicap.is_finite():
        raise BetfairDecisionQuoteError("handicap must be a finite Decimal")
    if side not in _SIDES:
        raise BetfairDecisionQuoteError("side must be BACK or LAY")
    price = _decimal(decision_price, "decision_price", odds=True)
    size = _decimal(requested_size, "requested_size")
    depth = _integer(best_prices_depth, "best_prices_depth", minimum=1)
    if depth > _MAX_DEPTH:
        raise BetfairDecisionQuoteError("best_prices_depth must be <= 10")

    network_origin = _canonical_network_transport(client)
    credentials = client._credentials
    request_id = client._next_request_id()
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": _METHOD,
            "params": {
                "marketIds": [market],
                "priceProjection": {
                    "priceData": ["EX_BEST_OFFERS"],
                    "exBestOffersOverrides": {"bestPricesDepth": depth},
                },
            },
            "id": request_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = client._transport.post(
        _base.BETTING_JSON_RPC_ENDPOINT,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Application": credentials.application_key,
            "X-Authentication": credentials.session_token,
        },
        body=body,
        timeout_seconds=client._timeout_seconds,
    )
    if not isinstance(payload, bytes):
        raise BetfairDecisionQuoteError("Betfair transport must return bytes")
    if network_origin:
        received_ns = _integer(
            production_monotonic_ns(),
            "product received_monotonic_ns",
        )
        captured_utc = production_utc_now(timezone.utc)
        if (
            not isinstance(captured_utc, datetime)
            or captured_utc.tzinfo is None
            or captured_utc.utcoffset() is None
        ):
            raise BetfairDecisionQuoteError(
                "product UTC clock must return timezone-aware datetime"
            )
        observed_at = captured_utc.astimezone(timezone.utc).isoformat()
    else:
        received_ns = _integer(monotonic_ns(), "received_monotonic_ns")
        observed_at = client._observed_at()

    decoded = _base._decode_json(payload)
    envelope = _base._mapping(decoded, "JSON-RPC response")
    if envelope.get("jsonrpc") != "2.0":
        raise BetfairDecisionQuoteError("invalid jsonrpc version")
    if type(envelope.get("id")) is not int or envelope.get("id") != request_id:
        raise BetfairDecisionQuoteError("response id does not match request id")
    if envelope.get("error") is not None:
        raise BetfairDecisionQuoteError("listMarketBook returned provider error")
    rows = _base._sequence(envelope.get("result"), "listMarketBook result")
    if len(rows) != 1:
        raise BetfairDecisionQuoteError("listMarketBook must return exactly one market")
    row = _base._mapping(rows[0], "listMarketBook[0]")
    if _base._provider_text(row, "marketId", "market_id") != market:
        raise BetfairDecisionQuoteError("listMarketBook returned a different market")
    market_version = _base._provider_int(row, "version", "market_version")
    bet_delay = _base._provider_int(row, "betDelay", "bet_delay_seconds")
    if market_version < 0 or bet_delay < 0:
        raise BetfairDecisionQuoteError("market version/betDelay must be non-negative")
    runner = _runner(row, selection, handicap)
    levels = _levels(runner, side, depth)

    context = (
        _authenticated_application_key_context(client, credentials=credentials)
        if network_origin
        else None
    )
    if network_origin and client._credentials is not credentials:
        raise BetfairDecisionQuoteError("authenticated Betfair context changed")

    return BetfairDecisionQuoteEvidence(
        client._venue_id,
        client._account_id,
        _base.ADAPTER_ID,
        _base.ADAPTER_VERSION,
        market,
        selection,
        handicap,
        side,
        price,
        size,
        depth,
        _base._provider_text(row, "status", "market_status"),
        _base._provider_text(runner, "status", "runner_status"),
        market_version,
        _base._provider_bool(row, "inplay"),
        bet_delay,
        _base._provider_bool(row, "isMarketDataDelayed"),
        levels,
        observed_at,
        received_ns,
        sha256(payload).hexdigest(),
        None if context is None else context.authenticated_context_sha256,
        None if context is None else context.developer_app_id,
        None if context is None else context.application_version_id,
        None if context is None else context.delay_data,
        None if context is None else context.active,
        None if context is None else context.owner_managed,
    )


def _install_authority() -> None:
    # Freeze production clock callables inside the authority closure. Module-global
    # rebinding remains useful for non-authoritative injected/test transports, but
    # cannot redefine freshness for product-issued network evidence/assessments.
    production_utc_now = datetime.now
    production_monotonic_ns = monotonic_ns

    evidence_registry: dict[int, tuple[object, str, bool, object, object]] = {}
    assessment_registry: dict[int, tuple[object, str]] = {}
    validate = BetfairDecisionQuoteEvidence.__post_init__

    def read_betfair_decision_quote(
        client: _base.BetfairReadOnlyClient,
        *,
        market_id: str,
        selection_id: int,
        handicap: Decimal,
        side: str,
        decision_price: Decimal,
        requested_size: Decimal,
        best_prices_depth: int = 3,
    ) -> BetfairDecisionQuoteEvidence:
        positive_origin = _canonical_network_transport(client)
        credentials = client._credentials
        evidence = _read(
            client,
            market_id=market_id,
            selection_id=selection_id,
            handicap=handicap,
            side=side,
            decision_price=decision_price,
            requested_size=requested_size,
            best_prices_depth=best_prices_depth,
            production_utc_now=production_utc_now,
            production_monotonic_ns=production_monotonic_ns,
        )
        if positive_origin and client._credentials is not credentials:
            raise BetfairDecisionQuoteError("authenticated context changed before issuance")
        key = id(evidence)
        evidence_registry[key] = (
            ref(evidence, lambda _r, key=key: evidence_registry.pop(key, None)),
            evidence.evidence_id,
            positive_origin,
            client,
            credentials,
        )
        return evidence

    def provider_record(self: BetfairDecisionQuoteEvidence):
        validate(self)
        record = evidence_registry.get(id(self))
        if record is None or record[0]() is not self or record[1] != self.evidence_id:
            raise BetfairDecisionQuoteError("decision quote is not product-issued")
        if not record[2]:
            raise BetfairDecisionQuoteError("decision quote lacks production network origin")
        client, credentials = record[3], record[4]
        if (
            self.authenticated_context_sha256 is None
            or self.application_key_class != "live"
            or self.application_key_active is not True
            or type(client) is not _base.BetfairReadOnlyClient
            or client._credentials is not credentials
            or not _canonical_network_transport(client)
        ):
            raise BetfairDecisionQuoteError("decision quote lacks current live App Key authority")
        return record

    def assert_provider_authoritative(self: BetfairDecisionQuoteEvidence) -> None:
        provider_record(self)

    def assess(
        self: BetfairDecisionQuoteEvidence,
        *,
        now_utc: datetime,
        now_monotonic_ns: int,
        max_age_seconds: Decimal,
    ) -> BetfairDecisionQuoteAssessment:
        provider_record(self)
        if (
            not isinstance(now_utc, datetime)
            or now_utc.tzinfo is None
            or now_utc.utcoffset() is None
        ):
            raise BetfairDecisionQuoteError("now_utc must be timezone-aware datetime")
        caller_ns = _integer(now_monotonic_ns, "now_monotonic_ns")
        requested_max_age = _decimal(max_age_seconds, "max_age_seconds")
        if requested_max_age > _PRODUCT_MAX_DECISION_AGE_SECONDS:
            raise BetfairDecisionQuoteError(
                "max_age_seconds exceeds product freshness ceiling"
            )

        product_ns = _integer(
            production_monotonic_ns(),
            "product now_monotonic_ns",
        )
        product_utc = production_utc_now(timezone.utc)
        if (
            not isinstance(product_utc, datetime)
            or product_utc.tzinfo is None
            or product_utc.utcoffset() is None
        ):
            raise BetfairDecisionQuoteError(
                "product UTC clock must return timezone-aware datetime"
            )
        product_utc = product_utc.astimezone(timezone.utc)
        caller_utc = now_utc.astimezone(timezone.utc)

        product_elapsed_ns = product_ns - self.received_monotonic_ns
        caller_elapsed_ns = caller_ns - self.received_monotonic_ns
        product_mono_age = Decimal(product_elapsed_ns) / Decimal("1000000000")
        caller_mono_age = Decimal(caller_elapsed_ns) / Decimal("1000000000")
        mono_age = max(product_mono_age, caller_mono_age)

        observed = _base._iso_timestamp(self.observed_at, "observed_at").astimezone(
            timezone.utc
        )
        product_delta = product_utc - observed
        caller_delta = caller_utc - observed
        product_utc_age = Decimal(str(product_delta.total_seconds()))
        caller_utc_age = Decimal(str(caller_delta.total_seconds()))
        utc_age = max(product_utc_age, caller_utc_age)

        reasons = []
        if product_elapsed_ns < 0 or caller_elapsed_ns < 0:
            reasons.append("MONOTONIC_CLOCK_PRECEDES_CAPTURE")
        elif mono_age > requested_max_age:
            reasons.append("MONOTONIC_STALE")
        if product_utc_age < 0 or caller_utc_age < 0:
            reasons.append("UTC_CLOCK_PRECEDES_CAPTURE")
        elif utc_age > requested_max_age:
            reasons.append("UTC_STALE")
        if self.application_key_delay_data is not False:
            reasons.append("APPLICATION_KEY_DELAYED_OR_UNKNOWN")
        if self.is_market_data_delayed:
            reasons.append("MARKET_DATA_DELAYED")
        if self.market_status != "OPEN":
            reasons.append("MARKET_NOT_OPEN")
        if self.runner_status != "ACTIVE":
            reasons.append("RUNNER_NOT_ACTIVE")
        if self.executable_size_at_or_better < self.requested_size:
            reasons.append("INSUFFICIENT_DISPLAYED_DEPTH")
        assessment = BetfairDecisionQuoteAssessment(
            self.evidence_id,
            not reasons,
            tuple(reasons),
            self.executable_size_at_or_better,
            self.requested_size,
            mono_age,
            utc_age,
            product_utc.isoformat(),
            product_ns,
            requested_max_age,
            self.bet_delay_seconds > 0,
        )
        key = id(assessment)
        assessment_registry[key] = (
            ref(assessment, lambda _r, key=key: assessment_registry.pop(key, None)),
            assessment.assessment_id,
        )
        return assessment

    def assert_assessment_authoritative(self: BetfairDecisionQuoteAssessment) -> None:
        record = assessment_registry.get(id(self))
        if record is None or record[0]() is not self or record[1] != self.assessment_id:
            raise BetfairDecisionQuoteError("assessment is not product-issued")

    globals()["read_betfair_decision_quote"] = read_betfair_decision_quote
    BetfairDecisionQuoteEvidence.assert_provider_authoritative = assert_provider_authoritative
    BetfairDecisionQuoteEvidence.assess = assess
    BetfairDecisionQuoteAssessment.assert_authoritative = assert_assessment_authoritative


_install_authority()
del _install_authority
