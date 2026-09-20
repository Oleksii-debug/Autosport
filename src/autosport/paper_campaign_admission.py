"""Crash-safe PAPER campaign admission bound to canonical #623/#646 execution truth.

The durable PREPARED -> COMMITTED journal implementation remains in the private base
module.  This public facade closes the post-#646 composition seam: admission may
consume only an already-materialized PaperBook ticket whose exact ACCEPTED/PARTIAL
execution attempt is present in the canonical PaperExecutionLedger.  It never opens
or fabricates PAPER exposure itself.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import _paper_campaign_admission_base as _base
from .decision_ledger import JsonlDecisionLedger
from .domain import PaperTicket
from .learning_environment import Observation
from .paper import PaperBook
from .paper_campaign_runtime import PaperCampaignRuntime
from .paper_execution_reality import (
    PaperAttemptOutcome,
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperExecutionStateError,
    PaperLegAttempt,
)

# Preserve the original module surface, including private helpers used by the
# existing deterministic crash tests.  The facade overrides only composition
# behavior that must consume integrated execution-adoption authority.
for _name in dir(_base):
    if not _name.startswith("__"):
        globals().setdefault(_name, getattr(_base, _name))

_EXECUTION_DECISION_ID = "paper_execution_decision_id"
_EXECUTION_RUN_ID = "paper_execution_run_id"
_EXECUTION_ATTEMPT_ID = "paper_execution_attempt_id"
_EXECUTION_TICKET_ID = "paper_execution_ticket_id"
_EXECUTION_FIELDS = frozenset(
    {
        _EXECUTION_DECISION_ID,
        _EXECUTION_RUN_ID,
        _EXECUTION_ATTEMPT_ID,
        _EXECUTION_TICKET_ID,
    }
)
_ACCEPTED_EQUIVALENT = frozenset(
    {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}
)


@dataclass(frozen=True, slots=True)
class _ExecutionAdmissionBinding:
    decision_id: str
    run_id: str
    attempt_id: str
    ticket_id: str


_ACTIVE_EXECUTION_BINDING: ContextVar[_ExecutionAdmissionBinding | None] = ContextVar(
    "autosport_paper_campaign_execution_binding",
    default=None,
)


class PaperCampaignAdmissionCoordinator(_base.PaperCampaignAdmissionCoordinator):
    """Converge learning admission from one exact durable PAPER execution exposure."""

    def __init__(
        self,
        state_path: str | Path,
        *,
        paper_book_path: str | Path,
        decision_ledger: JsonlDecisionLedger,
        runtime: PaperCampaignRuntime,
        execution_ledger: PaperExecutionLedger,
    ) -> None:
        if not isinstance(execution_ledger, PaperExecutionLedger):
            raise TypeError("execution_ledger must be PaperExecutionLedger")
        super().__init__(
            state_path,
            paper_book_path=paper_book_path,
            decision_ledger=decision_ledger,
            runtime=runtime,
        )
        expected_workspace = self.workspace.resolve(strict=False)
        if execution_ledger.path.parent.resolve(strict=False) != expected_workspace:
            raise PaperCampaignAdmissionError(
                "PAPER execution ledger must share the admission workspace"
            )
        self.execution_ledger = execution_ledger

    def _write(self, admissions: dict[str, object]) -> None:
        """Keep the base journal protocol while retaining patchable atomic I/O."""

        if self.state_path.exists():
            current = self._read()
            generation = current["generation"] + 1
        else:
            records = self._read_witnesses()
            if records:
                raise PaperCampaignAdmissionError(
                    "cannot recreate admission journal behind monotonic authority"
                )
            generation = 1
        state = _state_for(admissions, generation)
        records = self._read_witnesses()
        _, pending = self._witness_status(records)
        if pending is None:
            self._append_witness(
                event=_WITNESS_PREPARE,
                generation=generation,
                state_sha256=state["state_sha256"],
            )
        elif (
            pending["generation"] != generation
            or pending["state_sha256"] != state["state_sha256"]
        ):
            raise PaperCampaignAdmissionError(
                "pending admission witness conflicts with recovered publication"
            )
        atomic_write_json(self.state_path, state)
        self._append_witness(
            event=_WITNESS_COMMIT,
            generation=generation,
            state_sha256=state["state_sha256"],
        )
        self._read()

    def _execution_attempt(
        self,
        *,
        run_id: str,
        attempt_id: str,
    ) -> PaperLegAttempt:
        run_id = _text(run_id, "execution_run_id")
        attempt_id = _text(attempt_id, "execution_attempt_id")
        try:
            events = self.execution_ledger.events(run_id)
        except (PaperExecutionIntegrityError, PaperExecutionStateError) as exc:
            raise PaperCampaignAdmissionError(
                "PAPER execution ledger cannot re-resolve admission authority"
            ) from exc
        if not events:
            raise PaperCampaignAdmissionError(
                "admission execution run is missing from canonical ledger"
            )
        reservations = [event for event in events if event.get("event_type") == "RUN_RESERVED"]
        completions = [event for event in events if event.get("event_type") == "RUN_COMPLETED"]
        if len(reservations) != 1 or len(completions) != 1:
            raise PaperCampaignAdmissionError(
                "admission requires one completed canonical PAPER execution run"
            )
        attempts: list[PaperLegAttempt] = []
        try:
            for event in events:
                if event.get("event_type") == "ATTEMPT_RECORDED":
                    attempts.append(PaperLegAttempt.from_dict(event.get("payload")))
        except (PaperExecutionIntegrityError, ValueError, TypeError) as exc:
            raise PaperCampaignAdmissionError(
                "admission execution attempt evidence is invalid"
            ) from exc
        matches = [attempt for attempt in attempts if attempt.attempt_id == attempt_id]
        if len(matches) != 1:
            raise PaperCampaignAdmissionError(
                "admission execution attempt is missing or duplicated"
            )
        attempt = matches[0]
        if attempt.run_id != run_id:
            raise PaperCampaignAdmissionError(
                "admission execution attempt belongs to another run"
            )
        action_ids = reservations[0].get("payload", {}).get("action_ids")
        if (
            type(action_ids) is not list
            or attempt.sequence >= len(action_ids)
            or action_ids[attempt.sequence] != attempt.action_id
        ):
            raise PaperCampaignAdmissionError(
                "admission execution attempt is not bound to reserved action order"
            )
        if attempt.outcome not in _ACCEPTED_EQUIVALENT:
            raise PaperCampaignAdmissionError(
                "REJECTED/UNKNOWN PAPER execution cannot enter campaign admission"
            )
        if attempt.execution_odds is None or attempt.execution_stake is None:
            raise PaperCampaignAdmissionError(
                "accepted PAPER execution lacks exact execution odds/stake"
            )
        return attempt

    def _execution_ticket(
        self,
        binding: _ExecutionAdmissionBinding,
    ) -> PaperTicket:
        attempt = self._execution_attempt(
            run_id=binding.run_id,
            attempt_id=binding.attempt_id,
        )
        try:
            book = PaperBook.load(self.paper_book_path)
        except (OSError, TypeError, ValueError) as exc:
            raise PaperCampaignAdmissionError(
                "canonical execution PaperBook is unavailable"
            ) from exc
        marker = f"paper_execution_attempt_id={binding.attempt_id}"
        marker_matches = [
            ticket for ticket in book.tickets.values() if marker in ticket.strategy_reason
        ]
        ticket = book.tickets.get(binding.ticket_id)
        if (
            ticket is None
            or len(marker_matches) != 1
            or marker_matches[0].ticket_id != binding.ticket_id
        ):
            raise PaperCampaignAdmissionError(
                "execution attempt must resolve to exactly one canonical PaperTicket"
            )
        expected_reason = (
            "paper execution adoption; "
            f"decision_id={binding.decision_id}; "
            f"run_id={binding.run_id}; {marker}"
        )
        if ticket.strategy_reason != expected_reason:
            raise PaperCampaignAdmissionError(
                "PaperTicket execution marker conflicts with durable execution identity"
            )
        if (
            ticket.stake != attempt.execution_stake
            or ticket.placed_at != attempt.execution_observed_at
            or ticket.provider_source_ids != (attempt.bookmaker_id,)
            or ticket.provider_accounts != ((attempt.bookmaker_id, attempt.account_id),)
            or len(ticket.legs) != 1
        ):
            raise PaperCampaignAdmissionError(
                "PaperTicket conflicts with exact durable execution attempt"
            )
        leg = ticket.legs[0]
        if (
            leg.event_id != attempt.event_id
            or leg.market_id != attempt.market_id
            or leg.selection_id != attempt.selection_id
            or leg.locked_odds != attempt.execution_odds
        ):
            raise PaperCampaignAdmissionError(
                "PaperTicket leg conflicts with exact durable execution truth"
            )
        return ticket

    def _ticket(
        self,
        *,
        admission_id: str,
        intent_sha256: str,
        legs,
        stake,
        strategy_reason: str,
        placed_at: str,
        provider_source_ids,
        provider_accounts,
        bankroll_id: str,
        currency: str,
        expected_ticket_id: str | None = None,
    ) -> PaperTicket:
        del admission_id, intent_sha256, strategy_reason
        binding = _ACTIVE_EXECUTION_BINDING.get()
        if binding is None:
            raise PaperCampaignAdmissionError(
                "campaign admission lacks canonical PAPER execution authority"
            )
        if expected_ticket_id is not None and expected_ticket_id != binding.ticket_id:
            raise PaperCampaignAdmissionError(
                "committed admission execution ticket identity changed"
            )
        ticket = self._execution_ticket(binding)
        if (
            tuple(ticket.legs) != tuple(legs)
            or ticket.stake != stake
            or ticket.placed_at != placed_at
            or ticket.provider_source_ids != tuple(provider_source_ids)
            or ticket.provider_accounts != tuple(provider_accounts)
            or ticket.bankroll_id != bankroll_id
            or ticket.currency != currency
        ):
            raise PaperCampaignAdmissionError(
                "admission values differ from canonical execution-materialized PaperTicket"
            )
        return ticket

    def admit(
        self,
        *,
        admission_id: str,
        observation: Observation,
        action_type: str,
        decision_action: str,
        decision_at: str,
        at: str,
        replay_run_id: str,
        agent: str,
        execution_decision_id: str,
        execution_run_id: str,
        execution_attempt_id: str,
        execution_ticket_id: str,
        strategy_reason: str = "",
        decision_payload: Mapping[str, object] | None = None,
        action_parameters: tuple[tuple[str, str], ...] = (),
    ):
        binding = _ExecutionAdmissionBinding(
            decision_id=_text(execution_decision_id, _EXECUTION_DECISION_ID),
            run_id=_text(execution_run_id, _EXECUTION_RUN_ID),
            attempt_id=_text(execution_attempt_id, _EXECUTION_ATTEMPT_ID),
            ticket_id=_text(execution_ticket_id, _EXECUTION_TICKET_ID),
        )
        ticket = self._execution_ticket(binding)

        if decision_payload is None:
            payload: dict[str, object] = {}
        elif isinstance(decision_payload, Mapping):
            payload = dict(decision_payload)
        else:
            raise TypeError("decision_payload must be a mapping or None")
        collision = _EXECUTION_FIELDS & set(payload)
        if collision:
            raise PaperCampaignAdmissionError(
                "decision_payload attempts to replace PAPER execution authority fields"
            )
        payload.update(
            {
                _EXECUTION_DECISION_ID: binding.decision_id,
                _EXECUTION_RUN_ID: binding.run_id,
                _EXECUTION_ATTEMPT_ID: binding.attempt_id,
                _EXECUTION_TICKET_ID: binding.ticket_id,
            }
        )

        if type(action_parameters) is not tuple:
            raise TypeError("action_parameters must be a canonical tuple")
        supplied_parameter_keys = {
            item[0]
            for item in action_parameters
            if type(item) is tuple and len(item) == 2 and type(item[0]) is str
        }
        if _EXECUTION_FIELDS & supplied_parameter_keys:
            raise PaperCampaignAdmissionError(
                "action_parameters attempt to replace PAPER execution authority fields"
            )
        bound_parameters = tuple(
            (*action_parameters,
             (_EXECUTION_DECISION_ID, binding.decision_id),
             (_EXECUTION_RUN_ID, binding.run_id),
             (_EXECUTION_ATTEMPT_ID, binding.attempt_id),
             (_EXECUTION_TICKET_ID, binding.ticket_id))
        )

        token = _ACTIVE_EXECUTION_BINDING.set(binding)
        try:
            return super().admit(
                admission_id=admission_id,
                observation=observation,
                action_type=action_type,
                decision_action=decision_action,
                decision_at=decision_at,
                at=at,
                legs=tuple(ticket.legs),
                stake=ticket.stake,
                placed_at=ticket.placed_at,
                replay_run_id=replay_run_id,
                agent=agent,
                strategy_reason=strategy_reason,
                decision_payload=payload,
                action_parameters=bound_parameters,
                provider_source_ids=ticket.provider_source_ids,
                provider_accounts=ticket.provider_accounts,
                bankroll_id=ticket.bankroll_id,
                currency=ticket.currency,
            )
        finally:
            _ACTIVE_EXECUTION_BINDING.reset(token)
