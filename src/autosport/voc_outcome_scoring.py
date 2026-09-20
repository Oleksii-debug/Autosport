"""Canonical realized-VOC authority with explicit pre-compute admission.

The qualified causal scorer remains in ``_voc_outcome_scoring_base``.  This
wrapper adds explicit pre-output admission and fail-closed terminal semantics.
Negative terminals never carry caller-authored cost/latency values: they bind an
immutable ``ModelComputeRouterStore`` execution-authority record and scoring
re-resolves the canonical measurement before applying penalties.  Mixed legacy
and explicit admission windows fail closed until a frozen migration boundary is
part of protocol authority, so legacy denominator members cannot disappear.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from . import _voc_outcome_scoring_base as _base
from .decision_ledger import DecisionRecord, JsonlDecisionLedger
from .model_compute_router import (
    ComputeExecutionEvidence,
    ExecutionDisposition,
    ModelComputeRouterError,
    ModelComputeRouterStore,
)

# Preserve the existing public module surface while overriding only the authority
# whose denominator/terminal semantics changed.
for _name in dir(_base):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_base, _name)

_CONTEXT_ACTION = "VOC_ROUTE_CONTEXT"
_CONTEXT_KEY = "voc_current_context"
_ADMISSION_ACTION = "VOC_PAIRED_ADMISSION"
_ADMISSION_KEY = "voc_paired_admission"
_TERMINAL_ACTION = "VOC_PAIRED_TERMINAL"
_TERMINAL_KEY = "voc_terminal"

_SCOPE_FIELDS = (
    "sport_id",
    "league_id",
    "regime_id",
    "urgency_id",
    "contradiction_state",
)
_IDENTITY_FIELDS = ("candidate_id", "backend_id", "model_id", "config_sha256")
_ADMISSION_FIELDS = frozenset(
    {
        "schema_version",
        "admission_id",
        "decision_input_sha256",
        "decision_context_sha256",
        "decision_deadline",
        "research_protocol_id",
        "cohort_id",
        "task_class",
        "scope",
        "baseline_compute_identity",
        "challenger_compute_identity",
    }
)
_TERMINAL_FIELDS_V2 = frozenset(
    {
        "schema_version",
        "admission_sha256",
        "status",
        "execution_id",
        "execution_record_sha256",
    }
)
_TERMINAL_FIELDS_V3 = frozenset(
    {
        *_TERMINAL_FIELDS_V2,
        "authority_recorded_at",
    }
)
_NEGATIVE_TERMINAL_STATUSES = frozenset(
    {"deadline_missed", "timeout", "failed", "cancelled", "abstained", "null"}
)


def _authority_now() -> str:
    """Production-owned physical stamp for durable VOC terminal authority."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _scope(payload: Mapping[str, Any]) -> dict[str, object]:
    return {field: payload.get(field) for field in _SCOPE_FIELDS}


def _compute_identity(
    payload: Mapping[str, Any], *, prefix: str
) -> dict[str, object]:
    return {
        "candidate_id": payload.get(f"{prefix}_candidate_id"),
        "backend_id": payload.get(f"{prefix}_backend_id"),
        "model_id": payload.get(f"{prefix}_model_id"),
        "config_sha256": payload.get(f"{prefix}_config_sha256"),
    }


