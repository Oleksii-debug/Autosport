"""Bounded, observer-only five-stage latency and overload gate.

This is a composition helper for fixture experiments, not a second runtime or
financial authority. `observed_at_ns` uses the same monotonic clock as `clock_ns`.
No callback output, exception message or secret-bearing input is persisted.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Final

STAGES: Final = ("ingest", "mirror", "opportunity", "portfolio", "decision")


class HotPathError(ValueError):
    """Fail-closed configuration, clock, or stage failure without secret echoes."""


@dataclass(frozen=True, slots=True)
class HotPathPolicy:
    stage_budget_ns: int
    total_budget_ns: int
    max_backlog: int
    max_source_age_ns: int

    def __post_init__(self) -> None:
        for field in ("stage_budget_ns", "total_budget_ns", "max_source_age_ns"):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise HotPathError(f"{field} must be a positive integer")
        if type(self.max_backlog) is not int or self.max_backlog < 0:
            raise HotPathError("max_backlog must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class HotPathReport:
    source_sha: str
    disposition: str
    reason: str
    stage_latencies_ns: tuple[tuple[str, int], ...]
    total_elapsed_ns: int
    backlog: int
    execution_authority: bool = False
    target_machine_acceptance: bool = False


def run_hot_path(
    *,
    source_sha: str,
    policy: HotPathPolicy,
    observed_at_ns: int,
    backlog: int,
    stages: Mapping[str, Callable[[], object]],
    clock_ns: Callable[[], int] = perf_counter_ns,
    backlog_reader: Callable[[], int] | None = None,
) -> HotPathReport:
    """Stop before the next stage on stale source, overload or time breach.

    The final `decision` callback is proposal-only: this helper does not authorize
    order placement or persist any economic effect. Callback side effects before
    an exception cannot be rolled back; callers own their idempotency contracts.
    """
    if type(source_sha) is not str or len(source_sha) != 40 or any(c not in "0123456789abcdef" for c in source_sha):
        raise HotPathError("source_sha must be a lowercase 40-digit revision")
    if type(policy) is not HotPathPolicy:
        raise HotPathError("policy must be exact HotPathPolicy")
    # Frozen dataclasses can still be changed with object.__setattr__ by a
    # callback holding the caller's policy. Re-validate then detach all scalar
    # thresholds before executing any stage; never reread mutable caller state.
    policy = HotPathPolicy(
        stage_budget_ns=policy.stage_budget_ns,
        total_budget_ns=policy.total_budget_ns,
        max_backlog=policy.max_backlog,
        max_source_age_ns=policy.max_source_age_ns,
    )
    if type(observed_at_ns) is not int or observed_at_ns < 0:
        raise HotPathError("observed_at_ns must be monotonic nonnegative integer")
    if type(backlog) is not int or backlog < 0:
        raise HotPathError("backlog must be nonnegative integer")
    if type(stages) is not dict or tuple(stages) != STAGES or not all(callable(stages[k]) for k in STAGES):
        raise HotPathError("stages must have exactly five ordered callbacks")
    if not callable(clock_ns):
        raise HotPathError("clock_ns must be callable")
    if backlog_reader is not None and not callable(backlog_reader):
        raise HotPathError("backlog_reader must be callable")
    callbacks = tuple(stages[name] for name in STAGES)

    previous_tick: int | None = None

    def read_clock() -> int:
        nonlocal previous_tick
        clock_failed = False
        try:
            tick = clock_ns()
        except Exception:
            clock_failed = True
        # Raise after leaving the handler: exception chaining retains sensitive
        # callback payload in __context__ even with "from None".
        if clock_failed:
            raise HotPathError("monotonic clock unavailable")
        if type(tick) is not int or tick < 0:
            raise HotPathError("monotonic clock returned invalid timestamp")
        # Every sample must be >= the prior sample, including across stages.
        # Comparing only a stage's before/after would miss an interstage rewind.
        if previous_tick is not None and tick < previous_tick:
            raise HotPathError("monotonic clock moved backwards")
        previous_tick = tick
        return tick

    started = read_clock()
    if started < observed_at_ns:
        raise HotPathError("source timestamp is ahead of monotonic clock")
    samples: list[tuple[str, int]] = []
    # Highest observed pressure wins within this bounded window. A later decrease
    # cannot erase an overload already witnessed before a downstream proposal.
    observed_backlog = backlog

    def check_backlog() -> bool:
        nonlocal observed_backlog
        if backlog_reader is not None:
            sampler_failed = False
            try:
                latest = backlog_reader()
            except Exception:
                sampler_failed = True
            if sampler_failed:
                raise HotPathError("backlog sampling unavailable")
            if type(latest) is not int or latest < 0:
                raise HotPathError("backlog sample must be nonnegative integer")
            observed_backlog = max(observed_backlog, latest)
        return observed_backlog > policy.max_backlog

    def stop(reason: str, now: int) -> HotPathReport:
        return HotPathReport(source_sha, "WAIT", reason, tuple(samples), now - started, observed_backlog)

    if backlog > policy.max_backlog:
        return stop("BACKLOG", started)
    for name, callback in zip(STAGES, callbacks):
        before = read_clock()
        if check_backlog():
            return stop("BACKLOG", before)
        # A dynamic backlog probe may itself consume the freshness or total
        # budget. Re-sample before EVERY callback (not just decision); a slow
        # sampler must not start a stale intermediate stage.
        if backlog_reader is not None:
            before = read_clock()
        if before < started or before < observed_at_ns:
            raise HotPathError("monotonic clock moved backwards")
        if before - observed_at_ns > policy.max_source_age_ns:
            return stop("STALE_SOURCE", before)
        if before - started > policy.total_budget_ns:
            return stop("TOTAL_BUDGET", before)
        stage_failed = False
        try:
            callback()
        except Exception:
            stage_failed = True
        if stage_failed:
            raise HotPathError("stage failed closed")
        after = read_clock()
        if after < before:
            raise HotPathError("monotonic clock moved backwards")
        delta = after - before
        samples.append((name, delta))
        if check_backlog():
            return stop("BACKLOG", after)
        # Post-stage sampling can also consume the deadline, including after
        # the final decision. Never publish OK from the earlier clock tick.
        checked_at = read_clock() if backlog_reader is not None else after
        if delta > policy.stage_budget_ns:
            return stop("STAGE_BUDGET", checked_at)
        if checked_at - started > policy.total_budget_ns:
            return stop("TOTAL_BUDGET", checked_at)
        if checked_at - observed_at_ns > policy.max_source_age_ns:
            return stop("STALE_SOURCE", checked_at)
    return HotPathReport(source_sha, "OK", "WITHIN_BUDGET", tuple(samples), samples and checked_at - started or 0, observed_backlog)
