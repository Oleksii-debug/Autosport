from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from . import _paper_execution_reality_legacy as _impl
from .real_execution_ledger import ExecutionAction, ExecutionPlan


PaperExecutionRealityError = _impl.PaperExecutionRealityError
PaperExecutionIntegrityError = _impl.PaperExecutionIntegrityError
PaperExecutionStateError = _impl.PaperExecutionStateError
EvidenceGrade = _impl.EvidenceGrade
PaperAttemptOutcome = _impl.PaperAttemptOutcome
RecoveryDecision = _impl.RecoveryDecision
PaperExecutionModelConfig = _impl.PaperExecutionModelConfig
PaperExecutionEvidenceRecord = _impl.PaperExecutionEvidenceRecord
ObservedPaperExecution = _impl.ObservedPaperExecution
PaperLegAttempt = _impl.PaperLegAttempt
PaperExecutionRun = _impl.PaperExecutionRun
PaperExecutionEvidenceRegistry = _impl.PaperExecutionEvidenceRegistry


def _sha256_text(value: object, name: str) -> str:
    digest = _impl._text(value, name)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ValueError(
            f"{name} must be a lowercase 64-character SHA-256 digest"
        )
    return digest


def _decimal_coefficient(value: Decimal) -> tuple[int, int]:
    if not value.is_finite():
        raise ValueError("Decimal must be finite")
    parts = value.as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if parts.sign:
        coefficient = -coefficient
    return coefficient, int(parts.exponent)


def _decimal_from_coefficient(coefficient: int, exponent: int) -> Decimal:
    sign = 1 if coefficient < 0 else 0
    digits = tuple(int(ch) for ch in str(abs(coefficient)))
    return Decimal((sign, digits, exponent))


def _decimal_add_exact(left: Decimal, right: Decimal) -> Decimal:
    left_coefficient, left_exponent = _decimal_coefficient(left)
    right_coefficient, right_exponent = _decimal_coefficient(right)
    exponent = min(left_exponent, right_exponent)
    left_scaled = left_coefficient * (10 ** (left_exponent - exponent))
    right_scaled = right_coefficient * (10 ** (right_exponent - exponent))
    return _decimal_from_coefficient(left_scaled + right_scaled, exponent)


def _decimal_subtract_exact(left: Decimal, right: Decimal) -> Decimal:
    right_coefficient, right_exponent = _decimal_coefficient(right)
    return _decimal_add_exact(
        left,
        _decimal_from_coefficient(-right_coefficient, right_exponent),
    )


def _decimal_scale_bps_exact(value: Decimal, basis_points: int) -> Decimal:
    if type(basis_points) is not int or not 0 <= basis_points <= 10_000:
        raise ValueError("basis_points must be an int in 0..10000")
    coefficient, exponent = _decimal_coefficient(value)
    return _decimal_from_coefficient(coefficient * basis_points, exponent - 4)


@dataclass(frozen=True, slots=True)
class _DerivedRunEconomics:
    pending_action_ids: tuple[str, ...]
    recovery_decision: RecoveryDecision
    worst_case_exposure: Decimal
    can_complete: bool


def _derive_run_economics(
    action_ids: tuple[str, ...],
    attempts: tuple[PaperLegAttempt, ...],
) -> _DerivedRunEconomics:
    if len(attempts) > len(action_ids):
        raise PaperExecutionIntegrityError("durable attempts exceed reserved action list")

    known_exposure = Decimal("0")
    worst_case = Decimal("0")
    terminal_seen = False
    for index, attempt in enumerate(attempts):
        if attempt.sequence != index or attempt.action_id != action_ids[index]:
            raise PaperExecutionIntegrityError("durable attempts are not a reserved plan prefix")
        if terminal_seen:
            raise PaperExecutionIntegrityError(
                "durable attempts continue after a non-ACCEPTED terminal outcome"
            )

        if attempt.outcome in {
            PaperAttemptOutcome.ACCEPTED,
            PaperAttemptOutcome.PARTIAL,
        }:
            assert attempt.execution_stake is not None
            known_exposure = _decimal_add_exact(known_exposure, attempt.execution_stake)
            worst_case = max(worst_case, known_exposure)
        elif attempt.outcome is PaperAttemptOutcome.UNKNOWN:
            worst_case = max(
                worst_case,
                _decimal_add_exact(known_exposure, attempt.requested_stake),
            )

        if attempt.outcome is not PaperAttemptOutcome.ACCEPTED:
            terminal_seen = True

    pending = action_ids[len(attempts) :]
    all_accepted_complete = (
        bool(attempts)
        and len(attempts) == len(action_ids)
        and all(item.outcome is PaperAttemptOutcome.ACCEPTED for item in attempts)
    )
    can_complete = all_accepted_complete or (
        bool(attempts)
        and attempts[-1].outcome is not PaperAttemptOutcome.ACCEPTED
    )
    if all_accepted_complete:
        recovery = RecoveryDecision.NONE
    elif can_complete:
        recovery = (
            RecoveryDecision.HEDGE_REVIEW_REQUIRED
            if worst_case > 0
            else RecoveryDecision.NO_EXPOSURE
        )
    else:
        recovery = (
            RecoveryDecision.HEDGE_REVIEW_REQUIRED
            if worst_case > 0
            else RecoveryDecision.NONE
        )
    return _DerivedRunEconomics(
        pending_action_ids=pending,
        recovery_decision=recovery,
        worst_case_exposure=worst_case,
        can_complete=can_complete,
    )


