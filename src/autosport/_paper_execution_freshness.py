from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from . import _paper_execution_reality_legacy as _impl
from . import paper_execution_reality as _public


_ORIGINAL_LEGACY_SYNTHETIC = _impl._synthetic_attempt
_ORIGINAL_OBSERVED = _impl._observed_attempt
_ORIGINAL_PUBLIC_SYNTHETIC = _public._synthetic_attempt

_FUTURE_QUOTE_ERROR = "PAPER execution timestamp cannot predate decision quote"
_LEGACY_NEGATIVE_AGE_ERROR = "quote age must be non-negative"


def _age_exceeds_bound(*, execution_time, decision_time, max_quote_age_ms: int) -> bool:
    age = execution_time - decision_time
    if age < timedelta(0):
        raise _impl.PaperExecutionStateError(_FUTURE_QUOTE_ERROR)
    # Preserve the legacy validation/telemetry contract for representable ages;
    # the exact timedelta comparison below is the safety authority.
    _impl._milliseconds(age, "quote age")
    return age > timedelta(milliseconds=max_quote_age_ms)


def _synthetic_times(kwargs):
    config = kwargs["config"]
    action = kwargs["action"]
    started_at = kwargs["started_at"]
    start = _impl._timestamp(started_at, "started_at")
    delay_span = config.max_delay_ms - config.min_delay_ms
    delay_ms = config.min_delay_ms
    if delay_span:
        delay_ms += _impl._deterministic_int(
            f"{config.seed}:{kwargs['run_id']}:{action.action_id}",
            "delay",
            delay_span + 1,
        )
    execution_time = start + timedelta(milliseconds=delay_ms)
    decision_time = _impl._timestamp(action.quote_observed_at, "quote_observed_at")
    return execution_time, decision_time


def _synthetic_future_quote_proven(kwargs) -> bool:
    try:
        execution_time, decision_time = _synthetic_times(kwargs)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return decision_time > execution_time


def _exact_synthetic(original, **kwargs):
    try:
        attempt = original(**kwargs)
    except ValueError as exc:
        # Error text is not authority. Reclassify the legacy negative-age error
        # only when this wrapper independently reconstructs the exact deterministic
        # synthetic execution time and proves that the decision quote is future-dated.
        if (
            str(exc) == _LEGACY_NEGATIVE_AGE_ERROR
            and _synthetic_future_quote_proven(kwargs)
        ):
            raise _impl.PaperExecutionStateError(_FUTURE_QUOTE_ERROR) from exc
        raise

    config = kwargs["config"]
    action = kwargs["action"]
    suspended = kwargs["suspended"]
    execution_time, decision_time = _synthetic_times(kwargs)
    expires = _impl._timestamp(action.expires_at, "expires_at")

    # Preserve the original precedence for suspension and quote expiry. Only
    # replace the floor-induced acceptance when exact age exceeds the bound.
    if (
        not suspended
        and execution_time < expires
        and _age_exceeds_bound(
            execution_time=execution_time,
            decision_time=decision_time,
            max_quote_age_ms=config.max_quote_age_ms,
        )
        and attempt.reason != "decision quote exceeded configured PAPER freshness bound"
    ):
        return replace(
            attempt,
            outcome=_impl.PaperAttemptOutcome.REJECTED,
            execution_odds=None,
            execution_stake=None,
            reason="decision quote exceeded configured PAPER freshness bound",
        )
    return attempt


def _legacy_synthetic_attempt(**kwargs):
    return _exact_synthetic(_ORIGINAL_LEGACY_SYNTHETIC, **kwargs)


def _public_synthetic_attempt(**kwargs):
    return _exact_synthetic(_ORIGINAL_PUBLIC_SYNTHETIC, **kwargs)


def _observed_attempt(**kwargs):
    action = kwargs["action"]
    config = kwargs["config"]
    observation = kwargs["observation"]
    execution_time = _impl._timestamp(observation.observed_at, "observed_at")
    decision_time = _impl._timestamp(action.quote_observed_at, "quote_observed_at")
    expires = _impl._timestamp(action.expires_at, "expires_at")

    # Causality is a state invariant independent of the configured expiry/freshness
    # bounds. Classify it before the legacy helper can leak a generic ValueError.
    if decision_time > execution_time:
        raise _impl.PaperExecutionStateError(_FUTURE_QUOTE_ERROR)

    # Keep the legacy expiry error authoritative when both ordinary age bounds
    # are violated by a causally ordered quote.
    if execution_time < expires and _age_exceeds_bound(
        execution_time=execution_time,
        decision_time=decision_time,
        max_quote_age_ms=config.max_quote_age_ms,
    ):
        raise _impl.PaperExecutionStateError(
            "observed execution violates configured quote freshness"
        )
    return _ORIGINAL_OBSERVED(**kwargs)


def _install() -> None:
    if getattr(_impl, "_autosport_exact_freshness_installed", False):
        return
    _impl._synthetic_attempt = _legacy_synthetic_attempt
    _impl._observed_attempt = _observed_attempt
    _public._synthetic_attempt = _public_synthetic_attempt
    _impl._autosport_exact_freshness_installed = True


_install()