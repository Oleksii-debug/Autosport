"""Fail closed when observed promotion metrics do not strictly improve.

The canonical promotion controller already validates typed evidence, frozen sample
floors, holdout consumption, protective metrics, and the configured improvement
threshold. Its direct aggregate comparator historically used ``<`` against the
frozen threshold, so a zero-threshold rule could let an observed tie continue to
promotion evidence and ultimately return PROMOTE. Scientific superiority requires
strictly positive observed improvement as well as positive typed evidence.

This package-level guard follows the repository's existing compatibility-guard
composition pattern. It changes only the non-positive observed-comparator outcome;
all other promotion authority remains in ``_strategy_model_factory_impl``.
"""

from __future__ import annotations

from typing import Mapping

from . import _strategy_model_factory_impl as _impl

_GUARD_SENTINEL = "_autosport_strict_observed_improvement_guard_v1"
_REASON = "primary improvement must be strictly positive"


def _install() -> None:
    controller = _impl.PromotionController
    if getattr(controller, _GUARD_SENTINEL, False):
        return

    original_evaluate = controller.evaluate

    def evaluate(
        rule: _impl.PromotionRule,
        *,
        champion_metrics: Mapping[str, float],
        challenger_metrics: Mapping[str, float],
        provenance_complete: bool,
        rollback_target: str | None,
        promotion_evidence: _impl.PromotionEvidence | None = None,
    ) -> _impl.PromotionEvaluation:
        result = original_evaluate(
            rule,
            champion_metrics=champion_metrics,
            challenger_metrics=challenger_metrics,
            provenance_complete=provenance_complete,
            rollback_target=rollback_target,
            promotion_evidence=promotion_evidence,
        )
        if (
            result.primary_improvement <= 0
            and result.verdict is not _impl.PromotionVerdict.REJECT
        ):
            return _impl.PromotionEvaluation(
                _impl.PromotionVerdict.REJECT,
                _impl.PromotionAction.REJECT,
                result.primary_improvement,
                (_REASON,),
            )
        return result

    controller.evaluate = staticmethod(evaluate)
    setattr(controller, _GUARD_SENTINEL, True)


_install()
del _install

__all__: list[str] = []
