from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow, Underflow, localcontext

from .agents import AgentContext
from .decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionRecord,
    EconomicDecisionAuthority,
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

    @staticmethod
    def _forecast_matches_market_identity(
        forecast: ForecastLike,
        event: MarketEvent,
    ) -> bool:
        """Require forecast probability to describe the event's exact market identity."""

        if forecast.quote_key != event.quote_key:
            return False
        if isinstance(forecast, ForecastRecord):
            return forecast.market_semantics_id == event.market_semantics_id
        # Legacy Forecast has no rule-identity field. It remains valid only for
        # legacy events that likewise carry no canonical market semantics.
        return event.market_semantics_id is None

    @staticmethod
    def _material_quote_identity(event: MarketEvent) -> str:
        """Return the in-process duplicate key for one exact market-rules identity."""

        if event.market_semantics_id is None:
            # Preserve the exact historical duplicate key for legacy events.
            return event.quote_key
        canonical = json.dumps(
            {
                "quote_key": event.quote_key,
                "market_semantics_id": event.market_semantics_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _material_action_digest(
        cls,
        context: AgentContext,
        event: MarketEvent,
        *,
        bind_market_semantics: bool,
    ) -> str:
        identity = {
            "schema": _MATERIAL_ACTION_SCHEMA,
            "replay_run_id": context.replay_run_id,
            "agent": cls.name,
            "action": _MATERIAL_ACTION_NAME,
            "quote_key": event.quote_key,
        }
        if bind_market_semantics and event.market_semantics_id is not None:
            identity["market_semantics_id"] = event.market_semantics_id
        canonical = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _material_action_id(cls, context: AgentContext, event: MarketEvent) -> str:
        """Stable commit identity bound to exact rules when those rules are known."""

        return cls._material_action_digest(
            context,
            event,
            bind_market_semantics=True,
        )

    @classmethod
    def _legacy_material_action_id(
        cls,
        context: AgentContext,
        event: MarketEvent,
    ) -> str:
        """Return the pre-market-semantics action id for upgrade safety checks."""

        return cls._material_action_digest(
            context,
            event,
            bind_market_semantics=False,
        )

    @staticmethod
    def _decision_matches_market_identity(payload, event: MarketEvent) -> bool:
        if event.market_semantics_id is None:
            return "market_semantics_id" not in payload
        return payload.get("market_semantics_id") == event.market_semantics_id

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

    def _has_legacy_semantics_action_evidence(
        self,
        event: MarketEvent,
        context: AgentContext,
        material_action_id: str,
    ) -> bool:
        """Fence pre-binding durable actions that cannot prove exact market rules."""

        if event.market_semantics_id is None:
            return False
        legacy_action_id = self._legacy_material_action_id(context, event)
        if legacy_action_id == material_action_id:
            return False

        ledger = context.decision_ledger
        if (
            ledger is not None
            and getattr(ledger, "path", None) is not None
            and ledger.path.exists()
            and any(
                record.decision_id == legacy_action_id
                for record in ledger.verified_records()
            )
        ):
            return True
        if self._material_action_ticket(context, legacy_action_id) is not None:
            return True

        runtime = context.paper_execution
        return runtime is not None and any(
            item.get("event_type") == "RUN_RESERVED"
            and item.get("payload", {}).get("trigger_id") == legacy_action_id
            for item in runtime.ledger.events()
        )

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
            leg.quote_key == event.quote_key
            and leg.market_semantics_id == event.market_semantics_id
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

    @staticmethod
    def _risk_amount(event: MarketEvent, chosen_stake: Decimal) -> Decimal | None:
        """Return bankroll capital at risk while preserving the provider order-stake unit."""

        if event.exchange_side != "lay":
            return chosen_stake

        # chosen_stake remains the LAY order-stake unit. PaperRiskPolicy receives
        # committed bankroll capital, so LAY must present maximum-loss liability.
        # Match the canonical paper-risk Decimal envelope and fail closed rather
        # than silently rounding an exposure amount.
        arithmetic = Context(prec=28, Emin=-999999, Emax=999999)
        arithmetic.traps[Inexact] = True
        arithmetic.traps[InvalidOperation] = True
        arithmetic.traps[Overflow] = True
        arithmetic.traps[Underflow] = True
        arithmetic.clear_flags()
        try:
            with localcontext(arithmetic):
                liability = chosen_stake * (event.decimal_odds - Decimal("1"))
        except ArithmeticError:
            return None
        if not liability.is_finite() or liability <= 0:
            return None
        return liability

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
                risk_policy=self.risk_policy,
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
            or not self._decision_matches_market_identity(payload, event)
            or payload.get("ticket_id") != ticket.ticket_id
            or payload.get("stake") != str(ticket.stake)
            or not self._ticket_matches_event(ticket, event)
        ):
            raise PaperDecisionReconciliationRequired(
                "PaperBook and Decision Ledger material-action evidence do not match exactly"
            )

        self._acted.add(self._material_quote_identity(event))
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

    def _decision_is_durable(
        self,
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
            persisted = ledger.verified_economic_decision(
                record.decision_id,
                goal,
                risk_policy=self.risk_policy,
            )
            return persisted == bind_economic_goal(record, goal, self.risk_policy)
        except Exception:
            return False

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None:
        material_quote_identity = self._material_quote_identity(event)
        if material_quote_identity in self._acted or event.status != "open":
            return
        forecast = self.forecasts.get(event.quote_key)
        if forecast is None:
            return
        if not self._forecast_matches_market_identity(forecast, event):
            context.notes.append(
                "paper-value forecast withheld: forecast market identity does not "
                "match the canonical market event"
            )
            return
        if parse_iso_timestamp(forecast.as_of_ts) > parse_iso_timestamp(event.observed_ts):
            return
        estimate = paper_value(event.quote_key, forecast.probability, event.decimal_odds)
        expected_profit_per_unit = estimate.expected_profit_per_unit
        if event.exchange_side == "lay":
            # paper_value is the canonical BACK value p*O - 1. For a LAY quote
            # the economic unit is one unit of lay stake: EV = 1 - p*O,
            # exactly the negative of the BACK value before commission.
            expected_profit_per_unit = -expected_profit_per_unit
        if expected_profit_per_unit < self.minimum_edge:
            return

        # PaperValueAgent may decide/propose, but it no longer owns fill truth.
        # Any material PAPER exposure must traverse the canonical #623 runtime.
        runtime = context.paper_execution
        provider_accounts = dict(context.paper_provider_accounts)
        account_id = provider_accounts.get(event.source_id)
        if runtime is None or account_id is None:
            context.notes.append(
                "paper-value material action withheld: canonical #623 execution "
                "runtime/account authority is unavailable"
            )
            return

        goal = self.risk_policy.economic_goal
        ledger = context.decision_ledger
        if goal is not None and ledger is None:
            return

        material_action_id = self._material_action_id(context, event)
        if self._has_legacy_semantics_action_evidence(
            event,
            context,
            material_action_id,
        ):
            raise PaperDecisionReconciliationRequired(
                "legacy paper-value material action lacks exact market-semantics identity"
            )

        persisted: DecisionRecord | None = None
        if ledger is not None and getattr(ledger, "path", None) is not None and ledger.path.exists():
            if goal is None:
                matches = tuple(
                    record
                    for record in ledger.verified_records()
                    if record.decision_id == material_action_id
                )
                if len(matches) > 1:
                    raise PaperDecisionReconciliationRequired(
                        "duplicate durable paper-value decision identity"
                    )
                persisted = matches[0] if matches else None
            else:
                persisted = ledger.verified_economic_decision_for_material_action(
                    material_action_id,
                    goal,
                    risk_policy=self.risk_policy,
                )

        if persisted is None and ledger is not None:
            orphaned_execution = [
                item
                for item in runtime.ledger.events()
                if item.get("event_type") == "RUN_RESERVED"
                and item.get("payload", {}).get("trigger_id") == material_action_id
            ]
            if orphaned_execution:
                raise PaperDecisionReconciliationRequired(
                    "#623 execution history exists without its durable "
                    "paper-value decision"
                )

        chosen_stake: Decimal | None = None
        if persisted is not None:
            payload = persisted.payload
            if (
                persisted.replay_run_id != context.replay_run_id
                or persisted.agent != self.name
                or persisted.action != _MATERIAL_ACTION_NAME
                or persisted.observed_ts != event.observed_ts
                or payload.get("material_action_id") != material_action_id
                or payload.get("quote_key") != event.quote_key
                or not self._decision_matches_market_identity(payload, event)
            ):
                raise PaperDecisionReconciliationRequired(
                    "durable paper-value decision identity changed across restart"
                )
            try:
                chosen_stake = Decimal(str(payload["requested_stake"]))
            except Exception as exc:
                raise PaperDecisionReconciliationRequired(
                    "durable paper-value decision lacks canonical requested stake"
                ) from exc
            if not chosen_stake.is_finite() or chosen_stake <= 0:
                raise PaperDecisionReconciliationRequired(
                    "durable paper-value requested stake is invalid"
                )

        if chosen_stake is None:
            chosen_stake = (
                self.stake
                if goal is None
                else self._derive_goal_stake(
                    context,
                    expected_profit_per_unit,
                )
            )
            if chosen_stake is None:
                return

        if paper_quote_rejection_reason(event, chosen_stake) is not None:
            return

        leg = TicketLeg(
            event.event_id,
            event.market_id,
            event.selection_id,
            event.decimal_odds,
            sport=event.sport,
            exchange_side=event.exchange_side,
            market_semantics_id=event.market_semantics_id,
        )
        proposal_context = None
        if goal is not None:
            proposal_context = ProposedTicketRiskContext(
                legs=(leg,),
                quotes=(event,),
                provider_accounts=((event.source_id, account_id),),
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
                proposal_ts=event.observed_ts,
            )

        # A recovered durable decision has already passed the exact historical
        # risk gate. Re-evaluating after an accepted/partial ticket would resize
        # against changed exposure and could mint a different #623 plan.
        if persisted is None:
            risk_amount = self._risk_amount(event, chosen_stake)
            if risk_amount is None:
                return
            risk = self.risk_policy.evaluate(
                context.paper_book,
                risk_amount,
                context=proposal_context,
            )
            if not risk.allowed:
                return

        if event.exchange_side == "lay":
            context.notes.append(
                "paper-value material action withheld: canonical #623 paper "
                "execution bridge is BACK-only for LAY"
            )
            return

        prepared = runtime.prepare_paper_value_action(
            event=event,
            stake=chosen_stake,
            decision_id=material_action_id,
            account_id=account_id,
            bankroll_id=(goal.bankroll_id if goal is not None else None),
            currency=(goal.currency if goal is not None else None),
        )
        expected_run_id = runtime.expected_run_id(prepared, material_action_id)

        if ledger is not None:
            if persisted is None:
                payload = {
                    "quote_key": event.quote_key,
                    "forecast_model": forecast.model_id,
                    "probability": str(forecast.probability),
                    "expected_profit_per_unit": str(expected_profit_per_unit),
                    "stake": str(chosen_stake),
                    "requested_stake": str(chosen_stake),
                    "material_action_id": material_action_id,
                    "execution_plan_id": prepared.execution_plan.plan_id,
                    "execution_plan_fingerprint": prepared.execution_plan.fingerprint,
                    "execution_run_id": expected_run_id,
                    "execution_authority_json": prepared.intent_evidence_json,
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
                    if forecast.market_semantics_id is not None:
                        payload["market_semantics_id"] = forecast.market_semantics_id
                record = DecisionRecord(
                    replay_run_id=context.replay_run_id,
                    agent=self.name,
                    observed_ts=event.observed_ts,
                    action=_MATERIAL_ACTION_NAME,
                    payload=payload,
                    context_hash=context.market_context_hash(),
                    decision_id=material_action_id,
                    decision_kind=(
                        ECONOMIC_DECISION_KIND if goal is not None else "GENERAL"
                    ),
                )
                try:
                    if goal is None:
                        ledger.append(record)
                    else:
                        ledger.append_economic(
                            record,
                            EconomicDecisionAuthority(goal, self.risk_policy),
                        )
                except Exception:
                    if not self._decision_is_durable(context, record, goal):
                        raise

                if goal is None:
                    matches = tuple(
                        durable
                        for durable in ledger.verified_records()
                        if durable.decision_id == material_action_id
                    )
                    if len(matches) != 1:
                        raise PaperDecisionReconciliationRequired(
                            "paper-value decision append did not become uniquely durable"
                        )
                    persisted = matches[0]
                else:
                    persisted = ledger.verified_economic_decision_for_material_action(
                        material_action_id,
                        goal,
                        risk_policy=self.risk_policy,
                    )
                    if persisted is None:
                        raise PaperDecisionReconciliationRequired(
                            "paper-value decision append did not become durable"
                        )

            payload = persisted.payload
            if (
                payload.get("requested_stake") != str(chosen_stake)
                or payload.get("execution_plan_id") != prepared.execution_plan.plan_id
                or payload.get("execution_plan_fingerprint")
                != prepared.execution_plan.fingerprint
                or payload.get("execution_run_id") != expected_run_id
                or payload.get("execution_authority_json")
                != prepared.intent_evidence_json
            ):
                raise PaperDecisionReconciliationRequired(
                    "durable paper-value decision conflicts with #623 execution plan"
                )

        result = runtime.execute(
            prepared=prepared,
            trigger_id=material_action_id,
            started_at=event.observed_ts,
            materialize_exposure=True,
        )
        if result.run.run_id != expected_run_id:
            raise PaperDecisionReconciliationRequired(
                "paper-value execution resolved a different durable #623 run"
            )

        self._acted.add(material_quote_identity)
