from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from .domain import PaperTicket, TicketStatus
from .paper import PaperBook
from .paper_execution_reality import (
    PaperAttemptOutcome,
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
    RecoveryDecision,
)
from .portfolio import PortfolioEngine
from .real_execution_ledger import ExecutionAction, ExecutionPlan

_SCHEMA = "autosport.execution.multileg_realized_exposure"
_TICKET_MARKER = "paper_execution_attempt_id="
_EXPOSURE_SCOPE_EVENT_TYPE = "PAPER_EXPOSURE_SCOPE_BOUND"
_EXPOSURE_SCOPE_SCHEMA = "autosport.paper_execution.exposure_scope_binding"
_SHA256_HEX = frozenset("0123456789abcdef")


class MultiLegExposureProjectionError(RuntimeError):
    """Canonical execution evidence cannot support the requested projection."""


class RealizedLegState(str, Enum):
    UNATTEMPTED = "UNATTEMPTED"
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


def _text_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise MultiLegExposureProjectionError("projection Decimal must be finite")
    return str(value)


def _canonical_json(payload: object) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise MultiLegExposureProjectionError(
            "scope evidence is not canonical JSON"
        ) from exc


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(char not in _SHA256_HEX for char in value)
    ):
        raise MultiLegExposureProjectionError(
            f"{label} must be canonical SHA-256 hex"
        )
    return value


def _optional_scope_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or value.strip() != value:
        raise MultiLegExposureProjectionError(
            f"{label} must be non-empty canonical text or null"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MultiLegExposureProjectionError(
            f"{label} must be UTF-8 encodable"
        ) from exc
    return value


def _scope_triplet(
    sport: object,
    bankroll_id: object,
    currency: object,
) -> tuple[str | None, str | None, str | None]:
    canonical_sport = _optional_scope_text(sport, "scope sport")
    canonical_bankroll = _optional_scope_text(bankroll_id, "scope bankroll_id")
    canonical_currency = _optional_scope_text(currency, "scope currency")
    if (canonical_bankroll is None) != (canonical_currency is None):
        raise MultiLegExposureProjectionError(
            "scope bankroll_id and currency must be supplied together"
        )
    return canonical_sport, canonical_bankroll, canonical_currency


@dataclass(frozen=True, slots=True)
class _ExposureScope:
    sport: str | None
    bankroll_id: str | None
    currency: str | None
    authoritative: bool


_LEGACY_SCOPE = _ExposureScope(None, None, None, False)


def _event_token(
    events: tuple[dict[str, Any], ...],
) -> tuple[tuple[int, str], ...]:
    token: list[tuple[int, str]] = []
    for event in events:
        sequence = event.get("sequence")
        digest = event.get("event_sha256")
        if type(sequence) is not int or sequence < 0:
            raise MultiLegExposureProjectionError(
                "invalid canonical execution sequence"
            )
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(ch not in _SHA256_HEX for ch in digest)
        ):
            raise MultiLegExposureProjectionError(
                "invalid canonical execution digest"
            )
        token.append((sequence, digest))
    return tuple(token)


def _reservation(
    events: tuple[dict[str, Any], ...],
) -> tuple[str, str, dict[str, str]]:
    found = [
        event for event in events if event.get("event_type") == "RUN_RESERVED"
    ]
    if len(found) != 1 or type(found[0].get("payload")) is not dict:
        raise MultiLegExposureProjectionError(
            "projection requires exactly one valid durable run reservation"
        )
    payload = found[0]["payload"]
    trigger_id = payload.get("trigger_id")
    started_at = payload.get("started_at")
    evidence = payload.get("observation_evidence_ids")
    if type(trigger_id) is not str or not trigger_id:
        raise MultiLegExposureProjectionError(
            "invalid durable trigger identity"
        )
    if type(started_at) is not str or not started_at:
        raise MultiLegExposureProjectionError(
            "invalid durable run start timestamp"
        )
    if type(evidence) is not dict or any(
        type(key) is not str
        or not key
        or type(value) is not str
        or not value
        for key, value in evidence.items()
    ):
        raise MultiLegExposureProjectionError(
            "invalid durable observation identity"
        )
    return trigger_id, started_at, dict(evidence)


