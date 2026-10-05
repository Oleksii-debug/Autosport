from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from . import _paper_execution_reality_legacy as _impl
from .exchange_exposure import locked_capital_for_exchange_side
from .real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    _validate_decimal_text_resource_bound,
)


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
_LegacyPaperExecutionEvidenceRegistry = _impl.PaperExecutionEvidenceRegistry
_LEGACY_LEDGER_REGISTER_OBSERVATION_EVIDENCE = (
    _impl.PaperExecutionLedger.register_observation_evidence
)
_LEGACY_LEDGER_RESOLVE_OBSERVATION_EVIDENCE = (
    _impl.PaperExecutionLedger.resolve_observation_evidence
)


def _decimal_coefficient(value: Decimal) -> tuple[int, int]:
    if type(value) is not Decimal:
        raise ValueError("Decimal must be an exact canonical Decimal")
    if not value.is_finite():
        raise ValueError("Decimal must be finite")
    _validate_decimal_text_resource_bound(value)
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
    value = Decimal((sign, digits, exponent))
    _validate_decimal_text_resource_bound(value)
    return value


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


def _attempt_locked_capital(attempt: PaperLegAttempt) -> Decimal:
    if attempt.outcome not in {
        PaperAttemptOutcome.ACCEPTED,
        PaperAttemptOutcome.PARTIAL,
    }:
        raise PaperExecutionIntegrityError(
            "locked capital requires ACCEPTED or PARTIAL execution truth"
        )
    if attempt.execution_stake is None or attempt.execution_odds is None:
        raise PaperExecutionIntegrityError(
            "accepted/partial attempt is missing execution stake or odds"
        )
    try:
        return locked_capital_for_exchange_side(
            stake=attempt.execution_stake,
            odds=attempt.execution_odds,
            exchange_side=attempt.side,
        )
    except (TypeError, ValueError) as exc:
        raise PaperExecutionIntegrityError(
            "accepted/partial attempt has invalid exchange exposure economics"
        ) from exc


def _unknown_exposure_increment(attempt: PaperLegAttempt) -> Decimal:
    if attempt.side == "BACK":
        return attempt.requested_stake
    if attempt.side == "LAY":
        raise PaperExecutionStateError(
            "UNKNOWN LAY exposure has no canonical upper-odds liability bound"
        )
    raise PaperExecutionIntegrityError(
        "durable attempt has noncanonical exchange side"
    )


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
        if type(attempt.side) is not str or attempt.side not in {"BACK", "LAY"}:
            raise PaperExecutionIntegrityError(
                "durable attempt has noncanonical exchange side"
            )
        if terminal_seen:
            raise PaperExecutionIntegrityError(
                "durable attempts continue after a non-ACCEPTED terminal outcome"
            )

        try:
            if attempt.outcome in {
                PaperAttemptOutcome.ACCEPTED,
                PaperAttemptOutcome.PARTIAL,
            }:
                known_exposure = _decimal_add_exact(
                    known_exposure,
                    _attempt_locked_capital(attempt),
                )
                worst_case = max(worst_case, known_exposure)
            elif attempt.outcome is PaperAttemptOutcome.UNKNOWN:
                worst_case = max(
                    worst_case,
                    _decimal_add_exact(
                        known_exposure,
                        _unknown_exposure_increment(attempt),
                    ),
                )
        except ValueError as exc:
            raise PaperExecutionIntegrityError(
                "durable attempt exposure arithmetic exceeds canonical resource bounds"
            ) from exc

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



def _canonical_observation_evidence_ids(
    observation_evidence_ids: Mapping[str, str],
    *,
    action_ids: tuple[str, ...],
) -> dict[str, str]:
    if type(observation_evidence_ids) is not dict:
        raise TypeError("observation_evidence_ids must be an exact dict[str, str]")
    canonical: dict[str, str] = {}
    for action_id, evidence_id in observation_evidence_ids.items():
        if type(action_id) is not str or not action_id or action_id.strip() != action_id:
            raise TypeError("observation evidence action ids must be canonical strings")
        if type(evidence_id) is not str or not evidence_id or evidence_id.strip() != evidence_id:
            raise TypeError("observation evidence ids must be canonical strings")
        canonical[action_id] = evidence_id
    if set(canonical) - set(action_ids):
        raise PaperExecutionStateError(
            "observation evidence contains action outside execution plan"
        )
    return dict(sorted(canonical.items()))


def _canonical_run_reservation_inputs(
    *,
    run_id: str,
    trigger_id: str,
    plan: ExecutionPlan,
    config: PaperExecutionModelConfig,
    started_at: str,
    observation_evidence_ids: Mapping[str, str],
) -> tuple[str, str, tuple[str, ...], dict[str, str], str, str, str]:
    plan = _snapshot_execution_plan(plan)
    config = _snapshot_execution_config(config)
    run_id = _impl._text(run_id, "run_id")
    trigger_id = _impl._text(trigger_id, "trigger_id")
    _impl._timestamp(started_at, "started_at")
    canonical_run_id = _impl._run_id(plan, trigger_id, config)
    if run_id != canonical_run_id:
        raise PaperExecutionStateError(
            "run_id does not match canonical plan/trigger/config identity"
        )
    # Snapshot every reservation identity derived from caller-owned frozen
    # objects before consulting any caller-controlled evidence mapping. A
    # Mapping can execute arbitrary code while iterating, including mutating a
    # frozen dataclass through object.__setattr__. Durable state must therefore
    # never re-read plan/config after evidence canonicalization begins.
    plan_id = plan.plan_id
    plan_fingerprint = plan.fingerprint
    model_fingerprint = config.fingerprint
    action_ids = tuple(action.action_id for action in plan.actions)
    evidence_ids = _canonical_observation_evidence_ids(
        observation_evidence_ids,
        action_ids=action_ids,
    )
    return (
        run_id,
        trigger_id,
        action_ids,
        evidence_ids,
        plan_id,
        plan_fingerprint,
        model_fingerprint,
    )


