from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable

from .real_execution_ledger import ExecutionAction


SCHEMA_VERSION = 1
_SHA256_HEX = frozenset("0123456789abcdef")


class ExecutionMeasurementError(ValueError):
    pass


class ExecutionEffectState(str, Enum):
    FULL_MATCH = "FULL_MATCH"
    PARTIAL_MATCH = "PARTIAL_MATCH"
    ACCEPTED_UNMATCHED = "ACCEPTED_UNMATCHED"
    DELAYED = "DELAYED"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ExecutionMeasurementError(f"{name} must be non-empty trimmed text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ExecutionMeasurementError(f"{name} must be valid UTF-8 text") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in _SHA256_HEX for char in text):
        raise ExecutionMeasurementError(f"{name} must be lowercase SHA-256 hex")
    return text


def _decimal(
    value: object,
    name: str,
    *,
    greater_than: Decimal | None = None,
) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ExecutionMeasurementError(f"{name} must be a finite Decimal")
    if greater_than is not None and value <= greater_than:
        raise ExecutionMeasurementError(f"{name} must be > {greater_than}")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ExecutionMeasurementError(f"{name} must be a non-negative int")
    return value


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExecutionMeasurementError("measurement is not canonical JSON") from exc


@dataclass(frozen=True, slots=True)
class MatchedFragment:
    fragment_id: str
    price: Decimal
    stake: Decimal
    source_evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.fragment_id, "fragment_id")
        _decimal(self.price, "price", greater_than=Decimal("1"))
        _decimal(self.stake, "stake", greater_than=Decimal("0"))
        _sha256(self.source_evidence_sha256, "source_evidence_sha256")

    def to_dict(self) -> dict[str, str]:
        return {
            "fragment_id": self.fragment_id,
            "price": _decimal_text(self.price),
            "stake": _decimal_text(self.stake),
            "source_evidence_sha256": self.source_evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class ExecutionMeasurementInput:
    attempt_id: str
    action: ExecutionAction
    effect_state: ExecutionEffectState
    decision_odds: Decimal
    clock_epoch_id: str
    decision_monotonic_ns: int
    submitted_monotonic_ns: int
    acknowledgement_monotonic_ns: int | None
    execution_evidence_sha256: str
    matched_fragments: tuple[MatchedFragment, ...] = ()
    provider_event_time: str | None = None

    def __post_init__(self) -> None:
        _text(self.attempt_id, "attempt_id")
        if not isinstance(self.action, ExecutionAction):
            raise ExecutionMeasurementError("action must be canonical ExecutionAction")
        if not isinstance(self.effect_state, ExecutionEffectState):
            raise ExecutionMeasurementError(
                "effect_state must be ExecutionEffectState"
            )
        if self.action.side not in {"BACK", "LAY"}:
            raise ExecutionMeasurementError(
                "action.side must be canonical BACK or LAY"
            )
        _decimal(self.decision_odds, "decision_odds", greater_than=Decimal("1"))
        if (
            type(self.action.requested_odds) is not Decimal
            or not self.action.requested_odds.is_finite()
            or self.action.requested_odds <= 1
        ):
            raise ExecutionMeasurementError(
                "action.requested_odds must be finite Decimal > 1"
            )
        if (
            type(self.action.requested_stake) is not Decimal
            or not self.action.requested_stake.is_finite()
            or self.action.requested_stake <= 0
        ):
            raise ExecutionMeasurementError(
                "action.requested_stake must be finite Decimal > 0"
            )
        _text(self.clock_epoch_id, "clock_epoch_id")
        decision = _nonnegative_int(
            self.decision_monotonic_ns, "decision_monotonic_ns"
        )
        submitted = _nonnegative_int(
            self.submitted_monotonic_ns, "submitted_monotonic_ns"
        )
        if submitted < decision:
            raise ExecutionMeasurementError(
                "submitted_monotonic_ns precedes decision"
            )
        if self.acknowledgement_monotonic_ns is not None:
            acknowledgement = _nonnegative_int(
                self.acknowledgement_monotonic_ns,
                "acknowledgement_monotonic_ns",
            )
            if acknowledgement < submitted:
                raise ExecutionMeasurementError(
                    "acknowledgement_monotonic_ns precedes submission"
                )
        elif self.effect_state is not ExecutionEffectState.UNKNOWN:
            raise ExecutionMeasurementError(
                "only UNKNOWN may omit acknowledgement_monotonic_ns"
            )
        _sha256(
            self.execution_evidence_sha256,
            "execution_evidence_sha256",
        )
        if self.provider_event_time is not None:
            _text(self.provider_event_time, "provider_event_time")
        fragments = tuple(self.matched_fragments)
        if not all(isinstance(item, MatchedFragment) for item in fragments):
            raise ExecutionMeasurementError(
                "matched_fragments must contain MatchedFragment values"
            )
        if len({item.fragment_id for item in fragments}) != len(fragments):
            raise ExecutionMeasurementError(
                "matched fragment ids must be unique"
            )
        object.__setattr__(self, "matched_fragments", fragments)
        matched_stake = sum(
            (item.stake for item in fragments),
            Decimal("0"),
        )
        if matched_stake > self.action.requested_stake:
            raise ExecutionMeasurementError(
                "matched stake exceeds requested stake"
            )
        if self.effect_state is ExecutionEffectState.FULL_MATCH:
            if matched_stake != self.action.requested_stake:
                raise ExecutionMeasurementError(
                    "FULL_MATCH requires exact requested stake matched"
                )
        elif self.effect_state is ExecutionEffectState.PARTIAL_MATCH:
            if not (
                Decimal("0")
                < matched_stake
                < self.action.requested_stake
            ):
                raise ExecutionMeasurementError(
                    "PARTIAL_MATCH requires a strict partial match"
                )
        elif fragments:
            raise ExecutionMeasurementError(
                f"{self.effect_state.value} cannot carry matched fragments"
            )


@dataclass(frozen=True, slots=True)
class ExecutionMeasurement:
    attempt_id: str
    action_id: str
    bookmaker_id: str
    account_id: str
    market_id: str
    selection_id: str
    side: str
    effect_state: ExecutionEffectState
    decision_odds: Decimal
    submitted_limit_odds: Decimal
    requested_stake: Decimal
    matched_stake: Decimal
    unresolved_stake: Decimal
    matched_vwap: Decimal | None
    adverse_odds_stake_delta: Decimal | None
    decision_to_submit_ns: int
    submit_to_acknowledgement_ns: int | None
    clock_epoch_id: str
    execution_evidence_sha256: str
    provider_event_time: str | None
    matched_fragments: tuple[MatchedFragment, ...]
    schema_version: int = SCHEMA_VERSION

    @property
    def slippage_observed(self) -> bool:
        return self.matched_stake > 0

    @property
    def assertion_digest(self) -> str:
        payload = self.to_dict()
        return hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()

    @property
    def execution_authorized(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "action_id": self.action_id,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "effect_state": self.effect_state.value,
            "decision_odds": _decimal_text(self.decision_odds),
            "submitted_limit_odds": _decimal_text(
                self.submitted_limit_odds
            ),
            "requested_stake": _decimal_text(self.requested_stake),
            "matched_stake": _decimal_text(self.matched_stake),
            "unresolved_stake": _decimal_text(self.unresolved_stake),
            "matched_vwap": (
                None
                if self.matched_vwap is None
                else _decimal_text(self.matched_vwap)
            ),
            "adverse_odds_stake_delta": (
                None
                if self.adverse_odds_stake_delta is None
                else _decimal_text(self.adverse_odds_stake_delta)
            ),
            "decision_to_submit_ns": self.decision_to_submit_ns,
            "submit_to_acknowledgement_ns": (
                self.submit_to_acknowledgement_ns
            ),
            "clock_epoch_id": self.clock_epoch_id,
            "execution_evidence_sha256": (
                self.execution_evidence_sha256
            ),
            "provider_event_time": self.provider_event_time,
            "matched_fragments": [
                item.to_dict() for item in self.matched_fragments
            ],
            "execution_authorized": False,
        }


@dataclass(frozen=True, slots=True)
class ExecutionMeasurementAggregate:
    attempt_count: int
    slippage_observed_attempt_count: int
    acknowledgement_latency_observed_count: int
    total_requested_stake: Decimal
    total_matched_stake: Decimal
    realized_slippage_stake_coverage: Decimal
    total_adverse_odds_stake_delta: Decimal

    def __post_init__(self) -> None:
        if type(self.attempt_count) is not int or self.attempt_count <= 0:
            raise ExecutionMeasurementError(
                "attempt_count must be positive int"
            )
        if (
            type(self.slippage_observed_attempt_count) is not int
            or not 0
            <= self.slippage_observed_attempt_count
            <= self.attempt_count
        ):
            raise ExecutionMeasurementError(
                "invalid slippage_observed_attempt_count"
            )
        if (
            type(self.acknowledgement_latency_observed_count) is not int
            or not 0
            <= self.acknowledgement_latency_observed_count
            <= self.attempt_count
        ):
            raise ExecutionMeasurementError(
                "invalid acknowledgement_latency_observed_count"
            )


def derive_execution_measurement(
    value: ExecutionMeasurementInput,
) -> ExecutionMeasurement:
    if not isinstance(value, ExecutionMeasurementInput):
        raise ExecutionMeasurementError(
            "value must be ExecutionMeasurementInput"
        )
    matched_stake = sum(
        (item.stake for item in value.matched_fragments),
        Decimal("0"),
    )
    matched_vwap: Decimal | None = None
    adverse_delta: Decimal | None = None
    if matched_stake > 0:
        matched_vwap = sum(
            (
                item.price * item.stake
                for item in value.matched_fragments
            ),
            Decimal("0"),
        ) / matched_stake
        if value.action.side == "BACK":
            adverse_delta = (
                value.decision_odds - matched_vwap
            ) * matched_stake
        else:
            adverse_delta = (
                matched_vwap - value.decision_odds
            ) * matched_stake
    if value.effect_state in {
        ExecutionEffectState.FULL_MATCH,
        ExecutionEffectState.REJECTED,
    }:
        unresolved_stake = Decimal("0")
    else:
        unresolved_stake = (
            value.action.requested_stake - matched_stake
        )
    return ExecutionMeasurement(
        attempt_id=value.attempt_id,
        action_id=value.action.action_id,
        bookmaker_id=value.action.bookmaker_id,
        account_id=value.action.account_id,
        market_id=value.action.market_id,
        selection_id=value.action.selection_id,
        side=value.action.side,
        effect_state=value.effect_state,
        decision_odds=value.decision_odds,
        submitted_limit_odds=value.action.requested_odds,
        requested_stake=value.action.requested_stake,
        matched_stake=matched_stake,
        unresolved_stake=unresolved_stake,
        matched_vwap=matched_vwap,
        adverse_odds_stake_delta=adverse_delta,
        decision_to_submit_ns=(
            value.submitted_monotonic_ns
            - value.decision_monotonic_ns
        ),
        submit_to_acknowledgement_ns=(
            None
            if value.acknowledgement_monotonic_ns is None
            else value.acknowledgement_monotonic_ns
            - value.submitted_monotonic_ns
        ),
        clock_epoch_id=value.clock_epoch_id,
        execution_evidence_sha256=(
            value.execution_evidence_sha256
        ),
        provider_event_time=value.provider_event_time,
        matched_fragments=value.matched_fragments,
    )


def aggregate_execution_measurements(
    measurements: Iterable[ExecutionMeasurement],
) -> ExecutionMeasurementAggregate:
    values = tuple(measurements)
    if not values:
        raise ExecutionMeasurementError(
            "at least one measurement is required"
        )
    if not all(
        isinstance(item, ExecutionMeasurement)
        for item in values
    ):
        raise ExecutionMeasurementError(
            "measurements must contain ExecutionMeasurement values"
        )
    attempt_ids = [item.attempt_id for item in values]
    if len(set(attempt_ids)) != len(attempt_ids):
        raise ExecutionMeasurementError(
            "duplicate attempt_id in aggregate"
        )
    total_requested = sum(
        (item.requested_stake for item in values),
        Decimal("0"),
    )
    total_matched = sum(
        (item.matched_stake for item in values),
        Decimal("0"),
    )
    total_adverse = sum(
        (
            item.adverse_odds_stake_delta
            for item in values
            if item.adverse_odds_stake_delta is not None
        ),
        Decimal("0"),
    )
    return ExecutionMeasurementAggregate(
        attempt_count=len(values),
        slippage_observed_attempt_count=sum(
            item.slippage_observed for item in values
        ),
        acknowledgement_latency_observed_count=sum(
            item.submit_to_acknowledgement_ns is not None
            for item in values
        ),
        total_requested_stake=total_requested,
        total_matched_stake=total_matched,
        realized_slippage_stake_coverage=(
            total_matched / total_requested
        ),
        total_adverse_odds_stake_delta=total_adverse,
    )