def _marker(reason: str) -> str | None:
    if type(reason) is not str:
        raise MultiLegExposureProjectionError(
            "invalid PaperTicket strategy_reason"
        )
    values = [
        part.strip()[len(_TICKET_MARKER) :]
        for part in reason.split(";")
        if part.strip().startswith(_TICKET_MARKER)
    ]
    if len(values) > 1 or any(not value for value in values):
        raise MultiLegExposureProjectionError(
            "invalid PaperTicket execution marker"
        )
    return None if not values else values[0]


def _assert_attempt_action(
    attempt: Any,
    action: ExecutionAction,
    sequence: int,
) -> None:
    pairs = (
        (attempt.sequence, sequence, "sequence"),
        (attempt.action_id, action.action_id, "action_id"),
        (attempt.bookmaker_id, action.bookmaker_id, "bookmaker_id"),
        (attempt.account_id, action.account_id, "account_id"),
        (attempt.event_id, action.event_id, "event_id"),
        (attempt.market_id, action.market_id, "market_id"),
        (attempt.selection_id, action.selection_id, "selection_id"),
        (attempt.side, action.side, "side"),
        (attempt.decision_quote_id, action.quote_id, "decision_quote_id"),
        (attempt.decision_odds, action.requested_odds, "decision_odds"),
        (attempt.requested_stake, action.requested_stake, "requested_stake"),
    )
    for actual, expected, label in pairs:
        if actual != expected:
            raise MultiLegExposureProjectionError(
                f"durable attempt {label} does not match execution plan"
            )


def _scope_bindings_from_events(
    events: tuple[dict[str, Any], ...],
    plan: ExecutionPlan,
) -> dict[str, _ExposureScope]:
    scope_events = [
        event
        for event in events
        if event.get("event_type") == _EXPOSURE_SCOPE_EVENT_TYPE
    ]
    if not scope_events:
        # Historical/direct #623 runs did not carry the #646 binding. They remain
        # usable for aggregate exposure only and can never masquerade as scoped
        # bankroll/currency/sport evidence.
        return {
            action.action_id: _LEGACY_SCOPE
            for action in plan.actions
        }
    if len(scope_events) != 1 or type(scope_events[0].get("payload")) is not dict:
        raise MultiLegExposureProjectionError(
            "projection requires at most one valid durable #646 exposure-scope event"
        )
    payload = scope_events[0]["payload"]
    expected_fields = {
        "schema",
        "schema_version",
        "plan_id",
        "plan_fingerprint",
        "intent_evidence_sha256",
        "bindings",
        "binding_sha256",
    }
    if set(payload) != expected_fields:
        raise MultiLegExposureProjectionError(
            "durable exposure-scope event schema is invalid"
        )
    if (
        payload.get("schema") != _EXPOSURE_SCOPE_SCHEMA
        or payload.get("schema_version") != 1
        or payload.get("plan_id") != plan.plan_id
        or payload.get("plan_fingerprint") != plan.fingerprint
    ):
        raise MultiLegExposureProjectionError(
            "durable exposure-scope event does not bind exact execution plan"
        )
    _canonical_sha256(
        payload.get("intent_evidence_sha256"),
        "durable exposure intent_evidence_sha256",
    )
    binding_sha256 = _canonical_sha256(
        payload.get("binding_sha256"),
        "durable exposure binding_sha256",
    )
    body = {
        key: payload[key]
        for key in expected_fields
        if key != "binding_sha256"
    }
    if _digest(body) != binding_sha256:
        raise MultiLegExposureProjectionError(
            "durable exposure-scope binding digest mismatch"
        )

    raw_bindings = payload.get("bindings")
    if type(raw_bindings) is not list or len(raw_bindings) != len(plan.actions):
        raise MultiLegExposureProjectionError(
            "durable exposure-scope binding cardinality mismatch"
        )
    result: dict[str, _ExposureScope] = {}
    for action, raw in zip(plan.actions, raw_bindings, strict=True):
        if (
            type(raw) is not dict
            or set(raw) != {"action_id", "sport", "bankroll_id", "currency"}
            or raw.get("action_id") != action.action_id
        ):
            raise MultiLegExposureProjectionError(
                "durable exposure-scope binding action order/identity mismatch"
            )
        sport, bankroll_id, currency = _scope_triplet(
            raw.get("sport"),
            raw.get("bankroll_id"),
            raw.get("currency"),
        )
        if action.action_id in result:
            raise MultiLegExposureProjectionError(
                "durable exposure-scope binding contains duplicate action"
            )
        result[action.action_id] = _ExposureScope(
            sport,
            bankroll_id,
            currency,
            True,
        )
    if set(result) != {action.action_id for action in plan.actions}:
        raise MultiLegExposureProjectionError(
            "durable exposure-scope binding does not cover exact execution plan"
        )
    return result


