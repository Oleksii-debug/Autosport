from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .betfair_supervised_execution import (
    WRITE_ADAPTER_ID,
    WRITE_ADAPTER_VERSION,
    _validate_betfair_place_action,
)
from .real_execution_ledger import ExecutionAction
from .supervised_execution import BoundSupervisedExecutionPlan, SupervisedExecutionError

BETFAIR_STANDARD_BACK_LIMIT_SLIPPAGE_CONTRACT_ID = (
    "betfair-standard-back-limit-adverse-price-slippage"
)
BETFAIR_STANDARD_BACK_LIMIT_SLIPPAGE_CONTRACT_VERSION = "1"


class BetfairStandardLimitSlippageError(ValueError):
    pass


class ProspectiveAdversePriceSlippageKnowledge(str, Enum):
    KNOWN_ZERO = "KNOWN_ZERO"
    UNKNOWN_UNPROVEN = "UNKNOWN_UNPROVEN"


class ProspectiveSlippageUnknownReason(str, Enum):
    UNBOUND_PLAN = "UNBOUND_PLAN"
    ACTION_NOT_BOUND = "ACTION_NOT_BOUND"
    NON_BETFAIR_ACTION = "NON_BETFAIR_ACTION"
    NON_BACK_ACTION = "NON_BACK_ACTION"
    PROVIDER_IDENTITY_MISMATCH = "PROVIDER_IDENTITY_MISMATCH"
    QUOTE_NOT_ACTIVE_AT_DECISION = "QUOTE_NOT_ACTIVE_AT_DECISION"
    NONSTANDARD_LIMIT_INSTRUCTION = "NONSTANDARD_LIMIT_INSTRUCTION"


def _canonical_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetfairStandardLimitSlippageError(
            f"{name} must be non-empty canonical text"
        )
    return value


def _timestamp(value: str, name: str) -> datetime:
    raw = _canonical_text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairStandardLimitSlippageError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairStandardLimitSlippageError(f"{name} must be timezone-aware")
    return parsed


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairStandardLimitSlippageError("value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def build_betfair_standard_back_limit_instruction(
    action: ExecutionAction,
    *,
    provider_order_ref: str,
) -> dict[str, object]:
    """Project the exact plain BACK LIMIT instruction used by supervised placeOrders.

    This is intentionally narrower than Betfair's full order surface. It contains no
    timeInForce/FOK, market-on-close, limit-on-close, MatchMe, or price-widening
    semantics. A caller asking for any broader semantic must remain unproven.
    """

    if type(action) is not ExecutionAction:
        raise BetfairStandardLimitSlippageError(
            "action must be the canonical ExecutionAction type"
        )
    if action.bookmaker_id != "betfair":
        raise BetfairStandardLimitSlippageError("action must target canonical betfair")
    if action.side != "BACK":
        raise BetfairStandardLimitSlippageError("only BACK actions are in scope")

    provider_ref = _canonical_text(provider_order_ref, "provider_order_ref")
    try:
        selection_id = _validate_betfair_place_action(action)
    except Exception as exc:
        raise BetfairStandardLimitSlippageError(
            "action is not a canonical supervised Betfair place action"
        ) from exc

    return {
        "selectionId": selection_id,
        "handicap": 0,
        "side": "BACK",
        "orderType": "LIMIT",
        "limitOrder": {
            "size": str(action.requested_stake),
            "price": str(action.requested_odds),
            "persistenceType": "LAPSE",
        },
        "customerOrderRef": provider_ref,
    }


def _is_exact_standard_instruction(
    instruction: object,
    expected: dict[str, object],
) -> bool:
    if type(instruction) is not dict or instruction != expected:
        return False
    limit_order = instruction.get("limitOrder")
    return type(limit_order) is dict and set(limit_order) == {
        "size",
        "price",
        "persistenceType",
    }


@dataclass(frozen=True, slots=True)
class BetfairStandardBackLimitZeroSlippageEvidence:
    execution_plan_id: str
    execution_plan_sha256: str
    portfolio_plan_sha256: str
    action_id: str
    intent_id: str
    intent_sha256: str
    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    quote_id: str
    quote_observed_at: str
    quote_expires_at: str
    decision_at: str
    decision_quote: str
    provider_adapter_id: str
    provider_adapter_version: str
    provider_order_ref: str
    instruction_sha256: str
    contract_id: str = BETFAIR_STANDARD_BACK_LIMIT_SLIPPAGE_CONTRACT_ID
    contract_version: str = BETFAIR_STANDARD_BACK_LIMIT_SLIPPAGE_CONTRACT_VERSION
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        payload = self.to_dict(include_evidence_sha256=False)
        object.__setattr__(self, "evidence_sha256", _digest(payload))

    @property
    def knowledge(self) -> ProspectiveAdversePriceSlippageKnowledge:
        return ProspectiveAdversePriceSlippageKnowledge.KNOWN_ZERO

    @property
    def amount(self) -> Decimal:
        return Decimal("0")

    @property
    def complete(self) -> bool:
        return True

    def to_dict(self, *, include_evidence_sha256: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": "autosport.betfair_standard_back_limit_zero_slippage_evidence",
            "schema_version": 1,
            "execution_plan_id": self.execution_plan_id,
            "execution_plan_sha256": self.execution_plan_sha256,
            "portfolio_plan_sha256": self.portfolio_plan_sha256,
            "action_id": self.action_id,
            "intent_id": self.intent_id,
            "intent_sha256": self.intent_sha256,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "quote_id": self.quote_id,
            "quote_observed_at": self.quote_observed_at,
            "quote_expires_at": self.quote_expires_at,
            "decision_at": self.decision_at,
            "decision_quote": self.decision_quote,
            "provider_adapter_id": self.provider_adapter_id,
            "provider_adapter_version": self.provider_adapter_version,
            "provider_order_ref": self.provider_order_ref,
            "instruction_sha256": self.instruction_sha256,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "knowledge": self.knowledge.value,
            "amount": str(self.amount),
            "complete": self.complete,
        }
        if include_evidence_sha256:
            payload["evidence_sha256"] = self.evidence_sha256
        return payload


