"""Crash-safe PAPER campaign admission bound to canonical #623/#646 execution truth.

The durable PREPARED -> COMMITTED journal implementation remains in the private base
module.  This public facade closes the post-#646 composition seam: admission may
consume only an already-materialized PaperBook ticket whose exact ACCEPTED/PARTIAL
execution attempt is present in the canonical PaperExecutionLedger.  It never opens
or fabricates PAPER exposure itself.
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import _paper_campaign_admission_base as _base
from .decision_ledger import DecisionLedgerIntegrityError, JsonlDecisionLedger
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
_RESERVATION_FIELDS = frozenset(
    {
        "trigger_id",
        "plan_id",
        "plan_fingerprint",
        "model_fingerprint",
        "started_at",
        "action_ids",
        "observation_evidence_ids",
    }
)
_DECISION_ORIGIN_EVENT = "DECISION_ORIGIN_BOUND"
_DECISION_ORIGIN_FIELDS = frozenset(
    {"decision_id", "decision_record_sha256", "decision_record_ordinal"}
)
_LIVE_DECISION_SCHEMA = "autosport.persistent_live_decision"
_LIVE_DECISION_SCHEMA_VERSION = 2
_EXECUTION_ADOPTION_SCHEMA = "autosport.paper_execution_adoption"
_EXECUTION_ADOPTION_SCHEMA_VERSION = 1
_LEGACY_EXECUTION_AUTHORITY_SCHEMA = "autosport.paper_value.execution_authority"
_LEGACY_EXECUTION_AUTHORITY_SCHEMA_VERSION = 1


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
        # These are capability boundaries, not duck-typing boundaries.  A subclass
        # could override verified_records() or events() and synthesize authority
        # without consulting the canonical durable ledger bytes.
        if type(decision_ledger) is not JsonlDecisionLedger:
            raise TypeError("decision_ledger must be exact JsonlDecisionLedger")
        if type(execution_ledger) is not PaperExecutionLedger:
            raise TypeError("execution_ledger must be exact PaperExecutionLedger")
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

    @staticmethod
    def _reservation_payload(event: object) -> Mapping[str, object]:
        if not isinstance(event, Mapping):
            raise PaperCampaignAdmissionError("PAPER execution reservation is invalid")
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or set(payload) != _RESERVATION_FIELDS:
            raise PaperCampaignAdmissionError(
                "PAPER execution reservation schema is invalid"
            )
        for field in (
            "trigger_id",
            "plan_id",
            "plan_fingerprint",
            "model_fingerprint",
            "started_at",
        ):
            value = payload.get(field)
            if type(value) is not str or not value.strip():
                raise PaperCampaignAdmissionError(
                    f"PAPER execution reservation {field} is invalid"
                )
        action_ids = payload.get("action_ids")
        if (
            type(action_ids) is not list
            or not action_ids
            or any(type(item) is not str or not item.strip() for item in action_ids)
            or len(set(action_ids)) != len(action_ids)
        ):
            raise PaperCampaignAdmissionError(
                "PAPER execution reservation action_ids are invalid"
            )
        evidence = payload.get("observation_evidence_ids")
        if (
            type(evidence) is not dict
            or set(evidence) != set(action_ids)
            or any(
                type(key) is not str
                or type(value) is not str
                or not value.strip()
                for key, value in evidence.items()
            )
        ):
            raise PaperCampaignAdmissionError(
                "PAPER execution reservation observation evidence is invalid"
            )
        return payload

    @staticmethod
    def _decision_origin_payload(event: object) -> Mapping[str, object]:
        if not isinstance(event, Mapping):
            raise PaperCampaignAdmissionError("PAPER decision-origin event is invalid")
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or set(payload) != _DECISION_ORIGIN_FIELDS:
            raise PaperCampaignAdmissionError(
                "PAPER decision-origin payload schema is invalid"
            )
        decision_id = payload.get("decision_id")
        digest = payload.get("decision_record_sha256")
        ordinal = payload.get("decision_record_ordinal")
        if type(decision_id) is not str or not decision_id or decision_id.strip() != decision_id:
            raise PaperCampaignAdmissionError("PAPER decision-origin decision_id is invalid")
        if (
            type(digest) is not str
            or len(digest) != 64
            or digest.lower() != digest
            or any(ch not in "0123456789abcdef" for ch in digest)
        ):
            raise PaperCampaignAdmissionError("PAPER decision-origin digest is invalid")
        if type(ordinal) is not int or ordinal < 0:
            raise PaperCampaignAdmissionError("PAPER decision-origin ordinal is invalid")
        return payload

    @staticmethod
    def _decision_record_sha256(record) -> str:
        payload = json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _matches_live_execution_decision(
        record_payload: Mapping[str, object],
        *,
        decision_id: str,
        run_id: str,
        reservation: Mapping[str, object],
    ) -> bool:
        if (
            record_payload.get("schema") != _LIVE_DECISION_SCHEMA
            or record_payload.get("schema_version") != _LIVE_DECISION_SCHEMA_VERSION
            or record_payload.get("material_action_id") != decision_id
        ):
            return False
        adoption = record_payload.get("paper_execution")
        if not isinstance(adoption, Mapping):
            return False
        return (
            adoption.get("schema") == _EXECUTION_ADOPTION_SCHEMA
            and adoption.get("schema_version") == _EXECUTION_ADOPTION_SCHEMA_VERSION
            and adoption.get("plan_id") == reservation["plan_id"]
            and adoption.get("plan_fingerprint") == reservation["plan_fingerprint"]
            and adoption.get("model_fingerprint") == reservation["model_fingerprint"]
            and adoption.get("run_id") == run_id
            and type(adoption.get("intent_evidence_json")) is str
            and bool(str(adoption.get("intent_evidence_json")).strip())
        )

    @staticmethod
    def _matches_legacy_execution_decision(
        record_payload: Mapping[str, object],
        *,
        decision_id: str,
        run_id: str,
        reservation: Mapping[str, object],
    ) -> bool:
        if record_payload.get("schema") == _LIVE_DECISION_SCHEMA:
            return False
        if (
            record_payload.get("material_action_id") != decision_id
            or record_payload.get("execution_plan_id") != reservation["plan_id"]
            or record_payload.get("execution_plan_fingerprint")
            != reservation["plan_fingerprint"]
            or record_payload.get("execution_run_id") != run_id
        ):
            return False
        raw_authority = record_payload.get("execution_authority_json")
        if type(raw_authority) is not str or not raw_authority.strip():
            return False
        try:
            authority = json.loads(raw_authority)
        except (json.JSONDecodeError, TypeError):
            return False
        return (
            type(authority) is dict
            and authority.get("schema") == _LEGACY_EXECUTION_AUTHORITY_SCHEMA
            and authority.get("schema_version")
            == _LEGACY_EXECUTION_AUTHORITY_SCHEMA_VERSION
            and authority.get("decision_id") == decision_id
        )

    def _resolved_execution_decision_id(
        self,
        *,
        run_id: str,
        reservation: Mapping[str, object],
        origin: Mapping[str, object],
    ) -> str:
        trigger_id = _text(reservation.get("trigger_id"), "execution trigger_id")
        try:
            records = self.decision_ledger.verified_records()
        except DecisionLedgerIntegrityError as exc:
            raise PaperCampaignAdmissionError(
                "Decision Ledger cannot prove PAPER execution origin"
            ) from exc
        if origin.get("decision_id") != trigger_id:
            raise PaperCampaignAdmissionError(
                "PAPER pre-execution decision-origin identity conflicts with reservation"
            )
        ordinal = origin.get("decision_record_ordinal")
        if type(ordinal) is not int or ordinal < 0 or ordinal >= len(records):
            raise PaperCampaignAdmissionError(
                "PAPER pre-execution decision-origin record is unavailable"
            )
        record = records[ordinal]
        if (
            record.decision_id != trigger_id
            or self._decision_record_sha256(record) != origin.get("decision_record_sha256")
        ):
            raise PaperCampaignAdmissionError(
                "PAPER pre-execution decision-origin commitment no longer matches Decision Ledger"
            )
        matches = [candidate for candidate in records if candidate.decision_id == trigger_id]
        if len(matches) != 1:
            raise PaperCampaignAdmissionError(
                "PAPER execution must originate from one pre-existing durable decision"
            )
        payload = record.payload
        if not isinstance(payload, Mapping):
            raise PaperCampaignAdmissionError(
                "PAPER execution decision payload is invalid"
            )
        live = self._matches_live_execution_decision(
            payload,
            decision_id=trigger_id,
            run_id=run_id,
            reservation=reservation,
        )
        legacy = self._matches_legacy_execution_decision(
            payload,
            decision_id=trigger_id,
            run_id=run_id,
            reservation=reservation,
        )
        if live == legacy:
            raise PaperCampaignAdmissionError(
                "PAPER execution decision lacks one canonical execution authority"
            )
        return trigger_id

    def _execution_attempt(
        self,
        *,
        run_id: str,
        attempt_id: str,
    ) -> tuple[PaperLegAttempt, Mapping[str, object], Mapping[str, object]]:
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
        origins = [
            event for event in events if event.get("event_type") == _DECISION_ORIGIN_EVENT
        ]
        reservations = [event for event in events if event.get("event_type") == "RUN_RESERVED"]
        completions = [event for event in events if event.get("event_type") == "RUN_COMPLETED"]
        if len(origins) != 1:
            raise PaperCampaignAdmissionError(
                "admission requires one pre-execution decision-origin commitment"
            )
        if len(reservations) != 1 or len(completions) != 1:
            raise PaperCampaignAdmissionError(
                "admission requires one completed canonical PAPER execution run"
            )
        origin_event = origins[0]
        reservation_event = reservations[0]
        if (
            type(origin_event.get("sequence")) is not int
            or type(reservation_event.get("sequence")) is not int
            or origin_event["sequence"] >= reservation_event["sequence"]
        ):
            raise PaperCampaignAdmissionError(
                "PAPER decision origin was not committed before execution reservation"
            )
        origin = self._decision_origin_payload(origin_event)
        reservation = self._reservation_payload(reservation_event)
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
        action_ids = reservation["action_ids"]
        assert isinstance(action_ids, list)
        if (
            attempt.sequence >= len(action_ids)
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
        return attempt, reservation, origin

    def _execution_ticket(
        self,
        binding: _ExecutionAdmissionBinding,
    ) -> PaperTicket:
        attempt, reservation, origin = self._execution_attempt(
            run_id=binding.run_id,
            attempt_id=binding.attempt_id,
        )
        resolved_decision_id = self._resolved_execution_decision_id(
            run_id=binding.run_id,
            reservation=reservation,
            origin=origin,
        )
        if binding.decision_id != resolved_decision_id:
            raise PaperCampaignAdmissionError(
                "caller execution_decision_id conflicts with durable execution origin"
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
            f"decision_id={resolved_decision_id}; "
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
