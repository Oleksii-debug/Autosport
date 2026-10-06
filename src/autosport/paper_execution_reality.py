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

_CANONICAL_EVIDENCE_GRADE_TYPE = EvidenceGrade
_CANONICAL_PAPER_ATTEMPT_OUTCOME_TYPE = PaperAttemptOutcome
_CANONICAL_RECOVERY_DECISION_TYPE = RecoveryDecision
_CANONICAL_EXECUTION_PLAN_TYPE = ExecutionPlan
_CANONICAL_PAPER_EXECUTION_MODEL_CONFIG_TYPE = PaperExecutionModelConfig
_CANONICAL_PAPER_EXECUTION_EVIDENCE_REGISTRY_TYPE = PaperExecutionEvidenceRegistry
_CANONICAL_MAPPING_TYPE = Mapping
_EVIDENCE_SYNTHETIC = EvidenceGrade.SYNTHETIC
_OUTCOME_ACCEPTED = PaperAttemptOutcome.ACCEPTED
_OUTCOME_PARTIAL = PaperAttemptOutcome.PARTIAL
_OUTCOME_REJECTED = PaperAttemptOutcome.REJECTED
_OUTCOME_UNKNOWN = PaperAttemptOutcome.UNKNOWN
_RECOVERY_NONE = RecoveryDecision.NONE
_RECOVERY_NO_EXPOSURE = RecoveryDecision.NO_EXPOSURE
_RECOVERY_HEDGE_REVIEW_REQUIRED = RecoveryDecision.HEDGE_REVIEW_REQUIRED
_CANONICAL_DECIMAL_TYPE = Decimal
_CANONICAL_DECIMAL_TYPE_IDENTITY = _CANONICAL_DECIMAL_TYPE
_CANONICAL_DECIMAL_RESOURCE_VALIDATOR = _impl._CANONICAL_DECIMAL_RESOURCE_VALIDATOR
_CANONICAL_DECIMAL_RESOURCE_VALIDATOR_IDENTITY = _CANONICAL_DECIMAL_RESOURCE_VALIDATOR
_CANONICAL_DECIMAL_RESOURCE_VALIDATOR_CODE = _CANONICAL_DECIMAL_RESOURCE_VALIDATOR.__code__
_CANONICAL_TEXT_VALIDATOR = _impl._CANONICAL_TEXT_VALIDATOR
_CANONICAL_DECIMAL_PARSER = _impl._CANONICAL_DECIMAL_PARSER
_CANONICAL_DECIMAL_PARSER_IDENTITY = _CANONICAL_DECIMAL_PARSER
_CANONICAL_DECIMAL_PARSER_CODE = _CANONICAL_DECIMAL_PARSER.__code__
_CANONICAL_DECIMAL_TEXT_FORMATTER = _impl._CANONICAL_DECIMAL_TEXT_FORMATTER
_CANONICAL_DECIMAL_TEXT_FORMATTER_IDENTITY = _CANONICAL_DECIMAL_TEXT_FORMATTER
_CANONICAL_DECIMAL_TEXT_FORMATTER_CODE = _CANONICAL_DECIMAL_TEXT_FORMATTER.__code__
_CANONICAL_CANONICALIZER = _impl._CANONICAL_CANONICALIZER
_CANONICAL_TIMESTAMP_PARSER = _impl._CANONICAL_TIMESTAMP_PARSER
_CANONICAL_TIMESTAMP_FORMATTER = _impl._CANONICAL_TIMESTAMP_FORMATTER
_CANONICAL_DETERMINISTIC_INT = _impl._CANONICAL_DETERMINISTIC_INT
_CANONICAL_MILLISECONDS = _impl._milliseconds
_CANONICAL_TIMEDELTA = _impl.timedelta
_CANONICAL_ATTEMPT_ID = _impl._attempt_id
_CANONICAL_VERIFY_OBSERVATION_AUTHORITY = _impl._verify_observation_authority
_CANONICAL_RUN_ID = _impl._run_id
_CANONICAL_OBSERVED_ATTEMPT = _impl._observed_attempt
_CANONICAL_OS_FSYNC = os.fsync
_MAX_DURABLE_EVENT_LINE_CHARS = _impl._MAX_DURABLE_EVENT_LINE_CHARS