class PaperExecutionLedger(_impl.PaperExecutionLedger):
    """PAPER ledger with mechanically derived completion economics."""

    _ALLOWED_EVENT_TYPES = frozenset(
        {
            "OBSERVATION_EVIDENCE_REGISTERED",
            "PAPER_EXPOSURE_SCOPE_BOUND",
            "RUN_RESERVED",
            "ATTEMPT_RECORDED",
            "RUN_COMPLETED",
        }
    )

    def _load_unlocked(self) -> list[dict[str, Any]]:
        events = super()._load_unlocked()
        for event in events:
            event_type = event.get("event_type")
            if event_type not in self._ALLOWED_EVENT_TYPES:
                raise PaperExecutionIntegrityError(
                    "PAPER execution ledger contains unsupported event_type"
                )
            try:
                run_id = _impl._text(event.get("run_id"), "ledger run_id")
            except (TypeError, ValueError) as exc:
                raise PaperExecutionIntegrityError(
                    "PAPER execution ledger run_id is invalid"
                ) from exc
            event_key = event.get("event_key")
            payload = event.get("payload")
            if event_type == "OBSERVATION_EVIDENCE_REGISTERED":
                if (
                    type(payload) is not dict
                    or set(payload)
                    != {"evidence_id", "evidence_sha256", "record"}
                ):
                    raise PaperExecutionIntegrityError(
                        "PAPER execution evidence payload schema is invalid"
                    )
                evidence_id = payload.get("evidence_id")
                record = PaperExecutionEvidenceRecord.from_dict(
                    payload.get("record")
                )
                if (
                    type(evidence_id) is not str
                    or not evidence_id
                    or run_id != evidence_id
                    or event_key != f"evidence:{evidence_id}"
                    or record.evidence_id != evidence_id
                    or payload.get("evidence_sha256")
                    != record.evidence_sha256
                ):
                    raise PaperExecutionIntegrityError(
                        "PAPER execution evidence event identity is invalid"
                    )
            elif event_type == "PAPER_EXPOSURE_SCOPE_BOUND":
                expected_scope_keys = {
                    "schema",
                    "schema_version",
                    "plan_id",
                    "plan_fingerprint",
                    "intent_evidence_sha256",
                    "bindings",
                    "binding_sha256",
                }
                if (
                    event_key != f"{run_id}:exposure-scope"
                    or type(payload) is not dict
                    or set(payload) != expected_scope_keys
                    or payload.get("schema")
                    != "autosport.paper_execution.exposure_scope_binding"
                    or payload.get("schema_version") != 1
                    or type(payload.get("bindings")) is not list
                ):
                    raise PaperExecutionIntegrityError(
                        "PAPER exposure-scope payload is invalid"
                    )
                binding_action_ids: list[str] = []
                for binding in payload["bindings"]:
                    if (
                        type(binding) is not dict
                        or set(binding)
                        != {
                            "action_id",
                            "sport",
                            "bankroll_id",
                            "currency",
                        }
                    ):
                        raise PaperExecutionIntegrityError(
                            "PAPER exposure-scope binding is invalid"
                        )
                    try:
                        action_id = _impl._text(
                            binding.get("action_id"),
                            "scope action_id",
                        )
                        sport = binding.get("sport")
                        if sport is not None:
                            _impl._text(sport, "scope sport")
                        bankroll_id = binding.get("bankroll_id")
                        currency = binding.get("currency")
                        if (bankroll_id is None) != (currency is None):
                            raise ValueError(
                                "scope bankroll/currency must be paired"
                            )
                        if bankroll_id is not None:
                            _impl._text(
                                bankroll_id,
                                "scope bankroll_id",
                            )
                            currency = _impl._text(
                                currency,
                                "scope currency",
                            )
                            if (
                                len(currency) != 3
                                or not currency.isascii()
                                or not currency.isalpha()
                                or currency != currency.upper()
                            ):
                                raise ValueError(
                                    "scope currency is noncanonical"
                                )
                    except (TypeError, ValueError) as exc:
                        raise PaperExecutionIntegrityError(
                            "PAPER exposure-scope binding is invalid"
                        ) from exc
                    binding_action_ids.append(action_id)
                if len(binding_action_ids) != len(
                    set(binding_action_ids)
                ):
                    raise PaperExecutionIntegrityError(
                        "PAPER exposure-scope bindings are not unique"
                    )
                try:
                    _impl._text(
                        payload.get("plan_id"),
                        "scope plan_id",
                    )
                    _sha256_text(
                        payload.get("plan_fingerprint"),
                        "scope plan_fingerprint",
                    )
                    _sha256_text(
                        payload.get("intent_evidence_sha256"),
                        "scope intent_evidence_sha256",
                    )
                except (TypeError, ValueError) as exc:
                    raise PaperExecutionIntegrityError(
                        "PAPER exposure-scope identity is invalid"
                    ) from exc
                scope_body = dict(payload)
                binding_sha256 = scope_body.pop("binding_sha256")
                if binding_sha256 != _impl._digest(scope_body):
                    raise PaperExecutionIntegrityError(
                        "PAPER exposure-scope digest is invalid"
                    )
            elif event_type == "RUN_RESERVED":
                base_reservation_keys = {
                    "trigger_id",
                    "plan_id",
                    "plan_fingerprint",
                    "model_fingerprint",
                    "started_at",
                    "action_ids",
                    "observation_evidence_ids",
                }
                allowed_reservation_keys = {
                    frozenset(base_reservation_keys),
                    frozenset(
                        base_reservation_keys | {"suspended_action_ids"}
                    ),
                }
                if (
                    event_key != f"{run_id}:reserve"
                    or type(payload) is not dict
                    or frozenset(payload) not in allowed_reservation_keys
                ):
                    raise PaperExecutionIntegrityError(
                        "PAPER reservation payload is invalid"
                    )
                action_ids = payload.get("action_ids")
                observation_ids = payload.get(
                    "observation_evidence_ids"
                )
                suspended_ids = payload.get("suspended_action_ids", [])
                if (
                    type(action_ids) is not list
                    or any(
                        type(action_id) is not str or not action_id
                        for action_id in action_ids
                    )
                    or len(action_ids) != len(set(action_ids))
                    or type(observation_ids) is not dict
                    or any(
                        type(action_id) is not str
                        or not action_id
                        or action_id not in action_ids
                        or type(evidence_id) is not str
                        or not evidence_id
                        for action_id, evidence_id
                        in observation_ids.items()
                    )
                    or type(suspended_ids) is not list
                    or any(
                        type(action_id) is not str
                        or not action_id
                        or action_id not in action_ids
                        for action_id in suspended_ids
                    )
                    or suspended_ids != sorted(suspended_ids)
                    or len(suspended_ids) != len(set(suspended_ids))
                    or set(observation_ids) & set(suspended_ids)
                ):
                    raise PaperExecutionIntegrityError(
                        "PAPER reservation execution inputs are invalid"
                    )
                try:
                    _impl._text(payload.get("trigger_id"), "trigger_id")
                    _impl._text(payload.get("plan_id"), "plan_id")
                    _sha256_text(
                        payload.get("plan_fingerprint"),
                        "plan_fingerprint",
                    )
                    _sha256_text(
                        payload.get("model_fingerprint"),
                        "model_fingerprint",
                    )
                    _impl._timestamp(
                        payload.get("started_at"),
                        "started_at",
                    )
                except (TypeError, ValueError) as exc:
                    raise PaperExecutionIntegrityError(
                        "PAPER reservation identity fields are invalid"
                    ) from exc
            elif event_type == "ATTEMPT_RECORDED":
                attempt = PaperLegAttempt.from_dict(payload)
                if (
                    attempt.run_id != run_id
                    or event_key
                    != f"{run_id}:attempt:{attempt.sequence}"
                ):
                    raise PaperExecutionIntegrityError(
                        "PAPER attempt event identity is invalid"
                    )
            elif event_type == "RUN_COMPLETED":
                if (
                    event_key != f"{run_id}:complete"
                    or type(payload) is not dict
                    or set(payload)
                    != {
                        "pending_action_ids",
                        "recovery_decision",
                        "worst_case_exposure",
                    }
                ):
                    raise PaperExecutionIntegrityError(
                        "PAPER completion payload is invalid"
                    )
                pending_ids = payload.get("pending_action_ids")
                if (
                    type(pending_ids) is not list
                    or any(
                        type(action_id) is not str or not action_id
                        for action_id in pending_ids
                    )
                    or len(pending_ids) != len(set(pending_ids))
                ):
                    raise PaperExecutionIntegrityError(
                        "PAPER completion pending actions are invalid"
                    )
                try:
                    RecoveryDecision(payload.get("recovery_decision"))
                    _impl._decimal(
                        payload.get("worst_case_exposure"),
                        "worst_case_exposure",
                        allow_zero=True,
                    )
                except (TypeError, ValueError, InvalidOperation) as exc:
                    raise PaperExecutionIntegrityError(
                        "PAPER completion economics are invalid"
                    ) from exc
        return events

    @staticmethod
    def _reservation_payload(
        *,
        trigger_id: str,
        plan: ExecutionPlan,
        config: PaperExecutionModelConfig,
        started_at: str,
        observation_evidence_ids: Mapping[str, str],
        suspended_action_ids: frozenset[str],
    ) -> dict[str, Any]:
        if type(suspended_action_ids) is not frozenset or any(
            type(action_id) is not str or not action_id
            for action_id in suspended_action_ids
        ):
            raise TypeError(
                "suspended_action_ids must be a frozenset of non-empty strings"
            )
        plan_action_ids = {action.action_id for action in plan.actions}
        if len(plan_action_ids) != len(plan.actions):
            raise PaperExecutionStateError(
                "execution plan action identities must be unique"
            )
        if not isinstance(observation_evidence_ids, Mapping):
            raise TypeError(
                "observation_evidence_ids must be a mapping"
            )
        observation_ids = dict(observation_evidence_ids)
        if any(
            type(action_id) is not str
            or not action_id
            or action_id not in plan_action_ids
            or type(evidence_id) is not str
            or not evidence_id
            for action_id, evidence_id in observation_ids.items()
        ):
            raise PaperExecutionStateError(
                "observation_evidence_ids contain invalid execution evidence"
            )
        if not suspended_action_ids.issubset(plan_action_ids):
            raise PaperExecutionStateError(
                "suspended_action_ids contain action outside execution plan"
            )
        if set(observation_ids) & suspended_action_ids:
            raise PaperExecutionStateError(
                "one execution action cannot be both observed and synthetically suspended"
            )
        payload: dict[str, Any] = {
            "trigger_id": trigger_id,
            "plan_id": plan.plan_id,
            "plan_fingerprint": plan.fingerprint,
            "model_fingerprint": config.fingerprint,
            "started_at": started_at,
            "action_ids": [action.action_id for action in plan.actions],
            "observation_evidence_ids": dict(
                sorted(observation_ids.items())
            ),
        }
        if suspended_action_ids:
            payload["suspended_action_ids"] = sorted(suspended_action_ids)
        return payload

    def reserve_run(
        self,
        *,
        run_id: str,
        trigger_id: str,
        plan: ExecutionPlan,
        config: PaperExecutionModelConfig,
        started_at: str,
        observation_evidence_ids: Mapping[str, str],
        suspended_action_ids: frozenset[str] = frozenset(),
    ) -> None:
        payload = self._reservation_payload(
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
            suspended_action_ids=suspended_action_ids,
        )
        self._append_event(
            event_type="RUN_RESERVED",
            run_id=run_id,
            key=f"{run_id}:reserve",
            payload=payload,
        )

    @staticmethod
    def _attempts_in_event_order(
        events: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], tuple[PaperLegAttempt, ...]]:
        attempt_events = [
            event
            for event in events
            if event["event_type"] == "ATTEMPT_RECORDED"
        ]
        attempts = tuple(
            PaperLegAttempt.from_dict(event["payload"])
            for event in attempt_events
        )
        if tuple(
            attempt.sequence for attempt in attempts
        ) != tuple(range(len(attempts))):
            raise PaperExecutionIntegrityError(
                "durable attempt events are not in canonical sequence order"
            )
        for event, attempt in zip(
            attempt_events,
            attempts,
            strict=True,
        ):
            event_run_id = event["run_id"]
            if (
                attempt.run_id != event_run_id
                or event["event_key"]
                != f"{event_run_id}:attempt:{attempt.sequence}"
            ):
                raise PaperExecutionIntegrityError(
                    "durable attempt event identity is invalid"
                )
        return attempt_events, attempts

    def _append_completion_unlocked(
        self,
        *,
        events: list[dict[str, Any]],
        run_id: str,
        payload: dict[str, Any],
    ) -> None:
        key = f"{run_id}:complete"
        by_key = {item["event_key"]: item for item in events}
        prior = by_key.get(key)
        sequence = len(events)
        previous_sha256 = None if not events else events[-1]["event_sha256"]
        event = self._event(
            event_type="RUN_COMPLETED",
            run_id=run_id,
            key=key,
            payload=payload,
            sequence=sequence,
            previous_sha256=previous_sha256,
        )
        if prior is not None:
            comparable = dict(prior)
            comparable.pop("sequence", None)
            comparable.pop("previous_sha256", None)
            comparable.pop("event_sha256", None)
            proposed = dict(event)
            proposed.pop("sequence", None)
            proposed.pop("previous_sha256", None)
            proposed.pop("event_sha256", None)
            if comparable != proposed:
                raise PaperExecutionIntegrityError(
                    "event_key already has different payload"
                )
            return

        encoded = _impl._canonical(event) + "\n"
        path_existed_before = self.path.exists()
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if not path_existed_before or not self._path_durable:
                self._sync_parent_directory()
            self._write_anchor_unlocked(events + [event])
        except OSError as exc:
            self._path_durable = False
            raise PaperExecutionIntegrityError(
                "PAPER execution ledger durability barrier failed"
            ) from exc
        self._path_durable = True

    def record_attempt(self, attempt: PaperLegAttempt) -> None:
        if not isinstance(attempt, PaperLegAttempt):
            raise TypeError("attempt must be PaperLegAttempt")

        def mutate() -> None:
            self._ensure_existing_path_durable()
            events = self._load_unlocked()
            run_events = [
                event
                for event in events
                if event["run_id"] == attempt.run_id
            ]
            reservations = [
                event
                for event in run_events
                if event["event_type"] == "RUN_RESERVED"
            ]
            if len(reservations) != 1:
                raise PaperExecutionStateError(
                    "attempt requires exactly one durable reservation"
                )
            _, existing_attempts = self._attempts_in_event_order(
                run_events
            )
            retry_existing = attempt.sequence < len(
                existing_attempts
            )
            if attempt.sequence > len(existing_attempts):
                raise PaperExecutionStateError(
                    "attempt sequence must extend the exact durable prefix"
                )
            reservation = reservations[0]["payload"]
            action_ids = reservation["action_ids"]
            if (
                attempt.sequence >= len(action_ids)
                or action_ids[attempt.sequence] != attempt.action_id
            ):
                raise PaperExecutionStateError(
                    "attempt does not extend reserved action order"
                )
            if (
                attempt.plan_id != reservation["plan_id"]
                or attempt.model_fingerprint
                != reservation["model_fingerprint"]
            ):
                raise PaperExecutionStateError(
                    "attempt conflicts with reserved plan/model identity"
                )
            observation_ids = reservation[
                "observation_evidence_ids"
            ]
            suspended_ids = frozenset(
                reservation.get("suspended_action_ids", [])
            )
            evidence_id = observation_ids.get(attempt.action_id)
            if evidence_id is None:
                if (
                    attempt.evidence_grade is not EvidenceGrade.SYNTHETIC
                    or attempt.evidence_id is not None
                    or attempt.evidence_sha256 is not None
                    or attempt.suspended
                    is not (attempt.action_id in suspended_ids)
                ):
                    raise PaperExecutionStateError(
                        "attempt conflicts with reserved synthetic authority"
                    )
            elif (
                attempt.evidence_grade is EvidenceGrade.SYNTHETIC
                or attempt.evidence_id != evidence_id
                or attempt.evidence_sha256 is None
            ):
                raise PaperExecutionStateError(
                    "attempt conflicts with reserved observed authority"
                )
            if retry_existing:
                if existing_attempts[attempt.sequence] != attempt:
                    raise PaperExecutionIntegrityError(
                        "durable attempt sequence already has different payload"
                    )
                return
            if any(
                event["event_type"] == "RUN_COMPLETED"
                for event in run_events
            ):
                raise PaperExecutionStateError(
                    "attempt cannot be appended after RUN_COMPLETED"
                )
            if (
                existing_attempts
                and existing_attempts[-1].outcome
                is not PaperAttemptOutcome.ACCEPTED
            ):
                raise PaperExecutionStateError(
                    "attempt cannot follow a terminal execution outcome"
                )

            key = f"{attempt.run_id}:attempt:{attempt.sequence}"
            sequence = len(events)
            previous_sha256 = (
                None if not events else events[-1]["event_sha256"]
            )
            event = self._event(
                event_type="ATTEMPT_RECORDED",
                run_id=attempt.run_id,
                key=key,
                payload=attempt.to_dict(),
                sequence=sequence,
                previous_sha256=previous_sha256,
            )
            encoded = _impl._canonical(event) + "\n"
            path_existed_before = self.path.exists()
            try:
                with self.path.open(
                    "a",
                    encoding="utf-8",
                    newline="\n",
                ) as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                if not path_existed_before or not self._path_durable:
                    self._sync_parent_directory()
                self._write_anchor_unlocked(events + [event])
            except OSError as exc:
                self._path_durable = False
                raise PaperExecutionIntegrityError(
                    "PAPER execution ledger durability barrier failed"
                ) from exc
            self._path_durable = True

        self._with_writer_lock(mutate)

    def complete_run(
        self,
        *,
        run_id: str,
        pending_action_ids: tuple[str, ...],
        recovery_decision: RecoveryDecision,
        worst_case_exposure: Decimal,
    ) -> None:
        run_id = _impl._text(run_id, "run_id")
        if not isinstance(recovery_decision, RecoveryDecision):
            raise TypeError("recovery_decision must be RecoveryDecision")
        supplied_exposure = _impl._decimal(
            worst_case_exposure,
            "worst_case_exposure",
            allow_zero=True,
        )

        def mutate() -> None:
            self._ensure_existing_path_durable()
            events = self._load_unlocked()
            run_events = [event for event in events if event["run_id"] == run_id]
            reservations = [
                event for event in run_events if event["event_type"] == "RUN_RESERVED"
            ]
            if len(reservations) != 1:
                raise PaperExecutionIntegrityError(
                    "completion requires exactly one durable reservation"
                )
            action_ids_raw = reservations[0]["payload"].get("action_ids")
            if (
                type(action_ids_raw) is not list
                or any(type(item) is not str or not item for item in action_ids_raw)
            ):
                raise PaperExecutionIntegrityError(
                    "durable reservation action_ids are invalid"
                )
            _, attempts = self._attempts_in_event_order(run_events)
            derived = _derive_run_economics(tuple(action_ids_raw), attempts)
            if not derived.can_complete:
                raise PaperExecutionStateError(
                    "run cannot complete before a terminal outcome or all actions ACCEPTED"
                )
            if tuple(pending_action_ids) != derived.pending_action_ids:
                raise PaperExecutionStateError(
                    "pending_action_ids conflict with durable attempt state"
                )
            if recovery_decision is not derived.recovery_decision:
                raise PaperExecutionStateError(
                    "recovery_decision conflicts with durable attempt economics"
                )
            if supplied_exposure != derived.worst_case_exposure:
                raise PaperExecutionStateError(
                    "worst_case_exposure conflicts with durable attempt economics"
                )
            payload = {
                "pending_action_ids": list(derived.pending_action_ids),
                "recovery_decision": derived.recovery_decision.value,
                "worst_case_exposure": _impl._decimal_text(
                    derived.worst_case_exposure
                ),
            }
            self._append_completion_unlocked(
                events=events,
                run_id=run_id,
                payload=payload,
            )

        self._with_writer_lock(mutate)

    def load_run(
        self,
        *,
        run_id: str,
        trigger_id: str,
        plan: ExecutionPlan,
        config: PaperExecutionModelConfig,
        started_at: str,
        observation_evidence_ids: Mapping[str, str],
        suspended_action_ids: frozenset[str] = frozenset(),
        evidence_registry: PaperExecutionEvidenceRegistry | None = None,
    ) -> PaperExecutionRun | None:
        events = self.events(run_id)
        if not events:
            return None
        reserve = [event for event in events if event["event_type"] == "RUN_RESERVED"]
        if len(reserve) != 1:
            raise PaperExecutionIntegrityError("run needs exactly one reservation")
        expected_reserve = self._reservation_payload(
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
            suspended_action_ids=suspended_action_ids,
        )
        reservation_event = reserve[0]
        if reservation_event["event_key"] != f"{run_id}:reserve":
            raise PaperExecutionIntegrityError(
                "durable reservation event identity is invalid"
            )
        if reservation_event["payload"] != expected_reserve:
            raise PaperExecutionStateError(
                "run identity conflicts with durable reservation"
            )

        scopes = [
            event
            for event in events
            if event["event_type"] == "PAPER_EXPOSURE_SCOPE_BOUND"
        ]
        if len(scopes) > 1:
            raise PaperExecutionIntegrityError(
                "run has multiple exposure-scope events"
            )
        if scopes:
            scope_event = scopes[0]
            scope_payload = scope_event["payload"]
            scope_bindings = scope_payload["bindings"]
            if (
                scope_event["event_key"] != f"{run_id}:exposure-scope"
                or scope_event["sequence"]
                >= reservation_event["sequence"]
            ):
                raise PaperExecutionIntegrityError(
                    "durable exposure scope chronology is invalid"
                )
            if (
                scope_payload["plan_id"] != plan.plan_id
                or scope_payload["plan_fingerprint"] != plan.fingerprint
                or [
                    binding["action_id"]
                    for binding in scope_bindings
                ]
                != [
                    action.action_id for action in plan.actions
                ]
            ):
                raise PaperExecutionIntegrityError(
                    "durable exposure scope conflicts with execution plan"
                )

        attempt_events, attempts = self._attempts_in_event_order(events)
        if any(
            event["sequence"] <= reservation_event["sequence"]
            for event in attempt_events
        ):
            raise PaperExecutionIntegrityError(
                "durable attempt appears before RUN_RESERVED"
            )
        derived = _derive_run_economics(
            tuple(action.action_id for action in plan.actions),
            attempts,
        )
        for index, attempt in enumerate(attempts):
            action = plan.actions[index]
            if (
                attempt.plan_id != plan.plan_id
                or attempt.model_fingerprint != config.fingerprint
                or attempt.bookmaker_id != action.bookmaker_id
                or attempt.account_id != action.account_id
                or attempt.event_id != action.event_id
                or attempt.market_id != action.market_id
                or attempt.selection_id != action.selection_id
                or attempt.side != action.side
                or attempt.decision_quote_id != action.quote_id
                or attempt.decision_odds != action.requested_odds
                or attempt.requested_stake != action.requested_stake
                or attempt.decision_observed_at != action.quote_observed_at
            ):
                raise PaperExecutionIntegrityError(
                    "durable attempt conflicts with execution plan/model"
                )
            evidence_id = observation_evidence_ids.get(action.action_id)
            if evidence_id is None:
                expected_attempt = _synthetic_attempt(
                    run_id=run_id,
                    plan=plan,
                    action=action,
                    sequence=index,
                    config=config,
                    started_at=started_at,
                    suspended=action.action_id in suspended_action_ids,
                )
                if attempt != expected_attempt:
                    raise PaperExecutionIntegrityError(
                        "durable synthetic attempt is not reproducible"
                    )
            else:
                if not isinstance(
                    evidence_registry,
                    PaperExecutionEvidenceRegistry,
                ):
                    raise PaperExecutionIntegrityError(
                        "durable observed attempt requires evidence registry"
                    )
                try:
                    record = evidence_registry.resolve(evidence_id)
                    observation = record.as_observation()
                    _impl._verify_observation_authority(
                        action=action,
                        observation=observation,
                        registry=evidence_registry,
                    )
                    expected_attempt = _impl._observed_attempt(
                        run_id=run_id,
                        plan=plan,
                        action=action,
                        sequence=index,
                        config=config,
                        observation=observation,
                        started_at=started_at,
                    )
                except (
                    PaperExecutionIntegrityError,
                    PaperExecutionStateError,
                    TypeError,
                    ValueError,
                ) as exc:
                    raise PaperExecutionIntegrityError(
                        "durable observed attempt evidence is invalid"
                    ) from exc
                if attempt != expected_attempt:
                    raise PaperExecutionIntegrityError(
                        "durable observed attempt is not reproducible"
                    )

        completions = [event for event in events if event["event_type"] == "RUN_COMPLETED"]
        if len(completions) > 1:
            raise PaperExecutionIntegrityError("run has multiple completion events")
        if completions:
            completion = completions[0]
            if (
                completion["event_key"] != f"{run_id}:complete"
                or completion["sequence"] <= reservation_event["sequence"]
            ):
                raise PaperExecutionIntegrityError(
                    "durable completion event identity/chronology is invalid"
                )
            if any(
                event["sequence"] > completion["sequence"] for event in attempt_events
            ):
                raise PaperExecutionIntegrityError(
                    "durable attempt appears after RUN_COMPLETED"
                )
            if not derived.can_complete:
                raise PaperExecutionIntegrityError(
                    "RUN_COMPLETED exists before durable state is terminal"
                )
            payload = completion["payload"]
            if set(payload) != {
                "pending_action_ids",
                "recovery_decision",
                "worst_case_exposure",
            }:
                raise PaperExecutionIntegrityError(
                    "completion payload schema is invalid"
                )
            try:
                recovery = RecoveryDecision(payload["recovery_decision"])
                pending = tuple(payload["pending_action_ids"])
                exposure = Decimal(payload["worst_case_exposure"])
            except (KeyError, ValueError, InvalidOperation, TypeError) as exc:
                raise PaperExecutionIntegrityError("invalid completion payload") from exc
            if (
                pending != derived.pending_action_ids
                or recovery is not derived.recovery_decision
                or exposure != derived.worst_case_exposure
            ):
                raise PaperExecutionIntegrityError(
                    "completion payload does not match durable attempt economics"
                )
            return PaperExecutionRun(
                run_id=run_id,
                trigger_id=trigger_id,
                plan_id=plan.plan_id,
                plan_fingerprint=plan.fingerprint,
                model_fingerprint=config.fingerprint,
                started_at=started_at,
                attempts=attempts,
                pending_action_ids=derived.pending_action_ids,
                recovery_decision=derived.recovery_decision,
                worst_case_exposure=derived.worst_case_exposure,
                completed=True,
            )

        return PaperExecutionRun(
            run_id=run_id,
            trigger_id=trigger_id,
            plan_id=plan.plan_id,
            plan_fingerprint=plan.fingerprint,
            model_fingerprint=config.fingerprint,
            started_at=started_at,
            attempts=attempts,
            pending_action_ids=derived.pending_action_ids,
            recovery_decision=derived.recovery_decision,
            worst_case_exposure=derived.worst_case_exposure,
            completed=False,
        )