def _nested_identity(value: object, *, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(_IDENTITY_FIELDS):
        raise _base.VOCEvaluationError(f"{field} schema is invalid")
    return {
        "candidate_id": _base._text(
            value.get("candidate_id"), field=f"{field} candidate_id"
        ),
        "backend_id": _base._text(
            value.get("backend_id"), field=f"{field} backend_id"
        ),
        "model_id": _base._text(
            value.get("model_id"), field=f"{field} model_id"
        ),
        "config_sha256": _base._sha256(
            value.get("config_sha256"), field=f"{field} config_sha256"
        ),
    }


def _nested_scope(value: object, *, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(_SCOPE_FIELDS):
        raise _base.VOCEvaluationError(f"{field} schema is invalid")
    return {
        name: _base._text(value.get(name), field=f"{field} {name}")
        for name in _SCOPE_FIELDS
    }


def _ledger_records(
    ledger: JsonlDecisionLedger,
) -> tuple[list[tuple[str, object]], dict[str, object], dict[str, int]]:
    try:
        ledger.verified_snapshot()
        records = ledger.verified_records()
    except _base.DecisionLedgerIntegrityError as exc:
        raise _base.VOCEvaluationError(
            "canonical DecisionLedger verification failed"
        ) from exc
    ordered = [(_base._digest(record.to_dict()), record) for record in records]
    by_sha = {digest: record for digest, record in ordered}
    order = {digest: index for index, (digest, _) in enumerate(ordered)}
    return ordered, by_sha, order


def _canonical_execution(
    store: ModelComputeRouterStore,
    execution_id: object,
) -> ComputeExecutionEvidence:
    """Resolve one execution only from the verified router execution authority."""

    if not isinstance(store, ModelComputeRouterStore):
        raise TypeError("compute_execution_store must be ModelComputeRouterStore")
    execution_key = _base._text(execution_id, field="VOC terminal execution_id")
    evidence = store._executions.get(execution_key)
    if evidence is None:
        raise _base.VOCEvaluationError(
            "VOC terminal canonical compute execution is missing"
        )
    try:
        evidence.verify_record_sha256()
    except ModelComputeRouterError as exc:
        raise _base.VOCEvaluationError(
            "VOC terminal canonical compute execution record is invalid"
        ) from exc
    authority_records = [
        record
        for record in store._execution_authority_records
        if record.get("execution_id") == evidence.execution_id
        and record.get("execution_record_sha256")
        == evidence.execution_record_sha256
    ]
    if len(authority_records) != 1:
        raise _base.VOCEvaluationError(
            "VOC terminal execution lacks unique canonical execution authority"
        )
    authority_execution = authority_records[0].get("execution")
    if authority_execution != evidence.payload():
        raise _base.VOCEvaluationError(
            "VOC terminal execution does not match canonical execution authority"
        )
    return evidence


def _execution_matches_admission(
    store: ModelComputeRouterStore,
    evidence: ComputeExecutionEvidence,
    *,
    admission_record: object,
    admission: Mapping[str, Any],
    context_record: object,
    terminal_at: datetime,
) -> None:
    challenger = _nested_identity(
        admission.get("challenger_compute_identity"),
        field="VOC admission challenger_compute_identity",
    )
    if (
        evidence.backend_id != challenger["backend_id"]
        or evidence.model_id != challenger["model_id"]
        or evidence.config_sha256 != challenger["config_sha256"]
    ):
        raise _base.VOCEvaluationError(
            "VOC terminal canonical execution does not match challenger compute identity"
        )
    context_payload = getattr(context_record, "payload", None)
    context = (
        context_payload.get(_CONTEXT_KEY)
        if isinstance(context_payload, Mapping)
        else None
    )
    if not isinstance(context, Mapping):
        raise _base.VOCEvaluationError(
            "VOC terminal canonical route context is invalid"
        )
    request_id = _base._text(
        context.get("request_id"), field="VOC terminal route request_id"
    )
    decision = store.get_decision(request_id)
    if decision is None:
        raise _base.VOCEvaluationError(
            "VOC terminal execution is not bound to its canonical route request"
        )
    # A paired VOC challenger is deliberately a shadow computation and need
    # not be the candidate selected by the production route. Bind the terminal
    # execution to the exact canonical route request/decision, while the
    # challenger backend/model/config identity is independently checked above.
    if evidence.decision_id != decision.decision_id:
        raise _base.VOCEvaluationError(
            "VOC terminal execution does not match canonical route decision"
        )
    admitted_at = _base._instant(
        getattr(admission_record, "recorded_at"), field="VOC admission recorded_at"
    )
    completed_at = _base._instant(
        evidence.completed_at, field="VOC terminal execution completed_at"
    )
    observed_at = _base._instant(
        evidence.observed_at, field="VOC terminal execution observed_at"
    )
    if completed_at < admitted_at:
        raise _base.VOCEvaluationError(
            "VOC terminal canonical execution predates paired admission"
        )
    if observed_at > terminal_at:
        raise _base.VOCEvaluationError(
            "VOC terminal predates canonical execution measurement availability"
        )
    if evidence.disposition is ExecutionDisposition.ACCEPTED:
        raise _base.VOCEvaluationError(
            "VOC negative terminal cannot bind an accepted compute execution"
        )


def append_paired_voc_admission(
    decision_ledger: JsonlDecisionLedger,
    *,
    admission_id: str,
    decision_context_sha256: str,
    decision_input_sha256: str,
    decision_deadline: str,
    research_protocol_id: str,
    cohort_id: str,
    task_class: str,
    scope: Mapping[str, str],
    baseline_compute_identity: Mapping[str, str],
    challenger_compute_identity: Mapping[str, str],
    replay_run_id: str,
    agent: str,
    recorded_at: str,
) -> str:
    """Durably enroll one paired shadow attempt before candidate outputs exist."""

    if not isinstance(decision_ledger, JsonlDecisionLedger):
        raise TypeError("decision_ledger must be JsonlDecisionLedger")
    admission_identity = _base._text(admission_id, field="VOC admission_id")
    context_sha = _base._sha256(
        decision_context_sha256, field="VOC admission decision_context_sha256"
    )
    decision_input = _base._sha256(
        decision_input_sha256, field="VOC admission decision_input_sha256"
    )
    deadline = _base._instant(
        decision_deadline, field="VOC admission decision_deadline"
    )
    recorded = _base._instant(recorded_at, field="VOC admission recorded_at")
    if deadline <= recorded:
        raise _base.VOCEvaluationError(
            "VOC paired admission deadline must be after admission"
        )
    canonical_scope = _nested_scope(scope, field="VOC admission scope")
    baseline = _nested_identity(
        baseline_compute_identity, field="VOC admission baseline_compute_identity"
    )
    challenger = _nested_identity(
        challenger_compute_identity, field="VOC admission challenger_compute_identity"
    )
    if baseline["candidate_id"] == challenger["candidate_id"]:
        raise _base.VOCEvaluationError(
            "VOC paired admission candidates must be distinct"
        )

    _, by_sha, _ = _ledger_records(decision_ledger)
    context_record = by_sha.get(context_sha)
    if context_record is None:
        raise _base.VOCEvaluationError(
            "VOC paired admission decision context is missing"
        )
    if getattr(context_record, "action", None) != _CONTEXT_ACTION:
        raise _base.VOCEvaluationError(
            "VOC paired admission context action is invalid"
        )
    payload = getattr(context_record, "payload", None)
    current = payload.get(_CONTEXT_KEY) if isinstance(payload, Mapping) else None
    if not isinstance(current, Mapping):
        raise _base.VOCEvaluationError(
            "VOC paired admission context payload is invalid"
        )
    if (
        current.get("decision_input_sha256") != decision_input
        or current.get("task_class") != task_class
        or _scope(current) != canonical_scope
    ):
        raise _base.VOCEvaluationError(
            "VOC paired admission does not match its canonical route context"
        )
    if getattr(context_record, "context_hash", None) != decision_input:
        raise _base.VOCEvaluationError(
            "VOC paired admission context hash does not match decision input"
        )
    if _base._instant(
        getattr(context_record, "recorded_at"),
        field="VOC route context recorded_at",
    ) > recorded:
        raise _base.VOCEvaluationError(
            "VOC paired admission predates its route context"
        )

    admission = {
        "schema_version": 1,
        "admission_id": admission_identity,
        "decision_input_sha256": decision_input,
        "decision_context_sha256": context_sha,
        "decision_deadline": decision_deadline,
        "research_protocol_id": _base._text(
            research_protocol_id, field="VOC admission research_protocol_id"
        ),
        "cohort_id": _base._text(cohort_id, field="VOC admission cohort_id"),
        "task_class": _base._text(task_class, field="VOC admission task_class"),
        "scope": canonical_scope,
        "baseline_compute_identity": baseline,
        "challenger_compute_identity": challenger,
    }
    return decision_ledger.append(
        DecisionRecord(
            replay_run_id=_base._text(
                replay_run_id, field="VOC admission replay_run_id"
            ),
            agent=_base._text(agent, field="VOC admission agent"),
            observed_ts=recorded_at,
            action=_ADMISSION_ACTION,
            payload={_ADMISSION_KEY: admission},
            context_hash=decision_input,
            decision_id=f"voc-admission:{admission_identity}",
            recorded_at=recorded_at,
        )
    )


def append_paired_voc_terminal(
    decision_ledger: JsonlDecisionLedger,
    *,
    compute_execution_store: ModelComputeRouterStore,
    admission_sha256: str,
    status: str,
    execution_id: str,
    replay_run_id: str,
    agent: str,
    recorded_at: str,
) -> str:
    """Close an admitted failed attempt using canonical router measurements only."""

    if not isinstance(decision_ledger, JsonlDecisionLedger):
        raise TypeError("decision_ledger must be JsonlDecisionLedger")
    if not isinstance(compute_execution_store, ModelComputeRouterStore):
        raise TypeError("compute_execution_store must be ModelComputeRouterStore")
    admission_sha = _base._sha256(
        admission_sha256, field="VOC terminal admission_sha256"
    )
    status_text = _base._text(status, field="VOC terminal status")
    if status_text not in _NEGATIVE_TERMINAL_STATUSES:
        raise _base.VOCEvaluationError("VOC terminal status is unsupported")

    _, by_sha, _ = _ledger_records(decision_ledger)
    admission_record = by_sha.get(admission_sha)
    if (
        admission_record is None
        or getattr(admission_record, "action", None) != _ADMISSION_ACTION
    ):
        raise _base.VOCEvaluationError("VOC terminal admission is missing")
    admission_payload = getattr(admission_record, "payload", None)
    admission = (
        admission_payload.get(_ADMISSION_KEY)
        if isinstance(admission_payload, Mapping)
        else None
    )
    if not isinstance(admission, Mapping):
        raise _base.VOCEvaluationError("VOC terminal admission payload is invalid")
    terminal_at = _base._instant(recorded_at, field="VOC terminal recorded_at")
    admitted_at = _base._instant(
        getattr(admission_record, "recorded_at"), field="VOC admission recorded_at"
    )
    if terminal_at < admitted_at:
        raise _base.VOCEvaluationError("VOC terminal predates paired admission")
    deadline = _base._instant(
        admission.get("decision_deadline"), field="VOC admission decision_deadline"
    )
    if status_text in {"deadline_missed", "timeout"} and terminal_at < deadline:
        raise _base.VOCEvaluationError(
            "VOC deadline terminal was recorded before the frozen deadline"
        )
    context_sha = _base._sha256(
        admission.get("decision_context_sha256"),
        field="VOC admission decision_context_sha256",
    )
    context_record = by_sha.get(context_sha)
    if context_record is None:
        raise _base.VOCEvaluationError(
            "VOC terminal paired admission route context is missing"
        )
    evidence = _canonical_execution(compute_execution_store, execution_id)
    _execution_matches_admission(
        compute_execution_store,
        evidence,
        admission_record=admission_record,
        admission=admission,
        context_record=context_record,
        terminal_at=terminal_at,
    )
    decision_input = _base._sha256(
        admission.get("decision_input_sha256"),
        field="VOC admission decision_input_sha256",
    )
    authority_recorded_at = _authority_now()
    authority_at = _base._instant(
        authority_recorded_at,
        field="VOC terminal authority_recorded_at",
    )
    if authority_at < terminal_at:
        raise _base.VOCEvaluationError(
            "VOC terminal physical authority predates logical terminal time"
        )
    terminal = {
        "schema_version": 3,
        "admission_sha256": admission_sha,
        "status": status_text,
        "execution_id": evidence.execution_id,
        "execution_record_sha256": evidence.execution_record_sha256,
        "authority_recorded_at": authority_recorded_at,
    }
    return decision_ledger.append(
        DecisionRecord(
            replay_run_id=_base._text(
                replay_run_id, field="VOC terminal replay_run_id"
            ),
            agent=_base._text(agent, field="VOC terminal agent"),
            observed_ts=recorded_at,
            action=_TERMINAL_ACTION,
            payload={_TERMINAL_KEY: terminal},
            context_hash=decision_input,
            decision_id=f"voc-terminal:{admission_sha}",
            recorded_at=recorded_at,
        )
    )


class CanonicalOutcomeDerivedVOCScoreAuthority(
    _base.CanonicalOutcomeDerivedVOCScoreAuthority
):
    """Realized-VOC scorer with pre-compute enrollment and failure learning."""

    def __init__(
        self,
        *,
        compute_execution_store: ModelComputeRouterStore | None = None,
        **kwargs: Any,
    ) -> None:
        if (
            compute_execution_store is not None
            and not isinstance(compute_execution_store, ModelComputeRouterStore)
        ):
            raise TypeError(
                "compute_execution_store must be ModelComputeRouterStore or None"
            )
        super().__init__(**kwargs)
        self.compute_execution_store = compute_execution_store

    @staticmethod
    def _matching_binding(
        payload: Mapping[str, Any],
        *,
        context_sha: str,
    ) -> bool:
        binding = payload.get("voc_binding")
        return (
            isinstance(binding, Mapping)
            and binding.get("decision_context_sha256") == context_sha
        )

    def _decision_scoring_evidence(
        self,
        evaluation: _base.PairedVOCEvaluation,
    ) -> tuple[Mapping[str, Any], str]:
        evidence, record_sha = super()._decision_scoring_evidence(evaluation)
        store = self.compute_execution_store
        if store is None:
            return evidence, record_sha

        _, by_sha, _ = _ledger_records(self.decision_ledger)
        context_record = by_sha.get(evaluation.decision_context_sha256)
        if context_record is None:
            raise _base.VOCEvaluationError(
                "canonical VOC scoring route context is missing"
            )
        context_payload = getattr(context_record, "payload", None)
        context = (
            context_payload.get(_CONTEXT_KEY)
            if isinstance(context_payload, Mapping)
            else None
        )
        if not isinstance(context, Mapping):
            raise _base.VOCEvaluationError(
                "canonical VOC scoring route context is invalid"
            )
        request_id = _base._text(
            context.get("request_id"), field="VOC scoring request_id"
        )
        precompute = store.get_voc_precompute_admission(request_id)
        # A compute store may coexist with historical legacy cohorts. Only an
        # immutable router precommit opts an episode into the stronger explicit
        # production authority contract.
        if precompute is None:
            return evidence, record_sha

        reveal_at = _base._instant(
            evaluation.outcome_revealed_at,
            field="VOC outcome_revealed_at",
        )
        precompute_recorded_at = _base._instant(
            precompute.get("authority_recorded_at"),
            field="router VOC precompute authority_recorded_at",
        )
        if precompute_recorded_at >= reveal_at:
            raise _base.VOCEvaluationError(
                "router VOC precompute authority was not physically recorded before outcome reveal"
            )

        expected_identity = {
            "baseline": {
                "candidate_id": evaluation.baseline_candidate_id,
                "backend_id": evaluation.baseline_backend_id,
                "model_id": evaluation.baseline_model_id,
                "config_sha256": evaluation.baseline_config_sha256,
            },
            "challenger": {
                "candidate_id": evaluation.challenger_candidate_id,
                "backend_id": evaluation.challenger_backend_id,
                "model_id": evaluation.challenger_model_id,
                "config_sha256": evaluation.challenger_config_sha256,
            },
        }
        shadows: dict[str, Mapping[str, Any]] = {}
        for role in ("baseline", "challenger"):
            if precompute.get(f"{role}_compute_identity") != expected_identity[role]:
                raise _base.VOCEvaluationError(
                    "router VOC precompute compute identity does not match paired evaluation"
                )
            shadow = store.get_voc_shadow_execution(request_id, role)
            if shadow is None:
                raise _base.VOCEvaluationError(
                    "positive VOC score lacks canonical router shadow execution"
                )
            if shadow.get("candidate_identity") != expected_identity[role]:
                raise _base.VOCEvaluationError(
                    "canonical router shadow execution identity mismatch"
                )
            shadow_recorded_at = _base._instant(
                shadow.get("authority_recorded_at"),
                field=f"{role} shadow authority_recorded_at",
            )
            if shadow_recorded_at >= reveal_at:
                raise _base.VOCEvaluationError(
                    "canonical router shadow execution was not physically recorded before outcome reveal"
                )
            shadows[role] = shadow

        expected_output = {
            "baseline": evaluation.baseline_output_sha256,
            "challenger": evaluation.challenger_output_sha256,
        }
        expected_action = {
            "baseline": evaluation.baseline_action,
            "challenger": evaluation.challenger_action,
        }
        expected_abstained = {
            "baseline": evaluation.baseline_abstained,
            "challenger": evaluation.challenger_abstained,
        }
        expected_completed = {
            "baseline": _base._instant(
                evaluation.baseline_completed_at,
                field="baseline_completed_at",
            ),
            "challenger": _base._instant(
                evaluation.challenger_completed_at,
                field="challenger_completed_at",
            ),
        }
        for role, shadow in shadows.items():
            if (
                shadow.get("output_sha256") != expected_output[role]
                or shadow.get("action") != expected_action[role]
                or shadow.get("abstained") is not expected_abstained[role]
                or _base._instant(
                    shadow.get("completed_at"),
                    field=f"{role} shadow completed_at",
                )
                != expected_completed[role]
            ):
                raise _base.VOCEvaluationError(
                    "canonical router shadow execution does not match scored paired output"
                )

        samples = evidence.get("samples")
        if not isinstance(samples, (list, tuple)) or not samples:
            raise _base.VOCEvaluationError(
                "canonical VOC scoring samples are missing"
            )
        baseline_cost = _base._decimal(
            shadows["baseline"].get("actual_cost"),
            field="baseline canonical shadow actual_cost",
            nonnegative=True,
        )
        challenger_cost = _base._decimal(
            shadows["challenger"].get("actual_cost"),
            field="challenger canonical shadow actual_cost",
            nonnegative=True,
        )
        for sample in samples:
            if not isinstance(sample, Mapping):
                raise _base.VOCEvaluationError(
                    "canonical VOC scoring sample is invalid"
                )
            if (
                _base._decimal(
                    sample.get("baseline_compute_cost"),
                    field="baseline sample compute cost",
                    nonnegative=True,
                )
                != baseline_cost
                or _base._decimal(
                    sample.get("challenger_compute_cost"),
                    field="challenger sample compute cost",
                    nonnegative=True,
                )
                != challenger_cost
                or _base._instant(
                    sample.get("baseline_completed_at"),
                    field="baseline sample completed_at",
                )
                != expected_completed["baseline"]
                or _base._instant(
                    sample.get("challenger_completed_at"),
                    field="challenger sample completed_at",
                )
                != expected_completed["challenger"]
            ):
                raise _base.VOCEvaluationError(
                    "scored VOC cost/timing is not bound to canonical router shadow execution"
                )

        if challenger_cost < baseline_cost:
            raise _base.VOCEvaluationError(
                "canonical challenger shadow cost is below baseline cost"
            )
        with localcontext(_base._ARITHMETIC_CONTEXT):
            measured_extra_cost = +(challenger_cost - baseline_cost)
        if evaluation.measured_compute_cost != measured_extra_cost:
            raise _base.VOCEvaluationError(
                "paired evaluation measured compute cost is not canonical router cost delta"
            )
        return evidence, record_sha

    def _explicit_admissions(
        self,
        *,
        ordered: list[tuple[str, object]],
        by_sha: dict[str, object],
        order: dict[str, int],
        recorded_from: datetime,
        recorded_through: datetime,
        expected_task_class: str,
        expected_scope: Mapping[str, str],
        expected_baseline: Mapping[str, str],
        expected_challenger: Mapping[str, str],
        expected_protocol_id: str | None = None,
        expected_cohort_id: str | None = None,
    ) -> dict[str, tuple[object, Mapping[str, Any]]]:
        matches: dict[str, tuple[object, Mapping[str, Any]]] = {}
        admission_ids: set[str] = set()
        context_ids: set[str] = set()
        request_ids: set[str] = set()
        for admission_sha, record in ordered:
            if getattr(record, "action", None) != _ADMISSION_ACTION:
                continue
            recorded_at = _base._instant(
                getattr(record, "recorded_at"), field="VOC admission recorded_at"
            )
            if recorded_at < recorded_from or recorded_at > recorded_through:
                continue
            payload = getattr(record, "payload", None)
            raw = payload.get(_ADMISSION_KEY) if isinstance(payload, Mapping) else None
            if not isinstance(raw, Mapping) or set(raw) != _ADMISSION_FIELDS:
                raise _base.VOCEvaluationError(
                    "canonical VOC paired admission schema is invalid"
                )
            if raw.get("schema_version") != 1:
                raise _base.VOCEvaluationError(
                    "canonical VOC paired admission schema version is unsupported"
                )
            task_class = _base._text(
                raw.get("task_class"), field="VOC admission task_class"
            )
            scope = _nested_scope(raw.get("scope"), field="VOC admission scope")
            baseline = _nested_identity(
                raw.get("baseline_compute_identity"),
                field="VOC admission baseline_compute_identity",
            )
            challenger = _nested_identity(
                raw.get("challenger_compute_identity"),
                field="VOC admission challenger_compute_identity",
            )
            if (
                task_class != expected_task_class
                or scope != dict(expected_scope)
                or baseline != dict(expected_baseline)
                or challenger != dict(expected_challenger)
            ):
                continue
            protocol_id = _base._text(
                raw.get("research_protocol_id"),
                field="VOC admission research_protocol_id",
            )
            cohort_id = _base._text(
                raw.get("cohort_id"), field="VOC admission cohort_id"
            )
            if (
                expected_protocol_id is not None
                and protocol_id != expected_protocol_id
            ) or (
                expected_cohort_id is not None and cohort_id != expected_cohort_id
            ):
                continue
            admission_id = _base._text(
                raw.get("admission_id"), field="VOC admission admission_id"
            )
            decision_input = _base._sha256(
                raw.get("decision_input_sha256"),
                field="VOC admission decision_input_sha256",
            )
            context_sha = _base._sha256(
                raw.get("decision_context_sha256"),
                field="VOC admission decision_context_sha256",
            )
            deadline = _base._instant(
                raw.get("decision_deadline"),
                field="VOC admission decision_deadline",
            )
            if deadline <= recorded_at:
                raise _base.VOCEvaluationError(
                    "canonical VOC paired admission deadline is not after admission"
                )
            if getattr(record, "context_hash", None) != decision_input:
                raise _base.VOCEvaluationError(
                    "canonical VOC paired admission context hash mismatch"
                )
            context_record = by_sha.get(context_sha)
            if context_record is None or order[context_sha] >= order[admission_sha]:
                raise _base.VOCEvaluationError(
                    "canonical VOC paired admission lacks prior route context"
                )
            if getattr(context_record, "action", None) != _CONTEXT_ACTION:
                raise _base.VOCEvaluationError(
                    "canonical VOC paired admission route context action is invalid"
                )
            context_payload = getattr(context_record, "payload", None)
            context = (
                context_payload.get(_CONTEXT_KEY)
                if isinstance(context_payload, Mapping)
                else None
            )
            if not isinstance(context, Mapping):
                raise _base.VOCEvaluationError(
                    "canonical VOC paired admission route context is missing"
                )
            if (
                context.get("decision_input_sha256") != decision_input
                or context.get("task_class") != expected_task_class
                or _scope(context) != dict(expected_scope)
            ):
                raise _base.VOCEvaluationError(
                    "canonical VOC paired admission does not match route context"
                )
            request_id = _base._text(
                context.get("request_id"), field="VOC context request_id"
            )
            self._router_precompute_for_admission(
                admission_record=record,
                admission=raw,
                context_record=context_record,
            )
            if (
                admission_id in admission_ids
                or context_sha in context_ids
                or request_id in request_ids
            ):
                raise _base.VOCEvaluationError(
                    "precommitted VOC cohort reuses paired admission identity"
                )
            admission_ids.add(admission_id)
            context_ids.add(context_sha)
            request_ids.add(request_id)
            matches[admission_sha] = (record, raw)

        if expected_protocol_id is not None and expected_cohort_id is not None:
            canonical_request_ids = self._matching_router_precompute_request_ids(
                recorded_from=recorded_from,
                recorded_through=recorded_through,
                expected_task_class=expected_task_class,
                expected_scope=expected_scope,
                expected_baseline=expected_baseline,
                expected_challenger=expected_challenger,
                expected_protocol_id=expected_protocol_id,
                expected_cohort_id=expected_cohort_id,
            )
            if canonical_request_ids != request_ids:
                missing = canonical_request_ids - request_ids
                if missing:
                    raise _base.VOCEvaluationError(
                        "canonical router VOC precompute admission is missing from DecisionLedger"
                    )
                raise _base.VOCEvaluationError(
                    "DecisionLedger VOC admission is absent from canonical router precompute universe"
                )
        return matches

    def _router_precompute_for_admission(
        self,
        *,
        admission_record: object,
        admission: Mapping[str, Any],
        context_record: object,
    ) -> dict[str, Any]:
        store = self.compute_execution_store
        if store is None:
            raise _base.VOCEvaluationError(
                "explicit VOC paired admission requires canonical router precompute authority"
            )
        context_payload = getattr(context_record, "payload", None)
        context = (
            context_payload.get(_CONTEXT_KEY)
            if isinstance(context_payload, Mapping)
            else None
        )
        if not isinstance(context, Mapping):
            raise _base.VOCEvaluationError(
                "canonical VOC paired admission route context is missing"
            )
        request_id = _base._text(
            context.get("request_id"), field="VOC context request_id"
        )
        authority = store.get_voc_precompute_admission(request_id)
        if authority is None:
            raise _base.VOCEvaluationError(
                "explicit VOC paired admission lacks immutable router precompute authority"
            )
        expected = {
            "admission_id": _base._text(
                admission.get("admission_id"), field="VOC admission admission_id"
            ),
            "request_id": request_id,
            "decision_input_sha256": _base._sha256(
                admission.get("decision_input_sha256"),
                field="VOC admission decision_input_sha256",
            ),
            "decision_context_sha256": _base._sha256(
                admission.get("decision_context_sha256"),
                field="VOC admission decision_context_sha256",
            ),
            "decision_deadline": _base._instant(
                admission.get("decision_deadline"),
                field="VOC admission decision_deadline",
            ).isoformat().replace("+00:00", "Z"),
            "research_protocol_id": _base._text(
                admission.get("research_protocol_id"),
                field="VOC admission research_protocol_id",
            ),
            "cohort_id": _base._text(
                admission.get("cohort_id"), field="VOC admission cohort_id"
            ),
            "task_class": _base._text(
                admission.get("task_class"), field="VOC admission task_class"
            ),
            "scope": _nested_scope(
                admission.get("scope"), field="VOC admission scope"
            ),
            "baseline_compute_identity": _nested_identity(
                admission.get("baseline_compute_identity"),
                field="VOC admission baseline_compute_identity",
            ),
            "challenger_compute_identity": _nested_identity(
                admission.get("challenger_compute_identity"),
                field="VOC admission challenger_compute_identity",
            ),
        }
        for field, value in expected.items():
            if authority.get(field) != value:
                raise _base.VOCEvaluationError(
                    "explicit VOC paired admission does not match immutable router precompute authority"
                )
        context_recorded_at = _base._instant(
            getattr(context_record, "recorded_at"),
            field="VOC route context recorded_at",
        )
        _base._instant(
            authority.get("authority_recorded_at"),
            field="router VOC precompute authority_recorded_at",
        )
        router_admitted_at = _base._instant(
            authority.get("admitted_at"),
            field="router VOC precompute admitted_at",
        )
        ledger_admitted_at = _base._instant(
            getattr(admission_record, "recorded_at"),
            field="VOC admission recorded_at",
        )
        if router_admitted_at < context_recorded_at:
            raise _base.VOCEvaluationError(
                "router VOC precompute admission predates canonical route context"
            )
        if ledger_admitted_at < router_admitted_at:
            raise _base.VOCEvaluationError(
                "DecisionLedger VOC admission predates immutable router precompute authority"
            )
        _base._sha256(
            authority.get("route_record_sha256"),
            field="router VOC precompute route_record_sha256",
        )
        return authority

    def _matching_router_precompute_request_ids(
        self,
        *,
        recorded_from: datetime,
        recorded_through: datetime,
        expected_task_class: str,
        expected_scope: Mapping[str, str],
        expected_baseline: Mapping[str, str],
        expected_challenger: Mapping[str, str],
        expected_protocol_id: str,
        expected_cohort_id: str,
    ) -> set[str]:
        store = self.compute_execution_store
        if store is None:
            return set()
        result: set[str] = set()
        for authority in store.voc_precompute_admissions():
            admitted_at = _base._instant(
                authority.get("admitted_at"),
                field="router VOC precompute admitted_at",
            )
            if admitted_at < recorded_from or admitted_at > recorded_through:
                continue
            if (
                authority.get("task_class") != expected_task_class
                or authority.get("scope") != dict(expected_scope)
                or authority.get("baseline_compute_identity")
                != dict(expected_baseline)
                or authority.get("challenger_compute_identity")
                != dict(expected_challenger)
                or authority.get("research_protocol_id") != expected_protocol_id
                or authority.get("cohort_id") != expected_cohort_id
            ):
                continue
            request_id = _base._text(
                authority.get("request_id"),
                field="router VOC precompute request_id",
            )
            if request_id in result:
                raise _base.VOCEvaluationError(
                    "canonical router VOC precompute universe reuses request identity"
                )
            result.add(request_id)
        return result

    def _terminal_execution(
        self,
        *,
        terminal: Mapping[str, Any],
        admission_record: object,
        admission: Mapping[str, Any],
        context_record: object,
        terminal_record: object,
    ) -> ComputeExecutionEvidence:
        store = self.compute_execution_store
        if store is None:
            raise _base.VOCEvaluationError(
                "VOC negative terminal requires canonical compute execution authority"
            )
        evidence = _canonical_execution(store, terminal.get("execution_id"))
        record_sha = _base._sha256(
            terminal.get("execution_record_sha256"),
            field="VOC terminal execution_record_sha256",
        )
        if evidence.execution_record_sha256 != record_sha:
            raise _base.VOCEvaluationError(
                "VOC terminal execution reference does not match canonical measurement"
            )
        _execution_matches_admission(
            store,
            evidence,
            admission_record=admission_record,
            admission=admission,
            context_record=context_record,
            terminal_at=_base._instant(
                getattr(terminal_record, "recorded_at"),
                field="VOC terminal recorded_at",
            ),
        )
        return evidence

    def _terminal_for_admission(
        self,
        *,
        admission_sha: str,
        admission_record: object,
        admission: Mapping[str, Any],
        ordered: list[tuple[str, object]],
        order: dict[str, int],
        expected_scope: Mapping[str, str],
        expected_baseline: Mapping[str, str],
        expected_challenger: Mapping[str, str],
    ) -> tuple[str, object, Mapping[str, Any], str]:
        context_sha = _base._sha256(
            admission.get("decision_context_sha256"),
            field="VOC admission decision_context_sha256",
        )
        decision_input = _base._sha256(
            admission.get("decision_input_sha256"),
            field="VOC admission decision_input_sha256",
        )
        context_record: object | None = None
        candidates: list[tuple[str, object, Mapping[str, Any], str]] = []
        for record_sha, record in ordered:
            if record_sha == context_sha:
                context_record = record
            if order[record_sha] <= order[admission_sha]:
                continue
            payload = getattr(record, "payload", None)
            if not isinstance(payload, Mapping):
                continue
            if self._matching_binding(payload, context_sha=context_sha):
                candidates.append((record_sha, record, payload, "scored"))
                continue
            terminal = payload.get(_TERMINAL_KEY)
            if (
                isinstance(terminal, Mapping)
                and terminal.get("admission_sha256") == admission_sha
            ):
                candidates.append((record_sha, record, payload, "terminal"))

        if context_record is None:
            raise _base.VOCEvaluationError(
                "VOC paired admission canonical route context is missing"
            )
        if not candidates:
            raise _base.VOCEvaluationError(
                "eligible VOC paired admission lacks terminal record"
            )
        if len(candidates) != 1:
            raise _base.VOCEvaluationError(
                "eligible VOC paired admission has ambiguous terminal records"
            )
        record_sha, record, payload, kind = candidates[0]
        if _base._instant(
            getattr(record, "recorded_at"), field="VOC terminal recorded_at"
        ) < _base._instant(
            getattr(admission_record, "recorded_at"), field="VOC admission recorded_at"
        ):
            raise _base.VOCEvaluationError("VOC terminal predates paired admission")
        if getattr(record, "context_hash", None) != decision_input:
            raise _base.VOCEvaluationError(
                "VOC terminal context hash does not match paired admission"
            )

        if kind == "scored":
            binding = payload.get("voc_binding")
            evidence = payload.get(_base._SCORING_EVIDENCE_KEY)
            if not isinstance(binding, Mapping) or not isinstance(evidence, Mapping):
                raise _base.VOCEvaluationError(
                    "scored VOC terminal lacks canonical scoring evidence"
                )
            if (
                binding.get("decision_input_sha256") != decision_input
                or _scope(binding) != dict(expected_scope)
                or _compute_identity(binding, prefix="baseline")
                != dict(expected_baseline)
                or _compute_identity(binding, prefix="challenger")
                != dict(expected_challenger)
            ):
                raise _base.VOCEvaluationError(
                    "scored VOC terminal does not match paired admission"
                )
            if evidence.get("decision_input_sha256") != decision_input:
                raise _base.VOCEvaluationError(
                    "scored VOC evidence does not match paired admission"
                )
            return record_sha, record, payload, kind

        terminal = payload.get(_TERMINAL_KEY)
        if not isinstance(terminal, Mapping):
            raise _base.VOCEvaluationError("VOC terminal schema is invalid")
        terminal_version = terminal.get("schema_version")
        expected_terminal_fields = (
            _TERMINAL_FIELDS_V2
            if terminal_version == 2
            else _TERMINAL_FIELDS_V3
            if terminal_version == 3
            else None
        )
        if (
            expected_terminal_fields is None
            or set(terminal) != expected_terminal_fields
        ):
            raise _base.VOCEvaluationError(
                "VOC terminal schema/version is unsupported"
            )
        status = _base._text(terminal.get("status"), field="VOC terminal status")
        if status not in _NEGATIVE_TERMINAL_STATUSES:
            raise _base.VOCEvaluationError("VOC terminal status is unsupported")
        deadline = _base._instant(
            admission.get("decision_deadline"), field="VOC admission decision_deadline"
        )
        terminal_at = _base._instant(
            getattr(record, "recorded_at"), field="VOC terminal recorded_at"
        )
        if status in {"deadline_missed", "timeout"} and terminal_at < deadline:
            raise _base.VOCEvaluationError(
                "VOC deadline terminal predates frozen decision deadline"
            )
        self._terminal_execution(
            terminal=terminal,
            admission_record=admission_record,
            admission=admission,
            context_record=context_record,
            terminal_record=record,
        )
        return record_sha, record, payload, kind

    def _matching_route_contexts(
        self,
        *,
        ordered: list[tuple[str, object]],
        recorded_from: datetime,
        recorded_through: datetime,
        expected_task_class: str,
        expected_scope: Mapping[str, str],
    ) -> set[str]:
        matches: set[str] = set()
        request_ids: set[str] = set()
        for context_sha, record in ordered:
            if getattr(record, "action", None) != _CONTEXT_ACTION:
                continue
            recorded_at = _base._instant(
                getattr(record, "recorded_at"), field="VOC cohort context recorded_at"
            )
            if recorded_at < recorded_from or recorded_at > recorded_through:
                continue
            payload = getattr(record, "payload", None)
            context = payload.get(_CONTEXT_KEY) if isinstance(payload, Mapping) else None
            if not isinstance(context, Mapping):
                raise _base.VOCEvaluationError("canonical VOC route context is missing")
            if (
                context.get("task_class") != expected_task_class
                or _scope(context) != dict(expected_scope)
            ):
                continue
            request_id = _base._text(
                context.get("request_id"), field="VOC context request_id"
            )
            decision_input = _base._sha256(
                context.get("decision_input_sha256"),
                field="VOC context decision_input_sha256",
            )
            if getattr(record, "context_hash", None) != decision_input:
                raise _base.VOCEvaluationError(
                    "canonical VOC route context hash mismatch"
                )
            if request_id in request_ids:
                raise _base.VOCEvaluationError(
                    "precommitted VOC eligibility range reuses request identity"
                )
            request_ids.add(request_id)
            matches.add(context_sha)
        return matches

    def _legacy_eligible_cohort_decisions(
        self,
        *,
        ordered: list[tuple[str, object]],
        by_sha: dict[str, object],
        recorded_from: datetime,
        recorded_through: datetime,
        expected_task_class: str,
        expected_scope: Mapping[str, str],
        expected_baseline: Mapping[str, str],
        expected_challenger: Mapping[str, str],
    ) -> dict[str, str]:
        """Read pre-admission-format cohorts without allowing silent omission."""

        context_shas = self._matching_route_contexts(
            ordered=ordered,
            recorded_from=recorded_from,
            recorded_through=recorded_through,
            expected_task_class=expected_task_class,
            expected_scope=expected_scope,
        )
        if not context_shas:
            raise _base.VOCEvaluationError(
                "precommitted VOC eligibility range contains no canonical admissions"
            )
        admissions: dict[str, tuple[str, str]] = {}
        for context_sha in context_shas:
            record = by_sha[context_sha]
            payload = getattr(record, "payload", None)
            context = payload.get(_CONTEXT_KEY) if isinstance(payload, Mapping) else None
            if not isinstance(context, Mapping):
                raise _base.VOCEvaluationError("canonical VOC route context is missing")
            admissions[context_sha] = (
                _base._text(context.get("request_id"), field="VOC context request_id"),
                _base._sha256(
                    context.get("decision_input_sha256"),
                    field="VOC context decision_input_sha256",
                ),
            )

        eligible: dict[str, str] = {}
        for context_sha, (_, decision_input) in admissions.items():
            terminals: list[tuple[str, object, Mapping[str, Any]]] = []
            for record_sha, record in ordered:
                payload = getattr(record, "payload", None)
                if not isinstance(payload, Mapping):
                    continue
                if self._matching_binding(payload, context_sha=context_sha):
                    terminals.append((record_sha, record, payload))
                else:
                    terminal = payload.get(_TERMINAL_KEY)
                    if (
                        isinstance(terminal, Mapping)
                        and terminal.get("decision_context_sha256") == context_sha
                    ):
                        terminals.append((record_sha, record, payload))
            if not terminals:
                raise _base.VOCEvaluationError(
                    "eligible VOC admission lacks terminal paired record"
                )
            if len(terminals) != 1:
                raise _base.VOCEvaluationError(
                    "eligible VOC admission has ambiguous terminal paired records"
                )
            record_sha, _, payload = terminals[0]
            binding = payload.get("voc_binding")
            evidence = payload.get(_base._SCORING_EVIDENCE_KEY)
            if not isinstance(binding, Mapping) or not isinstance(evidence, Mapping):
                raise _base.VOCEvaluationError(
                    "legacy VOC admission terminated without score"
                )
            if (
                binding.get("decision_input_sha256") != decision_input
                or _scope(binding) != dict(expected_scope)
                or _compute_identity(binding, prefix="baseline")
                != dict(expected_baseline)
                or _compute_identity(binding, prefix="challenger")
                != dict(expected_challenger)
            ):
                raise _base.VOCEvaluationError(
                    "legacy VOC terminal does not match frozen cohort"
                )
            evaluation_id = _base._text(
                evidence.get("evaluation_id"), field="eligible VOC evaluation_id"
            )
            if evidence.get("decision_input_sha256") != decision_input:
                raise _base.VOCEvaluationError(
                    "eligible VOC scoring evidence does not match context input"
                )
            prior = eligible.get(evaluation_id)
            if prior is not None and prior != record_sha:
                raise _base.VOCEvaluationError(
                    "precommitted VOC eligibility range contains duplicate evaluation identity"
                )
            eligible[evaluation_id] = record_sha
        return dict(sorted(eligible.items()))

    def _eligible_cohort_decisions(
        self,
        *,
        recorded_from: datetime,
        recorded_through: datetime,
        expected_task_class: str,
        expected_scope: Mapping[str, str],
        expected_baseline: Mapping[str, str],
        expected_challenger: Mapping[str, str],
    ) -> dict[str, str]:
        ordered, by_sha, order = _ledger_records(self.decision_ledger)
        explicit = self._explicit_admissions(
            ordered=ordered,
            by_sha=by_sha,
            order=order,
            recorded_from=recorded_from,
            recorded_through=recorded_through,
            expected_task_class=expected_task_class,
            expected_scope=expected_scope,
            expected_baseline=expected_baseline,
            expected_challenger=expected_challenger,
        )
        if not explicit:
            return self._legacy_eligible_cohort_decisions(
                ordered=ordered,
                by_sha=by_sha,
                recorded_from=recorded_from,
                recorded_through=recorded_through,
                expected_task_class=expected_task_class,
                expected_scope=expected_scope,
                expected_baseline=expected_baseline,
                expected_challenger=expected_challenger,
            )

        matching_contexts = self._matching_route_contexts(
            ordered=ordered,
            recorded_from=recorded_from,
            recorded_through=recorded_through,
            expected_task_class=expected_task_class,
            expected_scope=expected_scope,
        )
        explicit_contexts = {
            _base._sha256(
                admission.get("decision_context_sha256"),
                field="VOC admission decision_context_sha256",
            )
            for _, admission in explicit.values()
        }
        if matching_contexts - explicit_contexts:
            raise _base.VOCEvaluationError(
                "mixed legacy and explicit VOC cohort formats require a frozen migration boundary"
            )

        eligible: dict[str, str] = {}
        protocol_ids: set[str] = set()
        cohort_ids: set[str] = set()
        for admission_sha, (admission_record, admission) in explicit.items():
            protocol_ids.add(
                _base._text(
                    admission.get("research_protocol_id"),
                    field="VOC admission research_protocol_id",
                )
            )
            cohort_ids.add(
                _base._text(
                    admission.get("cohort_id"), field="VOC admission cohort_id"
                )
            )
            record_sha, _, payload, kind = self._terminal_for_admission(
                admission_sha=admission_sha,
                admission_record=admission_record,
                admission=admission,
                ordered=ordered,
                order=order,
                expected_scope=expected_scope,
                expected_baseline=expected_baseline,
                expected_challenger=expected_challenger,
            )
            if kind != "scored":
                continue
            evidence = payload[_base._SCORING_EVIDENCE_KEY]
            evaluation_id = _base._text(
                evidence.get("evaluation_id"), field="eligible VOC evaluation_id"
            )
            prior = eligible.get(evaluation_id)
            if prior is not None and prior != record_sha:
                raise _base.VOCEvaluationError(
                    "precommitted VOC eligibility range contains duplicate evaluation identity"
                )
            eligible[evaluation_id] = record_sha

        if len(protocol_ids) != 1 or len(cohort_ids) != 1:
            raise _base.VOCEvaluationError(
                "explicit VOC admissions mix protocol or cohort identities"
            )
        if not eligible:
            raise _base.VOCEvaluationError(
                "explicit VOC cohort contains no scored evaluation target"
            )
        return dict(sorted(eligible.items()))

    def _protocol_contract(
        self, evaluation: _base.PairedVOCEvaluation
    ) -> tuple[str, datetime, datetime]:
        protocol_entry = self.scientific_registry.get(
            "ResearchProtocol", evaluation.research_protocol_id
        )
        if protocol_entry is None:
            raise _base.VOCEvaluationError(
                "canonical ResearchProtocol is missing for VOC admission"
            )
        binding = protocol_entry.payload.get("binding")
        design_text = (
            binding.get("evaluation_design") if isinstance(binding, Mapping) else None
        )
        if type(design_text) is not str:
            raise _base.VOCEvaluationError(
                "canonical VOC evaluation design is missing"
            )
        try:
            design = json.loads(design_text)
        except json.JSONDecodeError as exc:
            raise _base.VOCEvaluationError(
                "canonical VOC evaluation design is invalid JSON"
            ) from exc
        if not isinstance(design, Mapping):
            raise _base.VOCEvaluationError(
                "canonical VOC evaluation design is invalid"
            )
        cohort_id = _base._text(design.get("cohort_id"), field="VOC cohort_id")
        eligibility = design.get("cohort_eligibility")
        if not isinstance(eligibility, Mapping):
            raise _base.VOCEvaluationError(
                "canonical VOC cohort eligibility is missing"
            )
        recorded_from = _base._instant(
            eligibility.get("decision_recorded_from"),
            field="VOC cohort decision_recorded_from",
        )
        recorded_through = _base._instant(
            eligibility.get("decision_recorded_through"),
            field="VOC cohort decision_recorded_through",
        )
        return cohort_id, recorded_from, recorded_through

    def _negative_terminal_episodes(
        self,
        evaluation: _base.PairedVOCEvaluation,
        *,
        cohort_available_at: str,
    ) -> list[tuple[_base.OutcomeDerivedVOCScore, str, dict[str, str]]]:
        cohort_id, recorded_from, recorded_through = self._protocol_contract(evaluation)
        expected_scope = {
            "sport_id": evaluation.sport_id,
            "league_id": evaluation.league_id,
            "regime_id": evaluation.regime_id,
            "urgency_id": evaluation.urgency_id,
            "contradiction_state": evaluation.contradiction_state,
        }
        expected_baseline = {
            "candidate_id": evaluation.baseline_candidate_id,
            "backend_id": evaluation.baseline_backend_id,
            "model_id": evaluation.baseline_model_id,
            "config_sha256": evaluation.baseline_config_sha256,
        }
        expected_challenger = {
            "candidate_id": evaluation.challenger_candidate_id,
            "backend_id": evaluation.challenger_backend_id,
            "model_id": evaluation.challenger_model_id,
            "config_sha256": evaluation.challenger_config_sha256,
        }
        ordered, by_sha, order = _ledger_records(self.decision_ledger)
        explicit = self._explicit_admissions(
            ordered=ordered,
            by_sha=by_sha,
            order=order,
            recorded_from=recorded_from,
            recorded_through=recorded_through,
            expected_task_class=evaluation.task_class,
            expected_scope=expected_scope,
            expected_baseline=expected_baseline,
            expected_challenger=expected_challenger,
            expected_protocol_id=evaluation.research_protocol_id,
            expected_cohort_id=cohort_id,
        )
        if not explicit:
            return []

        rule = self._scoring_rule(evaluation)
        compute_multiplier = _base._decimal(
            rule.get("compute_cost_multiplier"),
            field="VOC compute_cost_multiplier",
            nonnegative=True,
        )
        latency_rate = _base._decimal(
            rule.get("latency_cost_per_second"),
            field="VOC latency_cost_per_second",
            nonnegative=True,
        )
        frozen_at = _base._instant(
            cohort_available_at, field="VOC cohort available_at"
        )
        outcome_revealed_at = _base._instant(
            evaluation.outcome_revealed_at,
            field="VOC outcome_revealed_at",
        )
        failures: list[tuple[_base.OutcomeDerivedVOCScore, str, dict[str, str]]] = []
        failure_cluster = _base._digest(
            {
                "schema": "autosport.voc_terminal_failure_cluster",
                "schema_version": 2,
                "research_protocol_id": evaluation.research_protocol_id,
                "cohort_id": cohort_id,
                "scope": expected_scope,
            }
        )
        for admission_sha, (admission_record, admission) in explicit.items():
            terminal_sha, terminal_record, payload, kind = self._terminal_for_admission(
                admission_sha=admission_sha,
                admission_record=admission_record,
                admission=admission,
                ordered=ordered,
                order=order,
                expected_scope=expected_scope,
                expected_baseline=expected_baseline,
                expected_challenger=expected_challenger,
            )
            if kind == "scored":
                continue
            terminal = payload[_TERMINAL_KEY]
            terminal_at = _base._instant(
                getattr(terminal_record, "recorded_at"),
                field="VOC terminal recorded_at",
            )
            if terminal_at > frozen_at:
                raise _base.VOCEvaluationError(
                    "VOC cohort froze before a terminal admission outcome"
                )
            context_sha = _base._sha256(
                admission.get("decision_context_sha256"),
                field="VOC admission decision_context_sha256",
            )
            context_record = by_sha.get(context_sha)
            if context_record is None:
                raise _base.VOCEvaluationError(
                    "VOC terminal canonical route context is missing"
                )
            precompute = self._router_precompute_for_admission(
                admission_record=admission_record,
                admission=admission,
                context_record=context_record,
            )
            if terminal.get("schema_version") != 3:
                raise _base.VOCEvaluationError(
                    "negative VOC terminal lacks production physical authority"
                )
            terminal_authority_at = _base._instant(
                terminal.get("authority_recorded_at"),
                field="VOC terminal authority_recorded_at",
            )
            precompute_authority_at = _base._instant(
                precompute.get("authority_recorded_at"),
                field="router VOC precompute authority_recorded_at",
            )
            if terminal_authority_at < precompute_authority_at:
                raise _base.VOCEvaluationError(
                    "negative VOC terminal physical authority predates router precompute authority"
                )
            if terminal_authority_at >= outcome_revealed_at:
                raise _base.VOCEvaluationError(
                    "negative VOC terminal was not physically recorded before outcome reveal"
                )
            execution = self._terminal_execution(
                terminal=terminal,
                admission_record=admission_record,
                admission=admission,
                context_record=context_record,
                terminal_record=terminal_record,
            )
            extra_cost = execution.actual_cost
            extra_latency = execution.actual_latency_seconds
            with localcontext(_base._ARITHMETIC_CONTEXT):
                compute_penalty = +(extra_cost * compute_multiplier)
                latency_penalty = +(extra_latency * latency_rate)
                terminal_net = +(_base._ZERO - compute_penalty - latency_penalty)
            source_sha = _base._digest(
                {
                    "schema": "autosport.canonical_voc_terminal_score_sources",
                    "schema_version": 2,
                    "admission_sha256": admission_sha,
                    "terminal_sha256": terminal_sha,
                    "terminal_status": terminal.get("status"),
                    "execution_id": execution.execution_id,
                    "execution_record_sha256": execution.execution_record_sha256,
                    "execution_evidence_sha256": execution.evidence_sha256,
                    "research_protocol_sha256": evaluation.research_protocol_sha256,
                    "scoring_rule_sha256": evaluation.scoring_rule_sha256,
                }
            )
            admission_id = _base._text(
                admission.get("admission_id"), field="VOC admission admission_id"
            )
            score = _base.OutcomeDerivedVOCScore(
                evaluation_id=f"terminal:{admission_id}",
                available_at=cohort_available_at,
                outcome_evidence_sha256=evaluation.outcome_evidence_sha256,
                scoring_rule_sha256=evaluation.scoring_rule_sha256,
                research_protocol_sha256=evaluation.research_protocol_sha256,
                holdout_access_id=evaluation.holdout_access_id,
                multiple_comparison_control_sha256=(
                    evaluation.multiple_comparison_control_sha256
                ),
                baseline_utility=_base._ZERO,
                challenger_utility=_base._ZERO,
                compute_cost_penalty=compute_penalty,
                latency_opportunity_cost_penalty=latency_penalty,
                measured_compute_cost=extra_cost,
                paired_sample_count=1,
                effective_sample_size=1,
                support_fraction=_base._ONE,
                incremental_value_interval_low=terminal_net,
                incremental_value_interval_high=terminal_net,
                source_artifact_sha256=source_sha,
            )
            failures.append(
                (
                    score,
                    failure_cluster,
                    {
                        "admission_id": admission_id,
                        "admission_sha256": admission_sha,
                        "terminal_sha256": terminal_sha,
                        "terminal_status": _base._text(
                            terminal.get("status"), field="VOC terminal status"
                        ),
                        "execution_id": execution.execution_id,
                        "execution_record_sha256": execution.execution_record_sha256,
                        "terminal_source_artifact_sha256": source_sha,
                    },
                )
            )
        return failures

    def _derive_score(
        self,
        *,
        evaluation: _base.PairedVOCEvaluation,
        as_of: datetime,
    ) -> _base.OutcomeDerivedVOCScore:
        cohort_id, cohort_available_at, cohort_sha256, members = self._cohort_members(
            evaluation, as_of=as_of
        )

        episode_scores: list[_base.OutcomeDerivedVOCScore] = []
        episode_clusters: list[str] = []
        member_sources: list[dict[str, str]] = []
        for member in members:
            evidence, decision_record_sha256 = self._decision_scoring_evidence(member)
            rule = self._scoring_rule(member)
            outcomes, _, outcome_source_sha256, outcome_cluster_sha256 = (
                self._revealed_outcomes(member, as_of=as_of)
            )
            episode = self._derive_episode_score(
                evaluation=member,
                evidence=evidence,
                decision_record_sha256=decision_record_sha256,
                rule=rule,
                outcomes=outcomes,
                outcome_source_sha256=outcome_source_sha256,
            )
            episode_scores.append(episode)
            episode_clusters.append(outcome_cluster_sha256)
            member_sources.append(
                {
                    "evaluation_id": member.evaluation_id,
                    "evaluation_sha256": member.evaluation_sha256,
                    "episode_source_artifact_sha256": episode.source_artifact_sha256,
                    "outcome_cluster_sha256": outcome_cluster_sha256,
                }
            )

        failures = self._negative_terminal_episodes(
            evaluation, cohort_available_at=cohort_available_at
        )
        for failure, cluster_sha, source in failures:
            episode_scores.append(failure)
            episode_clusters.append(cluster_sha)
            member_sources.append(source)

        clustered: dict[str, list[_base.OutcomeDerivedVOCScore]] = {}
        for cluster_sha256, episode in zip(episode_clusters, episode_scores):
            clustered.setdefault(cluster_sha256, []).append(episode)
        ordered_clusters = [clustered[key] for key in sorted(clustered)]

        def cluster_weighted_mean(attribute: str, *, field: str) -> Decimal:
            return _base._mean(
                [
                    _base._mean(
                        [getattr(score, attribute) for score in cluster],
                        field=f"{field} within outcome cluster",
                    )
                    for cluster in ordered_clusters
                ],
                field=field,
            )

        baseline_utility = cluster_weighted_mean(
            "baseline_utility", field="cohort baseline utility"
        )
        challenger_utility = cluster_weighted_mean(
            "challenger_utility", field="cohort challenger utility"
        )
        compute_cost_penalty = cluster_weighted_mean(
            "compute_cost_penalty", field="cohort compute cost penalty"
        )
        latency_opportunity_cost_penalty = cluster_weighted_mean(
            "latency_opportunity_cost_penalty",
            field="cohort latency opportunity cost penalty",
        )
        measured_compute_cost = cluster_weighted_mean(
            "measured_compute_cost", field="cohort measured compute cost"
        )
        cluster_net_values = [
            _base._mean(
                [score.net_value for score in cluster],
                field="net VOC within outcome cluster",
            )
            for cluster in ordered_clusters
        ]
        source_artifact_sha256 = _base._digest(
            {
                "schema": "autosport.canonical_voc_cohort_score_sources",
                "schema_version": 4,
                "target_evaluation_id": evaluation.evaluation_id,
                "cohort_id": cohort_id,
                "cohort_sha256": cohort_sha256,
                "members": member_sources,
                "paired_admission_count": len(episode_scores),
                "outcome_clusters": sorted(clustered),
            }
        )
        return _base.OutcomeDerivedVOCScore(
            evaluation_id=evaluation.evaluation_id,
            available_at=cohort_available_at,
            outcome_evidence_sha256=evaluation.outcome_evidence_sha256,
            scoring_rule_sha256=evaluation.scoring_rule_sha256,
            research_protocol_sha256=evaluation.research_protocol_sha256,
            holdout_access_id=evaluation.holdout_access_id,
            multiple_comparison_control_sha256=(
                evaluation.multiple_comparison_control_sha256
            ),
            baseline_utility=baseline_utility,
            challenger_utility=challenger_utility,
            compute_cost_penalty=compute_cost_penalty,
            latency_opportunity_cost_penalty=latency_opportunity_cost_penalty,
            measured_compute_cost=measured_compute_cost,
            paired_sample_count=len(episode_scores),
            effective_sample_size=len(ordered_clusters),
            support_fraction=_base._ONE,
            incremental_value_interval_low=min(cluster_net_values),
            incremental_value_interval_high=max(cluster_net_values),
            source_artifact_sha256=source_artifact_sha256,
        )


def build_canonical_voc_authority_resolver(
    *,
    decision_ledger: _base.JsonlDecisionLedger,
    scientific_registry: _base.ScientificRegistry,
    outcome_authority: _base.MarketSettlementOutcomeAuthority,
    outcome_source_root: str | Path,
    source_record_file: str,
    source_record_sha256: str,
    additional_outcome_sources: tuple[_base.CanonicalVOCOutcomeSource, ...] = (),
    compute_execution_store: ModelComputeRouterStore | None = None,
) -> _base.CanonicalVOCAuthorityResolver:
    """Build the production resolver with terminal-aware canonical authority."""

    score_authority = CanonicalOutcomeDerivedVOCScoreAuthority(
        decision_ledger=decision_ledger,
        scientific_registry=scientific_registry,
        outcome_authority=outcome_authority,
        outcome_source_root=outcome_source_root,
        source_record_file=source_record_file,
        source_record_sha256=source_record_sha256,
        additional_outcome_sources=additional_outcome_sources,
        compute_execution_store=compute_execution_store,
    )
    return _base.CanonicalVOCAuthorityResolver(
        decision_ledger=decision_ledger,
        scientific_registry=scientific_registry,
        outcome_authority=outcome_authority,
        outcome_score_authority=score_authority,
    )