def _decimal_coefficient(
    value: Decimal,
    _decimal_type: type[Decimal] = Decimal,
) -> tuple[int, int]:
    if type(value) is not _decimal_type:
        raise ValueError("exact Decimal required")
    if not value.is_finite():
        raise ValueError("Decimal must be finite")
    parts = value.as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if parts.sign:
        coefficient = -coefficient
    return coefficient, int(parts.exponent)


def _decimal_from_coefficient(
    coefficient: int,
    exponent: int,
    _decimal_type: type[Decimal] = Decimal,
    _resource_validator=_impl._CANONICAL_DECIMAL_RESOURCE_VALIDATOR,
    _resource_validator_code=_impl._CANONICAL_DECIMAL_RESOURCE_VALIDATOR.__code__,
) -> Decimal:
    if getattr(_resource_validator, "__code__", None) is not _resource_validator_code:
        raise ValueError("PAPER Decimal resource authority changed")
    sign = 1 if coefficient < 0 else 0
    magnitude = _decimal_type(abs(coefficient))
    digits = magnitude.as_tuple().digits
    result = _decimal_type((sign, digits, exponent))
    _resource_validator(result)
    return result


_CANONICAL_DECIMAL_COEFFICIENT = _decimal_coefficient
_CANONICAL_DECIMAL_COEFFICIENT_CODE = _decimal_coefficient.__code__
_CANONICAL_DECIMAL_FROM_COEFFICIENT = _decimal_from_coefficient
_CANONICAL_DECIMAL_FROM_COEFFICIENT_CODE = _decimal_from_coefficient.__code__


def _decimal_add_exact(
    left: Decimal,
    right: Decimal,
    _coefficient_fn=_decimal_coefficient,
    _coefficient_code=_decimal_coefficient.__code__,
    _from_coefficient=_decimal_from_coefficient,
    _from_coefficient_code=_decimal_from_coefficient.__code__,
) -> Decimal:
    if (
        getattr(_coefficient_fn, "__code__", None) is not _coefficient_code
        or getattr(_from_coefficient, "__code__", None) is not _from_coefficient_code
    ):
        raise ValueError("PAPER exact Decimal arithmetic dependency changed")
    left_coefficient, left_exponent = _coefficient_fn(left)
    right_coefficient, right_exponent = _coefficient_fn(right)
    exponent = min(left_exponent, right_exponent)
    left_scaled = left_coefficient * (10 ** (left_exponent - exponent))
    right_scaled = right_coefficient * (10 ** (right_exponent - exponent))
    return _from_coefficient(left_scaled + right_scaled, exponent)


def _decimal_subtract_exact(
    left: Decimal,
    right: Decimal,
    _coefficient_fn=_decimal_coefficient,
    _coefficient_code=_decimal_coefficient.__code__,
    _from_coefficient=_decimal_from_coefficient,
    _from_coefficient_code=_decimal_from_coefficient.__code__,
    _add_exact=_decimal_add_exact,
    _add_exact_code=_decimal_add_exact.__code__,
) -> Decimal:
    if (
        getattr(_coefficient_fn, "__code__", None) is not _coefficient_code
        or getattr(_from_coefficient, "__code__", None) is not _from_coefficient_code
        or getattr(_add_exact, "__code__", None) is not _add_exact_code
    ):
        raise ValueError("PAPER exact Decimal arithmetic dependency changed")
    right_coefficient, right_exponent = _coefficient_fn(right)
    return _add_exact(
        left,
        _from_coefficient(-right_coefficient, right_exponent),
    )


def _decimal_scale_bps_exact(
    value: Decimal,
    basis_points: int,
    _coefficient_fn=_decimal_coefficient,
    _coefficient_code=_decimal_coefficient.__code__,
    _from_coefficient=_decimal_from_coefficient,
    _from_coefficient_code=_decimal_from_coefficient.__code__,
) -> Decimal:
    if type(basis_points) is not int or not 0 <= basis_points <= 10_000:
        raise ValueError("basis_points must be an int in 0..10000")
    if (
        getattr(_coefficient_fn, "__code__", None) is not _coefficient_code
        or getattr(_from_coefficient, "__code__", None) is not _from_coefficient_code
    ):
        raise ValueError("PAPER exact Decimal arithmetic dependency changed")
    coefficient, exponent = _coefficient_fn(value)
    return _from_coefficient(coefficient * basis_points, exponent - 4)


