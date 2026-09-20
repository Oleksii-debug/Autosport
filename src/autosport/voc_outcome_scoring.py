"""Canonical realized-VOC authority with pre-output denominator admission.

The statistically qualified scorer remains in ``_voc_outcome_scoring_base``.
This wrapper narrows only cohort-completeness semantics: a matching canonical
``VOC_ROUTE_CONTEXT`` inside the frozen protocol window is a pre-output paired
admission.  Denominator membership therefore exists before either candidate
produces an output.  Every admission must resolve to exactly one terminal paired
record; a missing/failed/timed-out result cannot disappear merely because no
``voc_binding`` or ``voc_scoring_evidence`` was produced.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import _voc_outcome_scoring_base as _base

# Preserve the existing public module surface while overriding only the authority
# whose denominator admission semantics changed.
for _name in dir(_base):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_base, _name)

_CONTEXT_ACTION = "VOC_ROUTE_CONTEXT"
_CONTEXT_KEY = "voc_current_context"
_TERMINAL_KEY = "voc_terminal"
_SCOPE_FIELDS = (
    "sport_id",
    "league_id",
    "regime_id",
    "urgency_id",
    "contradiction_state",
)
_TERMINAL_STATUSES = frozenset(
    {"scored", "deadline_missed", "failed", "cancelled", "abstained", "null"}
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


class CanonicalOutcomeDerivedVOCScoreAuthority(
    _base.CanonicalOutcomeDerivedVOCScoreAuthority
):
    """Realized-VOC scorer whose cohort denominator starts before computation."""

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
        try:
            self.decision_ledger.verified_snapshot()
            records = self.decision_ledger.verified_records()
        except _base.DecisionLedgerIntegrityError as exc:
            raise _base.VOCEvaluationError(
                "canonical DecisionLedger verification failed"
            ) from exc

        by_sha = {_base._digest(record.to_dict()): record for record in records}

        # Admission is independent of whether baseline/challenger later succeed.
        # The frozen protocol already fixes task/scope and the cohort record fixes
        # the baseline/challenger compute identities.  A matching route context in
        # its precommitted window is therefore an admitted paired attempt.
        admissions: dict[str, tuple[str, str]] = {}
        request_ids: set[str] = set()
        for record_sha, record in by_sha.items():
            recorded_at = _base._instant(
                record.recorded_at,
                field="VOC cohort admission recorded_at",
            )
            if recorded_at < recorded_from or recorded_at > recorded_through:
                continue
            if record.action != _CONTEXT_ACTION:
                continue
            payload = record.payload
            if not isinstance(payload, Mapping):
                raise _base.VOCEvaluationError(
                    "canonical VOC admission payload is invalid"
                )
            context = payload.get(_CONTEXT_KEY)
            if not isinstance(context, Mapping):
                raise _base.VOCEvaluationError(
                    "canonical VOC admission context is missing"
                )
            if (
                context.get("task_class") != expected_task_class
                or _scope(context) != dict(expected_scope)
            ):
                continue
            request_id = _base._text(
                context.get("request_id"), field="VOC admission request_id"
            )
            decision_input_sha256 = _base._sha256(
                context.get("decision_input_sha256"),
                field="VOC admission decision_input_sha256",
            )
            if record.context_hash != decision_input_sha256:
                raise _base.VOCEvaluationError(
                    "canonical VOC admission context hash does not match decision input"
                )
            if request_id in request_ids:
                raise _base.VOCEvaluationError(
                    "precommitted VOC eligibility range reuses request identity"
                )
            request_ids.add(request_id)
            admissions[record_sha] = (request_id, decision_input_sha256)

        if not admissions:
            raise _base.VOCEvaluationError(
                "precommitted VOC eligibility range contains no canonical admissions"
            )

        eligible: dict[str, str] = {}
        for context_sha, (_, decision_input_sha256) in admissions.items():
            terminals: list[tuple[str, object, Mapping[str, Any]]] = []
            for record_sha, record in by_sha.items():
                payload = record.payload
                if not isinstance(payload, Mapping):
                    continue
                binding = payload.get("voc_binding")
                explicit_terminal = payload.get(_TERMINAL_KEY)
                binding_matches = (
                    isinstance(binding, Mapping)
                    and binding.get("decision_context_sha256") == context_sha
                )
                explicit_matches = (
                    isinstance(explicit_terminal, Mapping)
                    and explicit_terminal.get("decision_context_sha256") == context_sha
                )
                if binding_matches or explicit_matches:
                    terminals.append((record_sha, record, payload))

            if not terminals:
                raise _base.VOCEvaluationError(
                    "eligible VOC admission lacks terminal paired record"
                )
            if len(terminals) != 1:
                raise _base.VOCEvaluationError(
                    "eligible VOC admission has ambiguous terminal paired records"
                )

            record_sha, terminal_record, payload = terminals[0]
            binding = payload.get("voc_binding")
            evidence = payload.get(_base._SCORING_EVIDENCE_KEY)
            explicit_terminal = payload.get(_TERMINAL_KEY)

            if isinstance(binding, Mapping):
                if binding.get("decision_input_sha256") != decision_input_sha256:
                    raise _base.VOCEvaluationError(
                        "eligible VOC terminal decision input does not match admission"
                    )
                if _scope(binding) != dict(expected_scope):
                    raise _base.VOCEvaluationError(
                        "eligible VOC terminal scope does not match admission cohort"
                    )
                if _compute_identity(binding, prefix="baseline") != dict(
                    expected_baseline
                ) or _compute_identity(binding, prefix="challenger") != dict(
                    expected_challenger
                ):
                    raise _base.VOCEvaluationError(
                        "eligible VOC terminal compute identity does not match frozen cohort"
                    )

                # A binding without scoring evidence is a durable terminal failure,
                # not grounds to erase the pre-output admission from the population.
                # Qualification fails closed until that terminal has an explicit
                # protocol-supported non-positive score representation.
                if not isinstance(evidence, Mapping):
                    status = payload.get("voc_terminal_status")
                    if status is not None:
                        status_text = _base._text(
                            status, field="VOC terminal status"
                        )
                        if status_text not in _TERMINAL_STATUSES:
                            raise _base.VOCEvaluationError(
                                "eligible VOC terminal status is unsupported"
                            )
                    raise _base.VOCEvaluationError(
                        "eligible VOC decision lacks terminal scoring evidence"
                    )

                evaluation_id = _base._text(
                    evidence.get("evaluation_id"),
                    field="eligible VOC evaluation_id",
                )
                if evidence.get("decision_input_sha256") != decision_input_sha256:
                    raise _base.VOCEvaluationError(
                        "eligible VOC scoring evidence does not match admission input"
                    )
                if _base._instant(
                    terminal_record.recorded_at,
                    field="VOC terminal recorded_at",
                ) < _base._instant(
                    by_sha[context_sha].recorded_at,
                    field="VOC admission recorded_at",
                ):
                    raise _base.VOCEvaluationError(
                        "eligible VOC terminal predates its admission"
                    )
                prior = eligible.get(evaluation_id)
                if prior is not None and prior != record_sha:
                    raise _base.VOCEvaluationError(
                        "precommitted VOC eligibility range contains duplicate evaluation identity"
                    )
                eligible[evaluation_id] = record_sha
                continue

            if not isinstance(explicit_terminal, Mapping):
                raise _base.VOCEvaluationError(
                    "eligible VOC admission has malformed terminal record"
                )
            status = _base._text(
                explicit_terminal.get("status"), field="VOC terminal status"
            )
            if status not in _TERMINAL_STATUSES:
                raise _base.VOCEvaluationError(
                    "eligible VOC terminal status is unsupported"
                )
            # Non-scored terminal attempts stay in the denominator by making the
            # cohort ineligible for positive qualification instead of disappearing.
            # A future protocol may encode an explicit conservative score for such
            # a terminal, but it may never be silently promoted as success.
            raise _base.VOCEvaluationError(
                f"eligible VOC admission terminated without score: {status}"
            )

        return dict(sorted(eligible.items()))


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
    """Build the production resolver with pre-output denominator authority."""

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
