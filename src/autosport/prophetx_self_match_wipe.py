"""Fail-closed ProphetX self-match wipe safety and reconciliation contracts.

This module is provider-specific and deliberately side-effect free.  It does not place
orders, acquire credentials, mutate a generic execution ledger, move bankroll, or create
an account-wide writer lock.  Its two responsibilities are:

* screen a current own-order snapshot for known self-match conflicts without ever turning
  "none observed" into a positive safety guarantee; and
* project provider-observed ``wiped`` terminal states into deterministic, idempotent
  release effects while keeping the wipe cause unresolved unless the provider proves it.

ProphetX uses ``wiped`` for more than self-match prevention, so this module never labels a
wipe SELF_MATCH from status/correlation alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
import json
from typing import Iterable


class ProphetXSelfMatchError(ValueError):
    """Base contract violation."""


class ProphetXSelfMatchConflict(ProphetXSelfMatchError):
    """Contradictory provider/readback evidence."""


class SnapshotQuality(str, Enum):
    COMPLETE_CURRENT = "COMPLETE_CURRENT"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class WriterCoordination(str, Enum):
    PROVEN_PRODUCT_SERIALIZED = "PROVEN_PRODUCT_SERIALIZED"
    EXTERNAL_WRITERS_POSSIBLE = "EXTERNAL_WRITERS_POSSIBLE"
    UNKNOWN = "UNKNOWN"


class PreSubmitDecision(str, Enum):
    BLOCK_KNOWN_CONFLICT = "BLOCK_KNOWN_CONFLICT"
    WAIT_READBACK = "WAIT_READBACK"
    NO_KNOWN_CONFLICT_NOT_GUARANTEED = "NO_KNOWN_CONFLICT_NOT_GUARANTEED"


class TrackedOrderStatus(str, Enum):
    PENDING_NEW = "PENDING_NEW"
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"


class WipeCause(str, Enum):
    WIPED_CAUSE_UNRESOLVED = "WIPED_CAUSE_UNRESOLVED"


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProphetXSelfMatchError(f"{field} must be non-empty trimmed text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProphetXSelfMatchError(f"{field} must be valid UTF-8") from exc
    return value


def _decimal(value: Decimal | str | int, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ProphetXSelfMatchError(f"{field} must use exact Decimal/string/integer ingress")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProphetXSelfMatchError(f"{field} must be Decimal-compatible") from exc
    if not parsed.is_finite():
        raise ProphetXSelfMatchError(f"{field} must be finite")
    if positive and parsed <= 0:
        raise ProphetXSelfMatchError(f"{field} must be positive")
    if not positive and parsed < 0:
        raise ProphetXSelfMatchError(f"{field} must be non-negative")
    return parsed


def _dtext(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class IncomingOrderIntent:
    """Frozen pre-submit identity.  No provider order id exists yet."""

    environment: str
    account_id: str
    client_order_id: str
    strike_id: str
    price: Decimal | str | int
    quantity: Decimal | str | int

    def __post_init__(self) -> None:
        for field in ("environment", "account_id", "client_order_id", "strike_id"):
            _text(getattr(self, field), field)
        object.__setattr__(self, "price", _decimal(self.price, "price", positive=True))
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity", positive=True))

    @property
    def fingerprint(self) -> str:
        return _digest(
            {
                "environment": self.environment,
                "account_id": self.account_id,
                "client_order_id": self.client_order_id,
                "strike_id": self.strike_id,
                "price": _dtext(self.price),
                "quantity": _dtext(self.quantity),
            }
        )


@dataclass(frozen=True, slots=True)
class RestingOwnOrder:
    environment: str
    account_id: str
    provider_order_id: str
    strike_id: str
    open_quantity: Decimal | str | int

    def __post_init__(self) -> None:
        for field in ("environment", "account_id", "provider_order_id", "strike_id"):
            _text(getattr(self, field), field)
        object.__setattr__(
            self,
            "open_quantity",
            _decimal(self.open_quantity, "open_quantity", positive=True),
        )


@dataclass(frozen=True, slots=True)
class OwnOrderSnapshot:
    environment: str
    account_id: str
    snapshot_id: str
    quality: SnapshotQuality
    orders: tuple[RestingOwnOrder, ...]

    def __post_init__(self) -> None:
        for field in ("environment", "account_id", "snapshot_id"):
            _text(getattr(self, field), field)
        if not isinstance(self.quality, SnapshotQuality):
            raise ProphetXSelfMatchError("quality must be SnapshotQuality")
        if not isinstance(self.orders, tuple):
            raise ProphetXSelfMatchError("orders must be a tuple")
        seen: set[str] = set()
        for order in self.orders:
            if not isinstance(order, RestingOwnOrder):
                raise ProphetXSelfMatchError("orders must contain RestingOwnOrder")
            if (order.environment, order.account_id) != (self.environment, self.account_id):
                raise ProphetXSelfMatchConflict("snapshot order scope mismatch")
            if order.provider_order_id in seen:
                raise ProphetXSelfMatchConflict("duplicate provider order in snapshot")
            seen.add(order.provider_order_id)


@dataclass(frozen=True, slots=True)
class CrossingFact:
    """Exact provider-matching assessment for one snapshot order.

    A positive fact is sufficient to BLOCK.  A negative fact is deliberately insufficient
    to authorize a write; the final decision remains "not guaranteed".
    """

    snapshot_id: str
    incoming_fingerprint: str
    resting_provider_order_id: str
    incoming_strike_id: str
    resting_strike_id: str
    would_cross: bool
    rule_version: str

    def __post_init__(self) -> None:
        for field in (
            "snapshot_id",
            "incoming_fingerprint",
            "resting_provider_order_id",
            "incoming_strike_id",
            "resting_strike_id",
            "rule_version",
        ):
            _text(getattr(self, field), field)
        if type(self.would_cross) is not bool:
            raise ProphetXSelfMatchError("would_cross must be bool")


@dataclass(frozen=True, slots=True)
class PreSubmitScreen:
    decision: PreSubmitDecision
    conflicting_provider_order_ids: tuple[str, ...]
    writer_coordination: WriterCoordination
    residual_concurrency_risk: bool
    requires_immediate_recheck_before_submit: bool = True

    @property
    def authorizes_write(self) -> bool:
        """This provider screen never grants execution authority."""
        return False


def screen_pre_submit(
    intent: IncomingOrderIntent,
    snapshot: OwnOrderSnapshot,
    crossing_facts: Iterable[CrossingFact],
    *,
    writer_coordination: WriterCoordination,
) -> PreSubmitScreen:
    if not isinstance(intent, IncomingOrderIntent):
        raise ProphetXSelfMatchError("intent must be IncomingOrderIntent")
    if not isinstance(snapshot, OwnOrderSnapshot):
        raise ProphetXSelfMatchError("snapshot must be OwnOrderSnapshot")
    if not isinstance(writer_coordination, WriterCoordination):
        raise ProphetXSelfMatchError("writer_coordination must be WriterCoordination")
    if (intent.environment, intent.account_id) != (snapshot.environment, snapshot.account_id):
        raise ProphetXSelfMatchConflict("intent/snapshot account or environment mismatch")

    if snapshot.quality is not SnapshotQuality.COMPLETE_CURRENT:
        return PreSubmitScreen(
            decision=PreSubmitDecision.WAIT_READBACK,
            conflicting_provider_order_ids=(),
            writer_coordination=writer_coordination,
            residual_concurrency_risk=True,
        )

    by_id = {order.provider_order_id: order for order in snapshot.orders}
    seen_fact_ids: set[str] = set()
    conflicts: set[str] = set()

    for fact in tuple(crossing_facts):
        if not isinstance(fact, CrossingFact):
            raise ProphetXSelfMatchError("crossing_facts must contain CrossingFact")
        if fact.snapshot_id != snapshot.snapshot_id:
            raise ProphetXSelfMatchConflict("crossing fact snapshot mismatch")
        if fact.incoming_fingerprint != intent.fingerprint:
            raise ProphetXSelfMatchConflict("crossing fact incoming identity mismatch")
        if fact.incoming_strike_id != intent.strike_id:
            raise ProphetXSelfMatchConflict("crossing fact incoming strike mismatch")
        resting = by_id.get(fact.resting_provider_order_id)
        if resting is None:
            raise ProphetXSelfMatchConflict("crossing fact references order absent from snapshot")
        if fact.resting_strike_id != resting.strike_id:
            raise ProphetXSelfMatchConflict("crossing fact resting strike mismatch")
        if fact.resting_provider_order_id in seen_fact_ids:
            raise ProphetXSelfMatchConflict("duplicate crossing fact for one resting order")
        seen_fact_ids.add(fact.resting_provider_order_id)
        if fact.would_cross:
            conflicts.add(fact.resting_provider_order_id)

    if conflicts:
        return PreSubmitScreen(
            decision=PreSubmitDecision.BLOCK_KNOWN_CONFLICT,
            conflicting_provider_order_ids=tuple(sorted(conflicts)),
            writer_coordination=writer_coordination,
            residual_concurrency_risk=True,
        )

    # A COMPLETE_CURRENT order snapshot is not a complete self-match screen unless
    # every resting order in that exact snapshot has an explicit crossing result.
    # Missing assessment must never be silently interpreted as "does not cross".
    if seen_fact_ids != set(by_id):
        return PreSubmitScreen(
            decision=PreSubmitDecision.WAIT_READBACK,
            conflicting_provider_order_ids=(),
            writer_coordination=writer_coordination,
            residual_concurrency_risk=True,
        )

    return PreSubmitScreen(
        decision=PreSubmitDecision.NO_KNOWN_CONFLICT_NOT_GUARANTEED,
        conflicting_provider_order_ids=(),
        writer_coordination=writer_coordination,
        residual_concurrency_risk=(
            writer_coordination is not WriterCoordination.PROVEN_PRODUCT_SERIALIZED
        ),
    )


@dataclass(frozen=True, slots=True)
class TrackedOrder:
    canonical_order_id: str
    environment: str
    account_id: str
    provider_order_id: str
    strike_id: str
    order_quantity: Decimal | str | int
    cumulative_filled: Decimal | str | int
    status: TrackedOrderStatus

    def __post_init__(self) -> None:
        for field in (
            "canonical_order_id",
            "environment",
            "account_id",
            "provider_order_id",
            "strike_id",
        ):
            _text(getattr(self, field), field)
        if not isinstance(self.status, TrackedOrderStatus):
            raise ProphetXSelfMatchError("status must be TrackedOrderStatus")
        quantity = _decimal(self.order_quantity, "order_quantity", positive=True)
        filled = _decimal(self.cumulative_filled, "cumulative_filled")
        if filled > quantity:
            raise ProphetXSelfMatchError("cumulative_filled cannot exceed order_quantity")
        if self.status is TrackedOrderStatus.WORKING and filled != 0:
            raise ProphetXSelfMatchError("WORKING baseline cannot contain fills")
        if self.status is TrackedOrderStatus.PARTIALLY_FILLED and filled <= 0:
            raise ProphetXSelfMatchError("PARTIALLY_FILLED requires positive fill")
        object.__setattr__(self, "order_quantity", quantity)
        object.__setattr__(self, "cumulative_filled", filled)


@dataclass(frozen=True, slots=True)
class WipeObservation:
    evidence_id: str
    environment: str
    account_id: str
    provider_order_id: str
    strike_id: str
    cumulative_filled: Decimal | str | int
    provider_status: str = "wiped"

    def __post_init__(self) -> None:
        for field in (
            "evidence_id",
            "environment",
            "account_id",
            "provider_order_id",
            "strike_id",
        ):
            _text(getattr(self, field), field)
        if self.provider_status != "wiped":
            raise ProphetXSelfMatchError("WipeObservation provider_status must be 'wiped'")
        object.__setattr__(
            self,
            "cumulative_filled",
            _decimal(self.cumulative_filled, "cumulative_filled"),
        )

    @property
    def wire(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "environment": self.environment,
            "account_id": self.account_id,
            "provider_order_id": self.provider_order_id,
            "strike_id": self.strike_id,
            "cumulative_filled": _dtext(self.cumulative_filled),
            "provider_status": self.provider_status,
        }


@dataclass(frozen=True, slots=True)
class WipeIncident:
    environment: str
    account_id: str
    incoming_provider_order_id: str
    relevant_resting_provider_order_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("environment", "account_id", "incoming_provider_order_id"):
            _text(getattr(self, field), field)
        if not isinstance(self.relevant_resting_provider_order_ids, tuple):
            raise ProphetXSelfMatchError("relevant_resting_provider_order_ids must be tuple")
        if not self.relevant_resting_provider_order_ids:
            raise ProphetXSelfMatchError("incident requires at least one relevant resting order")
        seen: set[str] = set()
        for order_id in self.relevant_resting_provider_order_ids:
            _text(order_id, "relevant_resting_provider_order_id")
            if order_id == self.incoming_provider_order_id:
                raise ProphetXSelfMatchError("incoming order cannot also be a resting order")
            if order_id in seen:
                raise ProphetXSelfMatchError("duplicate relevant resting order")
            seen.add(order_id)

    @property
    def all_provider_order_ids(self) -> tuple[str, ...]:
        return (self.incoming_provider_order_id, *self.relevant_resting_provider_order_ids)


@dataclass(frozen=True, slots=True)
class WipeReleaseEffect:
    canonical_order_id: str
    provider_order_id: str
    filled_quantity: Decimal
    released_quantity: Decimal
    cause: WipeCause
    effect_id: str


@dataclass(frozen=True, slots=True)
class WipeReconciliation:
    effects: tuple[WipeReleaseEffect, ...]
    required_readback_provider_order_ids: tuple[str, ...]
    cause: WipeCause
    projection_id: str

    @property
    def total_released_quantity(self) -> Decimal:
        return sum((effect.released_quantity for effect in self.effects), Decimal("0"))

    @property
    def requires_readback(self) -> bool:
        return bool(self.required_readback_provider_order_ids)


def reconcile_wipes(
    incident: WipeIncident,
    tracked_orders: Iterable[TrackedOrder],
    observations: Iterable[WipeObservation],
) -> WipeReconciliation:
    if not isinstance(incident, WipeIncident):
        raise ProphetXSelfMatchError("incident must be WipeIncident")

    tracked_by_id: dict[str, TrackedOrder] = {}
    for order in tuple(tracked_orders):
        if not isinstance(order, TrackedOrder):
            raise ProphetXSelfMatchError("tracked_orders must contain TrackedOrder")
        if (order.environment, order.account_id) != (incident.environment, incident.account_id):
            raise ProphetXSelfMatchConflict("tracked order incident scope mismatch")
        if order.provider_order_id in tracked_by_id:
            raise ProphetXSelfMatchConflict("duplicate tracked provider order")
        tracked_by_id[order.provider_order_id] = order

    expected_ids = set(incident.all_provider_order_ids)
    missing_baseline = expected_ids - set(tracked_by_id)
    if missing_baseline:
        raise ProphetXSelfMatchConflict(
            "incident lacks tracked baseline for: " + ",".join(sorted(missing_baseline))
        )

    by_evidence_id: dict[str, WipeObservation] = {}
    final_by_order: dict[str, WipeObservation] = {}
    for observation in tuple(observations):
        if not isinstance(observation, WipeObservation):
            raise ProphetXSelfMatchError("observations must contain WipeObservation")
        previous_evidence = by_evidence_id.get(observation.evidence_id)
        if previous_evidence is not None:
            if previous_evidence != observation:
                raise ProphetXSelfMatchConflict("conflicting replay of wipe evidence id")
            continue
        by_evidence_id[observation.evidence_id] = observation

        if (observation.environment, observation.account_id) != (
            incident.environment,
            incident.account_id,
        ):
            raise ProphetXSelfMatchConflict("wipe observation incident scope mismatch")
        if observation.provider_order_id not in expected_ids:
            raise ProphetXSelfMatchConflict("wipe observation is outside incident order set")

        tracked = tracked_by_id[observation.provider_order_id]
        if observation.strike_id != tracked.strike_id:
            raise ProphetXSelfMatchConflict("wipe observation strike identity mismatch")
        if observation.cumulative_filled < tracked.cumulative_filled:
            raise ProphetXSelfMatchConflict("wipe cannot reduce known cumulative fill")
        if observation.cumulative_filled > tracked.order_quantity:
            raise ProphetXSelfMatchConflict("wipe cumulative fill exceeds order quantity")

        prior_terminal = final_by_order.get(observation.provider_order_id)
        if prior_terminal is not None and prior_terminal != observation:
            # A terminal wipe must be stable. Different evidence ids carrying the same
            # provider terminal state are harmless; any state change is contradictory.
            prior_state = (
                prior_terminal.environment,
                prior_terminal.account_id,
                prior_terminal.provider_order_id,
                prior_terminal.strike_id,
                prior_terminal.cumulative_filled,
                prior_terminal.provider_status,
            )
            current_state = (
                observation.environment,
                observation.account_id,
                observation.provider_order_id,
                observation.strike_id,
                observation.cumulative_filled,
                observation.provider_status,
            )
            if prior_state != current_state:
                raise ProphetXSelfMatchConflict("conflicting terminal wipe evidence")
        final_by_order[observation.provider_order_id] = observation

    effects: list[WipeReleaseEffect] = []
    for provider_order_id in sorted(final_by_order):
        observation = final_by_order[provider_order_id]
        tracked = tracked_by_id[provider_order_id]
        released = tracked.order_quantity - observation.cumulative_filled
        effect_payload = {
            "environment": incident.environment,
            "account_id": incident.account_id,
            "canonical_order_id": tracked.canonical_order_id,
            "provider_order_id": provider_order_id,
            "provider_status": "wiped",
            "filled_quantity": _dtext(observation.cumulative_filled),
            "released_quantity": _dtext(released),
            "cause": WipeCause.WIPED_CAUSE_UNRESOLVED.value,
        }
        effects.append(
            WipeReleaseEffect(
                canonical_order_id=tracked.canonical_order_id,
                provider_order_id=provider_order_id,
                filled_quantity=observation.cumulative_filled,
                released_quantity=released,
                cause=WipeCause.WIPED_CAUSE_UNRESOLVED,
                effect_id=_digest(effect_payload),
            )
        )

    observed_ids = set(final_by_order)
    required = tuple(sorted(expected_ids - observed_ids))
    projection_payload = {
        "incident": {
            "environment": incident.environment,
            "account_id": incident.account_id,
            "incoming_provider_order_id": incident.incoming_provider_order_id,
            "relevant_resting_provider_order_ids": list(
                incident.relevant_resting_provider_order_ids
            ),
        },
        "effects": [
            {
                "canonical_order_id": effect.canonical_order_id,
                "provider_order_id": effect.provider_order_id,
                "filled_quantity": _dtext(effect.filled_quantity),
                "released_quantity": _dtext(effect.released_quantity),
                "cause": effect.cause.value,
                "effect_id": effect.effect_id,
            }
            for effect in effects
        ],
        "required_readback_provider_order_ids": list(required),
        "cause": WipeCause.WIPED_CAUSE_UNRESOLVED.value,
    }
    return WipeReconciliation(
        effects=tuple(effects),
        required_readback_provider_order_ids=required,
        cause=WipeCause.WIPED_CAUSE_UNRESOLVED,
        projection_id=_digest(projection_payload),
    )