def _ticket_for_attempt(
    tickets: tuple[PaperTicket, ...],
    attempt: Any,
    action: ExecutionAction,
    run_id: str,
    decision_id: str,
    scope: _ExposureScope = _LEGACY_SCOPE,
) -> PaperTicket:
    matches = [
        ticket
        for ticket in tickets
        if _marker(ticket.strategy_reason) == attempt.attempt_id
    ]
    if len(matches) != 1:
        raise MultiLegExposureProjectionError(
            "accepted execution attempt must bind exactly one durable PaperTicket"
        )
    ticket = matches[0]
    if ticket.status is not TicketStatus.OPEN:
        raise MultiLegExposureProjectionError(
            "current run PaperTicket is not OPEN"
        )
    if (
        ticket.stake != attempt.execution_stake
        or ticket.placed_at != attempt.execution_observed_at
        or ticket.provider_source_ids != (attempt.bookmaker_id,)
        or ticket.provider_accounts
        != ((attempt.bookmaker_id, attempt.account_id),)
        or len(ticket.legs) != 1
    ):
        raise MultiLegExposureProjectionError(
            "PaperTicket does not match durable execution attempt"
        )
    leg = ticket.legs[0]
    if (
        leg.event_id != action.event_id
        or leg.market_id != action.market_id
        or leg.selection_id != action.selection_id
        or leg.locked_odds != attempt.execution_odds
    ):
        raise MultiLegExposureProjectionError(
            "PaperTicket leg does not match durable execution attempt"
        )
    if scope.authoritative and (
        leg.sport != scope.sport
        or ticket.bankroll_id != scope.bankroll_id
        or ticket.currency != scope.currency
    ):
        raise MultiLegExposureProjectionError(
            "PaperTicket economic scope does not match canonical #646 binding"
        )
    reason = {
        part.strip() for part in ticket.strategy_reason.split(";")
    }
    if (
        f"decision_id={decision_id}" not in reason
        or f"run_id={run_id}" not in reason
    ):
        raise MultiLegExposureProjectionError(
            "PaperTicket does not bind execution decision/run identity"
        )
    return ticket


