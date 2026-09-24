from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import Iterable, Sequence
from weakref import ref

from ..betfair_account_readonly import (
    BetfairMarketBookDepthObservation,
    assert_market_book_depth_authoritative,
    market_book_depth_acquisition_started_at,
)
from ..real_execution_ledger import RealExecutionLedger
from ..supervised_execution import BoundSupervisedExecutionPlan


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
    expected_market_version: int | None
    expected_inplay: bool | None
    expected_bet_delay_seconds: int | None
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
        if self.expected_market_version is not None and self.expected_market_version < 0:
            raise ValueError("expected_market_version must be non-negative")
        if self.expected_inplay is not None and type(self.expected_inplay) is not bool:
            raise ValueError("expected_inplay must be bool when supplied")
        if self.expected_bet_delay_seconds is not None and self.expected_bet_delay_seconds < 0:
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
    selection_status: str
    market_version: int
    inplay: bool
    bet_delay_seconds: int
    observed_at: datetime
    received_at: datetime
    sequence: int | None
    has_ordering_gap: bool | None
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
            self.selection_status,
        )
        if self.projection_depth is not None and self.projection_depth <= 0:
            raise ValueError("projection_depth must be positive when supplied")
        if self.market_version < 0:
            raise ValueError("market_version must be non-negative")
        if self.bet_delay_seconds < 0:
            raise ValueError("bet_delay_seconds must be non-negative")
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("sequence must be non-negative when supplied")
        if self.has_ordering_gap is not None and type(self.has_ordering_gap) is not bool:
            raise ValueError("has_ordering_gap must be bool or None")
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.received_at, "received_at")


@dataclass(frozen=True, slots=True, weakref_slot=True)
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
    liquidity_overlap_key: str

    @property
    def sufficient(self) -> bool:
        return (
            self.state is FeasibilityState.SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY
            and _is_execution_feasibility_result_authoritative(self)
        )


# This seal is an API-level provenance fence inside a trusted Python process.
# Ordinary imports expose neither a writable issuance registry nor a callable
# mint. Arbitrary same-interpreter reflection/object-graph/code mutation is
# explicitly outside this boundary; no cryptographic process isolation is claimed.
EXECUTION_FEASIBILITY_RESULT_TRUST_BOUNDARY = "trusted-process-api-provenance-v1"


def _feasibility_result_fingerprint(result: ExecutionFeasibilitySnapshot) -> str:
    """Bind every semantics-bearing result field to one exact issued object."""

    return _canonical_digest(
        {
            "schema": "autosport.execution-feasibility-result-authority.v1",
            "state": result.state.value,
            "evidence_digest": result.evidence_digest,
            "reasons": list(result.reasons),
            "requested_stake": str(result.requested_stake),
            "limit_price": str(result.limit_price),
            "displayed_acceptable_depth": str(result.displayed_acceptable_depth),
            "snapshot_id": result.snapshot_id,
            "snapshot_digest": result.snapshot_digest,
            "provider_id": result.provider_id,
            "account_id": result.account_id,
            "market_id": result.market_id,
            "selection_id": result.selection_id,
            "market_version": result.market_version,
            "inplay": result.inplay,
            "bet_delay_seconds": result.bet_delay_seconds,
            "observed_at": result.observed_at.isoformat(),
            "received_at": result.received_at.isoformat(),
            "decision_at": result.decision_at.isoformat(),
            "source_mode": result.source_mode.value,
            "projection_kind": result.projection_kind.value,
            "liquidity_overlap_key": result.liquidity_overlap_key,
        }
    )


def assess_execution_feasibility(
    request: ExecutionFeasibilityRequest,
    snapshot: MarketBookSnapshot,
    limits: ProviderLimitAuthority,
    *,
    max_snapshot_age: timedelta,
) -> ExecutionFeasibilitySnapshot:
    """Validate untrusted/caller-supplied assertions without issuing positive truth."""

    return _assess_execution_feasibility(
        request,
        snapshot,
        limits,
        max_snapshot_age=max_snapshot_age,
        product_owned=False,
    )


