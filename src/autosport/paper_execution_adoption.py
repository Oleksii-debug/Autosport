from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

from .domain import PaperTicket, TicketLeg
from .opportunity import QuoteRef
from .paper import PaperBook
from .paper_execution_reality import (
    ObservedPaperExecution,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRegistry,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    PaperExecutionRun,
    execute_paper_plan,
)
from .portfolio_plan import (
    OpportunityIntent,
    PortfolioPlan,
    _portfolio_intent_evidence_json,
)
from .real_execution_ledger import ExecutionAction, ExecutionPlan


class PaperExecutionAdoptionError(RuntimeError):
    """Raised when a durable execution attempt cannot be adopted without ambiguity."""


@dataclass(frozen=True, slots=True)
class PaperExposureBinding:
    action_id: str
    sport: str | None
    bankroll_id: str | None
    currency: str | None

    def __post_init__(self) -> None:
        if type(self.action_id) is not str or not self.action_id:
            raise ValueError("action_id must be non-empty text")


@dataclass(frozen=True, slots=True)
class PreparedPaperExecution:
    execution_plan: ExecutionPlan
    exposure_bindings: tuple[PaperExposureBinding, ...]
    intent_evidence_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution_plan, ExecutionPlan):
            raise TypeError("execution_plan must be ExecutionPlan")
        if (
            type(self.exposure_bindings) is not tuple
            or any(
                not isinstance(item, PaperExposureBinding)
                for item in self.exposure_bindings
            )
        ):
            raise TypeError(
                "exposure_bindings must be a tuple of PaperExposureBinding values"
            )
        action_ids = tuple(action.action_id for action in self.execution_plan.actions)
        binding_ids = tuple(item.action_id for item in self.exposure_bindings)
        if action_ids != binding_ids:
            raise ValueError("exposure bindings must exactly match execution action order")
        if type(self.intent_evidence_json) is not str or not self.intent_evidence_json:
            raise ValueError("intent_evidence_json must be non-empty canonical JSON")


