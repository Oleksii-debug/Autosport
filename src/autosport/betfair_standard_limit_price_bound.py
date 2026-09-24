from __future__ import annotations

"""Decision-time Betfair standard-LIMIT request-shape evidence.

This module is deliberately narrower than realized execution/slippage truth. It
re-resolves an exact canonical supervised execution action and captures the
ordinary BACK LIMIT request emitted by the canonical Betfair supervised write
adapter. Request shape alone is not enough to prove a prospective per-fragment
price floor: Betfair MatchMe can match BACK bets at lower prices during initial
placement, and the product currently has no product-owned authority proving
MatchMe is disabled or inapplicable for the exact API execution account.

Accordingly the current resolver is explicitly fail-closed: it can prove the
standard request projection, but the economic adverse-price conclusion remains
UNKNOWN_MATCHME_APPLICABILITY. It does not predict fill, timing, acceptance, or
realized price.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .betfair_supervised_execution import (
    PLACE_ORDERS_METHOD,
    BetfairSupervisedPlaceOrdersClient,
    WRITE_ADAPTER_ID,
    WRITE_ADAPTER_VERSION,
)
from .real_execution_ledger import ExecutionAction, ExecutionPlan
from .supervised_execution import (
    BoundSupervisedExecutionPlan,
    SupervisedExecutionError,
)


_SCHEMA_VERSION = 2
_PROVIDER_ID = "betfair"
_PROVIDER_CONTRACT_ID = "betfair-exchange-standard-back-limit-fragment-floor-v1"
_PROVIDER_CONTRACT_REF = (
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/"
    "pages/2687154/New+API+Release+-+8th+August+2016"
)
_WRITE_ADAPTER_ID = WRITE_ADAPTER_ID
_WRITE_ADAPTER_VERSION = WRITE_ADAPTER_VERSION
_CANONICAL_PLACE_ACTION = BetfairSupervisedPlaceOrdersClient.place_action
_CAPTURE_PROVIDER_ORDER_REF = "0" * 32
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BetfairStandardLimitPriceBoundError(ValueError):
    """Raised when an exact standard-LIMIT bound cannot be issued."""


class BetfairStandardLimitPriceBoundStatus(StrEnum):
    UNKNOWN_MATCHME_APPLICABILITY = "UNKNOWN_MATCHME_APPLICABILITY"
    PROVIDER_BOUND_ZERO_ADVERSE_PRICE_DETERIORATION = (
        "PROVIDER_BOUND_ZERO_ADVERSE_PRICE_DETERIORATION"
    )


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetfairStandardLimitPriceBoundError(
            f"{field} must be non-empty canonical text"
        )
    return value


def _sha(value: object, field: str) -> str:
    raw = _text(value, field)
    if _SHA256_RE.fullmatch(raw) is None:
        raise BetfairStandardLimitPriceBoundError(
            f"{field} must be lowercase SHA-256"
        )
    return raw


def _time(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairStandardLimitPriceBoundError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairStandardLimitPriceBoundError(
            f"{field} must be timezone-aware"
        )
    return parsed


def _positive_decimal(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise BetfairStandardLimitPriceBoundError(
            f"{field} must be an exact finite positive Decimal"
        )
    return value


def _digest(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairStandardLimitPriceBoundError(
            "price-bound evidence is not canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class BetfairStandardLimitPriceBoundEvidence:
    """Product-issued assessment for one canonical standard BACK LIMIT action.

    The ordinary request shape and submitted odds are always preserved. A
    positive per-fragment floor is authoritative only when MatchMe applicability
    has also been proved by product-owned evidence. Until that authority exists,
    status is UNKNOWN_MATCHME_APPLICABILITY and the submitted odds are merely the
    candidate standard-LIMIT floor, not a guaranteed adverse-price bound.
    """

    execution_plan_id: str
    execution_plan_sha256: str
    portfolio_plan_sha256: str
    intent_id: str
    intent_sha256: str
    action_id: str
    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    requested_stake: Decimal
    price_floor_odds: Decimal
    quote_id: str
    quote_observed_at: str
    quote_expires_at: str
    decision_at: str
    instruction_sha256: str
    provider_contract_id: str
    provider_contract_ref: str
    write_adapter_id: str
    write_adapter_version: str
    status: BetfairStandardLimitPriceBoundStatus
    matchme_applicability_proven: bool
    zero_adverse_price_deterioration: bool
    execution_feasibility_proven: bool
    realized_price_exact: bool

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise BetfairStandardLimitPriceBoundError(
            "BetfairStandardLimitPriceBoundEvidence is issued only by the canonical resolver"
        )

    def _validate(self) -> None:
        for field in (
            "execution_plan_id",
            "portfolio_plan_sha256",
            "intent_id",
            "action_id",
            "bookmaker_id",
            "account_id",
            "event_id",
            "market_id",
            "selection_id",
            "side",
            "quote_id",
            "provider_contract_id",
            "provider_contract_ref",
            "write_adapter_id",
            "write_adapter_version",
        ):
            _text(getattr(self, field), field)
        for field in (
            "execution_plan_sha256",
            "intent_sha256",
            "instruction_sha256",
        ):
            _sha(getattr(self, field), field)
        for field in (
            "quote_observed_at",
            "quote_expires_at",
            "decision_at",
        ):
            _time(getattr(self, field), field)
        if _time(self.quote_observed_at, "quote_observed_at") > _time(
            self.decision_at, "decision_at"
        ):
            raise BetfairStandardLimitPriceBoundError(
                "price bound cannot consume a quote observed after the decision"
            )
        if _time(self.quote_expires_at, "quote_expires_at") <= _time(
            self.decision_at, "decision_at"
        ):
            raise BetfairStandardLimitPriceBoundError(
                "price bound requires an unexpired decision-time quote"
            )
        _positive_decimal(self.requested_stake, "requested_stake")
        _positive_decimal(self.price_floor_odds, "price_floor_odds")
        if self.bookmaker_id != _PROVIDER_ID or self.side != "BACK":
            raise BetfairStandardLimitPriceBoundError(
                "schema v1 supports only canonical Betfair BACK actions"
            )
        if (
            self.provider_contract_id != _PROVIDER_CONTRACT_ID
            or self.provider_contract_ref != _PROVIDER_CONTRACT_REF
            or self.write_adapter_id != _WRITE_ADAPTER_ID
            or self.write_adapter_version != _WRITE_ADAPTER_VERSION
        ):
            raise BetfairStandardLimitPriceBoundError(
                "evidence provider contract does not match the canonical standard-LIMIT authority"
            )
        if type(self.status) is not BetfairStandardLimitPriceBoundStatus:
            raise BetfairStandardLimitPriceBoundError(
                "unsupported price-bound status"
            )
        if self.status is BetfairStandardLimitPriceBoundStatus.UNKNOWN_MATCHME_APPLICABILITY:
            if self.matchme_applicability_proven is not False:
                raise BetfairStandardLimitPriceBoundError(
                    "unknown MatchMe applicability cannot claim applicability proof"
                )
            if self.zero_adverse_price_deterioration is not False:
                raise BetfairStandardLimitPriceBoundError(
                    "unknown MatchMe applicability cannot claim zero adverse price deterioration"
                )
        elif (
            self.status
            is BetfairStandardLimitPriceBoundStatus.PROVIDER_BOUND_ZERO_ADVERSE_PRICE_DETERIORATION
        ):
            if self.matchme_applicability_proven is not True:
                raise BetfairStandardLimitPriceBoundError(
                    "positive price-floor status requires MatchMe applicability proof"
                )
            if self.zero_adverse_price_deterioration is not True:
                raise BetfairStandardLimitPriceBoundError(
                    "positive price-floor status requires zero adverse price deterioration"
                )
        else:
            raise BetfairStandardLimitPriceBoundError(
                "unsupported price-bound status"
            )
        if self.execution_feasibility_proven is not False:
            raise BetfairStandardLimitPriceBoundError(
                "price-bound evidence cannot claim execution feasibility"
            )
        if self.realized_price_exact is not False:
            raise BetfairStandardLimitPriceBoundError(
                "price-bound evidence cannot claim an exact realized price"
            )

    @property
    def evidence_id(self) -> str:
        self._validate()
        return _digest(self.to_dict(include_evidence_id=False))

    def to_dict(self, *, include_evidence_id: bool = True) -> dict[str, Any]:
        self._validate()
        payload: dict[str, Any] = {
            "schema": "autosport.betfair_standard_limit_price_bound",
            "schema_version": _SCHEMA_VERSION,
            "execution_plan_id": self.execution_plan_id,
            "execution_plan_sha256": self.execution_plan_sha256,
            "portfolio_plan_sha256": self.portfolio_plan_sha256,
            "intent_id": self.intent_id,
            "intent_sha256": self.intent_sha256,
            "action_id": self.action_id,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "requested_stake": str(self.requested_stake),
            "price_floor_odds": str(self.price_floor_odds),
            "quote_id": self.quote_id,
            "quote_observed_at": self.quote_observed_at,
            "quote_expires_at": self.quote_expires_at,
            "decision_at": self.decision_at,
            "instruction_sha256": self.instruction_sha256,
            "provider_contract_id": self.provider_contract_id,
            "provider_contract_ref": self.provider_contract_ref,
            "write_adapter_id": self.write_adapter_id,
            "write_adapter_version": self.write_adapter_version,
            "status": self.status.value,
            "matchme_applicability_proven": self.matchme_applicability_proven,
            "zero_adverse_price_deterioration": self.zero_adverse_price_deterioration,
            "execution_feasibility_proven": False,
            "realized_price_exact": False,
        }
        if include_evidence_id:
            payload["evidence_id"] = _digest(payload)
        return payload


class _CapturedPlaceOrdersRequest(RuntimeError):
    def __init__(self, body: bytes) -> None:
        super().__init__("captured canonical placeOrders request")
        self.body = body


class _CaptureGate:
    def require(self, **_kwargs: object) -> None:
        return None


class _CaptureCredentials:
    application_key = "autosport-price-bound-capture"
    session_token = "autosport-price-bound-capture"


class _CaptureTransport:
    def post(
        self,
        _url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        del headers, timeout_seconds
        if type(body) is not bytes:
            raise BetfairStandardLimitPriceBoundError(
                "canonical placeOrders request body is not bytes"
            )
        raise _CapturedPlaceOrdersRequest(body)


def _capture_canonical_instruction(action: ExecutionAction) -> dict[str, Any]:
    """Capture the exact instruction emitted by the real provider-write method.

    The transport is a local fail-before-I/O capture object. Therefore this path
    cannot contact Betfair, while any semantic change in ``place_action`` is
    observed at the same serialized request boundary the real transport consumes.
    Unsupported request drift fails closed instead of relying on a manually
    duplicated request builder or an adapter-version bump.
    """

    client = object.__new__(BetfairSupervisedPlaceOrdersClient)
    client._credentials = _CaptureCredentials()
    client._gate = _CaptureGate()
    client._transport = _CaptureTransport()
    client._timeout_seconds = 1.0
    client._clock = lambda: "1970-01-01T00:00:00+00:00"
    client._request_id = 0

    try:
        _CANONICAL_PLACE_ACTION(
            client,
            action,
            profile=None,
            bound=None,
            provider_order_ref=_CAPTURE_PROVIDER_ORDER_REF,
            execution_workspace=Path("."),
        )
    except _CapturedPlaceOrdersRequest as captured:
        body = captured.body
    except Exception as exc:
        raise BetfairStandardLimitPriceBoundError(
            "canonical Betfair placeOrders request could not be captured"
        ) from exc
    else:
        raise BetfairStandardLimitPriceBoundError(
            "canonical Betfair placeOrders path did not reach the sealed capture transport"
        )

    try:
        envelope = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BetfairStandardLimitPriceBoundError(
            "canonical Betfair placeOrders request is not valid JSON"
        ) from exc
    if type(envelope) is not dict or envelope.get("method") != PLACE_ORDERS_METHOD:
        raise BetfairStandardLimitPriceBoundError(
            "canonical Betfair write path did not emit placeOrders"
        )
    params = envelope.get("params")
    if type(params) is not dict or params.get("marketId") != action.market_id:
        raise BetfairStandardLimitPriceBoundError(
            "canonical Betfair placeOrders request does not bind the action market"
        )
    instructions = params.get("instructions")
    if type(instructions) is not list or len(instructions) != 1:
        raise BetfairStandardLimitPriceBoundError(
            "canonical Betfair placeOrders request must contain exactly one instruction"
        )
    instruction = instructions[0]
    if type(instruction) is not dict:
        raise BetfairStandardLimitPriceBoundError(
            "canonical Betfair placeOrders instruction is not an object"
        )
    expected_keys = {
        "selectionId",
        "handicap",
        "side",
        "orderType",
        "limitOrder",
        "customerOrderRef",
    }
    if set(instruction) != expected_keys:
        raise BetfairStandardLimitPriceBoundError(
            "nonstandard Betfair order transformation is not supported"
        )
    if instruction.get("customerOrderRef") != _CAPTURE_PROVIDER_ORDER_REF:
        raise BetfairStandardLimitPriceBoundError(
            "canonical Betfair instruction changed the sealed provider order reference"
        )
    return {
        key: instruction[key]
        for key in ("selectionId", "handicap", "side", "orderType", "limitOrder")
    }


def _canonical_instruction_projection(action: ExecutionAction) -> dict[str, Any]:
    """Return the exact semantic projection emitted by the real write request."""

    if type(action) is not ExecutionAction:
        raise BetfairStandardLimitPriceBoundError(
            "action must be the exact canonical ExecutionAction type"
        )
    _positive_decimal(action.requested_odds, "requested_odds")
    _positive_decimal(action.requested_stake, "requested_stake")
    if action.bookmaker_id != _PROVIDER_ID or action.side != "BACK":
        raise BetfairStandardLimitPriceBoundError(
            "only the canonical Betfair BACK standard-LIMIT path is supported"
        )
    if not callable(_CANONICAL_PLACE_ACTION):
        raise BetfairStandardLimitPriceBoundError(
            "canonical Betfair placeOrders implementation is unavailable"
        )

    instruction = _capture_canonical_instruction(action)
    limit_order = instruction.get("limitOrder")
    if type(limit_order) is not dict or set(limit_order) != {
        "size",
        "price",
        "persistenceType",
    }:
        raise BetfairStandardLimitPriceBoundError(
            "nonstandard Betfair LIMIT semantics are not supported"
        )
    try:
        selection_id = int(action.selection_id)
    except (TypeError, ValueError) as exc:
        raise BetfairStandardLimitPriceBoundError(
            "Betfair selection_id must be canonical positive integer text"
        ) from exc
    if selection_id <= 0 or str(selection_id) != action.selection_id:
        raise BetfairStandardLimitPriceBoundError(
            "Betfair selection_id must be canonical positive integer text"
        )
    action_payload = ExecutionAction.to_dict(action)
    if (
        type(instruction.get("selectionId")) is not int
        or instruction.get("selectionId") != selection_id
        or instruction.get("handicap") != 0
        or instruction.get("orderType") != "LIMIT"
        or instruction.get("side") != "BACK"
        or limit_order.get("persistenceType") != "LAPSE"
        or limit_order.get("price") != action_payload["requested_odds"]
        or limit_order.get("size") != action_payload["requested_stake"]
    ):
        raise BetfairStandardLimitPriceBoundError(
            "provider instruction projection does not preserve the bound standard LIMIT"
        )
    return instruction


def _issue_evidence(
    *,
    bound: BoundSupervisedExecutionPlan,
    action: ExecutionAction,
    instruction_sha256: str,
) -> BetfairStandardLimitPriceBoundEvidence:
    item = object.__new__(BetfairStandardLimitPriceBoundEvidence)
    action_payload = ExecutionAction.to_dict(action)
    values = {
        "execution_plan_id": bound.execution_plan.plan_id,
        "execution_plan_sha256": bound.execution_plan.fingerprint,
        "portfolio_plan_sha256": bound.portfolio_plan_sha256,
        "intent_id": bound.intent_id,
        "intent_sha256": bound.intent_sha256,
        "action_id": action.action_id,
        "bookmaker_id": action.bookmaker_id,
        "account_id": action.account_id,
        "event_id": action.event_id,
        "market_id": action.market_id,
        "selection_id": action.selection_id,
        "side": action.side,
        "requested_stake": Decimal(action_payload["requested_stake"]),
        "price_floor_odds": Decimal(action_payload["requested_odds"]),
        "quote_id": action.quote_id,
        "quote_observed_at": action.quote_observed_at,
        "quote_expires_at": action.expires_at,
        "decision_at": bound.execution_plan.created_at,
        "instruction_sha256": instruction_sha256,
        "provider_contract_id": _PROVIDER_CONTRACT_ID,
        "provider_contract_ref": _PROVIDER_CONTRACT_REF,
        "write_adapter_id": _WRITE_ADAPTER_ID,
        "write_adapter_version": _WRITE_ADAPTER_VERSION,
        "status": BetfairStandardLimitPriceBoundStatus.UNKNOWN_MATCHME_APPLICABILITY,
        "matchme_applicability_proven": False,
        "zero_adverse_price_deterioration": False,
        "execution_feasibility_proven": False,
        "realized_price_exact": False,
    }
    for field, value in values.items():
        object.__setattr__(item, field, value)
    item._validate()
    return item


def resolve_betfair_standard_limit_price_bound(
    *,
    bound: BoundSupervisedExecutionPlan,
    action_id: str,
) -> BetfairStandardLimitPriceBoundEvidence:
    """Re-resolve one exact current standard BACK LIMIT request assessment.

    The caller supplies only the bound plan and an action identity. No caller
    instruction, price bound, zero/slippage flag, provider setting, accepted
    price, or fill assumption is accepted. The action and its quote are
    re-resolved from the immutable bound plan.

    The current product has no authenticated product-owned MatchMe
    applicability/state authority for the exact Betfair execution account.
    Therefore an otherwise canonical ordinary LIMIT request remains
    UNKNOWN_MATCHME_APPLICABILITY and cannot claim zero adverse deterioration.
    """

    if type(bound) is not BoundSupervisedExecutionPlan:
        raise BetfairStandardLimitPriceBoundError(
            "bound must be the exact canonical BoundSupervisedExecutionPlan type"
        )
    _text(action_id, "action_id")

    if hasattr(bound, "__dict__") and any(
        name in vars(bound) for name in ("verify_binding", "action_for", "profile_for")
    ):
        raise BetfairStandardLimitPriceBoundError(
            "bound execution authority method shadow is not allowed"
        )

    BoundSupervisedExecutionPlan.verify_binding(bound)
    if type(bound.execution_plan) is not ExecutionPlan:
        raise BetfairStandardLimitPriceBoundError(
            "bound execution_plan must be the exact canonical ExecutionPlan type"
        )
    try:
        action = BoundSupervisedExecutionPlan.action_for(bound, action_id)
    except SupervisedExecutionError as exc:
        raise BetfairStandardLimitPriceBoundError(
            "action is not in bound execution plan"
        ) from exc
    if type(action) is not ExecutionAction:
        raise BetfairStandardLimitPriceBoundError(
            "bound plan returned a non-canonical ExecutionAction"
        )

    instruction = _canonical_instruction_projection(action)
    return _issue_evidence(
        bound=bound,
        action=action,
        instruction_sha256=_digest(instruction),
    )