def _assess_execution_feasibility(
    request: ExecutionFeasibilityRequest,
    snapshot: MarketBookSnapshot,
    limits: ProviderLimitAuthority,
    *,
    max_snapshot_age: timedelta,
    product_owned: bool,
) -> ExecutionFeasibilitySnapshot:
    """Validate caller assertions and derive conservative displayed depth.

    Public DTOs are assertion-only. Until this path re-resolves a canonical
    product-issued depth receipt plus provider/account limit authority, it must
    fail closed and cannot mint SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY.
    """
    if max_snapshot_age <= timedelta(0):
        raise ValueError("max_snapshot_age must be positive")

    reasons: list[str] = []
    _append_if(reasons, request.side != "BACK", "UNSUPPORTED_SIDE")
    _append_if(reasons, request.order_type != "LIMIT", "UNSUPPORTED_ORDER_TYPE")
    _append_if(reasons, request.leg_count != 1, "MULTI_LEG_LIQUIDITY_REUSE_FORBIDDEN")
    _append_if(reasons, request.fill_or_kill, "FILL_OR_KILL_NOT_STANDARD_LIMIT")
    _append_if(reasons, request.minimum_fill_size is not None, "MINIMUM_FILL_NOT_STANDARD_LIMIT")
    _append_if(reasons, request.bet_target_type is not None, "BET_TARGET_FORBIDDEN")
    _append_if(reasons, request.smart_order, "SMART_ORDER_FORBIDDEN")

    _append_if(reasons, snapshot.provider_id != request.provider_id, "PROVIDER_ID_MISMATCH")
    _append_if(reasons, snapshot.account_id != request.account_id, "ACCOUNT_ID_MISMATCH")
    _append_if(reasons, snapshot.market_id != request.market_id, "MARKET_ID_MISMATCH")
    _append_if(reasons, snapshot.selection_id != request.selection_id, "SELECTION_ID_MISMATCH")
    _append_if(reasons, snapshot.source_mode is not SourceMode.LIVE, "DELAYED_SOURCE")
    _append_if(reasons, snapshot.status.upper() != "OPEN", "MARKET_NOT_OPEN")
    _append_if(reasons, snapshot.selection_status.upper() != "ACTIVE", "SELECTION_NOT_ACTIVE")
    _append_if(
        reasons,
        request.expected_market_version is not None
        and snapshot.market_version != request.expected_market_version,
        "MARKET_VERSION_MISMATCH",
    )
    _append_if(
        reasons,
        request.expected_inplay is not None
        and snapshot.inplay != request.expected_inplay,
        "INPLAY_MISMATCH",
    )
    _append_if(
        reasons,
        request.expected_bet_delay_seconds is not None
        and snapshot.bet_delay_seconds != request.expected_bet_delay_seconds,
        "BET_DELAY_MISMATCH",
    )
    _append_if(reasons, snapshot.has_ordering_gap is True, "SNAPSHOT_ORDERING_GAP")
    _append_if(reasons, snapshot.received_at < snapshot.observed_at, "RECEIVED_BEFORE_OBSERVED")
    _append_if(reasons, snapshot.observed_at > request.decision_at, "FUTURE_SNAPSHOT")
    _append_if(reasons, snapshot.received_at > request.decision_at, "RECEIVED_AFTER_DECISION")
    _append_if(reasons, request.decision_at - snapshot.observed_at > max_snapshot_age, "STALE_SNAPSHOT")
    _append_if(reasons, snapshot.rollup_model not in (None, "", "NONE"), "ROLLUP_SUBSTITUTION_FORBIDDEN")
    _append_if(reasons, snapshot.virtualise, "VIRTUALISED_LADDER_FORBIDDEN")
    _append_if(reasons, snapshot.is_truncated, "TRUNCATED_PROJECTION")

    if snapshot.projection_kind is ProjectionKind.EX_BEST_OFFERS:
        _append_if(reasons, snapshot.projection_depth is None, "BEST_OFFERS_DEPTH_UNBOUND")
        if snapshot.projection_depth is not None:
            _append_if(reasons, len(snapshot.available_to_back) > snapshot.projection_depth, "BEST_OFFERS_DEPTH_INCONSISTENT")

    _append_if(reasons, limits.provider_id != request.provider_id, "LIMIT_PROVIDER_MISMATCH")
    _append_if(reasons, limits.account_id != request.account_id, "LIMIT_ACCOUNT_MISMATCH")
    _append_if(reasons, limits.market_id != request.market_id, "LIMIT_MARKET_MISMATCH")
    _append_if(reasons, not limits.permitted, "LIMIT_AUTHORITY_REJECTED")
    if limits.min_stake is not None:
        _append_if(reasons, request.requested_stake < limits.min_stake, "STAKE_BELOW_PROVIDER_MINIMUM")
    if limits.max_stake is not None:
        _append_if(reasons, request.requested_stake > limits.max_stake, "STAKE_ABOVE_PROVIDER_MAXIMUM")
    if limits.min_price is not None:
        _append_if(reasons, request.limit_price < limits.min_price, "PRICE_BELOW_PROVIDER_MINIMUM")
    if limits.max_price is not None:
        _append_if(reasons, request.limit_price > limits.max_price, "PRICE_ABOVE_PROVIDER_MAXIMUM")

    # Public DTOs are assertion-only. Only the product-owned resolver below can
    # cross this seam after validating a provider-issued receipt and durable plan.
    if not product_owned:
        reasons.append("PRODUCT_OWNED_EVIDENCE_UNRESOLVED")

    # Betfair names availableToBack from the customer's action perspective.
    # A standard BACK LIMIT can consume displayed available-to-back offers at the
    # requested price or better. This is still snapshot evidence, never a fill.
    displayed_depth = sum(
        (quote.size for quote in snapshot.available_to_back if quote.price >= request.limit_price),
        Decimal("0"),
    )

    evidence_valid = not reasons
    if displayed_depth < request.requested_stake:
        reasons.append("DISPLAYED_DEPTH_INSUFFICIENT")

    if not evidence_valid:
        state = FeasibilityState.UNKNOWN_UNPROVEN
    elif displayed_depth < request.requested_stake:
        state = FeasibilityState.DISPLAYED_DEPTH_AT_SNAPSHOT
    else:
        state = FeasibilityState.SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY

    liquidity_overlap_key = _liquidity_overlap_key(snapshot)
    evidence_digest = _evidence_digest(
        request=request,
        snapshot=snapshot,
        limits=limits,
        displayed_depth=displayed_depth,
        state=state,
        reasons=reasons,
        liquidity_overlap_key=liquidity_overlap_key,
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
        liquidity_overlap_key=liquidity_overlap_key,
    )


