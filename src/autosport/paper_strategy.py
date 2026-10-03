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
from .opportunity import ForecastRef, QuoteRef
from . import predictive_authority as _predictive_authority
from .price_truth import paper_quote_rejection_reason
from .probability import paper_value
from .risk import PaperRiskPolicy, ProposedTicketRiskContext
from .uncertainty_sizing import (
    SizingAction,
    UncertaintySizingDecision,
    UncertaintySizingEvidence,
    UncertaintySizingPolicy,
    UncertaintySizingRequest,
    evaluate_uncertainty_sizing,
)


_MATERIAL_ACTION_SCHEMA = "autosport.paper-value.open-ticket.v1"
_MATERIAL_ACTION_MARKER = "material_action_id="
_MATERIAL_ACTION_NAME = "OPEN_PAPER_VALUE_TICKET"

# Importing predictive_authority installs the process-local resolver guard.  Capture
# that exact producer-owned verifier after installation so this consumer never
# falls back to ForecastRef's structural/audit-only method through mutable class
# dispatch.  The code-object witness also fails closed on in-place mutation.
_PREDICTIVE_FORECAST_ELIGIBILITY_REASON = (
    _predictive_authority.ForecastRef.predictive_eligibility_reason
)
_PREDICTIVE_FORECAST_ELIGIBILITY_REASON_CODE = (
    _PREDICTIVE_FORECAST_ELIGIBILITY_REASON.__code__
)


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
        predictive_forecast_refs: dict[str, ForecastRef] | None = None,
        uncertainty_sizing_evidence: dict[str, UncertaintySizingEvidence] | None = None,
        uncertainty_sizing_policy: UncertaintySizingPolicy | None = None,
    ) -> None:
        self.forecasts = forecasts
        refs = {} if predictive_forecast_refs is None else predictive_forecast_refs
        if type(refs) is not dict:
            raise TypeError("predictive_forecast_refs must be a canonical dict")
        normalized_refs: dict[str, ForecastRef] = {}
        for quote_key, reference in refs.items():
            if (
                type(quote_key) is not str
                or not quote_key
                or quote_key.strip() != quote_key
            ):
                raise ValueError(
                    "predictive_forecast_refs keys must be canonical non-empty text"
                )
            if type(reference) is not ForecastRef:
                raise TypeError(
                    "predictive_forecast_refs values must be exact ForecastRef values"
                )
            if reference.quote_key != quote_key:
                raise ValueError(
                    "predictive ForecastRef key must match its bound quote_key"
                )
            normalized_refs[quote_key] = reference
        self.predictive_forecast_refs = normalized_refs

        sizing_evidence = (
            {} if uncertainty_sizing_evidence is None else uncertainty_sizing_evidence
        )
        if type(sizing_evidence) is not dict:
            raise TypeError("uncertainty_sizing_evidence must be a canonical dict")
        normalized_sizing_evidence: dict[str, UncertaintySizingEvidence] = {}
        for quote_key, evidence in sizing_evidence.items():
            if (
                type(quote_key) is not str
                or not quote_key
                or quote_key.strip() != quote_key
            ):
                raise ValueError(
                    "uncertainty_sizing_evidence keys must be canonical non-empty text"
                )
            if type(evidence) is not UncertaintySizingEvidence:
                raise TypeError(
                    "uncertainty_sizing_evidence values must be exact "
                    "UncertaintySizingEvidence values"
                )
            normalized_sizing_evidence[quote_key] = evidence
        if (
            uncertainty_sizing_policy is not None
            and type(uncertainty_sizing_policy) is not UncertaintySizingPolicy
        ):
            raise TypeError(
                "uncertainty_sizing_policy must be exact UncertaintySizingPolicy or None"
            )
        self.uncertainty_sizing_evidence = normalized_sizing_evidence
        self.uncertainty_sizing_policy = uncertainty_sizing_policy

        # ``stake`` remains a compatibility input for legacy/no-goal paper runs.
        # Once an EconomicGoalContract is active it has no financial authority:
        # the strategy derives a bounded proposal from edge + current PaperBook
        # exposure, then PaperRiskPolicy remains the final executable gate.
        self.stake = Decimal(str(stake))
        self.minimum_edge = Decimal(str(minimum_expected_profit_per_unit))
        self.risk_policy = risk_policy or PaperRiskPolicy()
        self._acted: set[str] = set()

    def _qualified_forecast_probability(
        self,
        forecast: ForecastLike,
        event: MarketEvent,
        context: AgentContext,
    ) -> Decimal | None:
        """Return one uncertainty-conservative probability or fail closed.

        Legacy Forecast remains a compatibility-only PAPER input. Modern
        ForecastRecord probability edge must reuse the canonical predictive
        authority from #597/#615: an exact resolver-minted ForecastRef bound to
        this forecast and this exact quote snapshot. Caller-authored/audit-only
        ForecastRef values are not positive authority because the installed
        predictive_eligibility_reason guard requires runtime resolver identity.

        Once that authority is present, its declared
        absolute_probability_radius_v1 uncertainty is applied exactly once.
        BACK uses the lower probability endpoint; LAY uses the upper endpoint.
        This is only an edge precondition and does not replace PaperRiskPolicy or
        the canonical #623 PAPER execution authority.
        """

        if type(forecast) is Forecast:
            return forecast.probability
        if type(forecast) is not ForecastRecord:
            context.notes.append(
                "paper-value material action withheld: forecast type is not canonical"
            )
            return None

        reference = self.predictive_forecast_refs.get(event.quote_key)
        if reference is None:
            context.notes.append(
                "paper-value material action withheld: modern ForecastRecord lacks "
                "canonical predictive ForecastRef authority"
            )
            return None
        if forecast.market_snapshot_hash is None:
            context.notes.append(
                "paper-value material action withheld: modern ForecastRecord lacks "
                "canonical market_snapshot_hash evidence"
            )
            return None

        try:
            rebound_quote = QuoteRef.from_market_event(
                event,
                market_snapshot_hash=forecast.market_snapshot_hash,
            )
        except Exception:
            context.notes.append(
                "paper-value material action withheld: current quote cannot be rebound "
                "to canonical predictive evidence"
            )
            return None

        if (
            reference.forecast_id != forecast.forecast_id
            or reference.forecast_hash != forecast.canonical_hash
            or reference.quote_key != forecast.quote_key
            or reference.probability != forecast.probability
            or reference.input_cutoff_ts != forecast.input_cutoff_ts
            or reference.market_snapshot_hash != forecast.market_snapshot_hash
            or reference.quote_market_event_hash != rebound_quote.market_event_hash
            or reference.model_id != forecast.model_id
            or reference.model_version != forecast.model_version
            or reference.strategy_version != forecast.strategy_version
            or reference.uncertainty != forecast.uncertainty
        ):
            context.notes.append(
                "paper-value material action withheld: predictive ForecastRef does "
                "not bind the exact forecast/current quote snapshot"
            )
            return None

        verifier = _PREDICTIVE_FORECAST_ELIGIBILITY_REASON
        if (
            type(reference) is not ForecastRef
            or _predictive_authority.ForecastRef.predictive_eligibility_reason
            is not verifier
            or verifier.__code__ is not _PREDICTIVE_FORECAST_ELIGIBILITY_REASON_CODE
        ):
            context.notes.append(
                "paper-value material action withheld: predictive authority "
                "verifier integrity changed"
            )
            return None
        try:
            reason = verifier(
                reference,
                parse_iso_timestamp(event.observed_ts),
                expected_model_id=forecast.model_id,
            )
        except Exception:
            context.notes.append(
                "paper-value material action withheld: predictive authority "
                "resolution could not be verified"
            )
            return None
        if reason is not None:
            context.notes.append(
                "paper-value material action withheld: " + reason
            )
            return None

        witness = reference.predictive_eligibility
        if (
            witness is None
            or witness.uncertainty_kind != "absolute_probability_radius_v1"
            or reference.uncertainty is None
        ):
            context.notes.append(
                "paper-value material action withheld: predictive uncertainty "
                "semantics are not canonical"
            )
            return None

        if event.exchange_side == "lay":
            return min(
                Decimal("1"),
                reference.probability + reference.uncertainty,
            )
        return max(
            Decimal("0"),
            reference.probability - reference.uncertainty,
        )

    def _canonical_uncertainty_sizing_decision(
        self,
        *,
        forecast: ForecastRecord,
        event: MarketEvent,
        context: AgentContext,
        goal,
        qualified_probability: Decimal,
    ) -> UncertaintySizingDecision | None:
        """Resolve the existing canonical uncertainty-sizing prerequisite.

        This bridge does not estimate costs, probabilities, or stake itself.  It
        requires externally produced canonical sizing evidence to bind the exact
        modern forecast candidate and current executable quote, then delegates all
        uncertainty/payoff sizing semantics to evaluate_uncertainty_sizing().
        """

        if event.exchange_side == "lay":
            context.notes.append(
                "paper-value material action withheld: canonical #623 paper "
                "execution bridge is BACK-only for LAY"
            )
            return None

        evidence = self.uncertainty_sizing_evidence.get(event.quote_key)
        policy = self.uncertainty_sizing_policy
        if evidence is None or policy is None:
            context.notes.append(
                "paper-value material action withheld: canonical uncertainty sizing "
                "evidence/policy is unavailable"
            )
            return None

        reference = self.predictive_forecast_refs.get(event.quote_key)
        if reference is None or forecast.market_snapshot_hash is None:
            context.notes.append(
                "paper-value material action withheld: predictive authority is "
                "unavailable for uncertainty sizing"
            )
            return None
        try:
            quote = QuoteRef.from_market_event(
                event,
                market_snapshot_hash=forecast.market_snapshot_hash,
            )
        except Exception:
            context.notes.append(
                "paper-value material action withheld: current quote cannot be "
                "bound to uncertainty sizing"
            )
            return None

        uncertainty = reference.uncertainty
        if uncertainty is None:
            context.notes.append(
                "paper-value material action withheld: predictive uncertainty "
                "is unavailable for sizing"
            )
            return None
        expected_lower = max(
            Decimal("0"),
            reference.probability - uncertainty,
        )
        expected_upper = min(
            Decimal("1"),
            reference.probability + uncertainty,
        )
        if (
            evidence.candidate_id != forecast.forecast_id
            or evidence.quote_sha256 != quote.market_event_hash
            or evidence.probability_model_version_id != forecast.model_version
            or evidence.probability_point != reference.probability
            or evidence.probability_lower != expected_lower
            or evidence.probability_upper != expected_upper
            or qualified_probability != expected_lower
        ):
            context.notes.append(
                "paper-value material action withheld: uncertainty sizing evidence "
                "does not bind the exact forecast/current quote/predictive interval"
            )
            return None

        try:
            request = UncertaintySizingRequest(
                candidate_id=forecast.forecast_id,
                quote_sha256=quote.market_event_hash,
                decision_ts=event.observed_ts,
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
                bankroll=context.paper_book.balance,
            )
            decision = evaluate_uncertainty_sizing(
                evidence,
                request,
                policy,
            )
        except Exception:
            context.notes.append(
                "paper-value material action withheld: canonical uncertainty sizing "
                "could not be evaluated"
            )
            return None

        if decision.action is not SizingAction.ELIGIBLE:
            reason = ",".join(decision.reasons) if decision.reasons else "abstain"
            context.notes.append(
                "paper-value material action withheld: canonical uncertainty sizing "
                "abstained: " + reason
            )
        return decision

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
        return leg.quote_key == event.quote_key and leg.locked_odds == event.decimal_odds

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
        if event.quote_key in self._acted or event.status != "open":
            return
        forecast = self.forecasts.get(event.quote_key)
        if forecast is None:
            return
        if parse_iso_timestamp(forecast.as_of_ts) > parse_iso_timestamp(event.observed_ts):
            return
        qualified_probability = self._qualified_forecast_probability(
            forecast,
            event,
            context,
        )
        if qualified_probability is None:
            return
        estimate = paper_value(
            event.quote_key,
            qualified_probability,
            event.decimal_odds,
        )
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

        sizing_decision: UncertaintySizingDecision | None = None
        if goal is not None and type(forecast) is ForecastRecord:
            sizing_decision = self._canonical_uncertainty_sizing_decision(
                forecast=forecast,
                event=event,
                context=context,
                goal=goal,
                qualified_probability=qualified_probability,
            )
            sizing_eligible = (
                sizing_decision is not None
                and sizing_decision.action is SizingAction.ELIGIBLE
                and sizing_decision.conservative_ev_per_stake >= self.minimum_edge
            )
            if not sizing_eligible:
                if persisted is not None:
                    raise PaperDecisionReconciliationRequired(
                        "durable paper-value decision cannot re-resolve current "
                        "canonical uncertainty sizing authority"
                    )
                return

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
            ):
                raise PaperDecisionReconciliationRequired(
                    "durable paper-value decision identity changed across restart"
                )
            if type(forecast) is ForecastRecord:
                reference = self.predictive_forecast_refs.get(event.quote_key)
                if (
                    reference is None
                    or payload.get("predictive_forecast_ref")
                    != reference.to_dict()
                    or payload.get("qualified_probability")
                    != str(qualified_probability)
                ):
                    raise PaperDecisionReconciliationRequired(
                        "durable paper-value decision does not bind current "
                        "predictive uncertainty authority"
                    )
                if goal is not None:
                    evidence = self.uncertainty_sizing_evidence.get(event.quote_key)
                    policy = self.uncertainty_sizing_policy
                    if (
                        sizing_decision is None
                        or evidence is None
                        or policy is None
                        or payload.get("uncertainty_sizing_evidence_fingerprint")
                        != evidence.fingerprint_sha256
                        or payload.get("uncertainty_sizing_policy_fingerprint")
                        != policy.fingerprint_sha256
                        or payload.get("uncertainty_sizing_decision_fingerprint")
                        != sizing_decision.decision_fingerprint_sha256
                        or payload.get("uncertainty_sizing_stake_ceiling")
                        != str(sizing_decision.stake_ceiling)
                        or payload.get(
                            "uncertainty_sizing_conservative_ev_per_stake"
                        )
                        != str(sizing_decision.conservative_ev_per_stake)
                    ):
                        raise PaperDecisionReconciliationRequired(
                            "durable paper-value decision does not bind current "
                            "canonical uncertainty sizing authority"
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
            sizing_edge = (
                sizing_decision.conservative_ev_per_stake
                if sizing_decision is not None
                else expected_profit_per_unit
            )
            chosen_stake = (
                self.stake
                if goal is None
                else self._derive_goal_stake(
                    context,
                    sizing_edge,
                )
            )
            if chosen_stake is None:
                return
            if sizing_decision is not None:
                chosen_stake = min(chosen_stake, sizing_decision.stake_ceiling)
                if chosen_stake <= 0:
                    return
        elif (
            sizing_decision is not None
            and chosen_stake > sizing_decision.stake_ceiling
        ):
            raise PaperDecisionReconciliationRequired(
                "durable paper-value requested stake exceeds current canonical "
                "uncertainty sizing ceiling"
            )

        if paper_quote_rejection_reason(event, chosen_stake) is not None:
            return

        leg = TicketLeg(
            event.event_id,
            event.market_id,
            event.selection_id,
            event.decimal_odds,
            sport=event.sport,
            exchange_side=event.exchange_side,
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
                            "qualified_probability": str(qualified_probability),
                            "predictive_forecast_ref": self.predictive_forecast_refs[
                                event.quote_key
                            ].to_dict(),
                            "evidence_hashes": list(forecast.evidence_hashes),
                            "market_snapshot_hash": forecast.market_snapshot_hash,
                        }
                    )
                    if goal is not None:
                        evidence = self.uncertainty_sizing_evidence[event.quote_key]
                        policy = self.uncertainty_sizing_policy
                        if sizing_decision is None or policy is None:
                            raise PaperDecisionReconciliationRequired(
                                "canonical uncertainty sizing disappeared before "
                                "durable decision persistence"
                            )
                        payload.update(
                            {
                                "uncertainty_sizing_evidence_fingerprint":
                                    evidence.fingerprint_sha256,
                                "uncertainty_sizing_policy_fingerprint":
                                    policy.fingerprint_sha256,
                                "uncertainty_sizing_decision_fingerprint":
                                    sizing_decision.decision_fingerprint_sha256,
                                "uncertainty_sizing_stake_ceiling":
                                    str(sizing_decision.stake_ceiling),
                                "uncertainty_sizing_conservative_ev_per_stake":
                                    str(sizing_decision.conservative_ev_per_stake),
                            }
                        )
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

        self._acted.add(event.quote_key)