_CANONICAL_DECIMAL_ADD_EXACT = _decimal_add_exact
_CANONICAL_DECIMAL_ADD_EXACT_CODE = _decimal_add_exact.__code__
_CANONICAL_DECIMAL_SUBTRACT_EXACT = _decimal_subtract_exact
_CANONICAL_DECIMAL_SUBTRACT_EXACT_CODE = _decimal_subtract_exact.__code__
_CANONICAL_DECIMAL_SCALE_BPS_EXACT = _decimal_scale_bps_exact
_CANONICAL_DECIMAL_SCALE_BPS_EXACT_CODE = _decimal_scale_bps_exact.__code__


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
    decimal_add = _decimal_add_exact
    from_coefficient = _decimal_from_coefficient
    if (
        decimal_add is not _CANONICAL_DECIMAL_ADD_EXACT
        or decimal_add.__code__ is not _CANONICAL_DECIMAL_ADD_EXACT_CODE
        or from_coefficient is not _CANONICAL_DECIMAL_FROM_COEFFICIENT
        or from_coefficient.__code__ is not _CANONICAL_DECIMAL_FROM_COEFFICIENT_CODE
    ):
        raise PaperExecutionIntegrityError(
            "PAPER exact Decimal arithmetic authority changed"
        )
    if len(attempts) > len(action_ids):
        raise PaperExecutionIntegrityError("durable attempts exceed reserved action list")

    known_exposure = from_coefficient(0, 0)
    worst_case = from_coefficient(0, 0)
    terminal_seen = False
    for index, attempt in enumerate(attempts):
        if attempt.sequence != index or attempt.action_id != action_ids[index]:
            raise PaperExecutionIntegrityError("durable attempts are not a reserved plan prefix")
        if terminal_seen:
            raise PaperExecutionIntegrityError(
                "durable attempts continue after a non-ACCEPTED terminal outcome"
            )

        if attempt.outcome in {
            _OUTCOME_ACCEPTED,
            _OUTCOME_PARTIAL,
        }:
            assert attempt.execution_stake is not None
            known_exposure = decimal_add(known_exposure, attempt.execution_stake)
            worst_case = max(worst_case, known_exposure)
        elif attempt.outcome is _OUTCOME_UNKNOWN:
            worst_case = max(
                worst_case,
                decimal_add(known_exposure, attempt.requested_stake),
            )

        if attempt.outcome is not _OUTCOME_ACCEPTED:
            terminal_seen = True

    pending = action_ids[len(attempts) :]
    all_accepted_complete = (
        bool(attempts)
        and len(attempts) == len(action_ids)
        and all(item.outcome is _OUTCOME_ACCEPTED for item in attempts)
    )
    can_complete = all_accepted_complete or (
        bool(attempts)
        and attempts[-1].outcome is not _OUTCOME_ACCEPTED
    )
    if all_accepted_complete:
        recovery = _RECOVERY_NONE
    elif can_complete:
        recovery = (
            _RECOVERY_HEDGE_REVIEW_REQUIRED
            if worst_case > 0
            else _RECOVERY_NO_EXPOSURE
        )
    else:
        recovery = (
            _RECOVERY_HEDGE_REVIEW_REQUIRED
            if worst_case > 0
            else _RECOVERY_NONE
        )
    return _DerivedRunEconomics(
        pending_action_ids=pending,
        recovery_decision=recovery,
        worst_case_exposure=worst_case,
        can_complete=can_complete,
    )


_CANONICAL_DERIVE_RUN_ECONOMICS = _derive_run_economics
_CANONICAL_DERIVE_RUN_ECONOMICS_CODE = _derive_run_economics.__code__


