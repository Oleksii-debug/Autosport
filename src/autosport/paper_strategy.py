from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .agents import AgentContext
from .decision_ledger import DecisionRecord, bind_economic_goal
from .domain import MarketEvent, PaperTicket, TicketLeg
from .forecasting import ForecastRecord, parse_iso_timestamp
from .price_truth import paper_quote_rejection_reason
from .probability import paper_value
from .risk import PaperRiskPolicy, ProposedTicketRiskContext


@dataclass(frozen=True, slots=True)
class Forecast:
    """Legacy minimal forecast contract kept for backward compatibility."""

    quote_key: str
    probability: Decimal
    model_id: str
    as_of_ts: str


ForecastLike = Forecast | ForecastRecord


class PaperValueAgent:
    """Paper-only strategy agent. It can open virtual tickets but has no real-money execution capability."""

    name = "paper-value-strategy"

    def __init__(
        self,
        forecasts: dict[str, ForecastLike],
        stake: Decimal | str = "50",
        minimum_expected_profit_per_unit: Decimal | str = "0.05",
        risk_policy: PaperRiskPolicy | None = None,
    ) -> None:
        self.forecasts = forecasts
        self.stake = Decimal(str(stake))
        self.minimum_edge = Decimal(str(minimum_expected_profit_per_unit))
        self.risk_policy = risk_policy or PaperRiskPolicy()
        self._acted: set[str] = set()

    @staticmethod
    def _rollback_uncommitted_ticket(
        context: AgentContext,
        ticket: PaperTicket,
        *,
        balance_before: Decimal,
        lifecycle_len_before: int,
    ) -> None:
        """Undo exactly one just-opened ticket when durable decision persistence fails.

        The strategy is synchronous, so the rollback is intentionally strict: it only
        accepts the exact ticket object and the exact final lifecycle witness created
        by the immediately preceding ``open_ticket`` call. Any unexpected mutation
        fails loudly instead of guessing at bankroll state.
        """

        book = context.paper_book
        if book.tickets.get(ticket.ticket_id) is not ticket:
            raise RuntimeError("paper decision rollback cannot prove ticket identity")
        expected_lifecycle = ("open", ticket.ticket_id, (), ())
        if (
            len(book._lifecycle) != lifecycle_len_before + 1
            or book._lifecycle[-1] != expected_lifecycle
        ):
            raise RuntimeError("paper decision rollback cannot prove lifecycle boundary")
        del book.tickets[ticket.ticket_id]
        book.balance = balance_before
        del book._lifecycle[lifecycle_len_before:]

    @staticmethod
    def _decision_is_durable(
        context: AgentContext,
        record: DecisionRecord,
        goal,
    ) -> bool:
        """Resolve an uncertain append outcome from exact verified durable evidence."""

        ledger = context.decision_ledger
        if ledger is None:
            return False
        try:
            if goal is None:
                return any(persisted == record for persisted in ledger.verified_records())
            persisted = ledger.verified_economic_decision(record.decision_id, goal)
            return persisted == bind_economic_goal(record, goal)
        except Exception:
            # Missing, unreadable, structurally invalid, or merely ID-colliding evidence
            # is never treated as a successful economic commit. The caller rolls back
            # the paper side unless the exact material decision is restart-verifiable.
            return False

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None:
        if event.quote_key in self._acted or event.status != "open":
            return
        # Provider truth is binding. Observational prices, positive-delay exchange
        # quotes, incomplete ladder caches, or quotes whose observed capacity is below
        # the configured paper stake must fail closed before virtual economics.
        if paper_quote_rejection_reason(event, self.stake) is not None:
            return
        forecast = self.forecasts.get(event.quote_key)
        if forecast is None:
            return
        if parse_iso_timestamp(forecast.as_of_ts) > parse_iso_timestamp(event.observed_ts):
            return
        estimate = paper_value(event.quote_key, forecast.probability, event.decimal_odds)
        if estimate.expected_profit_per_unit < self.minimum_edge:
            return

        leg = TicketLeg(event.event_id, event.market_id, event.selection_id, event.decimal_odds)
        goal = self.risk_policy.economic_goal
        # A material EconomicGoal-bound decision is not allowed to exist only in
        # mutable PaperBook state. Without the canonical Decision Ledger there is no
        # restart-verifiable goal/policy evidence, so fail closed before mutation.
        if goal is not None and context.decision_ledger is None:
            return
        proposal_context = None
        if goal is not None:
            proposal_context = ProposedTicketRiskContext(
                legs=(leg,),
                quotes=(event,),
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
                proposal_ts=event.observed_ts,
            )
        risk = self.risk_policy.evaluate(
            context.paper_book,
            self.stake,
            context=proposal_context,
        )
        if not risk.allowed:
            return

        balance_before = context.paper_book.balance
        lifecycle_len_before = len(context.paper_book._lifecycle)
        ticket = context.paper_book.open_ticket(
            [leg],
            self.stake,
            reason=f"paper forecast {forecast.model_id}; EV/unit={estimate.expected_profit_per_unit}",
            placed_at=event.observed_ts,
        )
        record: DecisionRecord | None = None
        try:
            if context.decision_ledger:
                payload = {
                    "ticket_id": ticket.ticket_id,
                    "quote_key": event.quote_key,
                    "forecast_model": forecast.model_id,
                    "probability": str(forecast.probability),
                    "expected_profit_per_unit": str(estimate.expected_profit_per_unit),
                    "stake": str(ticket.stake),
                }
                if isinstance(forecast, ForecastRecord):
                    payload.update(
                        {
                            "forecast_id": forecast.forecast_id,
                            "forecast_hash": forecast.canonical_hash,
                            "model_version": forecast.model_version,
                            "strategy_version": forecast.strategy_version,
                            "model_training_cutoff_ts": forecast.model_training_cutoff_ts,
                            "input_cutoff_ts": forecast.input_cutoff_ts,
                            "generated_at": forecast.generated_at,
                            "uncertainty": str(forecast.uncertainty),
                            "evidence_hashes": list(forecast.evidence_hashes),
                            "market_snapshot_hash": forecast.market_snapshot_hash,
                        }
                    )
                record = DecisionRecord(
                    replay_run_id=context.replay_run_id,
                    agent=self.name,
                    observed_ts=event.observed_ts,
                    action="OPEN_PAPER_VALUE_TICKET",
                    payload=payload,
                    context_hash=context.market_context_hash(),
                )
                if goal is None:
                    context.decision_ledger.append(record)
                else:
                    context.decision_ledger.append_economic(record, goal)
        except Exception:
            # An append can fail after bytes were actually written (for example an
            # uncertain fsync outcome). Resolve that uncertainty from the ledger's own
            # integrity/readback contract before deciding whether the paper mutation
            # belongs to the committed side or must be rolled back.
            if record is not None and self._decision_is_durable(context, record, goal):
                self._acted.add(event.quote_key)
                return
            self._rollback_uncommitted_ticket(
                context,
                ticket,
                balance_before=balance_before,
                lifecycle_len_before=lifecycle_len_before,
            )
            raise

        # Suppress repeat action only after both the paper mutation and its durable
        # decision evidence have crossed the same successful boundary.
        self._acted.add(event.quote_key)
