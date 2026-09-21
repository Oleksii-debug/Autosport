from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import Iterable, Sequence


class FeasibilityState(str, Enum):
    UNKNOWN_UNPROVEN = "UNKNOWN_UNPROVEN"
    DISPLAYED_DEPTH_AT_SNAPSHOT = "DISPLAYED_DEPTH_AT_SNAPSHOT"
    SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY = "SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY"


class SourceMode(str, Enum):
    LIVE = "LIVE"
    DELAYED = "DELAYED"


class ProjectionKind(str, Enum):
    EX_ALL_OFFERS = "EX_ALL_OFFERS"
    EX_BEST_OFFERS = "EX_BEST_OFFERS"


@dataclass(frozen=True, slots=True)
class PriceSize:
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError("price must be positive")
        if self.size < 0:
            raise ValueError("size must be non-negative")


@dataclass(frozen=True, slots=True)
class ExecutionFeasibilityRequest:
    opportunity_id: str
    opportunity_digest: str
    plan_id: str
    plan_digest: str
    action_id: str
    action_digest: str
    provider_id: str
    account_id: str
    market_id: str
    selection_id: str
    requested_stake: Decimal
    limit_price: Decimal
    decision_at: datetime
    expected_market_version: int
    expected_inplay: bool
    expected_bet_delay_seconds: int
    side: str = "BACK"
    order_type: str = "LIMIT"
    leg_count: int = 1
    fill_or_kill: bool = False
    minimum_fill_size: Decimal | None = None
    bet_target_type: str | None = None
    smart_order: bool = False

    def __post_init__(self) -> None:
        _require_nonempty(
            self.opportunity_id,
            self.opportunity_digest,
            self.plan_id,
            self.plan_digest,
            self.action_id,
            self.action_digest,
            self.provider_id,
            self.account_id,
            self.market_id,
            self.selection_id,
        )
        if self.requested_stake <= 0:
            raise ValueError("requested_stake must be positive")
        if self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if self.expected_market_version < 0:
            raise ValueError("expected_market_version must be non-negative")
        if self.expected_bet_delay_seconds < 0:
            raise ValueError("expected_bet_delay_seconds must be non-negative")
        _require_aware(self.decision_at, "decision_at")


@dataclass(frozen=True, slots=True)
class ProviderLimitAuthority:
    provider_id: str
    account_id: str
    market_id: str
    evidence_digest: str
    permitted: bool
    min_stake: Decimal | None = None
    max_stake: Decimal | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None

    def __post_init__(self) -> None:
        _require_nonempty(
            self.provider_id,
            self.account_id,
            self.market_id,
            self.evidence_digest,
        )


@dataclass(frozen=True, slots=True)
class MarketBookSnapshot:
    snapshot_id: str
    snapshot_digest: str
    provider_id: str
    account_id: str
    market_id: str
    selection_id: str
    source_mode: SourceMode
    projection_kind: ProjectionKind
    projection_depth: int | None
    rollup_model: str | None
    virtualise: bool
    is_truncated: bool
    status: str
    market_version: int
    inplay: bool
    bet_delay_seconds: int
    observed_at: datetime
    received_at: datetime
    sequence: int
    has_ordering_gap: bool
    available_to_back: tuple[PriceSize, ...]

    def __post_init__(self) -> None:
        _require_nonempty(
            self.snapshot_id,
            self.snapshot_digest,
            self.provider_id,
            self.account_id,
            self.market_id,
            self.selection_id,
            self.status,
        )
        if self.projection_depth is not None and self.projection_depth <= 0:
            raise ValueError("projection_depth must be positive when supplied")
        if self.market_version < 0:
            raise ValueError("market_version must be non-negative")
        if self.bet_delay_seconds < 0:
            raise ValueError("bet_delay_seconds must be non-negative")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.received_at, "received_at")


@dataclass(frozen=True, slots=True)
class ExecutionFeasibilitySnapshot:
    state: FeasibilityState
    evidence_digest: str
    reasons: tuple[str, ...]
    requested_stake: Decimal
    limit_price: Decimal
    displayed_acceptable_depth: Decimal
    snapshot_id: str
    snapshot_digest: str
    provider_id: str
    account_id: str
    market_id: str
    selection_id: str
    market_version: int
    inplay: bool
    bet_delay_seconds: int
    observed_at: datetime
    received_at: datetime
    decision_at: datetime
    source_mode: SourceMode
    projection_kind: ProjectionKind

    @property
    def sufficient(self) -> bool:
        return self.state is FeasibilityState.SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY


def assess_execution_feasibility(
    request: ExecutionFeasibilityRequest,
    snapshot: MarketBookSnapshot,
    limits: ProviderLimitAuthority,
    *,
    max_snapshot_age: timedelta,
) -> ExecutionFeasibilitySnapshot:
    """Derive conservative decision-time displayed-liquidity evidence.

    The strongest result intentionally says only that the displayed acceptable
    depth was sufficient at one authenticated snapshot. It never reserves
    liquidity, predicts a fill, or proves atomic execution.
    """
    if max_snapshot_age <= timedelta(0):
        raise ValueError("max_snapshot_age must be positive")

    reasons: list[str] = []
    _append_if(reasons, request.side != "BACK", "UNSUPPORTED_SIDE")
    _append_if(reasons, request.order_type != "LIMIT", "UNSUPPORTED_ORDER_TYPE")
    _append_if(reasons, request.leg_count != 1, "MULTI_LEG_LIQUIDITY_REUSE_FORBIDDEN")
    _append_if(reasons, request.fill_or_kill, "FILL_OR_KILL_NOT_STANDARD_LIMIT")
    _append_if(
        reasons,
        request.minimum_fill_size is not None,
        "MINIMUM_FILL_NOT_STANDARD_LIMIT",
    )
    _append_if(reasons, request.bet_target_type is not None, "BET_TARGET_FORBIDDEN")
    _append_if(reasons, request.smart_order, "SMART_ORDER_FORBIDDEN")

    _append_if(reasons, snapshot.provider_id != request.provider_id, "PROVIDER_ID_MISMATCH")
    _append_if(reasons, snapshot.account_id != request.account_id, "ACCOUNT_ID_MISMATCH")
    _append_if(reasons, snapshot.market_id != request.market_id, "MARKET_ID_MISMATCH")
    _append_if(reasons, snapshot.selection_id != request.selection_id, "SELECTION_ID_MISMATCH")
    _append_if(reasons, snapshot.source_mode is not SourceMode.LIVE, "DELAYED_SOURCE")
    _append_if(reasons, snapshot.status.upper() != "OPEN", "MARKET_NOT_OPEN")
    _append_if(
        reasons,
        snapshot.market_version != request.expected_market_version,
        "MARKET_VERSION_MISMATCH",
    )
    _append_if(reasons, snapshot.inplay != request.expected_inplay, "INPLAY_MISMATCH")
    _append_if(
        reasons,
        snapshot.bet_delay_seconds != request.expected_bet_delay_seconds,
        "BET_DELAY_MISMATCH",
    )
    _append_if(reasons, snapshot.has_ordering_gap, "SNAPSHOT_ORDERING_GAP")
    _append_if(reasons, snapshot.received_at < snapshot.observed_at, "RECEIVED_BEFORE_OBSERVED")
    _append_if(reasons, snapshot.observed_at > request.decision_at, "FUTURE_SNAPSHOT")
    _append_if(
        reasons,
        request.decision_at - snapshot.observed_at > max_snapshot_age,
        "STALE_SNAPSHOT",
    )
    _append_if(
        reasons,
        snapshot.rollup_model not in (None, "", "NONE"),
        "ROLLUP_SUBSTITUTION_FORBIDDEN",
    )
    _append_if(reasons, snapshot.virtualise, "VIRTUALISED_LADDER_FORBIDDEN")
    _append_if(reasons, snapshot.is_truncated, "TRUNCATED_PROJECTION")

    if snapshot.projection_kind is ProjectionKind.EX_BEST_OFFERS:
        _append_if(reasons, snapshot.projection_depth is None, "BEST_OFFERS_DEPTH_UNBOUND")
        if snapshot.projection_depth is not None:
            _append_if(
                reasons,
                len(snapshot.available_to_back) > snapshot.projection_depth,
                "BEST_OFFERS_DEPTH_INCONSISTENT",
            )

    _append_if(reasons, limits.provider_id != request.provider_id, "LIMIT_PROVIDER_MISMATCH")
    _append_if(reasons, limits.account_id != request.account_id, "LIMIT_ACCOUNT_MISMATCH")
    _append_if(reasons, limits.market_id != request.market_id, "LIMIT_MARKET_MISMATCH")
    _append_if(reasons, not limits.permitted, "LIMIT_AUTHORITY_REJECTED")
    if limits.min_stake is not None:
        _append_if(
            reasons,
            request.requested_stake < limits.min_stake,
            "STAKE_BELOW_PROVIDER_MINIMUM",
        )
    if limits.max_stake is not None:
        _append_if(
            reasons,
            request.requested_stake > limits.max_stake,
            "STAKE_ABOVE_PROVIDER_MAXIMUM",
        )
    if limits.min_price is not None:
        _append_if(
            reasons,
            request.limit_price < limits.min_price,
            "PRICE_BELOW_PROVIDER_MINIMUM",
        )
    if limits.max_price is not None:
        _append_if(
            reasons,
            request.limit_price > limits.max_price,
            "PRICE_ABOVE_PROVIDER_MAXIMUM",
        )

    displayed_depth = sum(
        (
            quote.size
            for quote in snapshot.available_to_back
            if quote.price >= request.limit_price
        ),
        Decimal("0"),
    )

    if reasons:
        state = FeasibilityState.UNKNOWN_UNPROVEN
    elif displayed_depth >= request.requested_stake:
        state = FeasibilityState.SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY
    else:
        state = FeasibilityState.DISPLAYED_DEPTH_AT_SNAPSHOT
        reasons.append("DISPLAYED_DEPTH_INSUFFICIENT")

    evidence_digest = _evidence_digest(
        request=request,
        snapshot=snapshot,
        limits=limits,
        displayed_depth=displayed_depth,
        state=state,
        reasons=reasons,
    )
    return ExecutionFeasibilitySnapshot(
        state=state,
        evidence_digest=evidence_digest,
        reasons=tuple(reasons),
        requested_stake=request.requested_stake,
        limit_price=request.limit_price,
        displayed_acceptable_depth=displayed_depth,
        snapshot_id=snapshot.snapshot_id,
        snapshot_digest=snapshot.snapshot_digest,
        provider_id=snapshot.provider_id,
        account_id=snapshot.account_id,
        market_id=snapshot.market_id,
        selection_id=snapshot.selection_id,
        market_version=snapshot.market_version,
        inplay=snapshot.inplay,
        bet_delay_seconds=snapshot.bet_delay_seconds,
        observed_at=snapshot.observed_at,
        received_at=snapshot.received_at,
        decision_at=request.decision_at,
        source_mode=snapshot.source_mode,
        projection_kind=snapshot.projection_kind,
    )