@dataclass(frozen=True, slots=True)
class RealizedLegExposure:
    sequence: int
    action_id: str
    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    requested_odds: Decimal
    requested_stake: Decimal
    state: RealizedLegState
    attempt_id: str | None
    execution_odds: Decimal | None
    realized_matched_stake: Decimal
    unresolved_stake_upper_bound: Decimal
    paper_ticket_id: str | None
    sport: str | None = None
    bankroll_id: str | None = None
    currency: str | None = None
    economic_scope_authoritative: bool = False

    def as_evidence(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "action_id": self.action_id,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "requested_odds": _text_decimal(self.requested_odds),
            "requested_stake": _text_decimal(self.requested_stake),
            "state": self.state.value,
            "attempt_id": self.attempt_id,
            "execution_odds": (
                None
                if self.execution_odds is None
                else _text_decimal(self.execution_odds)
            ),
            "realized_matched_stake": _text_decimal(
                self.realized_matched_stake
            ),
            "unresolved_stake_upper_bound": _text_decimal(
                self.unresolved_stake_upper_bound
            ),
            "paper_ticket_id": self.paper_ticket_id,
            "sport": self.sport,
            "bankroll_id": self.bankroll_id,
            "currency": self.currency,
            "economic_scope_authoritative": self.economic_scope_authoritative,
        }


@dataclass(frozen=True, slots=True)
class MultiLegRealizedExposureEnvelope:
    plan_id: str
    plan_fingerprint: str
    run_id: str
    paper_book_sha256: str
    legs: tuple[RealizedLegExposure, ...]
    confirmed_matched_stake: Decimal
    unresolved_unknown_stake_upper_bound: Decimal
    worst_case_execution_exposure: Decimal
    sequencing_exposure: bool
    recovery_decision: RecoveryDecision
    run_completed: bool
    all_actions_accepted: bool
    run_materialized_unconstrained_worst_case_pnl: Decimal
    portfolio_materialized_unconstrained_worst_case_pnl: Decimal
    complete_run_worst_case_pnl: Decimal | None
    projection_sha256: str
    economic_scope_authoritative: bool = False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def real_money_authorized(self) -> bool:
        return False

    def as_evidence(
        self,
        *,
        include_digest: bool = True,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": _SCHEMA,
            "schema_version": 2,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "run_id": self.run_id,
            "paper_book_sha256": self.paper_book_sha256,
            "legs": [leg.as_evidence() for leg in self.legs],
            "confirmed_matched_stake": _text_decimal(
                self.confirmed_matched_stake
            ),
            "unresolved_unknown_stake_upper_bound": _text_decimal(
                self.unresolved_unknown_stake_upper_bound
            ),
            "worst_case_execution_exposure": _text_decimal(
                self.worst_case_execution_exposure
            ),
            "sequencing_exposure": self.sequencing_exposure,
            "recovery_decision": self.recovery_decision.value,
            "run_completed": self.run_completed,
            "all_actions_accepted": self.all_actions_accepted,
            "run_materialized_unconstrained_worst_case_pnl": _text_decimal(
                self.run_materialized_unconstrained_worst_case_pnl
            ),
            "portfolio_materialized_unconstrained_worst_case_pnl": _text_decimal(
                self.portfolio_materialized_unconstrained_worst_case_pnl
            ),
            "complete_run_worst_case_pnl": (
                None
                if self.complete_run_worst_case_pnl is None
                else _text_decimal(self.complete_run_worst_case_pnl)
            ),
            "economic_scope_authoritative": self.economic_scope_authoritative,
            "execution_authorized": False,
            "provider_write_authorized": False,
            "real_money_authorized": False,
        }
        if include_digest:
            payload["projection_sha256"] = self.projection_sha256
        return payload


