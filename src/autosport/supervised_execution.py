from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from .bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerCapability,
    BookmakerCapabilityError,
    BookmakerCapabilityProfile,
    BookmakerPositionObservation,
)
from .bookmaker_routing import RoutingState
from .bookmaker_routing_plan import ParallelRoutingProposal
from .portfolio_plan import OpportunityIntent, PortfolioAction, PortfolioPlan
from .supervised_provider_evidence import (
    ProviderEvidenceError,
    VerifiedProviderAbsenceEvidence,
    VerifiedProviderEffectEvidence,
    assert_verified_provider_evidence_authoritative,
    verify_betfair_provider_state,
)
from .real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    ExecutionAction,
    ExecutionAttempt,
    ExecutionPlan,
    ExternalAcknowledgement,
    ExternalEffectReconciliation,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


class SupervisedExecutionError(RuntimeError):
    pass


class ApprovalState(str, Enum):
    APPROVED = "approved"
    REVOKED = "revoked"


class ReadbackOutcome(str, Enum):
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise SupervisedExecutionError(f"{name} must be non-empty canonical text")
    return value


def _time(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupervisedExecutionError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SupervisedExecutionError(f"{name} must be timezone-aware")
    return parsed


def _sha(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise SupervisedExecutionError(f"{name} must be lowercase SHA-256 hex")
    return raw


def _digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SupervisedExecutionError("bridge evidence is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def _trusted_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _quote_payload(quote: object) -> dict[str, object]:
    return {
        "event_id": getattr(quote, "event_id"),
        "market_id": getattr(quote, "market_id"),
        "selection_id": getattr(quote, "selection_id"),
        "source_id": getattr(quote, "source_id"),
        "sequence": getattr(quote, "sequence"),
        "decimal_odds": str(getattr(quote, "decimal_odds")),
        "observed_ts": getattr(quote, "observed_ts"),
        "source_ts": getattr(quote, "source_ts"),
        "ingest_ts": getattr(quote, "ingest_ts"),
        "sport": getattr(quote, "sport", None),
        "market_snapshot_hash": getattr(quote, "market_snapshot_hash", None),
        "market_event_hash": getattr(quote, "market_event_hash"),
    }


def _profile_payload(profile: BookmakerCapabilityProfile) -> dict[str, object]:
    return {
        "venue_id": profile.venue_id,
        "account_id": profile.account_id,
        "adapter_id": profile.adapter_id,
        "adapter_version": profile.adapter_version,
        "profile_version": profile.profile_version,
        "facts": [
            [fact.capability.value, fact.state.value]
            for fact in sorted(profile.facts, key=lambda fact: fact.capability.value)
        ],
        "observed_at": profile.observed_at,
        "source_ref": profile.source_ref,
        "source_payload_sha256": profile.source_payload_sha256,
    }


@dataclass(frozen=True, slots=True)
class SupervisedApproval:
    approval_id: str
    portfolio_plan_sha256: str
    intent_id: str
    routing_request_id: str
    execution_terms_sha256: str
    approved_at: str
    expires_at: str
    evidence_sha256: str
    state: ApprovalState = ApprovalState.APPROVED

    def __post_init__(self) -> None:
        _text(self.approval_id, "approval_id")
        _sha(self.portfolio_plan_sha256, "portfolio_plan_sha256")
        _text(self.intent_id, "intent_id")
        _text(self.routing_request_id, "routing_request_id")
        _sha(self.execution_terms_sha256, "execution_terms_sha256")
        if _time(self.expires_at, "expires_at") <= _time(self.approved_at, "approved_at"):
            raise SupervisedExecutionError("approval expiry must follow approval time")
        _sha(self.evidence_sha256, "evidence_sha256")
        if not isinstance(self.state, ApprovalState):
            raise SupervisedExecutionError("state must be ApprovalState")

    @property
    def fingerprint(self) -> str:
        return _digest(
            {
                "schema": "autosport.supervised_approval",
                "schema_version": 1,
                "approval_id": self.approval_id,
                "portfolio_plan_sha256": self.portfolio_plan_sha256,
                "intent_id": self.intent_id,
                "routing_request_id": self.routing_request_id,
                "execution_terms_sha256": self.execution_terms_sha256,
                "approved_at": self.approved_at,
                "expires_at": self.expires_at,
                "evidence_sha256": self.evidence_sha256,
                "state": self.state.value,
            }
        )

    @property
    def ledger_identity(self) -> str:
        return f"{self.approval_id}@{self.fingerprint}"

    def require_active(self, at: str) -> None:
        now = _time(at, "approval check time")
        if self.state is not ApprovalState.APPROVED:
            raise SupervisedExecutionError("supervised approval is not APPROVED")
        if now < _time(self.approved_at, "approved_at") or now >= _time(
            self.expires_at, "expires_at"
        ):
            raise SupervisedExecutionError("supervised approval is not active")


@dataclass(frozen=True, slots=True)
class ExecutionLegConstraint:
    leg_id: str
    side: str
    quote_expires_at: str
    max_slippage_fraction: Decimal

    def __post_init__(self) -> None:
        _sha(self.leg_id, "leg_id")
        if _text(self.side, "side") != "BACK":
            raise SupervisedExecutionError(
                "supervised bridge supports BACK only until upstream LAY liability authority exists"
            )
        _time(self.quote_expires_at, "quote_expires_at")
        if (
            not isinstance(self.max_slippage_fraction, Decimal)
            or not self.max_slippage_fraction.is_finite()
            or self.max_slippage_fraction < 0
            or self.max_slippage_fraction >= 1
        ):
            raise SupervisedExecutionError("max_slippage_fraction must be exact Decimal in [0,1)")

    def to_dict(self) -> dict[str, str]:
        return {
            "leg_id": self.leg_id,
            "side": self.side,
            "quote_expires_at": self.quote_expires_at,
            "max_slippage_fraction": str(self.max_slippage_fraction),
        }


def supervised_execution_terms_sha256(
    routing_proposal: ParallelRoutingProposal,
    constraints: tuple[ExecutionLegConstraint, ...],
) -> str:
    if not isinstance(routing_proposal, ParallelRoutingProposal):
        raise SupervisedExecutionError("routing_proposal must be ParallelRoutingProposal")
    if type(constraints) is not tuple or any(
        not isinstance(item, ExecutionLegConstraint) for item in constraints
    ):
        raise SupervisedExecutionError("constraints must contain ExecutionLegConstraint values")
    by_leg = {item.leg_id: item for item in constraints}
    if len(by_leg) != len(constraints) or set(by_leg) != {
        leg.leg_id for leg in routing_proposal.legs
    }:
        raise SupervisedExecutionError("constraints must exactly cover unique routing legs")
    return _digest(
        {
            "schema": "autosport.supervised_execution_terms",
            "schema_version": 1,
            "parent_plan_id": routing_proposal.parent_plan_id,
            "routing_request_id": routing_proposal.routing_request_id,
            "state": routing_proposal.state.value,
            "residual_before": str(routing_proposal.residual_before),
            "confirmed_total": str(routing_proposal.confirmed_total),
            "proposed_total": str(routing_proposal.proposed_total),
            "stake_quantum": str(routing_proposal.stake_quantum),
            "legs": [
                {
                    "leg_id": leg.leg_id,
                    "venue_id": leg.venue.venue_id,
                    "account_id": leg.venue.account_id,
                    "proposed_stake": str(leg.proposed_stake),
                    "quote": _quote_payload(leg.venue.quote),
                    "constraint": by_leg[leg.leg_id].to_dict(),
                }
                for leg in routing_proposal.legs
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class ProfileBinding:
    venue_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    profile_version: int
    profile_sha256: str


@dataclass(frozen=True, slots=True)
class BoundSupervisedExecutionPlan:
    execution_plan: ExecutionPlan
    portfolio_plan_sha256: str
    intent_id: str
    intent_sha256: str
    approval_fingerprint: str
    profile_bindings: tuple[ProfileBinding, ...]
    constraints: tuple[ExecutionLegConstraint, ...]

    def __post_init__(self) -> None:
        self.verify_binding()

    def verify_binding(self) -> None:
        expected = _bound_binding_sha256(
            self.execution_plan,
            self.portfolio_plan_sha256,
            self.intent_id,
            self.intent_sha256,
            self.approval_fingerprint,
            self.profile_bindings,
            self.constraints,
        )
        if self.execution_plan.plan_id != f"supervised-v2-{expected}":
            raise SupervisedExecutionError(
                "bound execution metadata does not match durable plan identity"
            )

    def action_for(self, action_id: str) -> ExecutionAction:
        matches = [item for item in self.execution_plan.actions if item.action_id == action_id]
        if len(matches) != 1:
            raise SupervisedExecutionError("action is not in bound execution plan")
        return matches[0]

    def constraint_for(self, action_id: str) -> ExecutionLegConstraint:
        matches = [item for item in self.constraints if item.leg_id == action_id]
        if len(matches) != 1:
            raise SupervisedExecutionError("action lacks exact constraint binding")
        return matches[0]

    def profile_for(self, venue_id: str, account_id: str) -> ProfileBinding:
        matches = [
            item
            for item in self.profile_bindings
            if (item.venue_id, item.account_id) == (venue_id, account_id)
        ]
        if len(matches) != 1:
            raise SupervisedExecutionError("action lacks exact profile binding")
        return matches[0]


def _bound_binding_sha256(
    execution_plan: ExecutionPlan,
    portfolio_plan_sha256: str,
    intent_id: str,
    intent_sha256: str,
    approval_fingerprint: str,
    profile_bindings: tuple[ProfileBinding, ...],
    constraints: tuple[ExecutionLegConstraint, ...],
) -> str:
    plan = execution_plan.to_dict()
    plan.pop("plan_id")
    return _digest(
        {
            "schema": "autosport.supervised_execution_bridge_binding",
            "schema_version": 2,
            "execution_plan_without_id": plan,
            "portfolio_plan_sha256": portfolio_plan_sha256,
            "intent_id": intent_id,
            "intent_sha256": intent_sha256,
            "approval_fingerprint": approval_fingerprint,
            "profile_bindings": [
                {
                    "venue_id": item.venue_id,
                    "account_id": item.account_id,
                    "adapter_id": item.adapter_id,
                    "adapter_version": item.adapter_version,
                    "profile_version": item.profile_version,
                    "profile_sha256": item.profile_sha256,
                }
                for item in profile_bindings
            ],
            "constraints": [item.to_dict() for item in constraints],
        }
    )


@dataclass(frozen=True, slots=True)
class ProviderReadback:
    bookmaker_id: str
    account_id: str
    action_id: str
    adapter_id: str
    adapter_version: str
    profile_version: int
    event_id: str
    market_id: str
    selection_id: str
    external_receipt_id: str
    observed_at: str
    source_payload_sha256: str
    status: AcknowledgementStatus
    reconciliation_evidence_required: bool = False
    accepted_odds: Decimal | None = None
    accepted_stake: Decimal | None = None
    terminal_settlement_exact: bool = False

    def __post_init__(self) -> None:
        for name in (
            "bookmaker_id",
            "account_id",
            "action_id",
            "adapter_id",
            "adapter_version",
            "event_id",
            "market_id",
            "selection_id",
            "external_receipt_id",
        ):
            _text(getattr(self, name), name)
        if (
            not isinstance(self.profile_version, int)
            or isinstance(self.profile_version, bool)
            or self.profile_version < 1
        ):
            raise SupervisedExecutionError("profile_version must be a positive integer")
        _time(self.observed_at, "observed_at")
        _sha(self.source_payload_sha256, "source_payload_sha256")
        if not isinstance(self.status, AcknowledgementStatus):
            raise SupervisedExecutionError("status must be AcknowledgementStatus")
        if type(self.reconciliation_evidence_required) is not bool:
            raise SupervisedExecutionError("reconciliation_evidence_required must be bool")
        if type(self.terminal_settlement_exact) is not bool:
            raise SupervisedExecutionError("terminal_settlement_exact must be bool")
        if self.status in {AcknowledgementStatus.ACCEPTED, AcknowledgementStatus.PARTIAL}:
            if (
                not isinstance(self.accepted_odds, Decimal)
                or not self.accepted_odds.is_finite()
                or self.accepted_odds <= 0
                or not isinstance(self.accepted_stake, Decimal)
                or not self.accepted_stake.is_finite()
                or self.accepted_stake <= 0
            ):
                raise SupervisedExecutionError("accepted/partial readback requires exact positive odds/stake")
        elif self.accepted_odds is not None or self.accepted_stake is not None:
            raise SupervisedExecutionError("rejected readback cannot claim accepted odds/stake")

    @property
    def evidence_id(self) -> str:
        return _digest(
            {
                "schema": "autosport.provider_execution_readback",
                "schema_version": 1,
                "bookmaker_id": self.bookmaker_id,
                "account_id": self.account_id,
                "action_id": self.action_id,
                "adapter_id": self.adapter_id,
                "adapter_version": self.adapter_version,
                "profile_version": self.profile_version,
                "event_id": self.event_id,
                "market_id": self.market_id,
                "selection_id": self.selection_id,
                "external_receipt_id": self.external_receipt_id,
                "observed_at": self.observed_at,
                "source_payload_sha256": self.source_payload_sha256,
                "status": self.status.value,
                "reconciliation_evidence_required": self.reconciliation_evidence_required,
                "accepted_odds": None if self.accepted_odds is None else str(self.accepted_odds),
                "accepted_stake": None if self.accepted_stake is None else str(self.accepted_stake),
                "terminal_settlement_exact": self.terminal_settlement_exact,
            }
        )


@dataclass(frozen=True, slots=True)
class ProviderNotFoundReadback:
    bookmaker_id: str
    account_id: str
    action_id: str
    adapter_id: str
    adapter_version: str
    profile_version: int
    event_id: str
    market_id: str
    selection_id: str
    observed_at: str
    current_source_payload_sha256: str
    cleared_source_payload_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "bookmaker_id",
            "account_id",
            "action_id",
            "adapter_id",
            "adapter_version",
            "event_id",
            "market_id",
            "selection_id",
        ):
            _text(getattr(self, name), name)
        if (
            not isinstance(self.profile_version, int)
            or isinstance(self.profile_version, bool)
            or self.profile_version < 1
        ):
            raise SupervisedExecutionError("profile_version must be a positive integer")
        _time(self.observed_at, "observed_at")
        _sha(self.current_source_payload_sha256, "current_source_payload_sha256")
        _sha(self.cleared_source_payload_sha256, "cleared_source_payload_sha256")

    @property
    def evidence_id(self) -> str:
        return _digest(
            {
                "schema": "autosport.provider_execution_not_found_readback",
                "schema_version": 1,
                "bookmaker_id": self.bookmaker_id,
                "account_id": self.account_id,
                "action_id": self.action_id,
                "adapter_id": self.adapter_id,
                "adapter_version": self.adapter_version,
                "profile_version": self.profile_version,
                "event_id": self.event_id,
                "market_id": self.market_id,
                "selection_id": self.selection_id,
                "observed_at": self.observed_at,
                "current_source_payload_sha256": self.current_source_payload_sha256,
                "cleared_source_payload_sha256": self.cleared_source_payload_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    outcome: ReadbackOutcome
    attempt_state: AttemptState
    evidence_id: str | None


def _profile_bindings(
    profiles: tuple[BookmakerCapabilityProfile, ...],
) -> tuple[tuple[ProfileBinding, ...], str]:
    if not profiles or any(not isinstance(item, BookmakerCapabilityProfile) for item in profiles):
        raise SupervisedExecutionError("profiles must contain canonical capability profiles")
    payloads: list[dict[str, object]] = []
    bindings: list[ProfileBinding] = []
    seen: set[tuple[str, str]] = set()
    for profile in profiles:
        identity = (profile.venue_id, profile.account_id)
        if identity in seen:
            raise SupervisedExecutionError("duplicate venue/account capability profile")
        seen.add(identity)
        payload = _profile_payload(profile)
        payloads.append(payload)
        bindings.append(
            ProfileBinding(
                profile.venue_id,
                profile.account_id,
                profile.adapter_id,
                profile.adapter_version,
                profile.profile_version,
                profile.profile_id,
            )
        )
    ordered = sorted(zip(bindings, payloads, strict=True), key=lambda pair: (pair[0].venue_id, pair[0].account_id))
    return tuple(pair[0] for pair in ordered), _digest([pair[1] for pair in ordered])


def build_supervised_execution_plan(
    portfolio_plan: PortfolioPlan,
    intents: tuple[OpportunityIntent, ...],
    routing_proposal: ParallelRoutingProposal,
    profiles: tuple[BookmakerCapabilityProfile, ...],
    approval: SupervisedApproval,
    constraints: tuple[ExecutionLegConstraint, ...],
    *,
    created_at: str,
) -> BoundSupervisedExecutionPlan:
    """Build a durable plan binding without invoking any bookmaker write surface."""

    if not isinstance(portfolio_plan, PortfolioPlan) or not isinstance(
        routing_proposal, ParallelRoutingProposal
    ):
        raise SupervisedExecutionError("canonical PortfolioPlan and routing proposal are required")
    if any(not isinstance(item, OpportunityIntent) for item in intents):
        raise SupervisedExecutionError("intents must contain canonical OpportunityIntent values")
    created = _time(created_at, "created_at")
    if created < _time(portfolio_plan.decision_ts, "decision_ts"):
        raise SupervisedExecutionError("execution plan cannot predate portfolio decision")
    approval.require_active(created_at)

    plan_sha = portfolio_plan.plan_sha256
    if approval.portfolio_plan_sha256 != plan_sha or routing_proposal.parent_plan_id != plan_sha:
        raise SupervisedExecutionError("approval/routing does not bind exact PortfolioPlan")
    if approval.routing_request_id != routing_proposal.routing_request_id:
        raise SupervisedExecutionError("approval does not bind exact routing request")
    if portfolio_plan.action in {PortfolioAction.WAIT, PortfolioAction.ZERO}:
        raise SupervisedExecutionError("WAIT/ZERO cannot become execution authority")
    if routing_proposal.state not in {RoutingState.ROUTE, RoutingState.PARTIAL} or not routing_proposal.legs:
        raise SupervisedExecutionError("routing proposal has no execution proposal authority")
    if routing_proposal.confirmed_total != 0:
        raise SupervisedExecutionError("execution requires fresh routing before any confirmed effect")
    if tuple(item.intent_id for item in intents) != portfolio_plan.intent_ids or tuple(
        item.intent_sha256 for item in intents
    ) != portfolio_plan.intent_sha256s:
        raise SupervisedExecutionError("intent vector does not exactly match PortfolioPlan")
    try:
        index = portfolio_plan.intent_ids.index(approval.intent_id)
    except ValueError as exc:
        raise SupervisedExecutionError("approved intent is not in PortfolioPlan") from exc
    intent = intents[index]
    selected_stake = portfolio_plan.stakes[index]
    if selected_stake <= 0 or routing_proposal.residual_before != selected_stake:
        raise SupervisedExecutionError("routing residual must equal positive approved portfolio stake")

    for profile in profiles:
        try:
            profile.require(BookmakerCapability.BET_READBACK)
        except BookmakerCapabilityError as exc:
            raise SupervisedExecutionError(
                "execution bridge requires SUPPORTED BET_READBACK capability"
            ) from exc
        if _time(profile.observed_at, "profile observed_at") > created:
            raise SupervisedExecutionError(
                "execution bridge cannot consume future capability evidence"
            )
    bindings, profile_set_sha = _profile_bindings(profiles)
    profiles_by_identity = {(item.venue_id, item.account_id): item for item in bindings}
    constraint_by_leg = {item.leg_id: item for item in constraints}
    if len(constraint_by_leg) != len(constraints) or set(constraint_by_leg) != {
        leg.leg_id for leg in routing_proposal.legs
    }:
        raise SupervisedExecutionError("constraints must exactly cover unique routing legs")
    if approval.execution_terms_sha256 != supervised_execution_terms_sha256(
        routing_proposal, constraints
    ):
        raise SupervisedExecutionError("approval does not bind exact execution terms")

    actions: list[ExecutionAction] = []
    action_bindings: list[dict[str, object]] = []
    for leg in routing_proposal.legs:
        quote = leg.venue.quote
        if quote not in intent.opportunity.quotes:
            raise SupervisedExecutionError("routing quote is outside selected OpportunityIntent")
        profile = profiles_by_identity.get((leg.venue.venue_id, leg.venue.account_id))
        if profile is None:
            raise SupervisedExecutionError("routing leg lacks venue/account profile binding")
        constraint = constraint_by_leg[leg.leg_id]
        if _time(quote.observed_ts, "quote observed_ts") > created:
            raise SupervisedExecutionError("execution cannot consume a future quote")
        if _time(constraint.quote_expires_at, "quote_expires_at") <= created:
            raise SupervisedExecutionError("execution quote is expired")
        if _time(constraint.quote_expires_at, "quote_expires_at") > _time(
            approval.expires_at, "approval expires_at"
        ):
            raise SupervisedExecutionError("quote expiry cannot outlive approval")
        quote_id = _digest(
            {
                "quote": _quote_payload(quote),
                "intent_sha256": intent.intent_sha256,
                "leg_id": leg.leg_id,
                "constraint": constraint.to_dict(),
            }
        )
        action = ExecutionAction(
            action_id=leg.leg_id,
            bookmaker_id=leg.venue.venue_id,
            account_id=leg.venue.account_id,
            event_id=quote.event_id,
            market_id=quote.market_id,
            selection_id=quote.selection_id,
            side=constraint.side,
            requested_odds=quote.decimal_odds,
            requested_stake=leg.proposed_stake,
            quote_id=quote_id,
            quote_observed_at=quote.observed_ts,
            expires_at=constraint.quote_expires_at,
        )
        actions.append(action)
        action_bindings.append(
            {
                "action": action.to_dict(),
                "profile_sha256": profile.profile_sha256,
                "constraint": constraint.to_dict(),
            }
        )

    profile_version_id = f"profile-set-v1-{profile_set_sha}"
    decision_id = f"portfolio:{plan_sha}:intent:{intent.intent_sha256}"
    provisional = ExecutionPlan(
        plan_id="pending-supervised-v2-binding",
        bookmaker_profile_version=profile_version_id,
        decision_id=decision_id,
        approval_id=approval.ledger_identity,
        created_at=created_at,
        actions=tuple(actions),
    )
    bridge_id = _bound_binding_sha256(
        provisional,
        plan_sha,
        intent.intent_id,
        intent.intent_sha256,
        approval.fingerprint,
        bindings,
        constraints,
    )
    execution = ExecutionPlan(
        plan_id=f"supervised-v2-{bridge_id}",
        bookmaker_profile_version=profile_version_id,
        decision_id=decision_id,
        approval_id=approval.ledger_identity,
        created_at=created_at,
        actions=tuple(actions),
    )
    return BoundSupervisedExecutionPlan(
        execution,
        plan_sha,
        intent.intent_id,
        intent.intent_sha256,
        approval.fingerprint,
        bindings,
        constraints,
    )


def _require_approval(
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    at: str,
) -> None:
    bound.verify_binding()
    approval.require_active(at)
    if (
        approval.portfolio_plan_sha256 != bound.portfolio_plan_sha256
        or approval.intent_id != bound.intent_id
        or approval.fingerprint != bound.approval_fingerprint
        or approval.ledger_identity != bound.execution_plan.approval_id
    ):
        raise SupervisedExecutionError("approval evidence changed or was revoked")


def _require_durable_approval(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
) -> None:
    if not ledger.supervised_approval_is_active(
        plan_id=bound.execution_plan.plan_id,
        approval_id=approval.ledger_identity,
        approval_fingerprint=approval.fingerprint,
    ):
        raise SupervisedExecutionError(
            "durable supervised approval is missing or revoked"
        )


def _require_reserved(ledger: RealExecutionLedger, bound: BoundSupervisedExecutionPlan) -> None:
    bound.verify_binding()
    try:
        saga = ledger.saga(bound.execution_plan.plan_id)
    except KeyError as exc:
        raise SupervisedExecutionError("execution plan is not reserved") from exc
    if saga.plan_fingerprint != bound.execution_plan.fingerprint:
        raise SupervisedExecutionError("durable execution-plan fingerprint mismatch")


def reserve_supervised_plan(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
) -> str:
    now = _trusted_now()
    _require_approval(bound, approval, now)
    fingerprint = ledger.reserve_plan(bound.execution_plan)
    ledger.bind_supervised_approval(
        plan_id=bound.execution_plan.plan_id,
        approval_id=approval.ledger_identity,
        approval_fingerprint=approval.fingerprint,
        approved_at=approval.approved_at,
        evidence_sha256=approval.evidence_sha256,
    )
    _require_durable_approval(ledger, bound, approval)
    return fingerprint


def revoke_supervised_approval(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    revocation_evidence_sha256: str,
) -> None:
    _require_reserved(ledger, bound)
    if (
        approval.fingerprint != bound.approval_fingerprint
        or approval.ledger_identity != bound.execution_plan.approval_id
    ):
        raise SupervisedExecutionError("approval identity mismatches bound plan")
    ledger.revoke_supervised_approval(
        plan_id=bound.execution_plan.plan_id,
        approval_id=approval.ledger_identity,
        approval_fingerprint=approval.fingerprint,
        revoked_at=_trusted_now(),
        revocation_evidence_sha256=_sha(
            revocation_evidence_sha256, "revocation_evidence_sha256"
        ),
    )


def begin_supervised_attempt(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    action_id: str,
    attempt_id: str,
) -> ExecutionAttempt:
    now = _trusted_now()
    _require_approval(bound, approval, now)
    _require_reserved(ledger, bound)
    _require_durable_approval(ledger, bound, approval)
    action = bound.action_for(action_id)
    if _time(now, "trusted current time") >= _time(action.expires_at, "expires_at"):
        raise SupervisedExecutionError("attempt is at/after quote expiry")
    return ledger.begin_attempt(
        plan_id=bound.execution_plan.plan_id,
        action_id=action_id,
        attempt_id=attempt_id,
        reserved_at=now,
    )


def _attempt_action(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    attempt_id: str,
) -> tuple[ExecutionAction, AttemptState]:
    _require_reserved(ledger, bound)
    saga = ledger.saga(bound.execution_plan.plan_id)
    action_id = saga.attempt_action_ids.get(attempt_id)
    if action_id is None:
        raise SupervisedExecutionError("attempt does not belong to bound plan")
    return bound.action_for(action_id), saga.attempts[attempt_id]


def _validate_slippage(
    action: ExecutionAction,
    constraint: ExecutionLegConstraint,
    accepted_odds: Decimal,
) -> None:
    if action.side == "BACK":
        limit = action.requested_odds * (Decimal("1") - constraint.max_slippage_fraction)
        if accepted_odds < limit:
            raise SupervisedExecutionError("accepted BACK odds exceed approved slippage")
    else:
        limit = action.requested_odds * (Decimal("1") + constraint.max_slippage_fraction)
        if accepted_odds > limit:
            raise SupervisedExecutionError("accepted LAY odds exceed approved slippage")


def _require_verified_profile(
    bound: BoundSupervisedExecutionPlan,
    action: ExecutionAction,
    *,
    adapter_id: str,
    adapter_version: str,
    profile_version: int,
) -> None:
    planned = bound.profile_for(action.bookmaker_id, action.account_id)
    if (
        adapter_id != planned.adapter_id
        or adapter_version != planned.adapter_version
        or profile_version != planned.profile_version
    ):
        raise SupervisedExecutionError(
            "provider evidence does not use exact approved capability profile"
        )


def reconcile_provider_readback(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    *,
    attempt_id: str,
    readback: VerifiedProviderEffectEvidence | ProviderReadback,
) -> ReconciliationResult:
    """Persist only mechanically verified canonical provider read-only evidence."""

    if isinstance(readback, ProviderReadback):
        if readback.terminal_settlement_exact:
            raise SupervisedExecutionError(
                "terminal settlement exactness is outside supervised execution authority"
            )
        raise SupervisedExecutionError(
            "verified canonical provider evidence is required"
        )
    if not isinstance(readback, VerifiedProviderEffectEvidence):
        raise SupervisedExecutionError(
            "verified canonical provider evidence is required"
        )
    try:
        assert_verified_provider_evidence_authoritative(readback)
    except ProviderEvidenceError as exc:
        raise SupervisedExecutionError(
            "verified canonical provider evidence is not authoritative"
        ) from exc
    action, state = _attempt_action(ledger, bound, attempt_id)
    if (
        readback.bookmaker_id,
        readback.account_id,
        readback.action_id,
        readback.event_id,
        readback.market_id,
        readback.selection_id,
    ) != (
        action.bookmaker_id,
        action.account_id,
        action.action_id,
        action.event_id,
        action.market_id,
        action.selection_id,
    ):
        raise SupervisedExecutionError(
            "verified provider evidence identity mismatches execution action"
        )
    _require_verified_profile(
        bound,
        action,
        adapter_id=readback.adapter_id,
        adapter_version=readback.adapter_version,
        profile_version=readback.profile_version,
    )
    if readback.accepted_stake > action.requested_stake:
        raise SupervisedExecutionError("readback exceeds requested stake")
    if (
        readback.status is AcknowledgementStatus.ACCEPTED
        and readback.accepted_stake != action.requested_stake
    ) or (
        readback.status is AcknowledgementStatus.PARTIAL
        and readback.accepted_stake >= action.requested_stake
    ):
        raise SupervisedExecutionError("provider status conflicts with accepted stake")
    _validate_slippage(
        action,
        bound.constraint_for(action.action_id),
        readback.accepted_odds,
    )

    direct_binding = ledger.provider_evidence_binding(attempt_id)
    reconciliation_evidence_id = (
        None if direct_binding is not None else readback.evidence_id
    )
    acknowledgement = ExternalAcknowledgement(
        attempt_id=attempt_id,
        external_receipt_id=readback.external_receipt_id,
        status=readback.status,
        acknowledged_at=readback.observed_at,
        accepted_odds=readback.accepted_odds,
        accepted_stake=readback.accepted_stake,
        reconciliation_evidence_id=reconciliation_evidence_id,
    )
    if state in {AttemptState.ACCEPTED, AttemptState.PARTIAL, AttemptState.REJECTED}:
        if direct_binding is not None and (
            direct_binding["evidence_id"] != readback.evidence_id
            or direct_binding["source"]
            != f"betfair-readonly:{readback.source_payload_sha256}"
        ):
            raise SupervisedExecutionError(
                "durable direct-ACK provider evidence conflicts on replay"
            )
        ledger.acknowledge(acknowledgement)
    elif state is AttemptState.UNKNOWN:
        ledger.reconcile_found(
            ExternalEffectReconciliation(
                attempt_id=attempt_id,
                evidence_id=readback.evidence_id,
                external_receipt_id=readback.external_receipt_id,
                observed_at=readback.observed_at,
                source=f"betfair-readonly:{readback.source_payload_sha256}",
            )
        )
        acknowledgement = ExternalAcknowledgement(
            attempt_id=attempt_id,
            external_receipt_id=readback.external_receipt_id,
            status=readback.status,
            acknowledged_at=readback.observed_at,
            accepted_odds=readback.accepted_odds,
            accepted_stake=readback.accepted_stake,
            reconciliation_evidence_id=readback.evidence_id,
        )
        ledger.acknowledge(acknowledgement)
    elif state is AttemptState.SUBMITTED:
        ledger.bind_provider_evidence(
            attempt_id=attempt_id,
            evidence_id=readback.evidence_id,
            observed_at=readback.observed_at,
            source=f"betfair-readonly:{readback.source_payload_sha256}",
        )
        acknowledgement = ExternalAcknowledgement(
            attempt_id=attempt_id,
            external_receipt_id=readback.external_receipt_id,
            status=readback.status,
            acknowledged_at=readback.observed_at,
            accepted_odds=readback.accepted_odds,
            accepted_stake=readback.accepted_stake,
            reconciliation_evidence_id=None,
        )
        ledger.acknowledge(acknowledgement)
    else:
        raise SupervisedExecutionError(
            "readback requires SUBMITTED/UNKNOWN or exact terminal replay"
        )
    final = ledger.attempt_state(attempt_id)
    outcome = {
        AttemptState.ACCEPTED: ReadbackOutcome.ACCEPTED,
        AttemptState.PARTIAL: ReadbackOutcome.PARTIAL,
        AttemptState.REJECTED: ReadbackOutcome.REJECTED,
    }.get(final)
    if outcome is None:
        raise SupervisedExecutionError(
            "readback did not reach deterministic acknowledgement"
        )
    return ReconciliationResult(outcome, final, readback.evidence_id)


def reconcile_provider_not_found(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    *,
    attempt_id: str,
    readback: VerifiedProviderAbsenceEvidence | ProviderNotFoundReadback,
) -> ReconciliationResult:
    """Release UNKNOWN retry only from complete canonical provider absence evidence."""

    if isinstance(readback, ProviderNotFoundReadback):
        raise SupervisedExecutionError(
            "verified complete provider absence evidence is required"
        )
    if not isinstance(readback, VerifiedProviderAbsenceEvidence):
        raise SupervisedExecutionError(
            "verified complete provider absence evidence is required"
        )
    try:
        assert_verified_provider_evidence_authoritative(readback)
    except ProviderEvidenceError as exc:
        raise SupervisedExecutionError(
            "verified complete provider absence evidence is not authoritative"
        ) from exc
    action, state = _attempt_action(ledger, bound, attempt_id)
    if state is not AttemptState.UNKNOWN:
        raise SupervisedExecutionError("not-found readback requires UNKNOWN attempt")
    if (
        readback.bookmaker_id,
        readback.account_id,
        readback.action_id,
        readback.event_id,
        readback.market_id,
        readback.selection_id,
    ) != (
        action.bookmaker_id,
        action.account_id,
        action.action_id,
        action.event_id,
        action.market_id,
        action.selection_id,
    ):
        raise SupervisedExecutionError(
            "verified not-found evidence identity mismatches execution action"
        )
    _require_verified_profile(
        bound,
        action,
        adapter_id=readback.adapter_id,
        adapter_version=readback.adapter_version,
        profile_version=readback.profile_version,
    )
    ledger.reconcile_not_found(
        ReconciliationSnapshot(
            attempt_id=attempt_id,
            evidence_id=readback.evidence_id,
            observed_at=readback.observed_at,
            external_effect_found=False,
            source=(
                "betfair-readonly-complete-current+cleared:"
                f"{readback.current_source_payload_sha256}:"
                f"{readback.cleared_source_payload_sha256}"
            ),
        )
    )
    return ReconciliationResult(
        ReadbackOutcome.NOT_FOUND,
        ledger.attempt_state(attempt_id),
        readback.evidence_id,
    )


def _position_receipt(position: BookmakerPositionObservation) -> str:
    return position.external_receipt_id or position.external_position_id


def reconcile_account_snapshot(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    *,
    attempt_id: str,
    snapshot: BookmakerAccountSnapshot,
    external_receipt_id: str,
) -> ReconciliationResult:
    """Qualify generic account evidence without releasing UNKNOWN.

    Canonical BookmakerPositionObservation omits market/selection identity. Therefore
    neither a matching generic position nor absence of a caller-selected receipt can
    prove the exact execution effect. Exact positive and NOT_FOUND transitions require
    provider-specific typed readback evidence.
    """

    receipt_id = _text(external_receipt_id, "external_receipt_id")
    action, state = _attempt_action(ledger, bound, attempt_id)
    if state is not AttemptState.UNKNOWN:
        raise SupervisedExecutionError("account snapshot reconciliation requires UNKNOWN attempt")
    if (snapshot.profile.venue_id, snapshot.profile.account_id) != (
        action.bookmaker_id,
        action.account_id,
    ):
        raise SupervisedExecutionError("snapshot provider/account mismatches action")
    planned = bound.profile_for(action.bookmaker_id, action.account_id)
    if (
        snapshot.profile.adapter_id != planned.adapter_id
        or snapshot.profile.adapter_version != planned.adapter_version
        or snapshot.profile.profile_version != planned.profile_version
    ):
        raise SupervisedExecutionError("snapshot adapter/profile authority drifted")

    matches = [
        item
        for item in tuple(snapshot.open_positions) + tuple(snapshot.settled_positions)
        if _position_receipt(item) == receipt_id
    ]
    if len(matches) > 1:
        raise SupervisedExecutionError("receipt appears in multiple account observations")
    if matches:
        position = matches[0]
        if position.provider_side is not None and position.provider_side != action.side:
            raise SupervisedExecutionError("snapshot side mismatches action")
        if position.provider_amount is not None and position.provider_amount > action.requested_stake:
            raise SupervisedExecutionError("matched stake exceeds requested stake")

    return ReconciliationResult(ReadbackOutcome.UNKNOWN, AttemptState.UNKNOWN, None)