@dataclass(frozen=True, slots=True)
class ProspectiveAdversePriceSlippageAssessment:
    evidence: BetfairStandardBackLimitZeroSlippageEvidence | None = None
    unknown_reason: ProspectiveSlippageUnknownReason | None = None

    def __post_init__(self) -> None:
        if (self.evidence is None) == (self.unknown_reason is None):
            raise BetfairStandardLimitSlippageError(
                "assessment must contain exactly one of evidence or unknown_reason"
            )

    @property
    def knowledge(self) -> ProspectiveAdversePriceSlippageKnowledge:
        if self.evidence is None:
            return ProspectiveAdversePriceSlippageKnowledge.UNKNOWN_UNPROVEN
        return self.evidence.knowledge

    @property
    def amount(self) -> Decimal | None:
        if self.evidence is None:
            return None
        return self.evidence.amount

    @property
    def complete(self) -> bool:
        return self.evidence is not None


def _unknown(
    reason: ProspectiveSlippageUnknownReason,
) -> ProspectiveAdversePriceSlippageAssessment:
    return ProspectiveAdversePriceSlippageAssessment(unknown_reason=reason)


def prove_betfair_standard_back_limit_zero_slippage(
    bound_plan: BoundSupervisedExecutionPlan,
    *,
    action_id: str,
    provider_order_ref: str,
    instruction: object,
    provider_adapter_id: str = WRITE_ADAPTER_ID,
    provider_adapter_version: str = WRITE_ADAPTER_VERSION,
) -> ProspectiveAdversePriceSlippageAssessment:
    """Prove only decision-time adverse *price* slippage for plain BACK LIMIT.

    KNOWN_ZERO means a matched fragment cannot execute below the submitted BACK limit
    price. It says nothing about whether any fragment fills, full-fill/atomicity,
    timing, latency, rejection, commission, execution feasibility, or profitability.
    """

    if type(bound_plan) is not BoundSupervisedExecutionPlan:
        return _unknown(ProspectiveSlippageUnknownReason.UNBOUND_PLAN)
    try:
        bound_plan.verify_binding()
        action = bound_plan.action_for(action_id)
    except (SupervisedExecutionError, ValueError, TypeError):
        return _unknown(ProspectiveSlippageUnknownReason.ACTION_NOT_BOUND)

    if action.bookmaker_id != "betfair":
        return _unknown(ProspectiveSlippageUnknownReason.NON_BETFAIR_ACTION)
    if action.side != "BACK":
        return _unknown(ProspectiveSlippageUnknownReason.NON_BACK_ACTION)
    if (
        provider_adapter_id != WRITE_ADAPTER_ID
        or provider_adapter_version != WRITE_ADAPTER_VERSION
    ):
        return _unknown(ProspectiveSlippageUnknownReason.PROVIDER_IDENTITY_MISMATCH)

    try:
        decision_at = _timestamp(bound_plan.execution_plan.created_at, "created_at")
        quote_observed_at = _timestamp(action.quote_observed_at, "quote_observed_at")
        quote_expires_at = _timestamp(action.expires_at, "expires_at")
    except BetfairStandardLimitSlippageError:
        return _unknown(ProspectiveSlippageUnknownReason.QUOTE_NOT_ACTIVE_AT_DECISION)
    if not (quote_observed_at <= decision_at < quote_expires_at):
        return _unknown(ProspectiveSlippageUnknownReason.QUOTE_NOT_ACTIVE_AT_DECISION)

    try:
        expected = build_betfair_standard_back_limit_instruction(
            action,
            provider_order_ref=provider_order_ref,
        )
    except BetfairStandardLimitSlippageError:
        return _unknown(ProspectiveSlippageUnknownReason.NONSTANDARD_LIMIT_INSTRUCTION)
    if not _is_exact_standard_instruction(instruction, expected):
        return _unknown(ProspectiveSlippageUnknownReason.NONSTANDARD_LIMIT_INSTRUCTION)

    instruction_sha256 = _digest(expected)
    evidence = BetfairStandardBackLimitZeroSlippageEvidence(
        execution_plan_id=bound_plan.execution_plan.plan_id,
        execution_plan_sha256=bound_plan.execution_plan.fingerprint,
        portfolio_plan_sha256=bound_plan.portfolio_plan_sha256,
        action_id=action.action_id,
        intent_id=bound_plan.intent_id,
        intent_sha256=bound_plan.intent_sha256,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        quote_id=action.quote_id,
        quote_observed_at=action.quote_observed_at,
        quote_expires_at=action.expires_at,
        decision_at=bound_plan.execution_plan.created_at,
        decision_quote=str(action.requested_odds),
        provider_adapter_id=provider_adapter_id,
        provider_adapter_version=provider_adapter_version,
        provider_order_ref=provider_order_ref,
        instruction_sha256=instruction_sha256,
    )
    return ProspectiveAdversePriceSlippageAssessment(evidence=evidence)