def project_multileg_realized_exposure(
    *,
    ledger: PaperExecutionLedger,
    plan: ExecutionPlan,
    config: PaperExecutionModelConfig,
    run_id: str,
    paper_book_path: str | Path,
) -> MultiLegRealizedExposureEnvelope:
    """Read #623/#646 truth; never create execution, fill, retry, or money authority."""
    if not isinstance(ledger, PaperExecutionLedger):
        raise TypeError("ledger must be PaperExecutionLedger")
    if not isinstance(plan, ExecutionPlan):
        raise TypeError("plan must be ExecutionPlan")
    if not isinstance(config, PaperExecutionModelConfig):
        raise TypeError("config must be PaperExecutionModelConfig")
    if (
        type(run_id) is not str
        or not run_id
        or run_id.strip() != run_id
    ):
        raise ValueError("run_id must be non-empty canonical text")

    try:
        before_events = tuple(ledger.events(run_id))
        before_token = _event_token(before_events)
        scope_by_action = _scope_bindings_from_events(before_events, plan)
        scope_authoritative = bool(plan.actions) and all(
            scope_by_action[action.action_id].authoritative
            for action in plan.actions
        )
        trigger_id, started_at, evidence = _reservation(before_events)
        run = ledger.load_run(
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=evidence,
        )
    except (
        PaperExecutionIntegrityError,
        PaperExecutionStateError,
        ValueError,
    ) as exc:
        raise MultiLegExposureProjectionError(
            "canonical execution ledger cannot support realized-exposure projection"
        ) from exc
    if run is None:
        raise MultiLegExposureProjectionError(
            "canonical execution run does not exist"
        )
    if (
        run.plan_id != plan.plan_id
        or run.plan_fingerprint != plan.fingerprint
    ):
        raise MultiLegExposureProjectionError(
            "execution run does not match exact plan"
        )
    if any(action.side != "BACK" for action in plan.actions):
        raise MultiLegExposureProjectionError(
            "projection supports BACK PAPER authority only"
        )

    path = Path(paper_book_path)
    try:
        paper_bytes = path.read_bytes()
        book = PaperBook.load_bytes(paper_bytes)
    except (OSError, ValueError) as exc:
        raise MultiLegExposureProjectionError(
            "cannot load durable PaperBook"
        ) from exc
    book_sha256 = hashlib.sha256(paper_bytes).hexdigest()
    tickets = tuple(book.tickets.values())

    attempts = {
        attempt.action_id: attempt for attempt in run.attempts
    }
    if len(attempts) != len(run.attempts):
        raise MultiLegExposureProjectionError(
            "duplicate action attempts in durable run"
        )

    legs: list[RealizedLegExposure] = []
    run_tickets: list[PaperTicket] = []
    confirmed = Decimal("0")
    unresolved = Decimal("0")
    for sequence, action in enumerate(plan.actions):
        scope = scope_by_action[action.action_id]
        attempt = attempts.get(action.action_id)
        if attempt is None:
            legs.append(
                RealizedLegExposure(
                    sequence=sequence,
                    action_id=action.action_id,
                    bookmaker_id=action.bookmaker_id,
                    account_id=action.account_id,
                    event_id=action.event_id,
                    market_id=action.market_id,
                    selection_id=action.selection_id,
                    side=action.side,
                    requested_odds=action.requested_odds,
                    requested_stake=action.requested_stake,
                    state=RealizedLegState.UNATTEMPTED,
                    attempt_id=None,
                    execution_odds=None,
                    realized_matched_stake=Decimal("0"),
                    unresolved_stake_upper_bound=Decimal("0"),
                    paper_ticket_id=None,
                    sport=scope.sport,
                    bankroll_id=scope.bankroll_id,
                    currency=scope.currency,
                    economic_scope_authoritative=scope.authoritative,
                )
            )
            continue

        _assert_attempt_action(attempt, action, sequence)
        if attempt.plan_id != plan.plan_id or attempt.run_id != run_id:
            raise MultiLegExposureProjectionError(
                "attempt belongs to another plan/run"
            )
        state = RealizedLegState(attempt.outcome.value)
        realized = Decimal("0")
        unknown = Decimal("0")
        ticket_id: str | None = None
        if attempt.outcome in {
            PaperAttemptOutcome.ACCEPTED,
            PaperAttemptOutcome.PARTIAL,
        }:
            if (
                attempt.execution_odds is None
                or attempt.execution_stake is None
            ):
                raise MultiLegExposureProjectionError(
                    "accepted/partial attempt lacks execution odds/stake"
                )
            ticket = _ticket_for_attempt(
                tickets,
                attempt,
                action,
                run_id,
                plan.decision_id,
                scope,
            )
            run_tickets.append(ticket)
            ticket_id = ticket.ticket_id
            realized = attempt.execution_stake
            confirmed += realized
        elif attempt.outcome is PaperAttemptOutcome.UNKNOWN:
            unknown = attempt.requested_stake
            unresolved += unknown
            if any(
                _marker(ticket.strategy_reason) == attempt.attempt_id
                for ticket in tickets
            ):
                raise MultiLegExposureProjectionError(
                    "UNKNOWN execution attempt must not materialize a PaperTicket"
                )
        elif attempt.outcome is PaperAttemptOutcome.REJECTED:
            if any(
                _marker(ticket.strategy_reason) == attempt.attempt_id
                for ticket in tickets
            ):
                raise MultiLegExposureProjectionError(
                    "REJECTED execution attempt must not materialize a PaperTicket"
                )

        legs.append(
            RealizedLegExposure(
                sequence=sequence,
                action_id=action.action_id,
                bookmaker_id=action.bookmaker_id,
                account_id=action.account_id,
                event_id=action.event_id,
                market_id=action.market_id,
                selection_id=action.selection_id,
                side=action.side,
                requested_odds=action.requested_odds,
                requested_stake=action.requested_stake,
                state=state,
                attempt_id=attempt.attempt_id,
                execution_odds=attempt.execution_odds,
                realized_matched_stake=realized,
                unresolved_stake_upper_bound=unknown,
                paper_ticket_id=ticket_id,
                sport=scope.sport,
                bankroll_id=scope.bankroll_id,
                currency=scope.currency,
                economic_scope_authoritative=scope.authoritative,
            )
        )

    exposure = confirmed + unresolved
    if run.worst_case_exposure != exposure:
        raise MultiLegExposureProjectionError(
            "run worst-case exposure disagrees with durable per-leg evidence"
        )
    sequencing = (not run.all_actions_accepted) and exposure > 0
    if (
        sequencing
        and run.recovery_decision
        is not RecoveryDecision.HEDGE_REVIEW_REQUIRED
    ):
        raise MultiLegExposureProjectionError(
            "recovery decision understates realized sequencing exposure"
        )

    run_pnl = PortfolioEngine.scenario_profit(run_tickets, set())
    portfolio_pnl = PortfolioEngine.scenario_profit(
        list(book.tickets.values()),
        set(),
    )
    complete_pnl = (
        run_pnl if run.completed and unresolved == 0 else None
    )

    try:
        after_token = _event_token(tuple(ledger.events(run_id)))
        paper_bytes_after = path.read_bytes()
    except (
        OSError,
        PaperExecutionIntegrityError,
        ValueError,
    ) as exc:
        raise MultiLegExposureProjectionError(
            "canonical evidence became unreadable during projection"
        ) from exc
    if before_token != after_token:
        raise MultiLegExposureProjectionError(
            "execution run changed during projection"
        )
    if paper_bytes != paper_bytes_after:
        raise MultiLegExposureProjectionError(
            "PaperBook changed during projection"
        )

    result = MultiLegRealizedExposureEnvelope(
        plan_id=plan.plan_id,
        plan_fingerprint=plan.fingerprint,
        run_id=run_id,
        paper_book_sha256=book_sha256,
        legs=tuple(legs),
        confirmed_matched_stake=confirmed,
        unresolved_unknown_stake_upper_bound=unresolved,
        worst_case_execution_exposure=exposure,
        sequencing_exposure=sequencing,
        recovery_decision=run.recovery_decision,
        run_completed=run.completed,
        all_actions_accepted=run.all_actions_accepted,
        run_materialized_unconstrained_worst_case_pnl=run_pnl,
        portfolio_materialized_unconstrained_worst_case_pnl=portfolio_pnl,
        complete_run_worst_case_pnl=complete_pnl,
        projection_sha256="",
        economic_scope_authoritative=scope_authoritative,
    )
    return replace(
        result,
        projection_sha256=_digest(
            result.as_evidence(include_digest=False)
        ),
    )