def _synthetic_attempt(
    *,
    run_id: str,
    plan: ExecutionPlan,
    action: ExecutionAction,
    sequence: int,
    config: PaperExecutionModelConfig,
    started_at: str,
    suspended: bool,
) -> PaperLegAttempt:
    if action.side != "BACK":
        raise PaperExecutionStateError(
            "synthetic PAPER exposure model supports BACK only; non-BACK must use "
            "explicit empirical/configured execution evidence"
        )
    start = _impl._timestamp(started_at, "started_at")
    delay_span = config.max_delay_ms - config.min_delay_ms
    delay_ms = config.min_delay_ms
    if delay_span:
        delay_ms += _impl._deterministic_int(
            f"{config.seed}:{run_id}:{action.action_id}",
            "delay",
            delay_span + 1,
        )
    execution_time = start + _impl.timedelta(milliseconds=delay_ms)
    decision_time = _impl._timestamp(action.quote_observed_at, "quote_observed_at")
    quote_age_ms = _impl._milliseconds(execution_time - decision_time, "quote age")
    expires = _impl._timestamp(action.expires_at, "expires_at")
    execution_odds: Decimal | None = None
    execution_stake: Decimal | None = None

    if suspended:
        outcome = PaperAttemptOutcome.REJECTED
        reason = "configured/synthetic suspension at execution time"
    elif execution_time >= expires:
        outcome = PaperAttemptOutcome.REJECTED
        reason = "decision quote expired before PAPER-equivalent execution"
    elif quote_age_ms > config.max_quote_age_ms:
        outcome = PaperAttemptOutcome.REJECTED
        reason = "decision quote exceeded configured PAPER freshness bound"
    else:
        bucket = _impl._deterministic_int(
            f"{config.seed}:{run_id}:{action.action_id}",
            "outcome",
            10_000,
        )
        if bucket < config.unknown_bps:
            outcome = PaperAttemptOutcome.UNKNOWN
            reason = "deterministic execution model produced UNKNOWN"
        elif bucket < config.unknown_bps + config.rejected_bps:
            outcome = PaperAttemptOutcome.REJECTED
            reason = "deterministic execution model produced REJECTED"
        elif bucket < config.unknown_bps + config.rejected_bps + config.partial_bps:
            outcome = PaperAttemptOutcome.PARTIAL
            reason = "deterministic execution model produced PARTIAL"
        else:
            outcome = PaperAttemptOutcome.ACCEPTED
            reason = "deterministic execution model produced ACCEPTED"

        if outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}:
            slippage_bps = (
                0
                if config.max_slippage_bps == 0
                else _impl._deterministic_int(
                    f"{config.seed}:{run_id}:{action.action_id}",
                    "slippage",
                    config.max_slippage_bps + 1,
                )
            )
            odds_margin = _decimal_subtract_exact(
                action.requested_odds,
                Decimal("1"),
            )
            execution_odds = _decimal_add_exact(
                Decimal("1"),
                _decimal_scale_bps_exact(
                    odds_margin,
                    10_000 - slippage_bps,
                ),
            )
            execution_stake = action.requested_stake
            if outcome is PaperAttemptOutcome.PARTIAL:
                execution_stake = _decimal_scale_bps_exact(
                    action.requested_stake,
                    config.partial_fill_bps,
                )

    return PaperLegAttempt(
        attempt_id=_impl._attempt_id(run_id, action, sequence),
        run_id=run_id,
        plan_id=plan.plan_id,
        action_id=action.action_id,
        sequence=sequence,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        side=action.side,
        decision_quote_id=action.quote_id,
        decision_odds=action.requested_odds,
        requested_stake=action.requested_stake,
        decision_observed_at=action.quote_observed_at,
        execution_observed_at=_impl._timestamp_text(execution_time),
        delay_ms=delay_ms,
        quote_age_ms=quote_age_ms,
        outcome=outcome,
        execution_odds=execution_odds,
        execution_stake=execution_stake,
        suspended=suspended,
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source=config.evidence_source,
        evidence_id=None,
        evidence_sha256=None,
        model_fingerprint=config.fingerprint,
        reason=reason,
    )


