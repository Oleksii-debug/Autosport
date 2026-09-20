"""Canonical realized-VOC authority with fail-closed cohort terminality.

The prior implementation is retained byte-for-byte in
``_voc_outcome_scoring_base``.  This narrow wrapper closes one causal selection
bias: cohort eligibility is derived from pre-outcome ``voc_binding`` admission,
not from the later presence of successful ``voc_scoring_evidence``.  Therefore a
matching admitted paired attempt that times out, fails, abstains, or otherwise
never emits scoring evidence cannot silently disappear from the denominator.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from . import _voc_outcome_scoring_base as _base

# Preserve the existing public module surface while overriding only the authority
# whose denominator semantics changed.
for _name in dir(_base):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_base, _name)


class CanonicalOutcomeDerivedVOCScoreAuthority(
    _base.CanonicalOutcomeDerivedVOCScoreAuthority
):
    """Realized-VOC scorer whose cohort denominator starts at paired admission."""

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
        eligible: dict[str, str] = {}
        for record_sha, record in by_sha.items():
            recorded_at = _base._instant(
                record.recorded_at,
                field="VOC cohort DecisionRecord.recorded_at",
            )
            if recorded_at < recorded_from or recorded_at > recorded_through:
                continue
            payload = record.payload
            if not isinstance(payload, Mapping):
                continue

            # Admission is the authority for denominator membership.  Scoring
            # evidence is a later terminal product and must never decide whether
            # this attempt existed.
            binding = payload.get("voc_binding")
            if not isinstance(binding, Mapping):
                continue
            binding_scope = {
                "sport_id": binding.get("sport_id"),
                "league_id": binding.get("league_id"),
                "regime_id": binding.get("regime_id"),
                "urgency_id": binding.get("urgency_id"),
                "contradiction_state": binding.get("contradiction_state"),
            }
            binding_baseline = {
                "candidate_id": binding.get("baseline_candidate_id"),
                "backend_id": binding.get("baseline_backend_id"),
                "model_id": binding.get("baseline_model_id"),
                "config_sha256": binding.get("baseline_config_sha256"),
            }
            binding_challenger = {
                "candidate_id": binding.get("challenger_candidate_id"),
                "backend_id": binding.get("challenger_backend_id"),
                "model_id": binding.get("challenger_model_id"),
                "config_sha256": binding.get("challenger_config_sha256"),
            }
            if (
                binding_scope != dict(expected_scope)
                or binding_baseline != dict(expected_baseline)
                or binding_challenger != dict(expected_challenger)
            ):
                continue

            context_sha = _base._sha256(
                binding.get("decision_context_sha256"),
                field="eligible VOC decision_context_sha256",
            )
            context_record = by_sha.get(context_sha)
            if context_record is None or not isinstance(
                context_record.payload, Mapping
            ):
                raise _base.VOCEvaluationError(
                    "eligible VOC decision is missing canonical decision context"
                )
            current_context = context_record.payload.get("voc_current_context")
            if not isinstance(current_context, Mapping):
                raise _base.VOCEvaluationError(
                    "eligible VOC decision context is missing canonical scope"
                )
            context_scope = {
                "sport_id": current_context.get("sport_id"),
                "league_id": current_context.get("league_id"),
                "regime_id": current_context.get("regime_id"),
                "urgency_id": current_context.get("urgency_id"),
                "contradiction_state": current_context.get(
                    "contradiction_state"
                ),
            }
            if (
                current_context.get("task_class") != expected_task_class
                or context_scope != dict(expected_scope)
            ):
                continue

            evidence = payload.get(_base._SCORING_EVIDENCE_KEY)
            if not isinstance(evidence, Mapping):
                raise _base.VOCEvaluationError(
                    "eligible VOC decision lacks terminal scoring evidence"
                )
            evaluation_id = _base._text(
                evidence.get("evaluation_id"),
                field="eligible VOC evaluation_id",
            )
            prior = eligible.get(evaluation_id)
            if prior is not None and prior != record_sha:
                raise _base.VOCEvaluationError(
                    "precommitted VOC eligibility range contains duplicate evaluation identity"
                )
            eligible[evaluation_id] = record_sha

        if not eligible:
            raise _base.VOCEvaluationError(
                "precommitted VOC eligibility range contains no canonical decisions"
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
    """Build the production resolver with admission-derived denominator authority."""

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
