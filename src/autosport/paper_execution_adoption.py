from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from . import _paper_execution_reality_legacy as _paper_impl
from .domain import MarketEvent, PaperTicket, TicketLeg
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
    market_semantics_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.action_id) is not str or not self.action_id:
            raise ValueError("action_id must be non-empty text")
        if self.market_semantics_id is not None:
            identity = self.market_semantics_id
            if (
                type(identity) is not str
                or not identity
                or identity.strip() != identity
                or identity != identity.lower()
                or any(
                    character
                    not in "abcdefghijklmnopqrstuvwxyz0123456789._:/-"
                    for character in identity
                )
                or identity in {"unknown", "mixed", "unspecified"}
            ):
                raise ValueError(
                    "market_semantics_id must be a non-reserved lowercase "
                    "canonical semantic identity"
                )


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

    This class never performs provider writes. It converts already-authorized,
    exact single-leg OpportunityIntent stakes into canonical ExecutionAction values,
    executes/resumes the #623 PAPER ledger, and materializes only ACCEPTED/PARTIAL
    attempt truth into PaperBook. REJECTED/UNKNOWN attempts remain durable without
    fabricated exposure.

    A positive multi-leg *single intent* is deliberately rejected until the
    portfolio contract carries an exact per-leg stake vector. Multiple independent
    single-leg intents still form one ordered multi-action execution plan, so #623
    preserves partial/second-leg/recovery semantics without inventing stake authority.

    Prepared execution values are audit data, not authority. A prepared value must
    be minted by this exact runtime instance from canonical inputs before any ledger
    execution or PaperBook materialization is allowed. This prevents callers from
    constructing a syntactically valid PreparedPaperExecution that enlarges stake,
    changes selection/account, or otherwise bypasses upstream portfolio/risk truth.
    """

    _TICKET_MARKER = "paper_execution_attempt_id="

    def __init__(
        self,
        *,
        book: PaperBook,
        ledger: PaperExecutionLedger,
        config: PaperExecutionModelConfig,
        max_quote_age: timedelta,
        paper_book_path: str | Path,
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
        # In-process capability registry. Object identity is intentional: serialized,
        # copied, reconstructed, or caller-authored PreparedPaperExecution values do
        # not carry execution authority. Restart re-mints from canonical inputs.
        self._prepared_authorities: dict[int, PreparedPaperExecution] = {}
        self.paper_book_path = Path(paper_book_path)
        if self.paper_book_path.exists():
            durable_book = PaperBook.load(self.paper_book_path)
            self._assert_same_book_state(
                durable_book,
                self.book,
                "configured PaperBook does not match durable snapshot",
            )
        else:
            self.book.save(self.paper_book_path)
            durable_book = PaperBook.load(self.paper_book_path)
            self._assert_same_book_state(
                durable_book,
                self.book,
                "initial PaperBook durability verification failed",
            )
        self.max_quote_age = min(
            max_quote_age,
            timedelta(milliseconds=config.max_quote_age_ms),
        )
        if self.max_quote_age <= timedelta(0):
            raise ValueError("effective max_quote_age must be positive")

    def _mint_prepared(self, prepared: PreparedPaperExecution) -> PreparedPaperExecution:
        if not isinstance(prepared, PreparedPaperExecution):
            raise TypeError("prepared must be PreparedPaperExecution")
        self._prepared_authorities[id(prepared)] = prepared
        return prepared

    def _require_minted(self, prepared: PreparedPaperExecution) -> None:
        if self._prepared_authorities.get(id(prepared)) is not prepared:
            raise PaperExecutionAdoptionError(
                "prepared execution was not minted by this runtime from canonical authority"
            )

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
                    market_semantics_id=leg.market_semantics_id,
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
        return self._mint_prepared(
            PreparedPaperExecution(
                execution_plan=execution_plan,
                exposure_bindings=tuple(bindings),
                intent_evidence_json=intent_evidence_json,
            )
        )

    def prepare_paper_value_action(
        self,
        *,
        event: MarketEvent,
        stake: Decimal,
        decision_id: str,
        account_id: str,
        bankroll_id: str | None,
        currency: str | None,
    ) -> PreparedPaperExecution:
        """Bind one legacy paper-value decision to canonical #623 execution truth.

        The legacy strategy may still decide that a value opportunity exists, but
        it no longer owns fill semantics. This bridge carries its already-risk-
        authorized single-leg stake into the same immutable execution plan/run
        authority used by the persistent live loop.
        """
        if not isinstance(event, MarketEvent):
            raise TypeError("event must be MarketEvent")
        if not isinstance(stake, Decimal) or not stake.is_finite() or stake <= 0:
            raise ValueError("stake must be a positive finite exact Decimal")
        if type(decision_id) is not str or not decision_id or decision_id.strip() != decision_id:
            raise ValueError("decision_id must be non-empty canonical text")
        if type(account_id) is not str or not account_id or account_id.strip() != account_id:
            raise ValueError("account_id must be non-empty canonical text")
        if (bankroll_id is None) != (currency is None):
            raise ValueError("bankroll_id and currency must be supplied together")

        quote_time = _utc_timestamp(
            event.source_ts or event.observed_ts,
            "execution quote observed time",
        )
        quote_id = "paper-value-quote-v1-" + _digest(
            {
                "schema": "autosport.paper_value.execution_quote",
                "schema_version": 1,
                "event": event.to_dict(),
            }
        )
        evidence_payload = {
            "schema": "autosport.paper_value.execution_authority",
            "schema_version": 1,
            "decision_id": decision_id,
            "quote_id": quote_id,
            "event": event.to_dict(),
            "stake": str(stake),
            "provider_account": [event.source_id, account_id],
            "bankroll_id": bankroll_id,
            "currency": currency,
        }
        evidence_json = json.dumps(
            evidence_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        action = ExecutionAction(
            action_id="paper-value-action-v1-" + _digest(evidence_payload),
            bookmaker_id=event.source_id,
            account_id=account_id,
            event_id=event.event_id,
            market_id=event.market_id,
            selection_id=event.selection_id,
            side="BACK",
            requested_odds=event.decimal_odds,
            requested_stake=stake,
            quote_id=quote_id,
            quote_observed_at=_timestamp_text(quote_time),
            expires_at=_timestamp_text(quote_time + self.max_quote_age),
        )
        execution_plan = ExecutionPlan(
            plan_id="paper-value-plan-v1-" + _digest(
                {
                    "decision_id": decision_id,
                    "action": action.to_dict(),
                    "model_fingerprint": self.config.fingerprint,
                }
            ),
            bookmaker_profile_version=(
                f"paper-execution-reality:{self.config.model_id}:"
                f"{self.config.model_version}"
            ),
            decision_id=decision_id,
            approval_id="paper-only-no-real-money",
            created_at=event.observed_ts,
            actions=(action,),
        )
        return self._mint_prepared(
            PreparedPaperExecution(
                execution_plan=execution_plan,
                exposure_bindings=(
                    PaperExposureBinding(
                        action_id=action.action_id,
                        sport=event.sport,
                        bankroll_id=bankroll_id,
                        market_semantics_id=event.market_semantics_id,
                        currency=currency,
                    ),
                ),
                intent_evidence_json=evidence_json,
            )
        )

    def expected_run_id(
        self,
        prepared: PreparedPaperExecution,
        trigger_id: str,
    ) -> str:
        if not isinstance(prepared, PreparedPaperExecution):
            raise TypeError("prepared must be PreparedPaperExecution")
        self._require_minted(prepared)
        return _paper_impl._run_id(
            prepared.execution_plan,
            trigger_id,
            self.config,
        )

    def assert_recoverable_book_state(
        self,
        *,
        pre_action_book: PaperBook,
        prepared: PreparedPaperExecution,
        trigger_id: str,
        started_at: str,
        materialize_exposure: bool,
    ) -> None:
        """Reject restart state not explained by the exact durable #623 run."""
        if not isinstance(pre_action_book, PaperBook):
            raise TypeError("pre_action_book must be PaperBook")
        if not isinstance(prepared, PreparedPaperExecution):
            raise TypeError("prepared must be PreparedPaperExecution")
        self._require_minted(prepared)
        if type(materialize_exposure) is not bool:
            raise TypeError("materialize_exposure must be bool")

        if self._same_book_state(self.book, pre_action_book):
            return
        if not materialize_exposure:
            raise PaperExecutionAdoptionError(
                "SHADOW recovery PaperBook differs from exact pre-action state"
            )

        run_id = self.expected_run_id(prepared, trigger_id)
        run = self.ledger.load_run(
            run_id=run_id,
            trigger_id=trigger_id,
            plan=prepared.execution_plan,
            config=self.config,
            started_at=started_at,
            observation_evidence_ids={},
        )
        if run is None:
            raise PaperExecutionAdoptionError(
                "PaperBook changed before any durable #623 run evidence"
            )

        expected = copy.deepcopy(pre_action_book)
        action_by_id = {
            action.action_id: action for action in prepared.execution_plan.actions
        }
        binding_by_id = {
            binding.action_id: binding for binding in prepared.exposure_bindings
        }
        for attempt in run.attempts:
            if attempt.outcome not in {
                PaperAttemptOutcome.ACCEPTED,
                PaperAttemptOutcome.PARTIAL,
            }:
                continue
            action = action_by_id.get(attempt.action_id)
            binding = binding_by_id.get(attempt.action_id)
            if action is None or binding is None:
                raise PaperExecutionAdoptionError(
                    "durable attempt is not bound to prepared execution action"
                )
            if attempt.execution_odds is None or attempt.execution_stake is None:
                raise PaperExecutionAdoptionError(
                    "accepted-equivalent durable attempt lacks execution truth"
                )
            if action.side != "BACK" or attempt.side != action.side:
                raise PaperExecutionAdoptionError(
                    "accepted-equivalent PAPER adoption requires exact BACK execution side"
                )
            expected.open_ticket(
                [
                    TicketLeg(
                        event_id=attempt.event_id,
                        market_id=attempt.market_id,
                        selection_id=attempt.selection_id,
                        locked_odds=attempt.execution_odds,
                        sport=binding.sport,
                        exchange_side="back",
                        market_semantics_id=binding.market_semantics_id,
                    )
                ],
                attempt.execution_stake,
                reason=(
                    f"paper execution adoption; "
                    f"decision_id={prepared.execution_plan.decision_id}; "
                    f"run_id={attempt.run_id}; "
                    f"{self._TICKET_MARKER}{attempt.attempt_id}"
                ),
                placed_at=attempt.execution_observed_at,
                provider_source_ids=(attempt.bookmaker_id,),
                provider_accounts=((attempt.bookmaker_id, attempt.account_id),),
                bankroll_id=binding.bankroll_id,
                currency=binding.currency,
            )

        if not self._same_book_state(self.book, expected):
            raise PaperExecutionAdoptionError(
                "PaperBook restart state is not the exact pre-action or "
                "#623-authorized post-action state"
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
        self._require_minted(prepared)
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
        accepted_attempts = []
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
            accepted_attempts.append((attempt, action, binding))

        if accepted_attempts:
            # COMMITTED live progress is not permitted until the exposure is a
            # canonical PaperBook snapshot, not merely an in-memory mutation.
            self.book.save(self.paper_book_path)
            durable_book = PaperBook.load(self.paper_book_path)
            self._assert_same_book_state(
                durable_book,
                self.book,
                "PaperBook changed across atomic durable publication",
            )
            for attempt, action, binding in accepted_attempts:
                marker = f"{self._TICKET_MARKER}{attempt.attempt_id}"
                matches = [
                    ticket
                    for ticket in durable_book.tickets.values()
                    if marker in ticket.strategy_reason
                ]
                if len(matches) != 1 or not self._ticket_matches_attempt(
                    ticket=matches[0],
                    attempt=attempt,
                    action=action,
                    binding=binding,
                ):
                    raise PaperExecutionAdoptionError(
                        "durable PaperBook does not bind exact execution attempt"
                    )

        return PaperExecutionAdoptionResult(run=run, ticket_ids=tuple(ticket_ids))

    @staticmethod
    def _same_book_state(left: PaperBook, right: PaperBook) -> bool:
        return (
            left.initial_bankroll == right.initial_bankroll
            and left.balance == right.balance
            and left.tickets == right.tickets
            and left._lifecycle == right._lifecycle
            and left._settlement_times == right._settlement_times
        )

    @classmethod
    def _assert_same_book_state(
        cls,
        left: PaperBook,
        right: PaperBook,
        message: str,
    ) -> None:
        if not cls._same_book_state(left, right):
            raise PaperExecutionAdoptionError(message)

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
        if action.side != "BACK" or attempt.side != action.side:
            raise PaperExecutionAdoptionError(
                "accepted-equivalent PAPER adoption requires exact BACK execution side"
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
                    exchange_side="back",
                    market_semantics_id=binding.market_semantics_id,
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
            and action.side == "BACK"
            and attempt.side == action.side
            and leg.exchange_side == "back"
            and leg.market_semantics_id == binding.market_semantics_id
        )
