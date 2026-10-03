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


_RUN_ACTION_BINDING_EVENT = "RUN_ACTION_BINDING"


def _run_action_binding_payload(
    plan: ExecutionPlan,
    config: PaperExecutionModelConfig,
) -> dict[str, Any]:
    """Canonical durable action/model projection for PAPER attempt authority."""

    if not isinstance(plan, ExecutionPlan):
        raise TypeError("plan must be ExecutionPlan")
    if not isinstance(config, PaperExecutionModelConfig):
        raise TypeError("config must be PaperExecutionModelConfig")
    return {
        "plan_id": plan.plan_id,
        "plan_fingerprint": plan.fingerprint,
        "model_fingerprint": config.fingerprint,
        "actions": [action.to_dict() for action in plan.actions],
    }


def _resolve_run_action_binding(
    run_events: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    run_id: str,
) -> tuple[dict[str, Any], tuple[ExecutionAction, ...]]:
    """Resolve one binding and prove it agrees with the durable reservation."""

    reservations = [
        event for event in run_events if event["event_type"] == "RUN_RESERVED"
    ]
    if len(reservations) != 1:
        raise PaperExecutionIntegrityError(
            "run needs exactly one durable reservation"
        )
    bindings = [
        event
        for event in run_events
        if event["event_type"] == _RUN_ACTION_BINDING_EVENT
    ]
    if len(bindings) != 1:
        raise PaperExecutionIntegrityError(
            "run needs exactly one durable action/model binding"
        )
    binding = bindings[0]
    if binding["run_id"] != run_id:
        raise PaperExecutionIntegrityError(
            "durable action/model binding run identity mismatch"
        )
    payload = binding["payload"]
    if type(payload) is not dict or set(payload) != {
        "plan_id",
        "plan_fingerprint",
        "model_fingerprint",
        "actions",
    }:
        raise PaperExecutionIntegrityError(
            "durable action/model binding payload schema is invalid"
        )
    for field in ("plan_id", "plan_fingerprint", "model_fingerprint"):
        if type(payload[field]) is not str or not payload[field]:
            raise PaperExecutionIntegrityError(
                f"durable action/model binding {field} is invalid"
            )
    raw_actions = payload["actions"]
    if type(raw_actions) is not list or not raw_actions:
        raise PaperExecutionIntegrityError(
            "durable action/model binding actions are invalid"
        )
    actions: list[ExecutionAction] = []
    for raw in raw_actions:
        if type(raw) is not dict:
            raise PaperExecutionIntegrityError(
                "durable action/model binding action is invalid"
            )
        try:
            action = ExecutionAction(**raw)
        except (TypeError, ValueError) as exc:
            raise PaperExecutionIntegrityError(
                "durable action/model binding action is invalid"
            ) from exc
        if action.to_dict() != raw:
            raise PaperExecutionIntegrityError(
                "durable action/model binding action is non-canonical"
            )
        actions.append(action)
    if len({action.action_id for action in actions}) != len(actions):
        raise PaperExecutionIntegrityError(
            "durable action/model binding action ids are not unique"
        )

    reservation = reservations[0]["payload"]
    if type(reservation) is not dict:
        raise PaperExecutionIntegrityError(
            "durable reservation payload is invalid"
        )
    action_ids = reservation.get("action_ids")
    if (
        type(action_ids) is not list
        or any(type(item) is not str or not item for item in action_ids)
    ):
        raise PaperExecutionIntegrityError(
            "durable reservation action_ids are invalid"
        )
    if (
        payload["plan_id"] != reservation.get("plan_id")
        or payload["plan_fingerprint"] != reservation.get("plan_fingerprint")
        or payload["model_fingerprint"] != reservation.get("model_fingerprint")
        or [action.action_id for action in actions] != action_ids
    ):
        raise PaperExecutionIntegrityError(
            "durable action/model binding conflicts with reservation"
        )
    return payload, tuple(actions)