def _append_if(reasons: list[str], condition: bool, reason: str) -> None:
    if condition:
        reasons.append(reason)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_nonempty(*values: str) -> None:
    if any(not value.strip() for value in values):
        raise ValueError("identity and digest fields must be non-empty")


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ladder_payload(quotes: Iterable[PriceSize]) -> list[dict[str, str | None]]:
    return [{"price": _decimal(q.price), "size": _decimal(q.size)} for q in quotes]


def _evidence_digest(
    *,
    request: ExecutionFeasibilityRequest,
    snapshot: MarketBookSnapshot,
    limits: ProviderLimitAuthority,
    displayed_depth: Decimal,
    state: FeasibilityState,
    reasons: Sequence[str],
) -> str:
    payload = {
        "schema": "autosport.execution-feasibility-snapshot.v1",
        "request": {
            "opportunity_id": request.opportunity_id,
            "opportunity_digest": request.opportunity_digest,
            "plan_id": request.plan_id,
            "plan_digest": request.plan_digest,
            "action_id": request.action_id,
            "action_digest": request.action_digest,
            "provider_id": request.provider_id,
            "account_id": request.account_id,
            "market_id": request.market_id,
            "selection_id": request.selection_id,
            "requested_stake": _decimal(request.requested_stake),
            "limit_price": _decimal(request.limit_price),
            "decision_at": _timestamp(request.decision_at),
            "expected_market_version": request.expected_market_version,
            "expected_inplay": request.expected_inplay,
            "expected_bet_delay_seconds": request.expected_bet_delay_seconds,
            "side": request.side,
            "order_type": request.order_type,
            "leg_count": request.leg_count,
            "fill_or_kill": request.fill_or_kill,
            "minimum_fill_size": _decimal(request.minimum_fill_size),
            "bet_target_type": request.bet_target_type,
            "smart_order": request.smart_order,
        },
        "snapshot": {
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot.snapshot_digest,
            "provider_id": snapshot.provider_id,
            "account_id": snapshot.account_id,
            "market_id": snapshot.market_id,
            "selection_id": snapshot.selection_id,
            "source_mode": snapshot.source_mode.value,
            "projection_kind": snapshot.projection_kind.value,
            "projection_depth": snapshot.projection_depth,
            "rollup_model": snapshot.rollup_model,
            "virtualise": snapshot.virtualise,
            "is_truncated": snapshot.is_truncated,
            "status": snapshot.status,
            "market_version": snapshot.market_version,
            "inplay": snapshot.inplay,
            "bet_delay_seconds": snapshot.bet_delay_seconds,
            "observed_at": _timestamp(snapshot.observed_at),
            "received_at": _timestamp(snapshot.received_at),
            "sequence": snapshot.sequence,
            "has_ordering_gap": snapshot.has_ordering_gap,
            "available_to_back": _ladder_payload(snapshot.available_to_back),
        },
        "limits": {
            "provider_id": limits.provider_id,
            "account_id": limits.account_id,
            "market_id": limits.market_id,
            "evidence_digest": limits.evidence_digest,
            "permitted": limits.permitted,
            "min_stake": _decimal(limits.min_stake),
            "max_stake": _decimal(limits.max_stake),
            "min_price": _decimal(limits.min_price),
            "max_price": _decimal(limits.max_price),
        },
        "derived": {
            "state": state.value,
            "displayed_acceptable_depth": _decimal(displayed_depth),
            "reasons": list(reasons),
        },
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
