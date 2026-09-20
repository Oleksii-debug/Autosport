"""Canonical realized-VOC authority with explicit pre-compute admission.

The qualified causal scorer remains in ``_voc_outcome_scoring_base``.  This
wrapper adds the missing experiment-admission/terminal layer:

* an explicit ``VOC_PAIRED_ADMISSION`` record is written before candidate
  outputs and binds protocol/cohort, deadline, scope and both compute identities;
* every explicit admission must close exactly once, either with the existing
  successful ``voc_binding`` + ``voc_scoring_evidence`` record or a typed
  non-scored terminal record;
* non-scored terminals contribute a protocol-derived, never-positive realized
  VOC observation instead of disappearing or making all history unusable.

Legacy cohorts that predate the explicit admission record remain readable through
the previous fail-closed route-context path.  Once an explicit admission exists
for a cohort window, generic route contexts are no longer denominator authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from . import _voc_outcome_scoring_base as _base
from .decision_ledger import DecisionRecord, JsonlDecisionLedger

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
_TERMINAL_FIELDS = frozenset(
    {
        "schema_version",
        "admission_sha256",
        "status",
        "observed_extra_compute_cost",
        "observed_extra_latency_seconds",
    }
)
_NEGATIVE_TERMINAL_STATUSES = frozenset(
    {"deadline_missed", "timeout", "failed", "cancelled", "abstained", "null"}
)


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
    result = {
        "candidate_id": _base._text(value.get("candidate_id"), field=f"{field} candidate_id"),
        "backend_id": _base._text(value.get("backend_id"), field=f"{field} backend_id"),
        "model_id": _base._text(value.get("model_id"), field=f"{field} model_id"),
        "config_sha256": _base._sha256(
            value.get("config_sha256"), field=f"{field} config_sha256"
        ),
    }
    return result


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
    deadline = _base._instant(decision_deadline, field="VOC admission decision_deadline")
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
    admission_sha256: str,
    status: str,
    observed_extra_compute_cost: Decimal,
    observed_extra_latency_seconds: Decimal,
    replay_run_id: str,
    agent: str,
    recorded_at: str,
) -> str:
    """Close an admitted non-scored attempt with conservative measured penalties."""

    if not isinstance(decision_ledger, JsonlDecisionLedger):
        raise TypeError("decision_ledger must be JsonlDecisionLedger")
    admission_sha = _base._sha256(
        admission_sha256, field="VOC terminal admission_sha256"
    )
    status_text = _base._text(status, field="VOC terminal status")
    if status_text not in _NEGATIVE_TERMINAL_STATUSES:
        raise _base.VOCEvaluationError("VOC terminal status is unsupported")
    if not isinstance(observed_extra_compute_cost, Decimal):
        raise TypeError("observed_extra_compute_cost must be Decimal")
    if not isinstance(observed_extra_latency_seconds, Decimal):
        raise TypeError("observed_extra_latency_seconds must be Decimal")
    if (
        not observed_extra_compute_cost.is_finite()
        or observed_extra_compute_cost < 0
        or not observed_extra_latency_seconds.is_finite()
        or observed_extra_latency_seconds < 0
    ):
        raise _base.VOCEvaluationError(
            "VOC terminal observed penalties must be finite non-negative"
        )

    _, by_sha, _ = _ledger_records(decision_ledger)
    admission_record = by_sha.get(admission_sha)
    if admission_record is None or getattr(admission_record, "action", None) != _ADMISSION_ACTION:
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
    decision_input = _base._sha256(
        admission.get("decision_input_sha256"),
        field="VOC admission decision_input_sha256",
    )
    terminal = {
        "schema_version": 1,
        "admission_sha256": admission_sha,
        "status": status_text,
        "observed_extra_compute_cost": str(observed_extra_compute_cost),
        "observed_extra_latency_seconds": str(observed_extra_latency_seconds),
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
                expected_cohort_id is not None
                and cohort_id != expected_cohort_id
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
            if admission_id in admission_ids or context_sha in context_ids:
                raise _base.VOCEvaluationError(
                    "precommitted VOC cohort reuses paired admission identity"
                )
            admission_ids.add(admission_id)
            context_ids.add(context_sha)
            matches[admission_sha] = (record, raw)
        return matches

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
        candidates: list[tuple[str, object, Mapping[str, Any], str]] = []
        for record_sha, record in ordered:
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
        if not isinstance(terminal, Mapping) or set(terminal) != _TERMINAL_FIELDS:
            raise _base.VOCEvaluationError("VOC terminal schema is invalid")
        if terminal.get("schema_version") != 1:
            raise _base.VOCEvaluationError("VOC terminal schema version is unsupported")
        status = _base._text(terminal.get("status"), field="VOC terminal status")
        if status not in _NEGATIVE_TERMINAL_STATUSES:
            raise _base.VOCEvaluationError("VOC terminal status is unsupported")
        _base._decimal(
            terminal.get("observed_extra_compute_cost"),
            field="VOC terminal observed_extra_compute_cost",
            nonnegative=True,
        )
        _base._decimal(
            terminal.get("observed_extra_latency_seconds"),
            field="VOC terminal observed_extra_latency_seconds",
            nonnegative=True,
        )
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
        return record_sha, record, payload, kind

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

        admissions: dict[str, tuple[str, str]] = {}
        request_ids: set[str] = set()
        for context_sha, record in ordered:
            recorded_at = _base._instant(
                getattr(record, "recorded_at"), field="VOC cohort context recorded_at"
            )
            if recorded_at < recorded_from or recorded_at > recorded_through:
                continue
            if getattr(record, "action", None) != _CONTEXT_ACTION:
                continue
            payload = getattr(record, "payload", None)
            context = payload.get(_CONTEXT_KEY) if isinstance(payload, Mapping) else None
            if not isinstance(context, Mapping):
                raise _base.VOCEvaluationError(
                    "canonical VOC route context is missing"
                )
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
            admissions[context_sha] = (request_id, decision_input)

        if not admissions:
            raise _base.VOCEvaluationError(
                "precommitted VOC eligibility range contains no canonical admissions"
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
            record_sha, record, payload = terminals[0]
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
                _base._text(admission.get("cohort_id"), field="VOC admission cohort_id")
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
                # Negative terminals remain denominator members but do not need a
                # PairedVOCEvaluation registry object.  _derive_score incorporates
                # their conservative contribution below.
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
            binding.get("evaluation_design")
            if isinstance(binding, Mapping)
            else None
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
        failures: list[tuple[_base.OutcomeDerivedVOCScore, str, dict[str, str]]] = []
        # All non-scored terminals share one conservative dependency cluster.
        failure_cluster = _base._digest(
            {
                "schema": "autosport.voc_terminal_failure_cluster",
                "schema_version": 1,
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
            extra_cost = _base._decimal(
                terminal.get("observed_extra_compute_cost"),
                field="VOC terminal observed_extra_compute_cost",
                nonnegative=True,
            )
            extra_latency = _base._decimal(
                terminal.get("observed_extra_latency_seconds"),
                field="VOC terminal observed_extra_latency_seconds",
                nonnegative=True,
            )
            with localcontext(_base._ARITHMETIC_CONTEXT):
                compute_penalty = +(extra_cost * compute_multiplier)
                latency_penalty = +(extra_latency * latency_rate)
                terminal_net = +(_base._ZERO - compute_penalty - latency_penalty)
            source_sha = _base._digest(
                {
                    "schema": "autosport.canonical_voc_terminal_score_sources",
                    "schema_version": 1,
                    "admission_sha256": admission_sha,
                    "terminal_sha256": terminal_sha,
                    "terminal_status": terminal.get("status"),
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
                "schema_version": 3,
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
) -> _base.CanonicalVOCAuthorityResolver:
    """Build the production resolver with pre-compute terminal-aware authority."""

    score_authority = CanonicalOutcomeDerivedVOCScoreAuthority(
        decision_ledger=decision_ledger,
        scientific_registry=scientific_registry,
        outcome_authority=outcome_authority,
        outcome_source_root=outcome_source_root,
        source_record_file=source_record_file,
        source_record_sha256=source_record_sha256,
        additional_outcome_sources=additional_outcome_sources,
    )
    return _base.CanonicalVOCAuthorityResolver(
        decision_ledger=decision_ledger,
        scientific_registry=scientific_registry,
        outcome_authority=outcome_authority,
        outcome_score_authority=score_authority,
    )