def execute_paper_plan(
    *,
    plan: ExecutionPlan,
    trigger_id: str,
    config: PaperExecutionModelConfig,
    ledger: PaperExecutionLedger,
    started_at: str,
    observations: Mapping[str, ObservedPaperExecution] | None = None,
    evidence_registry: PaperExecutionEvidenceRegistry | None = None,
    suspended_action_ids: frozenset[str] = frozenset(),
) -> PaperExecutionRun:
    """Execute/resume one PAPER/SHADOW run without provider writes or real money."""
    if not isinstance(plan, ExecutionPlan):
        raise TypeError("plan must be ExecutionPlan")
    if not isinstance(config, PaperExecutionModelConfig):
        raise TypeError("config must be PaperExecutionModelConfig")
    if not isinstance(ledger, PaperExecutionLedger):
        raise TypeError("ledger must be PaperExecutionLedger")
    trigger_id = _impl._text(trigger_id, "trigger_id")
    _impl._timestamp(started_at, "started_at")
    if observations is None:
        observations = {}
    if not isinstance(observations, Mapping):
        raise TypeError("observations must be a mapping")
    action_by_id = {action.action_id: action for action in plan.actions}
    if set(observations) - set(action_by_id):
        raise PaperExecutionStateError("observations contain action outside execution plan")
    if type(suspended_action_ids) is not frozenset or any(
        type(action_id) is not str or not action_id
        for action_id in suspended_action_ids
    ):
        raise TypeError(
            "suspended_action_ids must be a frozenset of non-empty strings"
        )
    if set(suspended_action_ids) - set(action_by_id):
        raise PaperExecutionStateError(
            "suspended_action_ids contain action outside execution plan"
        )
    if set(observations) & set(suspended_action_ids):
        raise PaperExecutionStateError(
            "one execution action cannot be both observed and synthetically suspended"
        )
    if observations and not isinstance(
        evidence_registry,
        PaperExecutionEvidenceRegistry,
    ):
        raise PaperExecutionStateError(
            "configured/empirical observations require a durable evidence registry"
        )

    observation_evidence_ids: dict[str, str] = {}
    for action_id, observation in observations.items():
        assert evidence_registry is not None
        _impl._verify_observation_authority(
            action=action_by_id[action_id],
            observation=observation,
            registry=evidence_registry,
        )
        observation_evidence_ids[action_id] = observation.evidence_id

    run_id = _impl._run_id(plan, trigger_id, config)
    ledger.reserve_run(
        run_id=run_id,
        trigger_id=trigger_id,
        plan=plan,
        config=config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
        suspended_action_ids=suspended_action_ids,
    )
    existing = ledger.load_run(
        run_id=run_id,
        trigger_id=trigger_id,
        plan=plan,
        config=config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
        suspended_action_ids=suspended_action_ids,
        evidence_registry=evidence_registry,
    )
    assert existing is not None
    if existing.completed:
        return existing

    attempts = list(existing.attempts)
    if attempts and attempts[-1].outcome is not PaperAttemptOutcome.ACCEPTED:
        ledger.complete_run(
            run_id=run_id,
            pending_action_ids=existing.pending_action_ids,
            recovery_decision=(
                RecoveryDecision.HEDGE_REVIEW_REQUIRED
                if existing.worst_case_exposure > 0
                else RecoveryDecision.NO_EXPOSURE
            ),
            worst_case_exposure=existing.worst_case_exposure,
        )
        result = ledger.load_run(
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
            suspended_action_ids=suspended_action_ids,
            evidence_registry=evidence_registry,
        )
        assert result is not None
        return result

    known_exposure = Decimal("0")
    worst_case_exposure = Decimal("0")
    for prior in attempts:
        assert prior.outcome is PaperAttemptOutcome.ACCEPTED
        assert prior.execution_stake is not None
        known_exposure = _decimal_add_exact(
            known_exposure,
            prior.execution_stake,
        )
        worst_case_exposure = max(worst_case_exposure, known_exposure)

    for sequence in range(len(attempts), len(plan.actions)):
        action = plan.actions[sequence]
        if action.side != "BACK":
            raise PaperExecutionStateError(
                "PAPER execution-reality exposure model supports BACK only "
                "until canonical LAY liability authority exists"
            )
        observation = observations.get(action.action_id)
        if observation is not None:
            attempt = _impl._observed_attempt(
                run_id=run_id,
                plan=plan,
                action=action,
                sequence=sequence,
                config=config,
                observation=observation,
                started_at=started_at,
            )
        else:
            attempt = _synthetic_attempt(
                run_id=run_id,
                plan=plan,
                action=action,
                sequence=sequence,
                config=config,
                started_at=started_at,
                suspended=action.action_id in suspended_action_ids,
            )
        ledger.record_attempt(attempt)
        attempts.append(attempt)

        if attempt.outcome in {
            PaperAttemptOutcome.ACCEPTED,
            PaperAttemptOutcome.PARTIAL,
        }:
            assert attempt.execution_stake is not None
            known_exposure = _decimal_add_exact(
                known_exposure,
                attempt.execution_stake,
            )
            worst_case_exposure = max(worst_case_exposure, known_exposure)
        elif attempt.outcome is PaperAttemptOutcome.UNKNOWN:
            worst_case_exposure = max(
                worst_case_exposure,
                _decimal_add_exact(
                    known_exposure,
                    attempt.requested_stake,
                ),
            )

        if attempt.outcome is not PaperAttemptOutcome.ACCEPTED:
            pending = tuple(
                item.action_id for item in plan.actions[sequence + 1 :]
            )
            recovery = (
                RecoveryDecision.HEDGE_REVIEW_REQUIRED
                if worst_case_exposure > 0
                else RecoveryDecision.NO_EXPOSURE
            )
            ledger.complete_run(
                run_id=run_id,
                pending_action_ids=pending,
                recovery_decision=recovery,
                worst_case_exposure=worst_case_exposure,
            )
            result = ledger.load_run(
                run_id=run_id,
                trigger_id=trigger_id,
                plan=plan,
                config=config,
                started_at=started_at,
                observation_evidence_ids=observation_evidence_ids,
                suspended_action_ids=suspended_action_ids,
                evidence_registry=evidence_registry,
            )
            assert result is not None
            return result

    ledger.complete_run(
        run_id=run_id,
        pending_action_ids=(),
        recovery_decision=RecoveryDecision.NONE,
        worst_case_exposure=worst_case_exposure,
    )
    result = ledger.load_run(
        run_id=run_id,
        trigger_id=trigger_id,
        plan=plan,
        config=config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
        suspended_action_ids=suspended_action_ids,
        evidence_registry=evidence_registry,
    )
    assert result is not None
    return result


__all__ = [
    "EvidenceGrade",
    "ObservedPaperExecution",
    "PaperAttemptOutcome",
    "PaperExecutionEvidenceRecord",
    "PaperExecutionEvidenceRegistry",
    "PaperExecutionIntegrityError",
    "PaperExecutionLedger",
    "PaperExecutionModelConfig",
    "PaperExecutionRealityError",
    "PaperExecutionRun",
    "PaperExecutionStateError",
    "PaperLegAttempt",
    "RecoveryDecision",
    "execute_paper_plan",
]
