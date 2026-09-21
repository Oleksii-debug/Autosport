"""Post-#727 convergence for PAPER campaign admission.

#727 makes the exact durable DecisionLedger origin part of RUN_RESERVED. Campaign
learning additionally requires that same pre-execution origin to carry the exact
learning Observation issued before any PAPER attempt.  No admission-time surrogate
or second origin store is accepted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from . import paper_campaign_admission as _admission
from .decision_ledger import DecisionLedgerIntegrityError
from .learning_environment import (
    CausalLearningEnvironment,
    LearningEnvironmentError,
    Observation,
)
from .paper_execution_reality import (
    PaperExecutionIntegrityError,
    PaperExecutionStateError,
    PaperLegAttempt,
)

_CANONICAL_ORIGIN_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "decision_id",
        "record_sha256",
        "learning_observation",
    }
)
_CANONICAL_ORIGIN_SCHEMA = "autosport.paper_execution_decision_origin"
_CANONICAL_ORIGIN_SCHEMA_VERSION = 2
_LEARNING_OBSERVATION_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "environment_id",
        "observed_at",
        "available_at",
        "evidence",
        "observation_id",
    }
)
_LEARNING_OBSERVATION_SCHEMA = "autosport.paper_execution_learning_observation"
_LEARNING_OBSERVATION_SCHEMA_VERSION = 1
_CANONICAL_RESERVATION_FIELDS = frozenset(
    {
        "trigger_id",
        "plan_id",
        "plan_fingerprint",
        "model_fingerprint",
        "started_at",
        "action_ids",
        "observation_evidence_ids",
        "decision_origin",
    }
)
_LEGACY_ORIGIN_EVENT = "DECISION_ORIGIN_BOUND"



class _ResolvedExecutionDecisionId(str):
    """String-compatible exact authority returned to legacy ticket code."""

    def __new__(cls, decision_id: str, *, record_sha256: str, record, observation: Observation):
        value = str.__new__(cls, decision_id)
        value.record_sha256 = record_sha256
        value.record = record
        value.observation = observation
        value.decision_at = record.observed_ts
        return value


def _install() -> None:
    coordinator = _admission.PaperCampaignAdmissionCoordinator
    error = _admission.PaperCampaignAdmissionError
    text = _admission._text
    digest_record = coordinator._decision_record_sha256
    matches_live = coordinator._matches_live_execution_decision
    matches_legacy = coordinator._matches_legacy_execution_decision
    original_admit = coordinator.admit
    binding_type = _admission._ExecutionAdmissionBinding
    accepted_equivalent = _admission._ACCEPTED_EQUIVALENT
    observation_type = Observation
    environment_type = CausalLearningEnvironment
    paper_leg_from_dict = PaperLegAttempt.from_dict
    decision_integrity_error = DecisionLedgerIntegrityError
    execution_integrity_errors = (PaperExecutionIntegrityError, PaperExecutionStateError)
    sha_chars = frozenset("0123456789abcdef")
    reservation_fields = _CANONICAL_RESERVATION_FIELDS
    origin_fields = _CANONICAL_ORIGIN_FIELDS
    origin_schema = _CANONICAL_ORIGIN_SCHEMA
    origin_schema_version = _CANONICAL_ORIGIN_SCHEMA_VERSION
    learning_fields = _LEARNING_OBSERVATION_FIELDS
    learning_schema = _LEARNING_OBSERVATION_SCHEMA
    learning_schema_version = _LEARNING_OBSERVATION_SCHEMA_VERSION
    legacy_origin_event = _LEGACY_ORIGIN_EVENT
    resolved_type = _ResolvedExecutionDecisionId
    execution_decision_key = _admission._EXECUTION_DECISION_ID
    execution_run_key = _admission._EXECUTION_RUN_ID
    execution_attempt_key = _admission._EXECUTION_ATTEMPT_ID
    execution_ticket_key = _admission._EXECUTION_TICKET_ID

    def reservation_payload(event: object) -> Mapping[str, object]:
        if not isinstance(event, Mapping):
            raise error("PAPER execution reservation is invalid")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise error("PAPER execution reservation schema is invalid")
        if "decision_origin" not in payload:
            raise error(
                "PAPER pre-execution decision-origin is missing from canonical reservation"
            )
        if set(payload) != reservation_fields:
            raise error("PAPER execution reservation schema is invalid")
        for field in (
            "trigger_id",
            "plan_id",
            "plan_fingerprint",
            "model_fingerprint",
            "started_at",
        ):
            value = payload.get(field)
            if type(value) is not str or not value.strip():
                raise error(f"PAPER execution reservation {field} is invalid")
        action_ids = payload.get("action_ids")
        if (
            type(action_ids) is not list
            or not action_ids
            or any(type(item) is not str or not item.strip() for item in action_ids)
            or len(set(action_ids)) != len(action_ids)
        ):
            raise error("PAPER execution reservation action_ids are invalid")
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
            raise error("PAPER execution reservation observation evidence is invalid")
        origin = payload.get("decision_origin")
        if type(origin) is not dict or set(origin) != origin_fields:
            raise error("PAPER execution reservation decision_origin schema is invalid")
        if (
            origin.get("schema") != origin_schema
            or origin.get("schema_version") != origin_schema_version
        ):
            raise error(
                "PAPER execution origin lacks canonical pre-execution learning Observation"
            )
        decision_id = origin.get("decision_id")
        record_sha256 = origin.get("record_sha256")
        if (
            type(decision_id) is not str
            or not decision_id
            or decision_id.strip() != decision_id
        ):
            raise error("PAPER execution decision_origin decision_id is invalid")
        if (
            type(record_sha256) is not str
            or len(record_sha256) != 64
            or record_sha256.lower() != record_sha256
            or any(ch not in sha_chars for ch in record_sha256)
        ):
            raise error("PAPER execution decision_origin record_sha256 is invalid")
        raw_learning = origin.get("learning_observation")
        if type(raw_learning) is not dict or set(raw_learning) != learning_fields:
            raise error("PAPER pre-execution learning Observation schema is invalid")
        if (
            raw_learning.get("schema") != learning_schema
            or raw_learning.get("schema_version") != learning_schema_version
        ):
            raise error("unsupported PAPER pre-execution learning Observation schema")
        return payload

    def execution_attempt(self, *, run_id: str, attempt_id: str):
        run_id = text(run_id, "execution_run_id")
        attempt_id = text(attempt_id, "execution_attempt_id")
        try:
            events = self.execution_ledger.events(run_id)
        except execution_integrity_errors as exc:
            raise error("PAPER execution ledger cannot re-resolve admission authority") from exc
        if not events:
            raise error("admission execution run is missing from canonical ledger")
        if any(event.get("event_type") == legacy_origin_event for event in events):
            raise error("legacy standalone PAPER decision-origin event is not canonical")
        reservations = [event for event in events if event.get("event_type") == "RUN_RESERVED"]
        completions = [event for event in events if event.get("event_type") == "RUN_COMPLETED"]
        if len(reservations) != 1 or len(completions) != 1:
            raise error("admission requires one completed canonical PAPER execution run")
        reservation = reservation_payload(reservations[0])
        origin = reservation["decision_origin"]
        assert isinstance(origin, Mapping)
        attempts: list[PaperLegAttempt] = []
        try:
            for event in events:
                if event.get("event_type") == "ATTEMPT_RECORDED":
                    attempts.append(paper_leg_from_dict(event.get("payload")))
        except (PaperExecutionIntegrityError, ValueError, TypeError) as exc:
            raise error("admission execution attempt evidence is invalid") from exc
        matches = [attempt for attempt in attempts if attempt.attempt_id == attempt_id]
        if len(matches) != 1:
            raise error("admission execution attempt is missing or duplicated")
        attempt = matches[0]
        if attempt.run_id != run_id:
            raise error("admission execution attempt belongs to another run")
        action_ids = reservation["action_ids"]
        assert isinstance(action_ids, list)
        if attempt.sequence >= len(action_ids) or action_ids[attempt.sequence] != attempt.action_id:
            raise error("admission execution attempt is not bound to reserved action order")
        if attempt.outcome not in accepted_equivalent:
            raise error("REJECTED/UNKNOWN PAPER execution cannot enter campaign admission")
        if attempt.execution_odds is None or attempt.execution_stake is None:
            raise error("accepted PAPER execution lacks exact execution odds/stake")
        return attempt, reservation, origin

    def resolved_execution_decision_id(
        self,
        *,
        run_id: str,
        reservation: Mapping[str, object],
        origin: Mapping[str, object],
    ):
        try:
            durable_events = self.execution_ledger.events(run_id)
        except execution_integrity_errors as exc:
            raise error(
                "PAPER execution ledger cannot re-resolve reservation authority"
            ) from exc
        durable_reservations = [
            event
            for event in durable_events
            if event.get("event_type") == "RUN_RESERVED"
        ]
        if len(durable_reservations) != 1:
            raise error("PAPER execution requires one canonical durable reservation")
        canonical_reservation = reservation_payload(durable_reservations[0])
        if dict(reservation) != dict(canonical_reservation):
            raise error("PAPER reservation argument conflicts with canonical ledger")
        reservation = canonical_reservation
        canonical_origin = reservation["decision_origin"]
        assert isinstance(canonical_origin, Mapping)
        if dict(origin) != dict(canonical_origin):
            raise error("PAPER execution decision_origin is not reservation authority")
        origin = canonical_origin

        trigger_id = text(reservation.get("trigger_id"), "execution trigger_id")
        if origin is not reservation.get("decision_origin") and dict(origin) != reservation.get("decision_origin"):
            raise error("PAPER execution decision_origin is not reservation authority")
        if origin.get("decision_id") != trigger_id:
            raise error("PAPER pre-execution decision-origin identity conflicts with reservation")
        record_sha256 = origin.get("record_sha256")
        if type(record_sha256) is not str:
            raise error("PAPER pre-execution decision-origin digest is unavailable")
        try:
            records = self.decision_ledger.verified_records()
        except decision_integrity_error as exc:
            raise error("Decision Ledger cannot prove PAPER execution origin") from exc
        matches = [candidate for candidate in records if candidate.decision_id == trigger_id]
        if len(matches) != 1:
            raise error("PAPER execution must originate from one pre-existing durable decision")
        record = matches[0]
        if digest_record(record) != record_sha256:
            raise error("PAPER pre-execution decision-origin commitment no longer matches Decision Ledger")
        payload = record.payload
        if not isinstance(payload, Mapping):
            raise error("PAPER execution decision payload is invalid")
        live = matches_live(payload, decision_id=trigger_id, run_id=run_id, reservation=reservation)
        legacy = matches_legacy(payload, decision_id=trigger_id, run_id=run_id, reservation=reservation)
        if live == legacy:
            raise error("PAPER execution decision lacks one canonical execution authority")

        raw_learning = origin.get("learning_observation")
        if not isinstance(raw_learning, Mapping) or set(raw_learning) != learning_fields:
            raise error("PAPER pre-execution learning Observation is unavailable")

        source_learning = payload.get("learning_observation")
        if not isinstance(source_learning, Mapping) or set(source_learning) != learning_fields:
            raise error(
                "DecisionRecord lacks the product-issued pre-execution learning Observation"
            )

        def rebuild_observation(
            raw: Mapping[str, object],
            source_name: str,
        ) -> Observation:
            evidence = raw.get("evidence")
            if not isinstance(evidence, (list, tuple)):
                raise error(f"{source_name} learning Observation evidence is invalid")
            normalized: list[tuple[str, str]] = []
            for item in evidence:
                if (
                    not isinstance(item, (list, tuple))
                    or len(item) != 2
                    or type(item[0]) is not str
                    or type(item[1]) is not str
                ):
                    raise error(
                        f"{source_name} learning Observation evidence is invalid"
                    )
                normalized.append((item[0], item[1]))
            try:
                rebuilt = observation_type(
                    environment_id=raw.get("environment_id"),
                    observed_at=raw.get("observed_at"),
                    available_at=raw.get("available_at"),
                    evidence=tuple(normalized),
                )
            except (LearningEnvironmentError, TypeError, ValueError) as exc:
                raise error(f"{source_name} learning Observation is invalid") from exc
            if raw.get("observation_id") != rebuilt.observation_id:
                raise error(f"{source_name} learning Observation identity changed")
            return rebuilt

        observation = rebuild_observation(raw_learning, "PAPER origin")
        source_observation = rebuild_observation(source_learning, "DecisionRecord")
        if observation != source_observation:
            raise error(
                "PAPER learning Observation conflicts with DecisionRecord commitment"
            )

        environment = self.runtime.environment
        if type(environment) is not environment_type:
            raise error("PAPER campaign learning environment must be canonical")
        if observation.environment_id != environment.environment_id:
            raise error(
                "PAPER pre-execution learning Observation belongs to another environment"
            )

        try:
            available_time = datetime.fromisoformat(
                observation.available_at.replace("Z", "+00:00")
            )
            decision_time = datetime.fromisoformat(
                record.observed_ts.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise error(
                "PAPER pre-execution learning Observation time is invalid"
            ) from exc
        if (
            available_time.tzinfo is None
            or available_time.utcoffset() is None
            or decision_time.tzinfo is None
            or decision_time.utcoffset() is None
        ):
            raise error("PAPER pre-execution learning Observation time is invalid")
        if (
            available_time.astimezone(timezone.utc)
            > decision_time.astimezone(timezone.utc)
        ):
            raise error(
                "PAPER pre-execution learning Observation was unavailable at decision time"
            )

        return resolved_type(
            trigger_id,
            record_sha256=record_sha256,
            record=record,
            observation=observation,
        )

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
        binding = binding_type(
            decision_id=text(execution_decision_id, execution_decision_key),
            run_id=text(execution_run_id, execution_run_key),
            attempt_id=text(execution_attempt_id, execution_attempt_key),
            ticket_id=text(execution_ticket_id, execution_ticket_key),
        )
        _, reservation, origin = self._execution_attempt(
            run_id=binding.run_id,
            attempt_id=binding.attempt_id,
        )
        authority = self._resolved_execution_decision_id(
            run_id=binding.run_id,
            reservation=reservation,
            origin=origin,
        )
        if binding.decision_id != authority:
            raise error("caller execution_decision_id conflicts with durable execution origin")
        canonical_observation = authority.observation
        canonical_decision_at = authority.decision_at
        if observation != canonical_observation:
            raise error("caller observation conflicts with durable pre-execution decision evidence")
        if decision_at != canonical_decision_at:
            raise error("caller decision_at conflicts with durable pre-execution decision time")
        return original_admit(
            self,
            admission_id=admission_id,
            observation=canonical_observation,
            action_type=action_type,
            decision_action=decision_action,
            decision_at=canonical_decision_at,
            at=at,
            replay_run_id=replay_run_id,
            agent=agent,
            execution_decision_id=execution_decision_id,
            execution_run_id=execution_run_id,
            execution_attempt_id=execution_attempt_id,
            execution_ticket_id=execution_ticket_id,
            strategy_reason=strategy_reason,
            decision_payload=decision_payload,
            action_parameters=action_parameters,
        )

    coordinator._reservation_payload = staticmethod(reservation_payload)
    coordinator._execution_attempt = execution_attempt
    coordinator._resolved_execution_decision_id = resolved_execution_decision_id
    coordinator.admit = admit
    _admission._RESERVATION_FIELDS = reservation_fields


_install()