def _assess_authoritative_betfair_execution_feasibility_unsealed(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    receipt: BetfairMarketBookDepthObservation,
    *,
    action_id: str,
    max_snapshot_age: timedelta,
) -> ExecutionFeasibilitySnapshot:
    """Resolve provider depth against the durable plan and fail closed on limits.

    The receipt must have been minted by the authenticated Betfair read-only
    client. That same authority seam supplies the product-owned decision instant;
    callers cannot backdate freshness or action expiry. The exact execution plan
    must already exist in the canonical real execution ledger with the same
    fingerprint. Positive standard-LIMIT
    feasibility additionally needs canonical provider/account/currency/market
    admissibility evidence; profile/adapter identity alone is not that authority.
    Until that authority is composed here, the result remains UNKNOWN_UNPROVEN
    while preserving the measured displayed-depth diagnostics.
    """

    if not isinstance(ledger, RealExecutionLedger):
        raise TypeError("ledger must be RealExecutionLedger")
    if not isinstance(bound, BoundSupervisedExecutionPlan):
        raise TypeError("bound must be BoundSupervisedExecutionPlan")
    if not isinstance(receipt, BetfairMarketBookDepthObservation):
        raise TypeError("receipt must be BetfairMarketBookDepthObservation")
    acquisition_started_at = market_book_depth_acquisition_started_at(receipt)
    decision_at = assert_market_book_depth_authoritative(receipt)
    _require_aware(acquisition_started_at, "acquisition_started_at")
    _require_aware(decision_at, "decision_at")
    bound.verify_binding()
    try:
        saga = ledger.saga(bound.execution_plan.plan_id)
    except KeyError as exc:
        raise ValueError(
            "product-owned feasibility requires a durably reserved execution plan"
        ) from exc
    if saga.plan_fingerprint != bound.execution_plan.fingerprint:
        raise ValueError("durable execution-plan fingerprint mismatch")

    action = bound.action_for(action_id)
    try:
        provider_selection_id = int(action.selection_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Betfair selection_id must be canonical positive integer text") from exc
    if provider_selection_id <= 0 or str(provider_selection_id) != action.selection_id:
        raise ValueError("Betfair selection_id must be canonical positive integer text")

    action_expiry = datetime.fromisoformat(
        action.expires_at.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    decision_utc = decision_at.astimezone(timezone.utc)
    if decision_utc >= action_expiry:
        raise ValueError("execution action expired before feasibility decision")

    binding = bound.profile_for(action.bookmaker_id, action.account_id)
    action_digest = _canonical_digest(action.to_dict())
    response_received_at = _provider_timestamp(receipt.evidence.observed_at)
    if response_received_at < acquisition_started_at:
        raise ValueError("market-book response received before acquisition started")
    try:
        action_quote_observed_at = datetime.fromisoformat(
            action.quote_observed_at.replace("Z", "+00:00")
        )
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            "execution action quote_observed_at must be ISO-8601"
        ) from exc
    _require_aware(
        action_quote_observed_at,
        "execution action quote_observed_at",
    )
    action_quote_observed_at = action_quote_observed_at.astimezone(timezone.utc)
    if acquisition_started_at.astimezone(timezone.utc) < action_quote_observed_at:
        raise ValueError(
            "market-book depth observation predates durable execution quote"
        )
    request = ExecutionFeasibilityRequest(
        opportunity_id=bound.intent_id,
        opportunity_digest=bound.intent_sha256,
        plan_id=bound.execution_plan.plan_id,
        plan_digest=bound.execution_plan.fingerprint,
        action_id=action.action_id,
        action_digest=action_digest,
        provider_id=action.bookmaker_id,
        account_id=action.account_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        requested_stake=action.requested_stake,
        limit_price=action.requested_odds,
        decision_at=decision_at,
        # No independent durable pre-snapshot market-state expectation is
        # currently composed into this authority. Keep the provider-observed
        # values bound in MarketBookSnapshot/evidence instead of copying them
        # into "expected" fields and creating tautological mismatch checks.
        expected_market_version=None,
        expected_inplay=None,
        expected_bet_delay_seconds=None,
        side=action.side,
        order_type="LIMIT",
    )
    snapshot_digest = _canonical_digest(
        {
            "schema": "autosport.betfair.market-book-authority.v2",
            "request_scope_sha256": receipt.request_scope_sha256,
            "source_payload_sha256": receipt.evidence.source_payload_sha256,
            "acquisition_started_at": _timestamp(acquisition_started_at),
            "response_received_at": _timestamp(response_received_at),
        }
    )
    snapshot = MarketBookSnapshot(
        snapshot_id=(
            "betfair-market-book:"
            + receipt.request_scope_sha256
            + ":"
            + receipt.evidence.source_payload_sha256
        ),
        snapshot_digest=snapshot_digest,
        provider_id=receipt.venue_id,
        account_id=receipt.account_id,
        market_id=receipt.market_id,
        selection_id=str(receipt.selection_id),
        source_mode=(
            SourceMode.DELAYED
            if receipt.is_market_data_delayed
            else SourceMode.LIVE
        ),
        projection_kind=ProjectionKind.EX_ALL_OFFERS,
        projection_depth=None,
        rollup_model=None,
        virtualise=receipt.virtualise,
        is_truncated=False,
        status=receipt.status,
        selection_status=receipt.selection_status,
        market_version=receipt.market_version,
        inplay=receipt.inplay,
        bet_delay_seconds=receipt.bet_delay_seconds,
        # listMarketBook has no provider publish timestamp. Use the sealed local
        # request start as a conservative observation lower bound and the
        # post-response product timestamp as received_at. Generic freshness then
        # covers the entire acquisition interval instead of only post-receipt age.
        observed_at=acquisition_started_at,
        received_at=response_received_at,
        # listMarketBook is an independent point-in-time read, not a Stream API
        # sequence. Keep stream sequence/gap explicitly unavailable instead of
        # inventing one from marketVersion.
        sequence=None,
        has_ordering_gap=None,
        available_to_back=tuple(
            PriceSize(item.price, item.size)
            for item in receipt.available_to_back
        ),
    )
    limit_evidence = _canonical_digest(
        {
            "schema": (
                "autosport.execution-feasibility-"
                "unqualified-provider-limit-evidence.v1"
            ),
            "provider_limit_authority_available": False,
            "plan_id": bound.execution_plan.plan_id,
            "plan_fingerprint": bound.execution_plan.fingerprint,
            "venue_id": binding.venue_id,
            "account_id": binding.account_id,
            "adapter_id": binding.adapter_id,
            "adapter_version": binding.adapter_version,
            "profile_version": binding.profile_version,
            "profile_sha256": binding.profile_sha256,
        }
    )
    limits = ProviderLimitAuthority(
        provider_id=action.bookmaker_id,
        account_id=action.account_id,
        market_id=action.market_id,
        evidence_digest=limit_evidence,
        # Profile/adapter identity proves implementation compatibility only. It
        # does not prove current provider/account funds/exposure headroom,
        # currency/jurisdiction minimum-bet rules, or the market price-ladder
        # tick contract. Until those canonical authorities are composed, a
        # positive execution-feasibility verdict would overstate executable
        # truth, so this authoritative resolver deliberately fails closed.
        permitted=False,
    )
    result = _assess_execution_feasibility(
        request,
        snapshot,
        limits,
        max_snapshot_age=max_snapshot_age,
        product_owned=True,
    )
    return result


def _install_execution_feasibility_result_authority():
    issued: dict[int, tuple[object, str]] = {}
    raw_assess = _assess_authoritative_betfair_execution_feasibility_unsealed
    fingerprint = _feasibility_result_fingerprint

    def assess(
        ledger: RealExecutionLedger,
        bound: BoundSupervisedExecutionPlan,
        receipt: BetfairMarketBookDepthObservation,
        *,
        action_id: str,
        max_snapshot_age: timedelta,
    ) -> ExecutionFeasibilitySnapshot:
        result = raw_assess(
            ledger,
            bound,
            receipt,
            action_id=action_id,
            max_snapshot_age=max_snapshot_age,
        )
        if type(result) is not ExecutionFeasibilitySnapshot:
            raise TypeError("authoritative feasibility resolver returned invalid result type")
        result_id = id(result)
        result_fingerprint = fingerprint(result)

        def forget(current: object, *, result_id: int = result_id) -> None:
            existing = issued.get(result_id)
            if existing is not None and existing[0] is current:
                issued.pop(result_id, None)

        reference = ref(result, forget)
        issued[result_id] = (reference, result_fingerprint)
        return result

    def is_authoritative(result: ExecutionFeasibilitySnapshot) -> bool:
        if type(result) is not ExecutionFeasibilitySnapshot:
            return False
        current = issued.get(id(result))
        if current is None or current[0]() is not result:
            return False
        try:
            return current[1] == fingerprint(result)
        except Exception:
            return False

    return assess, is_authoritative


(
    assess_authoritative_betfair_execution_feasibility,
    _is_execution_feasibility_result_authoritative,
) = _install_execution_feasibility_result_authority()


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




def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provider_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("provider observed_at must be ISO-8601") from exc
    _require_aware(parsed, "provider observed_at")
    return parsed


def _liquidity_overlap_key(snapshot: MarketBookSnapshot) -> str:
    return _canonical_digest(
        {
            "schema": "autosport.execution-feasibility-liquidity-overlap.v1",
            "provider_id": snapshot.provider_id,
            "account_id": snapshot.account_id,
            "market_id": snapshot.market_id,
            "selection_id": snapshot.selection_id,
            "snapshot_digest": snapshot.snapshot_digest,
            "market_version": snapshot.market_version,
            "selection_status": snapshot.selection_status,
            "projection_kind": snapshot.projection_kind.value,
            "projection_depth": snapshot.projection_depth,
            "rollup_model": snapshot.rollup_model,
            "virtualise": snapshot.virtualise,
            "side": "BACK",
            "ladder": _ladder_payload(snapshot.available_to_back),
        }
    )


def _evidence_digest(
    *, request: ExecutionFeasibilityRequest, snapshot: MarketBookSnapshot,
    limits: ProviderLimitAuthority, displayed_depth: Decimal,
    state: FeasibilityState, reasons: Sequence[str],
    liquidity_overlap_key: str,
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
            "selection_status": snapshot.selection_status,
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
            "liquidity_overlap_key": liquidity_overlap_key,
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