def _validate_attempt_against_binding(
    attempt: PaperLegAttempt,
    *,
    run_id: str,
    binding: dict[str, Any],
    actions: tuple[ExecutionAction, ...],
) -> None:
    """Fail closed before caller-authored attempt economics can become truth."""

    if not isinstance(attempt, PaperLegAttempt):
        raise TypeError("attempt must be PaperLegAttempt")
    if (
        attempt.run_id != run_id
        or attempt.plan_id != binding["plan_id"]
        or attempt.model_fingerprint != binding["model_fingerprint"]
        or attempt.sequence >= len(actions)
    ):
        raise PaperExecutionIntegrityError(
            "PAPER attempt conflicts with reserved action/model binding"
        )
    action = actions[attempt.sequence]
    if action.side != "BACK":
        raise PaperExecutionStateError(
            "PAPER capital-at-risk binding supports BACK only"
        )
    if (
        attempt.action_id != action.action_id
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
            "PAPER attempt conflicts with reserved action/model binding"
        )


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

    def reserve_run(
        self,
        *,
        run_id: str,
        trigger_id: str,
        plan: ExecutionPlan,
        config: PaperExecutionModelConfig,
        started_at: str,
        observation_evidence_ids: Mapping[str, str],
    ) -> None:
        # Keep the historical RUN_RESERVED payload stable, then add a separate
        # append-only authority projection.  A crash between these events is safe:
        # attempts fail closed until the same canonical reservation call retries
        # and durably installs the idempotent binding.
        super().reserve_run(
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
        )
        self._append_event(
            event_type=_RUN_ACTION_BINDING_EVENT,
            run_id=run_id,
            key=f"{run_id}:action-binding",
            payload=_run_action_binding_payload(plan, config),
        )

    def record_attempt(self, attempt: PaperLegAttempt) -> None:
        if not isinstance(attempt, PaperLegAttempt):
            raise TypeError("attempt must be PaperLegAttempt")
        run_id = _impl._text(attempt.run_id, "run_id")
        run_events = list(self.events(run_id))
        binding, actions = _resolve_run_action_binding(
            run_events,
            run_id=run_id,
        )
        _validate_attempt_against_binding(
            attempt,
            run_id=run_id,
            binding=binding,
            actions=actions,
        )
        super().record_attempt(attempt)

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
            binding, actions = _resolve_run_action_binding(
                run_events,
                run_id=run_id,
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
            for attempt in attempts:
                _validate_attempt_against_binding(
                    attempt,
                    run_id=run_id,
                    binding=binding,
                    actions=actions,
                )
            derived = _derive_run_economics(
                tuple(action.action_id for action in actions),
                attempts,
            )
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
    ) -> PaperExecutionRun | None:
        events = self.events(run_id)
        if not events:
            return None
        reserve = [event for event in events if event["event_type"] == "RUN_RESERVED"]
        if len(reserve) != 1:
            raise PaperExecutionIntegrityError("run needs exactly one reservation")
        expected_reserve = {
            "trigger_id": trigger_id,
            "plan_id": plan.plan_id,
            "plan_fingerprint": plan.fingerprint,
            "model_fingerprint": config.fingerprint,
            "started_at": started_at,
            "action_ids": [action.action_id for action in plan.actions],
            "observation_evidence_ids": dict(sorted(observation_evidence_ids.items())),
        }
        if reserve[0]["payload"] != expected_reserve:
            raise PaperExecutionStateError("run identity conflicts with durable reservation")

        binding, bound_actions = _resolve_run_action_binding(
            list(events),
            run_id=run_id,
        )
        if binding != _run_action_binding_payload(plan, config):
            raise PaperExecutionIntegrityError(
                "durable action/model binding conflicts with supplied plan/config"
            )

        attempt_events = [
            event for event in events if event["event_type"] == "ATTEMPT_RECORDED"
        ]
        attempts = tuple(
            sorted(
                (PaperLegAttempt.from_dict(event["payload"]) for event in attempt_events),
                key=lambda item: item.sequence,
            )
        )
        for attempt in attempts:
            _validate_attempt_against_binding(
                attempt,
                run_id=run_id,
                binding=binding,
                actions=bound_actions,
            )
        derived = _derive_run_economics(
            tuple(action.action_id for action in bound_actions),
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
    if set(suspended_action_ids) - set(action_by_id):
        raise PaperExecutionStateError(
            "suspended_action_ids contain action outside execution plan"
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
    )
    existing = ledger.load_run(
        run_id=run_id,
        trigger_id=trigger_id,
        plan=plan,
        config=config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
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
