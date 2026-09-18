from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from .agents import AgentContext
from .decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionRecord,
    bind_economic_goal,
)
from .domain import MarketEvent, PaperTicket, TicketLeg
from .forecasting import ForecastRecord, parse_iso_timestamp
from .price_truth import paper_quote_rejection_reason
from .probability import paper_value
from .risk import PaperRiskPolicy, ProposedTicketRiskContext


_MATERIAL_ACTION_SCHEMA = "autosport.paper-value.open-ticket.v1"
_MATERIAL_ACTION_MARKER = "material_action_id="
_MATERIAL_ACTION_NAME = "OPEN_PAPER_VALUE_TICKET"


class PaperDecisionReconciliationRequired(RuntimeError):
    """Raised when PaperBook and Decision Ledger disagree about one material action."""


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
        # ``stake`` remains a compatibility input for legacy/no-goal paper runs.
        # Once an EconomicGoalContract is active it has no financial authority:
        # the strategy derives a bounded proposal from edge + current PaperBook
        # exposure, then PaperRiskPolicy remains the final executable gate.
        self.stake = Decimal(str(stake))
        self.minimum_edge = Decimal(str(minimum_expected_profit_per_unit))
        self.risk_policy = risk_policy or PaperRiskPolicy()
        self._acted: set[str] = set()

    @classmethod
    def _material_action_id(cls, context: AgentContext, event: MarketEvent) -> str:
        """Stable logical commit identity mirroring the agent's quote-level duplicate guard."""

        identity = {
            "schema": _MATERIAL_ACTION_SCHEMA,
            "replay_run_id": context.replay_run_id,
            "agent": cls.name,
            "action": _MATERIAL_ACTION_NAME,
            "quote_key": event.quote_key,
        }
        canonical = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _strategy_reason(forecast: ForecastLike, expected_profit: Decimal, material_action_id: str) -> str:
        return (
            f"paper forecast {forecast.model_id}; EV/unit={expected_profit}; "
            f"{_MATERIAL_ACTION_MARKER}{material_action_id}"
        )

    @staticmethod
    def _material_action_ticket(context: AgentContext, material_action_id: str) -> PaperTicket | None:
        marker = f"{_MATERIAL_ACTION_MARKER}{material_action_id}"
        matches = [
            ticket
            for ticket in context.paper_book.tickets.values()
            if ticket.strategy_reason.endswith(f"; {marker}")
        ]
        if len(matches) > 1:
            raise PaperDecisionReconciliationRequired(
                "PaperBook contains duplicate tickets for one material_action_id"
            )
        return matches[0] if matches else None

    @staticmethod
    def _ticket_matches_event(ticket: PaperTicket, event: MarketEvent) -> bool:
        if (
            not isinstance(ticket.stake, Decimal)
            or not ticket.stake.is_finite()
            or ticket.stake <= 0
            or ticket.placed_at != event.observed_ts
            or len(ticket.legs) != 1
        ):
            return False
        leg = ticket.legs[0]
        return (
            leg.event_id == event.event_id
            and leg.market_id == event.market_id
            and leg.selection_id == event.selection_id
            and leg.locked_odds == event.decimal_odds
        )

    def _derive_goal_stake(
        self,
        context: AgentContext,
        expected_profit_per_unit: Decimal,
    ) -> Decimal | None:
        """Use the canonical risk authority for goal-active sizing."""

        if self.risk_policy.economic_goal is None:
            return self.stake
        return self.risk_policy.derive_goal_stake(
            context.paper_book,
            expected_profit_per_unit,
        )

    def _reconcile_existing_economic_action(
        self,
        event: MarketEvent,
        context: AgentContext,
        goal,
        material_action_id: str,
    ) -> bool:
        """Resolve a restarted material action without creating a second authority record."""

        ledger = context.decision_ledger
        if ledger is None:
            return False
        if getattr(ledger, "path", None) is not None and not ledger.path.exists():
            persisted = None
        else:
            persisted = ledger.verified_economic_decision_for_material_action(
                material_action_id,
                goal,
            )
        ticket = self._material_action_ticket(context, material_action_id)

        if persisted is None and ticket is None:
            return False
        if persisted is None:
            raise PaperDecisionReconciliationRequired(
                "PaperBook material action exists without a durable Decision Ledger record"
            )
        if ticket is None:
            raise PaperDecisionReconciliationRequired(
                "Decision Ledger material action exists without a durable PaperBook ticket"
            )

        payload = persisted.payload
        if (
            persisted.replay_run_id != context.replay_run_id
            or persisted.agent != self.name
            or persisted.action != _MATERIAL_ACTION_NAME
            or persisted.observed_ts != event.observed_ts
            or payload.get("material_action_id") != material_action_id
            or payload.get("quote_key") != event.quote_key
            or payload.get("ticket_id") != ticket.ticket_id
            or payload.get("stake") != str(ticket.stake)
            or not self._ticket_matches_event(ticket, event)
        ):
            raise PaperDecisionReconciliationRequired(
                "PaperBook and Decision Ledger material-action evidence do not match exactly"
            )

        self._acted.add(event.quote_key)
        return True

    @staticmethod
    def _rollback_uncommitted_ticket(
        context: AgentContext,
        ticket: PaperTicket,
        *,
        balance_before: Decimal,
        lifecycle_len_before: int,
    ) -> None:
        """Undo exactly one just-opened ticket when durable decision persistence fails."""

        book = context.paper_book
        if book.tickets.get(ticket.ticket_id) is not ticket:
            raise RuntimeError("paper decision rollback cannot prove ticket identity")
        expected_lifecycle = ("open", ticket.ticket_id, (), ())
        actual_lifecycle = book._lifecycle[-1] if book._lifecycle else None
        if (
            len(book._lifecycle) != lifecycle_len_before + 1
            or not isinstance(actual_lifecycle, tuple)
            or tuple(actual_lifecycle[:4]) != expected_lifecycle
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
            return False

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None:
        if event.quote_key in self._acted or event.status != "open":
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
        if goal is not None and context.decision_ledger is None:
            return

        material_action_id: str | None = None
        if goal is not None:
            material_action_id = self._material_action_id(context, event)
            # Reconcile before deriving a fresh stake. The durable ticket itself
            # changes current exposure, so re-sizing first could turn a valid
            # redelivery into ZERO or a different amount.
            if self._reconcile_existing_economic_action(
                event,
                context,
                goal,
                material_action_id,
            ):
                return

        chosen_stake = (
            self.stake
            if goal is None
            else self._derive_goal_stake(
                context,
                estimate.expected_profit_per_unit,
            )
        )
        if chosen_stake is None:
            return
        if paper_quote_rejection_reason(event, chosen_stake) is not None:
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
            chosen_stake,
            context=proposal_context,
        )
        if not risk.allowed:
            return

        reason = (
            self._strategy_reason(
                forecast,
                estimate.expected_profit_per_unit,
                material_action_id,
            )
            if material_action_id is not None
            else f"paper forecast {forecast.model_id}; EV/unit={estimate.expected_profit_per_unit}"
        )
        balance_before = context.paper_book.balance
        lifecycle_len_before = len(context.paper_book._lifecycle)
        ticket = context.paper_book.open_ticket(
            [leg],
            chosen_stake,
            reason=reason,
            placed_at=event.observed_ts,
            provider_source_ids=(
                tuple(sorted(proposal_context.source_ids))
                if proposal_context is not None
                else ()
            ),
            bankroll_id=goal.bankroll_id if goal is not None else None,
            currency=goal.currency if goal is not None else None,
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
                if material_action_id is not None:
                    payload["material_action_id"] = material_action_id
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
                    action=_MATERIAL_ACTION_NAME,
                    payload=payload,
                    context_hash=context.market_context_hash(),
                    decision_kind=(ECONOMIC_DECISION_KIND if goal is not None else "GENERAL"),
                )
                if goal is None:
                    context.decision_ledger.append(record)
                else:
                    context.decision_ledger.append_economic(record, goal)
        except Exception:
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

        self._acted.add(event.quote_key)