class PaperExecutionLedger(_impl.PaperExecutionLedger):
    """PAPER ledger with mechanically derived completion economics."""

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

        encoded = _CANONICAL_CANONICALIZER(event) + "\n"
        if len(encoded) > _MAX_DURABLE_EVENT_LINE_CHARS + 1:
            raise PaperExecutionIntegrityError(
                "PAPER execution ledger event exceeds resource limit"
            )
        path_existed_before = self.path.exists()
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                _CANONICAL_OS_FSYNC(handle.fileno())
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
        run_id = _CANONICAL_TEXT_VALIDATOR(run_id, "run_id")
        if type(recovery_decision) is not _CANONICAL_RECOVERY_DECISION_TYPE:
            raise TypeError("recovery_decision must be exact RecoveryDecision")
        decimal_parser = _CANONICAL_DECIMAL_PARSER
        decimal_formatter = _CANONICAL_DECIMAL_TEXT_FORMATTER
        derive_run_economics = _derive_run_economics
        if (
            decimal_parser is not _CANONICAL_DECIMAL_PARSER_IDENTITY
            or decimal_parser.__code__ is not _CANONICAL_DECIMAL_PARSER_CODE
        ):
            raise PaperExecutionIntegrityError(
                "PAPER Decimal parser authority changed"
            )
        if (
            decimal_formatter is not _CANONICAL_DECIMAL_TEXT_FORMATTER_IDENTITY
            or decimal_formatter.__code__
            is not _CANONICAL_DECIMAL_TEXT_FORMATTER_CODE
        ):
            raise PaperExecutionIntegrityError(
                "PAPER Decimal formatter authority changed"
            )
        if (
            derive_run_economics is not _CANONICAL_DERIVE_RUN_ECONOMICS
            or derive_run_economics.__code__
            is not _CANONICAL_DERIVE_RUN_ECONOMICS_CODE
        ):
            raise PaperExecutionIntegrityError(
                "PAPER run economics authority changed"
            )
        supplied_exposure = decimal_parser(
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
            derived = derive_run_economics(tuple(action_ids_raw), attempts)
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
                "worst_case_exposure": decimal_formatter(
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
        decimal_parser = _CANONICAL_DECIMAL_PARSER
        derive_run_economics = _derive_run_economics
        if (
            decimal_parser is not _CANONICAL_DECIMAL_PARSER_IDENTITY
            or decimal_parser.__code__ is not _CANONICAL_DECIMAL_PARSER_CODE
        ):
            raise PaperExecutionIntegrityError(
                "PAPER Decimal parser authority changed"
            )
        if (
            derive_run_economics is not _CANONICAL_DERIVE_RUN_ECONOMICS
            or derive_run_economics.__code__
            is not _CANONICAL_DERIVE_RUN_ECONOMICS_CODE
        ):
            raise PaperExecutionIntegrityError(
                "PAPER run economics authority changed"
            )
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

        attempt_events = [
            event for event in events if event["event_type"] == "ATTEMPT_RECORDED"
        ]
        attempts = tuple(
            sorted(
                (PaperLegAttempt.from_dict(event["payload"]) for event in attempt_events),
                key=lambda item: item.sequence,
            )
        )
        derived = derive_run_economics(
            tuple(action.action_id for action in plan.actions),
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
            if type(payload) is not dict or set(payload) != {
                "pending_action_ids",
                "recovery_decision",
                "worst_case_exposure",
            }:
                raise PaperExecutionIntegrityError(
                    "completion payload schema is invalid"
                )
            pending_raw = payload["pending_action_ids"]
            exposure_raw = payload["worst_case_exposure"]
            if (
                type(pending_raw) is not list
                or any(type(item) is not str or not item for item in pending_raw)
                or type(exposure_raw) is not str
            ):
                raise PaperExecutionIntegrityError(
                    "completion payload schema is invalid"
                )
            try:
                recovery = _CANONICAL_RECOVERY_DECISION_TYPE(
                    payload["recovery_decision"]
                )
                pending = tuple(pending_raw)
                exposure = decimal_parser(
                    exposure_raw,
                    "worst_case_exposure",
                    allow_zero=True,
                )
            except (ValueError, TypeError) as exc:
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


_CANONICAL_PUBLIC_PAPER_EXECUTION_LEDGER_TYPE = PaperExecutionLedger


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
    decimal_add = _decimal_add_exact
    decimal_subtract = _decimal_subtract_exact
    decimal_scale = _decimal_scale_bps_exact
    from_coefficient = _decimal_from_coefficient
    if (
        decimal_add is not _CANONICAL_DECIMAL_ADD_EXACT
        or decimal_add.__code__ is not _CANONICAL_DECIMAL_ADD_EXACT_CODE
        or decimal_subtract is not _CANONICAL_DECIMAL_SUBTRACT_EXACT
        or decimal_subtract.__code__ is not _CANONICAL_DECIMAL_SUBTRACT_EXACT_CODE
        or decimal_scale is not _CANONICAL_DECIMAL_SCALE_BPS_EXACT
        or decimal_scale.__code__ is not _CANONICAL_DECIMAL_SCALE_BPS_EXACT_CODE
        or from_coefficient is not _CANONICAL_DECIMAL_FROM_COEFFICIENT
        or from_coefficient.__code__ is not _CANONICAL_DECIMAL_FROM_COEFFICIENT_CODE
    ):
        raise PaperExecutionIntegrityError(
            "PAPER synthetic Decimal arithmetic authority changed"
        )
    if action.side != "BACK":
        raise PaperExecutionStateError(
            "synthetic PAPER exposure model supports BACK only; non-BACK must use "
            "explicit empirical/configured execution evidence"
        )
    start = _CANONICAL_TIMESTAMP_PARSER(started_at, "started_at")
    delay_span = config.max_delay_ms - config.min_delay_ms
    delay_ms = config.min_delay_ms
    if delay_span:
        delay_ms += _CANONICAL_DETERMINISTIC_INT(
            f"{config.seed}:{run_id}:{action.action_id}",
            "delay",
            delay_span + 1,
        )
    execution_time = start + _CANONICAL_TIMEDELTA(milliseconds=delay_ms)
    decision_time = _CANONICAL_TIMESTAMP_PARSER(action.quote_observed_at, "quote_observed_at")
    quote_age_ms = _CANONICAL_MILLISECONDS(execution_time - decision_time, "quote age")
    expires = _CANONICAL_TIMESTAMP_PARSER(action.expires_at, "expires_at")
    execution_odds: Decimal | None = None
    execution_stake: Decimal | None = None

    if suspended:
        outcome = _OUTCOME_REJECTED
        reason = "configured/synthetic suspension at execution time"
    elif execution_time >= expires:
        outcome = _OUTCOME_REJECTED
        reason = "decision quote expired before PAPER-equivalent execution"
    elif quote_age_ms > config.max_quote_age_ms:
        outcome = _OUTCOME_REJECTED
        reason = "decision quote exceeded configured PAPER freshness bound"
    else:
        bucket = _CANONICAL_DETERMINISTIC_INT(
            f"{config.seed}:{run_id}:{action.action_id}",
            "outcome",
            10_000,
        )
        if bucket < config.unknown_bps:
            outcome = _OUTCOME_UNKNOWN
            reason = "deterministic execution model produced UNKNOWN"
        elif bucket < config.unknown_bps + config.rejected_bps:
            outcome = _OUTCOME_REJECTED
            reason = "deterministic execution model produced REJECTED"
        elif bucket < config.unknown_bps + config.rejected_bps + config.partial_bps:
            outcome = _OUTCOME_PARTIAL
            reason = "deterministic execution model produced PARTIAL"
        else:
            outcome = _OUTCOME_ACCEPTED
            reason = "deterministic execution model produced ACCEPTED"

        if outcome in {_OUTCOME_ACCEPTED, _OUTCOME_PARTIAL}:
            slippage_bps = (
                0
                if config.max_slippage_bps == 0
                else _CANONICAL_DETERMINISTIC_INT(
                    f"{config.seed}:{run_id}:{action.action_id}",
                    "slippage",
                    config.max_slippage_bps + 1,
                )
            )
            odds_margin = decimal_subtract(
                action.requested_odds,
                from_coefficient(1, 0),
            )
            execution_odds = decimal_add(
                from_coefficient(1, 0),
                decimal_scale(
                    odds_margin,
                    10_000 - slippage_bps,
                ),
            )
            execution_stake = action.requested_stake
            if outcome is _OUTCOME_PARTIAL:
                execution_stake = decimal_scale(
                    action.requested_stake,
                    config.partial_fill_bps,
                )

    return PaperLegAttempt(
        attempt_id=_CANONICAL_ATTEMPT_ID(run_id, action, sequence),
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
        execution_observed_at=_CANONICAL_TIMESTAMP_FORMATTER(execution_time),
        delay_ms=delay_ms,
        quote_age_ms=quote_age_ms,
        outcome=outcome,
        execution_odds=execution_odds,
        execution_stake=execution_stake,
        suspended=suspended,
        evidence_grade=_EVIDENCE_SYNTHETIC,
        evidence_source=config.evidence_source,
        evidence_id=None,
        evidence_sha256=None,
        model_fingerprint=config.fingerprint,
        reason=reason,
    )


_CANONICAL_SYNTHETIC_ATTEMPT = _synthetic_attempt
_CANONICAL_SYNTHETIC_ATTEMPT_CODE = _synthetic_attempt.__code__


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
    decimal_add = _decimal_add_exact
    from_coefficient = _decimal_from_coefficient
    synthetic_attempt = _synthetic_attempt
    if (
        decimal_add is not _CANONICAL_DECIMAL_ADD_EXACT
        or decimal_add.__code__ is not _CANONICAL_DECIMAL_ADD_EXACT_CODE
        or from_coefficient is not _CANONICAL_DECIMAL_FROM_COEFFICIENT
        or from_coefficient.__code__ is not _CANONICAL_DECIMAL_FROM_COEFFICIENT_CODE
        or synthetic_attempt is not _CANONICAL_SYNTHETIC_ATTEMPT
        or synthetic_attempt.__code__ is not _CANONICAL_SYNTHETIC_ATTEMPT_CODE
    ):
        raise PaperExecutionIntegrityError(
            "PAPER execution Decimal authority changed"
        )
    if not isinstance(plan, _CANONICAL_EXECUTION_PLAN_TYPE):
        raise TypeError("plan must be ExecutionPlan")
    if not isinstance(config, _CANONICAL_PAPER_EXECUTION_MODEL_CONFIG_TYPE):
        raise TypeError("config must be PaperExecutionModelConfig")
    if not isinstance(ledger, _CANONICAL_PUBLIC_PAPER_EXECUTION_LEDGER_TYPE):
        raise TypeError("ledger must be PaperExecutionLedger")
    trigger_id = _CANONICAL_TEXT_VALIDATOR(trigger_id, "trigger_id")
    _CANONICAL_TIMESTAMP_PARSER(started_at, "started_at")
    if observations is None:
        observations = {}
    if not isinstance(observations, _CANONICAL_MAPPING_TYPE):
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
        _CANONICAL_PAPER_EXECUTION_EVIDENCE_REGISTRY_TYPE,
    ):
        raise PaperExecutionStateError(
            "configured/empirical observations require a durable evidence registry"
        )

    observation_evidence_ids: dict[str, str] = {}
    for action_id, observation in observations.items():
        assert evidence_registry is not None
        _CANONICAL_VERIFY_OBSERVATION_AUTHORITY(
            action=action_by_id[action_id],
            observation=observation,
            registry=evidence_registry,
        )
        observation_evidence_ids[action_id] = observation.evidence_id

    run_id = _CANONICAL_RUN_ID(plan, trigger_id, config)
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
    if attempts and attempts[-1].outcome is not _OUTCOME_ACCEPTED:
        ledger.complete_run(
            run_id=run_id,
            pending_action_ids=existing.pending_action_ids,
            recovery_decision=(
                _RECOVERY_HEDGE_REVIEW_REQUIRED
                if existing.worst_case_exposure > 0
                else _RECOVERY_NO_EXPOSURE
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

    known_exposure = from_coefficient(0, 0)
    worst_case_exposure = from_coefficient(0, 0)
    for prior in attempts:
        assert prior.outcome is _OUTCOME_ACCEPTED
        assert prior.execution_stake is not None
        known_exposure = decimal_add(
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
            attempt = _CANONICAL_OBSERVED_ATTEMPT(
                run_id=run_id,
                plan=plan,
                action=action,
                sequence=sequence,
                config=config,
                observation=observation,
                started_at=started_at,
            )
        else:
            attempt = synthetic_attempt(
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
            _OUTCOME_ACCEPTED,
            _OUTCOME_PARTIAL,
        }:
            assert attempt.execution_stake is not None
            known_exposure = decimal_add(
                known_exposure,
                attempt.execution_stake,
            )
            worst_case_exposure = max(worst_case_exposure, known_exposure)
        elif attempt.outcome is _OUTCOME_UNKNOWN:
            worst_case_exposure = max(
                worst_case_exposure,
                decimal_add(
                    known_exposure,
                    attempt.requested_stake,
                ),
            )

        if attempt.outcome is not _OUTCOME_ACCEPTED:
            pending = tuple(
                item.action_id for item in plan.actions[sequence + 1 :]
            )
            recovery = (
                _RECOVERY_HEDGE_REVIEW_REQUIRED
                if worst_case_exposure > 0
                else _RECOVERY_NO_EXPOSURE
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
        recovery_decision=_RECOVERY_NONE,
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