def _require_durable_attempt_evidence_binding(
    *,
    events: list[dict[str, Any]],
    reservation_payload: dict[str, Any],
    attempt: PaperLegAttempt,
) -> None:
    raw_ids = reservation_payload.get("observation_evidence_ids")
    if type(raw_ids) is not dict:
        raise PaperExecutionIntegrityError(
            "durable reservation observation evidence map is invalid"
        )
    for action_id, evidence_id in raw_ids.items():
        if (
            type(action_id) is not str
            or not action_id
            or action_id.strip() != action_id
            or type(evidence_id) is not str
            or not evidence_id
            or evidence_id.strip() != evidence_id
        ):
            raise PaperExecutionIntegrityError(
                "durable reservation observation evidence identity is invalid"
            )

    reserved_evidence_id = raw_ids.get(attempt.action_id)
    if attempt.evidence_grade is EvidenceGrade.SYNTHETIC:
        if reserved_evidence_id is not None:
            raise PaperExecutionStateError(
                "synthetic attempt conflicts with reserved observation evidence"
            )
        return

    if reserved_evidence_id is None or attempt.evidence_id != reserved_evidence_id:
        raise PaperExecutionStateError(
            "attempt evidence identity is not authorized by durable reservation"
        )
    matches = [
        event
        for event in events
        if event["event_type"] == "OBSERVATION_EVIDENCE_REGISTERED"
        and event["payload"].get("evidence_id") == reserved_evidence_id
    ]
    if len(matches) != 1:
        raise PaperExecutionStateError(
            "attempt evidence is not uniquely registered in durable ledger"
        )
    payload = matches[0]["payload"]
    if set(payload) != {"evidence_id", "evidence_sha256", "record"}:
        raise PaperExecutionIntegrityError(
            "registered evidence payload schema is invalid"
        )
    try:
        record = PaperExecutionEvidenceRecord.from_dict(payload["record"])
    except (PaperExecutionIntegrityError, TypeError, ValueError) as exc:
        raise PaperExecutionIntegrityError(
            "registered attempt evidence record is invalid"
        ) from exc
    if (
        payload["evidence_id"] != record.evidence_id
        or payload["evidence_sha256"] != record.evidence_sha256
    ):
        raise PaperExecutionIntegrityError(
            "registered attempt evidence identity is inconsistent"
        )
    if (
        attempt.evidence_sha256 != record.evidence_sha256
        or attempt.action_id != record.action_id
        or attempt.bookmaker_id != record.bookmaker_id
        or attempt.account_id != record.account_id
        or attempt.event_id != record.event_id
        or attempt.market_id != record.market_id
        or attempt.selection_id != record.selection_id
        or attempt.side != record.side
        or attempt.decision_quote_id != record.quote_id
        or attempt.outcome is not record.outcome
        or attempt.execution_observed_at != record.observed_at
        or attempt.evidence_grade is not record.evidence_grade
        or attempt.evidence_source != record.evidence_source
        or attempt.execution_odds != record.accepted_odds
        or attempt.execution_stake != record.accepted_stake
        or attempt.suspended is not record.suspended
        or attempt.reason != record.reason
    ):
        raise PaperExecutionStateError(
            "attempt execution truth does not match durable observation evidence"
        )