@dataclass(frozen=True, slots=True)
class PaperExecutionAdoptionResult:
    run: PaperExecutionRun
    ticket_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.run, PaperExecutionRun):
            raise TypeError("run must be PaperExecutionRun")
        if type(self.ticket_ids) is not tuple or any(
            type(value) is not str or not value for value in self.ticket_ids
        ):
            raise TypeError("ticket_ids must contain non-empty strings")


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_timestamp(value: str, field_name: str) -> datetime:
    if type(value) is not str or not value or value.strip() != value:
        raise PaperExecutionAdoptionError(f"{field_name} must be canonical text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperExecutionAdoptionError(
            f"{field_name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperExecutionAdoptionError(
            f"{field_name} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class PaperExecutionAdoptionRuntime:
    """Bridge canonical PortfolioPlan decisions through #623 PAPER attempt truth.

    This class never performs provider writes.  It converts already-authorized,
    exact single-leg OpportunityIntent stakes into canonical ExecutionAction values,
    executes/resumes the #623 PAPER ledger, and materializes only ACCEPTED/PARTIAL
    attempt truth into PaperBook.  REJECTED/UNKNOWN attempts remain durable without
    fabricated exposure.

    A positive multi-leg *single intent* is deliberately rejected until the
    portfolio contract carries an exact per-leg stake vector.  Multiple independent
    single-leg intents still form one ordered multi-action execution plan, so #623
    preserves partial/second-leg/recovery semantics without inventing stake authority.
    """

    _TICKET_MARKER = "paper_execution_attempt_id="

    def __init__(
        self,
        *,
        book: PaperBook,
        ledger: PaperExecutionLedger,
        config: PaperExecutionModelConfig,
        max_quote_age: timedelta,
    ) -> None:
        if not isinstance(book, PaperBook):
            raise TypeError("book must be PaperBook")
        if not isinstance(ledger, PaperExecutionLedger):
            raise TypeError("ledger must be PaperExecutionLedger")
        if not isinstance(config, PaperExecutionModelConfig):
            raise TypeError("config must be PaperExecutionModelConfig")
        if not isinstance(max_quote_age, timedelta) or max_quote_age <= timedelta(0):
            raise ValueError("max_quote_age must be a positive timedelta")
        self.book = book
        self.ledger = ledger
        self.config = config
        self.max_quote_age = min(
            max_quote_age,
            timedelta(milliseconds=config.max_quote_age_ms),
        )
        if self.max_quote_age <= timedelta(0):
            raise ValueError("effective max_quote_age must be positive")

    def prepare(
        self,
        *,
        plan: PortfolioPlan,
        intents: tuple[OpportunityIntent, ...],
        decision_id: str,
    ) -> PreparedPaperExecution | None:
        if not isinstance(plan, PortfolioPlan):
            raise TypeError("plan must be PortfolioPlan")
        if type(intents) is not tuple or any(
            not isinstance(intent, OpportunityIntent) for intent in intents
        ):
            raise TypeError("intents must be a tuple of OpportunityIntent values")
        if type(decision_id) is not str or not decision_id:
            raise ValueError("decision_id must be non-empty text")
        if tuple(intent.intent_id for intent in intents) != plan.intent_ids:
            raise PaperExecutionAdoptionError(
                "execution intents do not match PortfolioPlan intent ids"
            )
        if tuple(intent.intent_sha256 for intent in intents) != plan.intent_sha256s:
            raise PaperExecutionAdoptionError(
                "execution intents do not match PortfolioPlan intent hashes"
            )
        if len(plan.stakes) != len(intents):
            raise PaperExecutionAdoptionError(
                "PortfolioPlan stake vector does not match intent vector"
            )

        intent_evidence_json = _portfolio_intent_evidence_json(plan, intents)
        actions: list[ExecutionAction] = []
        bindings: list[PaperExposureBinding] = []

        for index, (intent, stake) in enumerate(zip(intents, plan.stakes, strict=True)):
            if not isinstance(stake, Decimal) or not stake.is_finite() or stake < 0:
                raise PaperExecutionAdoptionError(
                    "PortfolioPlan execution stake must be a non-negative exact Decimal"
                )
            if stake == 0:
                continue

            context = intent.risk_context
            if len(context.legs) != 1 or len(context.quotes) != 1:
                raise PaperExecutionAdoptionError(
                    "positive multi-leg intent lacks exact per-leg stake authority"
                )
            leg = context.legs[0]
            event = context.quotes[0]
            opportunity_quotes = {
                quote.quote_key: quote for quote in intent.opportunity.quotes
            }
            quote_ref = opportunity_quotes.get(event.quote_key)
            if quote_ref is None:
                raise PaperExecutionAdoptionError(
                    "risk quote is not bound to canonical Opportunity quote evidence"
                )
            exact_ref = QuoteRef.from_market_event(
                event,
                market_snapshot_hash=quote_ref.market_snapshot_hash,
            )
            if exact_ref != quote_ref:
                raise PaperExecutionAdoptionError(
                    "risk quote bytes do not match canonical Opportunity quote reference"
                )
            if (
                leg.event_id != event.event_id
                or leg.market_id != event.market_id
                or leg.selection_id != event.selection_id
                or leg.sport != event.sport
            ):
                raise PaperExecutionAdoptionError(
                    "ticket leg identity does not match canonical execution quote"
                )

            account_by_source = dict(context.provider_accounts)
            if set(account_by_source) != {event.source_id}:
                raise PaperExecutionAdoptionError(
                    "positive PAPER execution requires one exact account for its provider"
                )
            account_id = account_by_source[event.source_id]

            quote_time = _utc_timestamp(
                event.source_ts or event.observed_ts,
                "execution quote observed time",
            )
            expires_at = _timestamp_text(quote_time + self.max_quote_age)
            action_id = "paper-action-v1-" + _digest(
                {
                    "decision_id": decision_id,
                    "intent_id": intent.intent_id,
                    "intent_sha256": intent.intent_sha256,
                    "quote_market_event_hash": quote_ref.market_event_hash,
                    "stake": str(stake),
                    "index": index,
                }
            )
            actions.append(
                ExecutionAction(
                    action_id=action_id,
                    bookmaker_id=event.source_id,
                    account_id=account_id,
                    event_id=event.event_id,
                    market_id=event.market_id,
                    selection_id=event.selection_id,
                    # PaperBook currently represents positive-selection BACK exposure
                    # only; no lay-side authority exists in this contract.
                    side="BACK",
                    requested_odds=event.decimal_odds,
                    requested_stake=stake,
                    quote_id=quote_ref.market_event_hash,
                    quote_observed_at=_timestamp_text(quote_time),
                    expires_at=expires_at,
                )
            )
            bindings.append(
                PaperExposureBinding(
                    action_id=action_id,
                    sport=leg.sport,
                    bankroll_id=context.bankroll_id,
                    currency=context.currency,
                )
            )

        if not actions:
            return None

        execution_plan = ExecutionPlan(
            plan_id="paper-plan-v1-" + _digest(
                {
                    "decision_id": decision_id,
                    "portfolio_plan_sha256": plan.plan_sha256,
                    "intent_evidence_json": intent_evidence_json,
                    "model_fingerprint": self.config.fingerprint,
                    "action_ids": [action.action_id for action in actions],
                }
            ),
            bookmaker_profile_version=(
                f"paper-execution-reality:{self.config.model_id}:"
                f"{self.config.model_version}"
            ),
            decision_id=decision_id,
            approval_id="paper-only-no-real-money",
            created_at=plan.decision_ts,
            actions=tuple(actions),
        )
        return PreparedPaperExecution(
            execution_plan=execution_plan,
            exposure_bindings=tuple(bindings),
            intent_evidence_json=intent_evidence_json,
        )

    def execute(
        self,
        *,
        prepared: PreparedPaperExecution,
        trigger_id: str,
        started_at: str,
        materialize_exposure: bool,
        observations: Mapping[str, ObservedPaperExecution] | None = None,
        evidence_registry: PaperExecutionEvidenceRegistry | None = None,
        suspended_action_ids: frozenset[str] = frozenset(),
    ) -> PaperExecutionAdoptionResult:
        if not isinstance(prepared, PreparedPaperExecution):
            raise TypeError("prepared must be PreparedPaperExecution")
        if type(materialize_exposure) is not bool:
            raise TypeError("materialize_exposure must be bool")
        run = execute_paper_plan(
            plan=prepared.execution_plan,
            trigger_id=trigger_id,
            config=self.config,
            ledger=self.ledger,
            started_at=started_at,
            observations=observations,
            evidence_registry=evidence_registry,
            suspended_action_ids=suspended_action_ids,
        )
        if not materialize_exposure:
            return PaperExecutionAdoptionResult(run=run, ticket_ids=())

        action_by_id = {
            action.action_id: action for action in prepared.execution_plan.actions
        }
        binding_by_id = {
            binding.action_id: binding for binding in prepared.exposure_bindings
        }
        ticket_ids: list[str] = []
        for attempt in run.attempts:
            if attempt.outcome not in {
                PaperAttemptOutcome.ACCEPTED,
                PaperAttemptOutcome.PARTIAL,
            }:
                continue
            action = action_by_id[attempt.action_id]
            binding = binding_by_id[attempt.action_id]
            ticket = self._materialize_attempt(
                attempt=attempt,
                action=action,
                binding=binding,
                decision_id=prepared.execution_plan.decision_id,
            )
            ticket_ids.append(ticket.ticket_id)
        return PaperExecutionAdoptionResult(run=run, ticket_ids=tuple(ticket_ids))

    def _materialize_attempt(
        self,
        *,
        attempt,
        action: ExecutionAction,
        binding: PaperExposureBinding,
        decision_id: str,
    ) -> PaperTicket:
        if attempt.execution_odds is None or attempt.execution_stake is None:
            raise PaperExecutionAdoptionError(
                "accepted-equivalent attempt lacks execution odds/stake"
            )
        marker = f"{self._TICKET_MARKER}{attempt.attempt_id}"
        matches = [
            ticket
            for ticket in self.book.tickets.values()
            if marker in ticket.strategy_reason
        ]
        if len(matches) > 1:
            raise PaperExecutionAdoptionError(
                "PaperBook contains duplicate exposure for one execution attempt"
            )
        if matches:
            ticket = matches[0]
            if not self._ticket_matches_attempt(
                ticket=ticket,
                attempt=attempt,
                action=action,
                binding=binding,
            ):
                raise PaperExecutionAdoptionError(
                    "existing PaperBook exposure conflicts with durable execution attempt"
                )
            return ticket

        ticket = self.book.open_ticket(
            [
                TicketLeg(
                    event_id=attempt.event_id,
                    market_id=attempt.market_id,
                    selection_id=attempt.selection_id,
                    locked_odds=attempt.execution_odds,
                    sport=binding.sport,
                )
            ],
            attempt.execution_stake,
            reason=(
                f"paper execution adoption; decision_id={decision_id}; "
                f"run_id={attempt.run_id}; {marker}"
            ),
            placed_at=attempt.execution_observed_at,
            provider_source_ids=(attempt.bookmaker_id,),
            provider_accounts=((attempt.bookmaker_id, attempt.account_id),),
            bankroll_id=binding.bankroll_id,
            currency=binding.currency,
        )
        return ticket

    @staticmethod
    def _ticket_matches_attempt(
        *,
        ticket: PaperTicket,
        attempt,
        action: ExecutionAction,
        binding: PaperExposureBinding,
    ) -> bool:
        if (
            ticket.stake != attempt.execution_stake
            or ticket.placed_at != attempt.execution_observed_at
            or len(ticket.legs) != 1
            or ticket.provider_source_ids != (attempt.bookmaker_id,)
            or ticket.provider_accounts
            != ((attempt.bookmaker_id, attempt.account_id),)
            or ticket.bankroll_id != binding.bankroll_id
            or ticket.currency != binding.currency
            or attempt.decision_quote_id != action.quote_id
            or attempt.decision_odds != action.requested_odds
            or attempt.requested_stake != action.requested_stake
        ):
            return False
        leg = ticket.legs[0]
        return (
            leg.event_id == attempt.event_id
            and leg.market_id == attempt.market_id
            and leg.selection_id == attempt.selection_id
            and leg.locked_odds == attempt.execution_odds
            and leg.sport == binding.sport
        )
