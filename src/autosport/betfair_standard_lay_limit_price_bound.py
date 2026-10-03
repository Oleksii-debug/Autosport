"""Prospective adverse-price ceiling for one exact Betfair standard LAY LIMIT request.

The resolver consumes candidate serialized ``placeOrders`` request bytes and
proves only a provider-contract price property: for an ordinary Betfair Exchange
LAY LIMIT order, every matched fragment is at requested odds or better for the
layer, so the requested odds are the maximum adverse matched odds.  For layers,
lower matched odds reduce liability and are therefore better.

Betfair documents that Best Price Execution improves prices when enabled and
lapses rather than improving a request when disabled.  Betfair also documents
MatchMe as a feature that can relax the requested-price boundary, but MatchMe is
BACK-only.  The resolver rejects nonstandard placement options rather than
silently extending this ordinary-LAY proof to different order semantics.

This module does not widen Autosport's current BACK-only Betfair write seam.
It deliberately does *not* prove where the supplied bytes came from, whether
the market-specific price ladder/selection makes the request valid, or that the
request was sent, accepted, matched, or reconciled.  It does not prove latency,
commission, exact realized fill price, or real-money execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any

_SCHEMA_VERSION = 1
# Keep the provider wire method local so a future write seam can import this resolver
# without creating a circular dependency back into execution code.
_PLACE_ORDERS_METHOD = "SportsAPING/v1.0/placeOrders"
_PROVIDER_ID = "betfair"
_PROVIDER_CONTRACT_ID = "betfair-exchange-standard-lay-limit-price-ceiling-v1"
_PROVIDER_CONTRACT_REFS = (
    (
        "https://betfair-developer-docs.atlassian.net/wiki/spaces/"
        "1smk3cen4v3lu3yomq5qye0ni/pages/2687496/"
    ),
    "https://support.betfair.com/app/answers/detail/404-exchange-what-is-best-price-execution/",
    "https://support.betfair.com/app/answers/detail/a_id/10001",
    "https://support.betfair.com/app/answers/detail/a_id/10620",
    (
        "https://support.developer.betfair.com/hc/en-us/articles/"
        "360017675098-How-do-I-improve-the-chances-of-my-bet-being-matched"
    ),
)
_MIN_ODDS = Decimal("1.01")
_MAX_ODDS = Decimal("1000")


class BetfairStandardLayLimitPriceBoundError(ValueError):
    """The request cannot support the bounded ordinary-LAY price claim."""


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairStandardLayLimitPriceBoundError(
            "LAY price-bound evidence is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetfairStandardLayLimitPriceBoundError(
            f"{field} must be non-empty canonical text"
        )
    return value


def _lower_hex_ref(
    value: object,
    field: str,
    *,
    exact_length: int | None = None,
) -> str:
    raw = _text(value, field)
    if exact_length is not None and len(raw) != exact_length:
        raise BetfairStandardLayLimitPriceBoundError(
            f"{field} must be exactly {exact_length} lowercase hex characters"
        )
    if exact_length is None and len(raw) > 32:
        raise BetfairStandardLayLimitPriceBoundError(
            f"{field} must be at most 32 lowercase hex characters"
        )
    if any(character not in "0123456789abcdef" for character in raw):
        raise BetfairStandardLayLimitPriceBoundError(
            f"{field} must contain only lowercase hex characters"
        )
    return raw


def _positive_decimal_text(value: object, field: str) -> Decimal:
    raw = _text(value, field)
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise BetfairStandardLayLimitPriceBoundError(
            f"{field} must be exact finite decimal text"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise BetfairStandardLayLimitPriceBoundError(
            f"{field} must be exact finite decimal text > 0"
        )
    return parsed


def _decode_request(body: object) -> dict[str, object]:
    if type(body) is not bytes or not body:
        raise BetfairStandardLayLimitPriceBoundError(
            "placeOrders request must be non-empty exact bytes"
        )

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise BetfairStandardLayLimitPriceBoundError(
                    "placeOrders request contains duplicate JSON object keys"
                )
            decoded[key] = value
        return decoded

    def reject_constant(value: str) -> object:
        raise BetfairStandardLayLimitPriceBoundError(
            f"placeOrders request contains non-finite JSON number {value}"
        )

    try:
        decoded = json.loads(
            body.decode("utf-8"),
            parse_float=Decimal,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise BetfairStandardLayLimitPriceBoundError(
            "placeOrders request must be UTF-8 JSON"
        ) from exc
    except json.JSONDecodeError as exc:
        raise BetfairStandardLayLimitPriceBoundError(
            "placeOrders request must be valid JSON"
        ) from exc
    if type(decoded) is not dict:
        raise BetfairStandardLayLimitPriceBoundError(
            "placeOrders request envelope must be a JSON object"
        )
    return decoded


@dataclass(frozen=True, slots=True, init=False)
class BetfairStandardLayLimitPriceBoundEvidence:
    """Product-issued evidence for the prospective ordinary-LAY price ceiling."""

    request_sha256: str
    market_id: str
    selection_id: int
    handicap: int
    customer_ref: str
    customer_order_ref: str
    requested_stake: Decimal
    price_ceiling_odds: Decimal
    persistence_type: str
    provider_id: str
    provider_contract_id: str
    provider_contract_refs: tuple[str, ...]
    adverse_price_relation: str
    request_shape_proven: bool
    matched_fragment_price_ceiling_proven: bool
    request_origin_proven: bool
    provider_request_validity_proven: bool
    acceptance_proven: bool
    fill_proven: bool
    latency_proven: bool
    commission_proven: bool
    realized_price_exact: bool
    real_money_execution_proven: bool

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise BetfairStandardLayLimitPriceBoundError(
            "BetfairStandardLayLimitPriceBoundEvidence is issued only by the canonical resolver"
        )

    def _validate(self) -> None:
        if (
            type(self.request_sha256) is not str
            or len(self.request_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.request_sha256)
        ):
            raise BetfairStandardLayLimitPriceBoundError(
                "request_sha256 must be lowercase SHA-256"
            )
        _text(self.market_id, "market_id")
        _lower_hex_ref(self.customer_ref, "customer_ref", exact_length=32)
        _lower_hex_ref(self.customer_order_ref, "customer_order_ref")
        expected_customer_ref = sha256(
            f"placeOrders:{self.customer_order_ref}".encode("utf-8")
        ).hexdigest()[:32]
        if self.customer_ref != expected_customer_ref:
            raise BetfairStandardLayLimitPriceBoundError(
                "customer_ref does not match canonical customer_order_ref projection"
            )
        if type(self.selection_id) is not int or self.selection_id <= 0:
            raise BetfairStandardLayLimitPriceBoundError(
                "selection_id must be positive int"
            )
        if type(self.handicap) is not int or self.handicap != 0:
            raise BetfairStandardLayLimitPriceBoundError(
                "only the canonical zero-handicap request projection is supported"
            )
        if (
            type(self.requested_stake) is not Decimal
            or not self.requested_stake.is_finite()
            or self.requested_stake <= 0
        ):
            raise BetfairStandardLayLimitPriceBoundError(
                "requested_stake must be finite positive Decimal"
            )
        if (
            type(self.price_ceiling_odds) is not Decimal
            or not self.price_ceiling_odds.is_finite()
            or self.price_ceiling_odds < _MIN_ODDS
            or self.price_ceiling_odds > _MAX_ODDS
        ):
            raise BetfairStandardLayLimitPriceBoundError(
                "price_ceiling_odds must be within Betfair exchange odds bounds"
            )
        if self.persistence_type != "LAPSE":
            raise BetfairStandardLayLimitPriceBoundError(
                "only canonical LAPSE standard LIMIT requests are supported"
            )
        if (
            self.provider_id != _PROVIDER_ID
            or self.provider_contract_id != _PROVIDER_CONTRACT_ID
            or self.provider_contract_refs != _PROVIDER_CONTRACT_REFS
            or self.adverse_price_relation != "MATCHED_LAY_ODDS_LE_REQUESTED_ODDS"
        ):
            raise BetfairStandardLayLimitPriceBoundError(
                "LAY price-bound authority metadata drifted"
            )
        if self.request_shape_proven is not True:
            raise BetfairStandardLayLimitPriceBoundError(
                "positive LAY price bound requires exact request-shape proof"
            )
        if self.matched_fragment_price_ceiling_proven is not True:
            raise BetfairStandardLayLimitPriceBoundError(
                "ordinary LAY evidence must bind the matched-fragment price ceiling"
            )
        for field in (
            "request_origin_proven",
            "provider_request_validity_proven",
            "acceptance_proven",
            "fill_proven",
            "latency_proven",
            "commission_proven",
            "realized_price_exact",
            "real_money_execution_proven",
        ):
            if getattr(self, field) is not False:
                raise BetfairStandardLayLimitPriceBoundError(
                    f"prospective LAY price bound cannot claim {field}"
                )

    @property
    def evidence_id(self) -> str:
        return _digest(self.to_dict(include_evidence_id=False))

    def to_dict(self, *, include_evidence_id: bool = True) -> dict[str, Any]:
        self._validate()
        payload: dict[str, Any] = {
            "schema": "autosport.betfair_standard_lay_limit_price_bound",
            "schema_version": _SCHEMA_VERSION,
            "request_sha256": self.request_sha256,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "handicap": self.handicap,
            "customer_ref": self.customer_ref,
            "customer_order_ref": self.customer_order_ref,
            "requested_stake": str(self.requested_stake),
            "price_ceiling_odds": str(self.price_ceiling_odds),
            "persistence_type": self.persistence_type,
            "provider_id": self.provider_id,
            "provider_contract_id": self.provider_contract_id,
            "provider_contract_refs": list(self.provider_contract_refs),
            "adverse_price_relation": self.adverse_price_relation,
            "request_shape_proven": True,
            "matched_fragment_price_ceiling_proven": True,
            "request_origin_proven": False,
            "provider_request_validity_proven": False,
            "acceptance_proven": False,
            "fill_proven": False,
            "latency_proven": False,
            "commission_proven": False,
            "realized_price_exact": False,
            "real_money_execution_proven": False,
        }
        if include_evidence_id:
            payload["evidence_id"] = _digest(payload)
        return payload


def _issue_evidence(
    *,
    request_body: bytes,
    market_id: str,
    selection_id: int,
    handicap: int,
    customer_ref: str,
    customer_order_ref: str,
    requested_stake: Decimal,
    price_ceiling_odds: Decimal,
    persistence_type: str,
) -> BetfairStandardLayLimitPriceBoundEvidence:
    item = object.__new__(BetfairStandardLayLimitPriceBoundEvidence)
    values = {
        "request_sha256": sha256(request_body).hexdigest(),
        "market_id": market_id,
        "selection_id": selection_id,
        "handicap": handicap,
        "customer_ref": customer_ref,
        "customer_order_ref": customer_order_ref,
        "requested_stake": requested_stake,
        "price_ceiling_odds": price_ceiling_odds,
        "persistence_type": persistence_type,
        "provider_id": _PROVIDER_ID,
        "provider_contract_id": _PROVIDER_CONTRACT_ID,
        "provider_contract_refs": _PROVIDER_CONTRACT_REFS,
        "adverse_price_relation": "MATCHED_LAY_ODDS_LE_REQUESTED_ODDS",
        "request_shape_proven": True,
        "matched_fragment_price_ceiling_proven": True,
        "request_origin_proven": False,
        "provider_request_validity_proven": False,
        "acceptance_proven": False,
        "fill_proven": False,
        "latency_proven": False,
        "commission_proven": False,
        "realized_price_exact": False,
        "real_money_execution_proven": False,
    }
    for field, value in values.items():
        object.__setattr__(item, field, value)
    item._validate()
    return item


def resolve_betfair_standard_lay_limit_price_bound(
    request_body: bytes,
) -> BetfairStandardLayLimitPriceBoundEvidence:
    """Validate exact request bytes and issue the ordinary-LAY price ceiling.

    The exact accepted shape mirrors the canonical Autosport Betfair
    ``placeOrders`` projection, except that ``side`` must be ``LAY``.  This does
    not prove that Autosport or any particular adapter emitted the bytes.
    Rejecting unknown keys is intentional: any new Betfair order option must
    receive its own economic-semantics review instead of inheriting this proof.
    """

    envelope = _decode_request(request_body)
    if set(envelope) != {"jsonrpc", "method", "params", "id"}:
        raise BetfairStandardLayLimitPriceBoundError(
            "nonstandard Betfair JSON-RPC envelope is not supported"
        )
    if envelope.get("jsonrpc") != "2.0" or envelope.get("method") != _PLACE_ORDERS_METHOD:
        raise BetfairStandardLayLimitPriceBoundError(
            "request is not canonical Betfair placeOrders JSON-RPC"
        )
    request_id = envelope.get("id")
    if type(request_id) is not int or request_id <= 0:
        raise BetfairStandardLayLimitPriceBoundError(
            "placeOrders request id must be positive int"
        )

    params = envelope.get("params")
    if type(params) is not dict or set(params) != {
        "marketId",
        "instructions",
        "customerRef",
        "async",
    }:
        raise BetfairStandardLayLimitPriceBoundError(
            "nonstandard Betfair placeOrders params are not supported"
        )
    market_id = _text(params.get("marketId"), "marketId")
    customer_ref = _lower_hex_ref(
        params.get("customerRef"),
        "customerRef",
        exact_length=32,
    )
    if params.get("async") is not False:
        raise BetfairStandardLayLimitPriceBoundError(
            "canonical bounded request requires synchronous placeOrders"
        )
    instructions = params.get("instructions")
    if type(instructions) is not list or len(instructions) != 1:
        raise BetfairStandardLayLimitPriceBoundError(
            "bounded request requires exactly one Betfair instruction"
        )

    instruction = instructions[0]
    if type(instruction) is not dict or set(instruction) != {
        "selectionId",
        "handicap",
        "side",
        "orderType",
        "limitOrder",
        "customerOrderRef",
    }:
        raise BetfairStandardLayLimitPriceBoundError(
            "nonstandard Betfair LAY instruction is not supported"
        )
    selection_id = instruction.get("selectionId")
    if type(selection_id) is not int or selection_id <= 0:
        raise BetfairStandardLayLimitPriceBoundError(
            "selectionId must be positive int"
        )
    handicap = instruction.get("handicap")
    if type(handicap) is not int or handicap != 0:
        raise BetfairStandardLayLimitPriceBoundError(
            "only canonical zero-handicap instruction is supported"
        )
    if instruction.get("side") != "LAY" or instruction.get("orderType") != "LIMIT":
        raise BetfairStandardLayLimitPriceBoundError(
            "price ceiling applies only to standard LAY LIMIT instructions"
        )
    customer_order_ref = _lower_hex_ref(
        instruction.get("customerOrderRef"),
        "customerOrderRef",
    )
    expected_customer_ref = sha256(
        f"placeOrders:{customer_order_ref}".encode("utf-8")
    ).hexdigest()[:32]
    if customer_ref != expected_customer_ref:
        raise BetfairStandardLayLimitPriceBoundError(
            "customerRef does not match canonical customerOrderRef projection"
        )

    limit_order = instruction.get("limitOrder")
    if type(limit_order) is not dict or set(limit_order) != {
        "size",
        "price",
        "persistenceType",
    }:
        raise BetfairStandardLayLimitPriceBoundError(
            "special or nonstandard Betfair LIMIT semantics are not supported"
        )
    requested_stake = _positive_decimal_text(limit_order.get("size"), "limitOrder.size")
    requested_odds = _positive_decimal_text(limit_order.get("price"), "limitOrder.price")
    # This is only the broad Exchange odds range.  Market-specific ladder
    # validity remains explicitly unproven by this request-shape contract.
    if requested_odds < _MIN_ODDS or requested_odds > _MAX_ODDS:
        raise BetfairStandardLayLimitPriceBoundError(
            "limitOrder.price is outside Betfair exchange odds bounds"
        )
    persistence_type = limit_order.get("persistenceType")
    if persistence_type != "LAPSE":
        raise BetfairStandardLayLimitPriceBoundError(
            "only canonical LAPSE standard LIMIT requests are supported"
        )

    return _issue_evidence(
        request_body=request_body,
        market_id=market_id,
        selection_id=selection_id,
        handicap=handicap,
        customer_ref=customer_ref,
        customer_order_ref=customer_order_ref,
        requested_stake=requested_stake,
        price_ceiling_odds=requested_odds,
        persistence_type=persistence_type,
    )