class PaperExecutionLedger(_impl.PaperExecutionLedger):
    """PAPER ledger with mechanically derived completion economics."""

    def register_observation_evidence(
        self,
        record: PaperExecutionEvidenceRecord,
    ) -> None:
        if type(self) is not PaperExecutionLedger:
            raise TypeError("ledger must be exact PaperExecutionLedger")
        _impl._require_canonical_evidence_record_surface(record)
        try:
            canonical_record = PaperExecutionEvidenceRecord.from_dict(
                record.to_dict()
            )
        except (PaperExecutionIntegrityError, TypeError, ValueError) as exc:
            raise PaperExecutionIntegrityError(
                "evidence record no longer satisfies canonical value invariants"
            ) from exc
        if canonical_record != record:
            raise PaperExecutionIntegrityError(
                "evidence record changed outside canonical construction authority"
            )
        # Persist the reconstructed snapshot, never the caller-owned object that
        # may be mutated after validation.
        _LEGACY_LEDGER_REGISTER_OBSERVATION_EVIDENCE(self, canonical_record)

    def resolve_observation_evidence(
        self,
        evidence_id: str,
    ) -> PaperExecutionEvidenceRecord:
        if type(self) is not PaperExecutionLedger:
            raise TypeError("ledger must be exact PaperExecutionLedger")
        evidence_id = _impl._text(evidence_id, "evidence_id")
        return _LEGACY_LEDGER_RESOLVE_OBSERVATION_EVIDENCE(self, evidence_id)

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
        if type(self) is not PaperExecutionLedger:
            raise TypeError("ledger must be exact PaperExecutionLedger")
        (
            run_id,
            trigger_id,
            action_ids,
            observation_evidence_ids,
            plan_id,
            plan_fingerprint,
            model_fingerprint,
        ) = _canonical_run_reservation_inputs(
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
        )
        if type(suspended_action_ids) is not frozenset or any(
            type(item) is not str for item in suspended_action_ids
        ):
            raise TypeError("suspended_action_ids must be a frozenset[str]")
        if suspended_action_ids - set(action_ids):
            raise PaperExecutionStateError(
                "suspended_action_ids contain action outside execution plan"
            )
        base_payload = {
            "trigger_id": trigger_id,
            "plan_id": plan_id,
            "plan_fingerprint": plan_fingerprint,
            "model_fingerprint": model_fingerprint,
            "started_at": started_at,
            "action_ids": list(action_ids),
            "observation_evidence_ids": observation_evidence_ids,
        }
        payload = {
            **base_payload,
            "suspended_action_ids": sorted(suspended_action_ids),
        }

        existing = [
            event
            for event in _CANONICAL_LEDGER_EVENTS(self, run_id)
            if event["event_type"] == "RUN_RESERVED"
        ]
        if existing:
            if len(existing) != 1:
                raise PaperExecutionIntegrityError(
                    "run needs exactly one reservation"
                )
            prior_payload = existing[0]["payload"]
            if prior_payload == payload:
                return
            if not suspended_action_ids and prior_payload == base_payload:
                return
            raise PaperExecutionStateError(
                "run execution-control state conflicts with durable reservation"
            )

        _CANONICAL_LEDGER_APPEND_EVENT(
            self,
            event_type="RUN_RESERVED",
            run_id=run_id,
            key=f"{run_id}:reserve",
            payload=payload,
        )

    def _append_attempt_unlocked(
        self,
        *,
        events: list[dict[str, Any]],
        attempt: PaperLegAttempt,
    ) -> None:
        key = f"{attempt.run_id}:attempt:{attempt.sequence}"
        by_key = {item["event_key"]: item for item in events}
        prior = by_key.get(key)
        sequence = len(events)
        previous_sha256 = None if not events else events[-1]["event_sha256"]
        event = _CANONICAL_LEDGER_EVENT(
            self,
            event_type="ATTEMPT_RECORDED",
            run_id=attempt.run_id,
            key=key,
            payload=attempt.to_dict(),
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
                _CANONICAL_LEDGER_SYNC_PARENT_DIRECTORY(self)
            _CANONICAL_LEDGER_WRITE_ANCHOR_UNLOCKED(self, events + [event])
        except OSError as exc:
            self._path_durable = False
            raise PaperExecutionIntegrityError(
                "PAPER execution ledger durability barrier failed"
            ) from exc
        self._path_durable = True

    def record_attempt(self, attempt: PaperLegAttempt) -> None:
        if type(self) is not PaperExecutionLedger:
            raise TypeError("ledger must be exact PaperExecutionLedger")
        _impl._require_canonical_attempt_surface(attempt)
        try:
            canonical_attempt = PaperLegAttempt.from_dict(attempt.to_dict())
        except (PaperExecutionIntegrityError, TypeError, ValueError) as exc:
            raise PaperExecutionIntegrityError(
                "attempt no longer satisfies canonical value invariants"
            ) from exc
        if canonical_attempt != attempt:
            raise PaperExecutionIntegrityError(
                "attempt changed outside canonical construction authority"
            )
        # Continue exclusively from the reconstructed snapshot so the durable
        # write cannot race a caller mutating the original frozen dataclass.
        attempt = canonical_attempt

        def mutate() -> None:
            _CANONICAL_LEDGER_ENSURE_EXISTING_PATH_DURABLE(self)
            events = _CANONICAL_LEDGER_LOAD_UNLOCKED(self)
            run_events = [event for event in events if event["run_id"] == attempt.run_id]
            reservations = [
                event for event in run_events if event["event_type"] == "RUN_RESERVED"
            ]
            if len(reservations) != 1:
                raise PaperExecutionStateError(
                    "attempt recording requires exactly one durable reservation"
                )
            if any(event["event_type"] == "RUN_COMPLETED" for event in run_events):
                raise PaperExecutionStateError(
                    "cannot record attempt after durable run completion"
                )
            reservation_payload = reservations[0]["payload"]
            _require_durable_attempt_evidence_binding(
                events=events,
                reservation_payload=reservation_payload,
                attempt=attempt,
            )
            action_ids_raw = reservation_payload.get("action_ids")
            if (
                type(action_ids_raw) is not list
                or any(type(item) is not str or not item for item in action_ids_raw)
            ):
                raise PaperExecutionIntegrityError(
                    "durable reservation action_ids are invalid"
                )
            existing_attempts = tuple(
                sorted(
                    (
                        PaperLegAttempt.from_dict(event["payload"])
                        for event in run_events
                        if event["event_type"] == "ATTEMPT_RECORDED"
                    ),
                    key=lambda item: item.sequence,
                )
            )
            _derive_run_economics(tuple(action_ids_raw), existing_attempts)
            if (
                reservation_payload.get("plan_id") != attempt.plan_id
                or reservation_payload.get("model_fingerprint")
                != attempt.model_fingerprint
            ):
                raise PaperExecutionStateError(
                    "attempt plan/model identity is not authorized by durable reservation"
                )
            if attempt.sequence < len(existing_attempts):
                if existing_attempts[attempt.sequence] != attempt:
                    raise PaperExecutionStateError(
                        "attempt conflicts with already durable sequence"
                    )
                _CANONICAL_LEDGER_APPEND_ATTEMPT_UNLOCKED(self, events=events, attempt=attempt)
                return
            if attempt.sequence != len(existing_attempts):
                raise PaperExecutionStateError(
                    "attempt sequence must extend durable plan prefix by one"
                )
            if attempt.sequence >= len(action_ids_raw):
                raise PaperExecutionStateError(
                    "attempt sequence exceeds durable reserved action list"
                )
            if attempt.action_id != action_ids_raw[attempt.sequence]:
                raise PaperExecutionStateError(
                    "attempt action_id does not match durable reserved action"
                )
            if (
                existing_attempts
                and existing_attempts[-1].outcome is not PaperAttemptOutcome.ACCEPTED
            ):
                raise PaperExecutionStateError(
                    "cannot record attempt after terminal non-ACCEPTED outcome"
                )
            _CANONICAL_LEDGER_APPEND_ATTEMPT_UNLOCKED(self, events=events, attempt=attempt)

        _CANONICAL_LEDGER_WITH_WRITER_LOCK(self, mutate)

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
        event = _CANONICAL_LEDGER_EVENT(
            self,
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
                _CANONICAL_LEDGER_SYNC_PARENT_DIRECTORY(self)
            _CANONICAL_LEDGER_WRITE_ANCHOR_UNLOCKED(self, events + [event])
        except OSError as exc:
            self._path_durable = False
            raise PaperExecutionIntegrityError(
                "PAPER execution ledger durability barrier failed"
            ) from exc
        self._path_durable = True

    def complete_run(
        self,
        *,
        run_id: str,
        pending_action_ids: tuple[str, ...],
        recovery_decision: RecoveryDecision,
        worst_case_exposure: Decimal,
    ) -> None:
        if type(self) is not PaperExecutionLedger:
            raise TypeError("ledger must be exact PaperExecutionLedger")
        run_id = _impl._text(run_id, "run_id")
        if (
            type(pending_action_ids) is not tuple
            or any(
                type(item) is not str
                or not item
                or item.strip() != item
                for item in pending_action_ids
            )
        ):
            raise TypeError(
                "pending_action_ids must be a canonical tuple[str, ...]"
            )
        if type(recovery_decision) is not RecoveryDecision:
            raise TypeError("recovery_decision must be exact RecoveryDecision")
        if type(worst_case_exposure) is not Decimal:
            raise TypeError("worst_case_exposure must be exact Decimal")
        try:
            _validate_decimal_text_resource_bound(worst_case_exposure)
        except ValueError as exc:
            raise PaperExecutionStateError(
                "worst_case_exposure exceeds canonical Decimal resource bounds"
            ) from exc
        supplied_exposure = _impl._decimal(
            worst_case_exposure,
            "worst_case_exposure",
            allow_zero=True,
        )

        def mutate() -> None:
            _CANONICAL_LEDGER_ENSURE_EXISTING_PATH_DURABLE(self)
            events = _CANONICAL_LEDGER_LOAD_UNLOCKED(self)
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
            attempts = tuple(
                sorted(
                    (
                        PaperLegAttempt.from_dict(event["payload"])
                        for event in run_events
                        if event["event_type"] == "ATTEMPT_RECORDED"
                    ),
                    key=lambda item: item.sequence,
                )
            )
            for durable_attempt in attempts:
                _require_durable_attempt_evidence_binding(
                    events=events,
                    reservation_payload=reservations[0]["payload"],
                    attempt=durable_attempt,
                )
            derived = _derive_run_economics(tuple(action_ids_raw), attempts)
            if not derived.can_complete:
                raise PaperExecutionStateError(
                    "run cannot complete before a terminal outcome or all actions ACCEPTED"
                )
            if pending_action_ids != derived.pending_action_ids:
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
            _CANONICAL_LEDGER_APPEND_COMPLETION_UNLOCKED(
                self,
                events=events,
                run_id=run_id,
                payload=payload,
            )

        _CANONICAL_LEDGER_WITH_WRITER_LOCK(self, mutate)

    def load_run(
        self,
        *,
        run_id: str,
        trigger_id: str,
        plan: ExecutionPlan,
        config: PaperExecutionModelConfig,
        started_at: str,
        observation_evidence_ids: Mapping[str, str],
        suspended_action_ids: frozenset[str] | None = None,
    ) -> PaperExecutionRun | None:
        if type(self) is not PaperExecutionLedger:
            raise TypeError("ledger must be exact PaperExecutionLedger")
        (
            run_id,
            trigger_id,
            action_ids,
            observation_evidence_ids,
            plan_id,
            plan_fingerprint,
            model_fingerprint,
        ) = _canonical_run_reservation_inputs(
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
        )
        events = _CANONICAL_LEDGER_EVENTS(self, run_id)
        if not events:
            return None
        reserve = [event for event in events if event["event_type"] == "RUN_RESERVED"]
        if len(reserve) != 1:
            raise PaperExecutionIntegrityError("run needs exactly one reservation")
        expected_reserve = {
            "trigger_id": trigger_id,
            "plan_id": plan_id,
            "plan_fingerprint": plan_fingerprint,
            "model_fingerprint": model_fingerprint,
            "started_at": started_at,
            "action_ids": list(action_ids),
            "observation_evidence_ids": observation_evidence_ids,
        }
        durable_reserve = reserve[0]["payload"]
        if "suspended_action_ids" in durable_reserve:
            raw_suspended = durable_reserve.get("suspended_action_ids")
            if (
                type(raw_suspended) is not list
                or any(type(item) is not str for item in raw_suspended)
                or raw_suspended != sorted(set(raw_suspended))
                or set(raw_suspended)
                - set(action_ids)
            ):
                raise PaperExecutionIntegrityError(
                    "durable suspended_action_ids are invalid"
                )
            expected_reserve = {
                **expected_reserve,
                "suspended_action_ids": raw_suspended,
            }
            if suspended_action_ids is not None:
                if type(suspended_action_ids) is not frozenset or any(
                    type(item) is not str for item in suspended_action_ids
                ):
                    raise TypeError(
                        "suspended_action_ids must be a frozenset[str] or None"
                    )
                if sorted(suspended_action_ids) != raw_suspended:
                    raise PaperExecutionStateError(
                        "run execution-control state conflicts with durable reservation"
                    )
        elif suspended_action_ids:
            raise PaperExecutionStateError(
                "legacy reservation cannot prove requested suspended_action_ids"
            )
        if durable_reserve != expected_reserve:
            raise PaperExecutionStateError("run identity conflicts with durable reservation")

        attempt_events = [
            event for event in events if event["event_type"] == "ATTEMPT_RECORDED"
        ]
        attempts = tuple(
            sorted(
                (PaperLegAttempt.from_dict(event["payload"]) for event in attempt_events),
                key=lambda item: item.sequence,
            )
        )
        evidence_events = (
            _CANONICAL_LEDGER_EVENTS(self)
            if any(
                attempt.evidence_grade is not EvidenceGrade.SYNTHETIC
                for attempt in attempts
            )
            else events
        )
        for durable_attempt in attempts:
            _require_durable_attempt_evidence_binding(
                events=evidence_events,
                reservation_payload=durable_reserve,
                attempt=durable_attempt,
            )
        derived = _derive_run_economics(
            action_ids,
            attempts,
        )

        completions = [event for event in events if event["event_type"] == "RUN_COMPLETED"]
        if len(completions) > 1:
            raise PaperExecutionIntegrityError("run has multiple completion events")
        if completions:
            completion = completions[0]
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
                exposure = _impl._serialized_decimal(
                    payload["worst_case_exposure"],
                    "completion worst_case_exposure",
                    allow_zero=True,
                )
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
                plan_id=plan_id,
                plan_fingerprint=plan_fingerprint,
                model_fingerprint=model_fingerprint,
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
            plan_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            model_fingerprint=model_fingerprint,
            started_at=started_at,
            attempts=attempts,
            pending_action_ids=derived.pending_action_ids,
            recovery_decision=derived.recovery_decision,
            worst_case_exposure=derived.worst_case_exposure,
            completed=False,
        )



class PaperExecutionEvidenceRegistry(_LegacyPaperExecutionEvidenceRegistry):
    """Evidence resolver permanently bound to one canonical PAPER ledger."""

    def __init__(self, ledger: PaperExecutionLedger) -> None:
        if type(ledger) is not PaperExecutionLedger:
            raise TypeError("ledger must be exact PaperExecutionLedger")
        super().__init__(ledger)
        self._authority_ledger = ledger

    def _require_authority(self) -> PaperExecutionLedger:
        if type(self) is not PaperExecutionEvidenceRegistry:
            raise TypeError(
                "evidence registry must be exact PaperExecutionEvidenceRegistry"
            )
        if (
            type(self._authority_ledger) is not PaperExecutionLedger
            or self._ledger is not self._authority_ledger
        ):
            raise PaperExecutionStateError(
                "evidence registry durable ledger authority changed after construction"
            )
        return self._authority_ledger

    @property
    def authority_ledger(self) -> PaperExecutionLedger:
        return _CANONICAL_EVIDENCE_REGISTRY_REQUIRE_AUTHORITY(self)

    def register(self, record: PaperExecutionEvidenceRecord) -> str:
        ledger = _CANONICAL_EVIDENCE_REGISTRY_REQUIRE_AUTHORITY(self)
        _impl._require_canonical_evidence_record_surface(record)
        _CANONICAL_LEDGER_REGISTER_OBSERVATION_EVIDENCE(ledger, record)
        return record.evidence_id

    def resolve(self, evidence_id: str) -> PaperExecutionEvidenceRecord:
        ledger = _CANONICAL_EVIDENCE_REGISTRY_REQUIRE_AUTHORITY(self)
        return _CANONICAL_LEDGER_RESOLVE_OBSERVATION_EVIDENCE(ledger, evidence_id)


_CANONICAL_EVIDENCE_REGISTRY_REQUIRE_AUTHORITY = (
    PaperExecutionEvidenceRegistry._require_authority
)
_CANONICAL_LEDGER_REGISTER_OBSERVATION_EVIDENCE = (
    PaperExecutionLedger.register_observation_evidence
)
_CANONICAL_LEDGER_RESOLVE_OBSERVATION_EVIDENCE = (
    PaperExecutionLedger.resolve_observation_evidence
)


def _verify_observation_authority(
    *,
    action: ExecutionAction,
    observation: ObservedPaperExecution,
    registry: PaperExecutionEvidenceRegistry,
) -> PaperExecutionEvidenceRecord:
    _impl._require_canonical_action_surface(action)
    _impl._require_canonical_observation_surface(observation)
    ledger = _CANONICAL_EVIDENCE_REGISTRY_REQUIRE_AUTHORITY(registry)
    record = _CANONICAL_LEDGER_RESOLVE_OBSERVATION_EVIDENCE(
        ledger,
        observation.evidence_id,
    )
    if observation.evidence_sha256 != record.evidence_sha256:
        raise PaperExecutionStateError("observation evidence digest mismatch")
    expected = record.as_observation()
    if observation != expected:
        raise PaperExecutionStateError(
            "observation does not match immutable registered evidence"
        )
    exact_action = (
        record.action_id == action.action_id
        and record.bookmaker_id == action.bookmaker_id
        and record.account_id == action.account_id
        and record.event_id == action.event_id
        and record.market_id == action.market_id
        and record.selection_id == action.selection_id
        and record.side == action.side
        and record.quote_id == action.quote_id
    )
    if not exact_action:
        raise PaperExecutionStateError(
            "registered observation evidence does not bind exact action/quote/provider/account"
        )
    return record


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
    if action.side != "BACK" and not (
        action.side == "LAY" and suspended
    ):
        raise PaperExecutionStateError(
            "synthetic PAPER exposure model supports BACK only; non-BACK must use "
            "explicit empirical execution evidence"
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


def _require_canonical_execution_config_surface(
    config: PaperExecutionModelConfig,
) -> None:
    if type(config) is not PaperExecutionModelConfig:
        raise TypeError("config must be exact PaperExecutionModelConfig")
    for name in ("model_id", "model_version", "evidence_source", "seed"):
        if type(getattr(config, name)) is not str:
            raise PaperExecutionStateError(
                f"execution config {name} must retain exact canonical text authority"
            )
    if type(config.evidence_grade) is not EvidenceGrade:
        raise PaperExecutionStateError(
            "execution config evidence_grade must retain canonical evidence authority"
        )
    for name in (
        "max_quote_age_ms",
        "min_delay_ms",
        "max_delay_ms",
        "rejected_bps",
        "partial_bps",
        "unknown_bps",
        "partial_fill_bps",
        "max_slippage_bps",
    ):
        value = getattr(config, name)
        if type(value) is not int or value < 0:
            raise PaperExecutionStateError(
                f"execution config {name} must retain canonical non-negative integer authority"
            )
    if config.max_delay_ms < config.min_delay_ms:
        raise PaperExecutionStateError(
            "execution config delay bounds are no longer canonical"
        )
    if config.max_quote_age_ms <= 0:
        raise PaperExecutionStateError(
            "execution config max_quote_age_ms must remain positive"
        )
    if config.rejected_bps + config.partial_bps + config.unknown_bps > 10_000:
        raise PaperExecutionStateError(
            "execution config outcome basis points exceed canonical total"
        )
    if not 0 < config.partial_fill_bps < 10_000:
        raise PaperExecutionStateError(
            "execution config partial_fill_bps left canonical range"
        )
    if config.max_slippage_bps >= 10_000:
        raise PaperExecutionStateError(
            "execution config max_slippage_bps left canonical range"
        )


    try:
        canonical = PaperExecutionModelConfig(
            model_id=config.model_id,
            model_version=config.model_version,
            evidence_grade=config.evidence_grade,
            evidence_source=config.evidence_source,
            seed=config.seed,
            max_quote_age_ms=config.max_quote_age_ms,
            min_delay_ms=config.min_delay_ms,
            max_delay_ms=config.max_delay_ms,
            rejected_bps=config.rejected_bps,
            partial_bps=config.partial_bps,
            unknown_bps=config.unknown_bps,
            partial_fill_bps=config.partial_fill_bps,
            max_slippage_bps=config.max_slippage_bps,
        )
    except (TypeError, ValueError) as exc:
        raise PaperExecutionStateError(
            "execution config no longer satisfies canonical value invariants"
        ) from exc
    if canonical != config:
        raise PaperExecutionStateError(
            "execution config changed outside canonical construction authority"
        )


def _require_canonical_execution_plan_surface(plan: ExecutionPlan) -> None:
    if type(plan) is not ExecutionPlan:
        raise TypeError("plan must be exact ExecutionPlan")
    for name in (
        "plan_id",
        "bookmaker_profile_version",
        "decision_id",
        "approval_id",
        "created_at",
    ):
        if type(getattr(plan, name)) is not str:
            raise PaperExecutionStateError(
                f"execution plan {name} must retain exact canonical text authority"
            )
    if type(plan.schema_version) is not int:
        raise PaperExecutionStateError(
            "execution plan schema_version must retain canonical integer authority"
        )
    if type(plan.actions) is not tuple or not plan.actions:
        raise PaperExecutionStateError(
            "execution plan actions must retain canonical tuple authority"
        )


    try:
        _impl._text(plan.plan_id, "plan_id")
        _impl._text(plan.bookmaker_profile_version, "bookmaker_profile_version")
        _impl._text(plan.decision_id, "decision_id")
        _impl._text(plan.approval_id, "approval_id")
        _impl._timestamp(plan.created_at, "created_at")
    except ValueError as exc:
        raise PaperExecutionStateError(
            "execution plan no longer satisfies canonical value invariants"
        ) from exc


def _snapshot_execution_config(
    config: PaperExecutionModelConfig,
) -> PaperExecutionModelConfig:
    _require_canonical_execution_config_surface(config)
    try:
        canonical = PaperExecutionModelConfig(
            model_id=config.model_id,
            model_version=config.model_version,
            evidence_grade=config.evidence_grade,
            evidence_source=config.evidence_source,
            seed=config.seed,
            max_quote_age_ms=config.max_quote_age_ms,
            min_delay_ms=config.min_delay_ms,
            max_delay_ms=config.max_delay_ms,
            rejected_bps=config.rejected_bps,
            partial_bps=config.partial_bps,
            unknown_bps=config.unknown_bps,
            partial_fill_bps=config.partial_fill_bps,
            max_slippage_bps=config.max_slippage_bps,
        )
    except (TypeError, ValueError) as exc:
        raise PaperExecutionStateError(
            "execution config changed during canonical snapshot"
        ) from exc
    if canonical != config:
        raise PaperExecutionStateError(
            "execution config changed during canonical snapshot"
        )
    return canonical


def _snapshot_execution_plan(plan: ExecutionPlan) -> ExecutionPlan:
    _require_canonical_execution_plan_surface(plan)
    actions: list[ExecutionAction] = []
    try:
        for action in plan.actions:
            _impl._require_canonical_action_surface(action)
            actions.append(
                ExecutionAction(
                    action_id=action.action_id,
                    bookmaker_id=action.bookmaker_id,
                    account_id=action.account_id,
                    event_id=action.event_id,
                    market_id=action.market_id,
                    selection_id=action.selection_id,
                    side=action.side,
                    requested_odds=action.requested_odds,
                    requested_stake=action.requested_stake,
                    quote_id=action.quote_id,
                    quote_observed_at=action.quote_observed_at,
                    expires_at=action.expires_at,
                )
            )
        canonical = ExecutionPlan(
            plan_id=plan.plan_id,
            bookmaker_profile_version=plan.bookmaker_profile_version,
            decision_id=plan.decision_id,
            approval_id=plan.approval_id,
            created_at=plan.created_at,
            actions=tuple(actions),
            schema_version=plan.schema_version,
        )
    except (TypeError, ValueError) as exc:
        raise PaperExecutionStateError(
            "execution plan changed during canonical snapshot"
        ) from exc
    if canonical != plan:
        raise PaperExecutionStateError(
            "execution plan changed during canonical snapshot"
        )
    return canonical


def _validate_lay_execution_surface(
    *,
    plan: ExecutionPlan,
    observations: Mapping[str, ObservedPaperExecution],
    suspended_action_ids: frozenset[str],
) -> None:
    _require_canonical_execution_plan_surface(plan)
    for action in plan.actions:
        _impl._require_canonical_action_surface(action)
        if action.side not in {"BACK", "LAY"}:
            raise PaperExecutionStateError(
                "PAPER execution requires canonical BACK or LAY action side "
                "before reservation"
            )
    try:
        canonical_plan = ExecutionPlan(
            plan_id=plan.plan_id,
            bookmaker_profile_version=plan.bookmaker_profile_version,
            decision_id=plan.decision_id,
            approval_id=plan.approval_id,
            created_at=plan.created_at,
            actions=plan.actions,
            schema_version=plan.schema_version,
        )
    except (TypeError, ValueError) as exc:
        raise PaperExecutionStateError(
            "execution plan no longer satisfies canonical aggregate invariants"
        ) from exc
    if canonical_plan != plan:
        raise PaperExecutionStateError(
            "execution plan changed outside canonical construction authority"
        )

    lay_actions = tuple(
        action for action in plan.actions if action.side == "LAY"
    )
    if not lay_actions:
        return
    if len(plan.actions) != 1 or len(lay_actions) != 1:
        raise PaperExecutionStateError(
            "LAY PAPER execution is limited to one single-leg action"
        )
    action = lay_actions[0]
    observation = observations.get(action.action_id)
    if observation is None:
        if action.action_id in suspended_action_ids:
            return
        raise PaperExecutionStateError(
            "LAY PAPER execution requires explicit empirical execution evidence"
        )
    if observation.evidence_grade is not EvidenceGrade.EMPIRICAL:
        raise PaperExecutionStateError(
            "configured/synthetic LAY execution cannot establish liability truth"
        )
    if observation.outcome is PaperAttemptOutcome.UNKNOWN:
        raise PaperExecutionStateError(
            "UNKNOWN LAY exposure has no canonical upper-odds liability bound"
        )



_CANONICAL_LEDGER_RESERVE_RUN = PaperExecutionLedger.reserve_run
_CANONICAL_LEDGER_LOAD_RUN = PaperExecutionLedger.load_run
_CANONICAL_LEDGER_RECORD_ATTEMPT = PaperExecutionLedger.record_attempt
_CANONICAL_LEDGER_COMPLETE_RUN = PaperExecutionLedger.complete_run
_CANONICAL_LEDGER_EVENTS = PaperExecutionLedger.events
_CANONICAL_LEDGER_APPEND_EVENT = PaperExecutionLedger._append_event
_CANONICAL_LEDGER_EVENT = PaperExecutionLedger._event
_CANONICAL_LEDGER_SYNC_PARENT_DIRECTORY = PaperExecutionLedger._sync_parent_directory
_CANONICAL_LEDGER_WRITE_ANCHOR_UNLOCKED = PaperExecutionLedger._write_anchor_unlocked
_CANONICAL_LEDGER_WITH_WRITER_LOCK = PaperExecutionLedger._with_writer_lock
_CANONICAL_LEDGER_LOAD_UNLOCKED = PaperExecutionLedger._load_unlocked
_CANONICAL_LEDGER_ENSURE_EXISTING_PATH_DURABLE = PaperExecutionLedger._ensure_existing_path_durable
_CANONICAL_LEDGER_APPEND_ATTEMPT_UNLOCKED = PaperExecutionLedger._append_attempt_unlocked
_CANONICAL_LEDGER_APPEND_COMPLETION_UNLOCKED = PaperExecutionLedger._append_completion_unlocked

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
    plan = _snapshot_execution_plan(plan)
    config = _snapshot_execution_config(config)
    if type(ledger) is not PaperExecutionLedger:
        raise TypeError("ledger must be exact PaperExecutionLedger")
    trigger_id = _impl._text(trigger_id, "trigger_id")
    _impl._timestamp(started_at, "started_at")
    if observations is None:
        observations = {}
    if not isinstance(observations, Mapping):
        raise TypeError("observations must be a mapping")
    try:
        observations = dict(observations)
    except (TypeError, ValueError) as exc:
        raise TypeError("observations must be a stable mapping") from exc
    if any(type(action_id) is not str for action_id in observations):
        raise TypeError("observation keys must be exact str action ids")
    if type(suspended_action_ids) is not frozenset or any(
        type(item) is not str for item in suspended_action_ids
    ):
        raise TypeError("suspended_action_ids must be a frozenset[str]")
    action_by_id = {action.action_id: action for action in plan.actions}
    if set(observations) - set(action_by_id):
        raise PaperExecutionStateError("observations contain action outside execution plan")
    if set(suspended_action_ids) - set(action_by_id):
        raise PaperExecutionStateError(
            "suspended_action_ids contain action outside execution plan"
        )
    if observations:
        if type(evidence_registry) is not PaperExecutionEvidenceRegistry:
            raise PaperExecutionStateError(
                "configured/empirical observations require the exact durable evidence registry authority"
            )
        if _CANONICAL_EVIDENCE_REGISTRY_REQUIRE_AUTHORITY(evidence_registry) is not ledger:
            raise PaperExecutionStateError(
                "execution evidence registry must be bound to the exact run ledger"
            )

    observation_evidence_ids: dict[str, str] = {}
    canonical_observations: dict[str, ObservedPaperExecution] = {}
    for action_id, observation in observations.items():
        assert evidence_registry is not None
        record = _verify_observation_authority(
            action=action_by_id[action_id],
            observation=observation,
            registry=evidence_registry,
        )
        # From this point onward execution uses only an observation reconstructed
        # from the durable registry record. The caller-owned frozen observation
        # may be mutated after verification and must never regain execution
        # authority.
        canonical_observation = record.as_observation()
        canonical_observations[action_id] = canonical_observation
        observation_evidence_ids[action_id] = record.evidence_id
    observations = canonical_observations

    _validate_lay_execution_surface(
        plan=plan,
        observations=observations,
        suspended_action_ids=suspended_action_ids,
    )

    run_id = _impl._run_id(plan, trigger_id, config)
    # Validate every empirical/configured observation through the exact canonical
    # attempt constructor before the first durable write. Invalid fill/suspension,
    # freshness, expiry or stake facts must not strand a RUN_RESERVED record.
    for sequence, action in enumerate(plan.actions):
        observation = observations.get(action.action_id)
        if observation is None:
            continue
        _impl._observed_attempt(
            run_id=run_id,
            plan=plan,
            action=action,
            sequence=sequence,
            config=config,
            observation=observation,
            started_at=started_at,
        )

    _CANONICAL_LEDGER_RESERVE_RUN(
        ledger,
        run_id=run_id,
        trigger_id=trigger_id,
        plan=plan,
        config=config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
        suspended_action_ids=suspended_action_ids,
    )
    existing = _CANONICAL_LEDGER_LOAD_RUN(
        ledger,
        run_id=run_id,
        trigger_id=trigger_id,
        plan=plan,
        config=config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
        suspended_action_ids=suspended_action_ids,
    )
    assert existing is not None
    if existing.completed:
        return existing

    attempts = list(existing.attempts)
    if attempts and attempts[-1].outcome is not PaperAttemptOutcome.ACCEPTED:
        _CANONICAL_LEDGER_COMPLETE_RUN(
            ledger,
            run_id=run_id,
            pending_action_ids=existing.pending_action_ids,
            recovery_decision=(
                RecoveryDecision.HEDGE_REVIEW_REQUIRED
                if existing.worst_case_exposure > 0
                else RecoveryDecision.NO_EXPOSURE
            ),
            worst_case_exposure=existing.worst_case_exposure,
        )
        result = _CANONICAL_LEDGER_LOAD_RUN(
            ledger,
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
            suspended_action_ids=suspended_action_ids,
        )
        assert result is not None
        return result

    known_exposure = Decimal("0")
    worst_case_exposure = Decimal("0")
    for prior in attempts:
        assert prior.outcome is PaperAttemptOutcome.ACCEPTED
        known_exposure = _decimal_add_exact(
            known_exposure,
            _attempt_locked_capital(prior),
        )
        worst_case_exposure = max(worst_case_exposure, known_exposure)

    for sequence in range(len(attempts), len(plan.actions)):
        action = plan.actions[sequence]
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
        _CANONICAL_LEDGER_RECORD_ATTEMPT(ledger, attempt)
        attempts.append(attempt)

        if attempt.outcome in {
            PaperAttemptOutcome.ACCEPTED,
            PaperAttemptOutcome.PARTIAL,
        }:
            known_exposure = _decimal_add_exact(
                known_exposure,
                _attempt_locked_capital(attempt),
            )
            worst_case_exposure = max(worst_case_exposure, known_exposure)
        elif attempt.outcome is PaperAttemptOutcome.UNKNOWN:
            worst_case_exposure = max(
                worst_case_exposure,
                _decimal_add_exact(
                    known_exposure,
                    _unknown_exposure_increment(attempt),
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
            _CANONICAL_LEDGER_COMPLETE_RUN(
            ledger,
                run_id=run_id,
                pending_action_ids=pending,
                recovery_decision=recovery,
                worst_case_exposure=worst_case_exposure,
            )
            result = _CANONICAL_LEDGER_LOAD_RUN(
            ledger,
                run_id=run_id,
                trigger_id=trigger_id,
                plan=plan,
                config=config,
                started_at=started_at,
                observation_evidence_ids=observation_evidence_ids,
                suspended_action_ids=suspended_action_ids,
            )
            assert result is not None
            return result

    _CANONICAL_LEDGER_COMPLETE_RUN(
        ledger,
        run_id=run_id,
        pending_action_ids=(),
        recovery_decision=RecoveryDecision.NONE,
        worst_case_exposure=worst_case_exposure,
    )
    result = _CANONICAL_LEDGER_LOAD_RUN(
        ledger,
        run_id=run_id,
        trigger_id=trigger_id,
        plan=plan,
        config=config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
        suspended_action_ids=suspended_action_ids,
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
