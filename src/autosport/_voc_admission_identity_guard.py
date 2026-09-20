from __future__ import annotations

"""Fail closed when explicit VOC admissions target a foreign protocol/cohort.

The canonical cohort resolver already derives the exact ResearchProtocol and
protocol-owned VOCCohort before it computes the eligible DecisionLedger
population.  The terminal-aware VOC wrapper, however, historically filtered
explicit admissions only by task/scope/compute identity and merely required all
admissions to agree with one another.  A self-consistent foreign protocol/cohort
could therefore become the positive scored denominator.

This narrow guard threads the already-canonical target identity through the
existing polymorphic cohort resolver without creating another scoring authority.
Legacy cohorts remain unchanged; once explicit admissions exist in the frozen
window, every matching admission must belong to the exact target protocol and
cohort.
"""

from contextvars import ContextVar
from typing import Any

from . import voc_outcome_scoring as scoring


_TARGET_IDENTITY: ContextVar[tuple[str, str] | None] = ContextVar(
    "autosport_voc_target_protocol_cohort",
    default=None,
)

_ORIGINAL_AUTHORITY = scoring.CanonicalOutcomeDerivedVOCScoreAuthority


class CanonicalOutcomeDerivedVOCScoreAuthority(_ORIGINAL_AUTHORITY):
    """Terminal-aware VOC authority with exact admission identity fencing."""

    def _cohort_members(self, evaluation, *, as_of):
        cohort_id, _, _ = self._protocol_contract(evaluation)
        token = _TARGET_IDENTITY.set((evaluation.research_protocol_id, cohort_id))
        try:
            return super()._cohort_members(evaluation, as_of=as_of)
        finally:
            _TARGET_IDENTITY.reset(token)

    def _eligible_cohort_decisions(
        self,
        *,
        recorded_from,
        recorded_through,
        expected_task_class: str,
        expected_scope,
        expected_baseline,
        expected_challenger,
    ) -> dict[str, str]:
        target = _TARGET_IDENTITY.get()
        if target is None:
            raise scoring._base.VOCEvaluationError(
                "explicit VOC eligibility lacks canonical protocol/cohort context"
            )
        expected_protocol_id, expected_cohort_id = target

        ordered, by_sha, order = scoring._ledger_records(self.decision_ledger)
        common: dict[str, Any] = {
            "ordered": ordered,
            "by_sha": by_sha,
            "order": order,
            "recorded_from": recorded_from,
            "recorded_through": recorded_through,
            "expected_task_class": expected_task_class,
            "expected_scope": expected_scope,
            "expected_baseline": expected_baseline,
            "expected_challenger": expected_challenger,
        }
        all_explicit = self._explicit_admissions(**common)
        if all_explicit:
            canonical_explicit = self._explicit_admissions(
                **common,
                expected_protocol_id=expected_protocol_id,
                expected_cohort_id=expected_cohort_id,
            )
            if set(canonical_explicit) != set(all_explicit):
                raise scoring._base.VOCEvaluationError(
                    "explicit VOC paired admission protocol/cohort does not match canonical target"
                )

        return super()._eligible_cohort_decisions(
            recorded_from=recorded_from,
            recorded_through=recorded_through,
            expected_task_class=expected_task_class,
            expected_scope=expected_scope,
            expected_baseline=expected_baseline,
            expected_challenger=expected_challenger,
        )


# ``build_canonical_voc_authority_resolver`` resolves this module global at call
# time.  Rebinding preserves the existing public API while installing the stricter
# subclass before consumers import ``autosport.voc_outcome_scoring``.
scoring.CanonicalOutcomeDerivedVOCScoreAuthority = (
    CanonicalOutcomeDerivedVOCScoreAuthority
)


__all__: list[str] = []
