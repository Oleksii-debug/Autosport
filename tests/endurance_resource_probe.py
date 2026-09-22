from __future__ import annotations

import threading
import tracemalloc
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Mapping, Sequence


_SUPPORTED_SIGNALS = frozenset(
    {
        "traced_memory_bytes",
        "thread_count",
        "open_fd_count",
        "workspace_file_count",
        "workspace_bytes",
    }
)


@dataclass(frozen=True, slots=True)
class ResourceSample:
    checkpoint: str
    work_units: int
    traced_memory_bytes: int | None
    thread_count: int
    open_fd_count: int | None
    workspace_file_count: int
    workspace_bytes: int

    def __post_init__(self) -> None:
        if not self.checkpoint or self.checkpoint != self.checkpoint.strip():
            raise ValueError("checkpoint must be a non-empty trimmed string")
        if type(self.work_units) is not int or self.work_units < 0:
            raise ValueError("work_units must be a non-negative integer")
        for field_name in (
            "traced_memory_bytes",
            "open_fd_count",
        ):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{field_name} must be a non-negative integer or None")
        for field_name in (
            "thread_count",
            "workspace_file_count",
            "workspace_bytes",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResourceLimit:
    """A workload-specific envelope; there are intentionally no universal defaults."""

    max_net_growth: int
    max_span: int
    rationale: str

    def __post_init__(self) -> None:
        if type(self.max_net_growth) is not int or self.max_net_growth < 0:
            raise ValueError("max_net_growth must be a non-negative integer")
        if type(self.max_span) is not int or self.max_span < 0:
            raise ValueError("max_span must be a non-negative integer")
        if not self.rationale or self.rationale != self.rationale.strip():
            raise ValueError("rationale must be a non-empty trimmed string")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResourceTrend:
    signal: str
    sample_count: int
    first: int
    last: int
    minimum: int
    maximum: int
    net_growth: int
    span: int
    max_positive_step: int
    slope_per_work_unit: str
    monotonic_nondecreasing: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResourceQualification:
    status: str
    warmup_samples: int
    failures: tuple[str, ...]
    unsupported_signals: tuple[str, ...]
    unbounded_observed_signals: tuple[str, ...]
    declared_limits: tuple[tuple[str, ResourceLimit], ...]
    trends: tuple[ResourceTrend, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "warmup_samples": self.warmup_samples,
            "failures": list(self.failures),
            "unsupported_signals": list(self.unsupported_signals),
            "unbounded_observed_signals": list(self.unbounded_observed_signals),
            "declared_limits": {
                signal: limit.to_dict() for signal, limit in self.declared_limits
            },
            "trends": [trend.to_dict() for trend in self.trends],
        }


def _open_fd_count() -> int | None:
    """Return a process descriptor count where the host exposes /proc, else UNKNOWN."""

    fd_root = Path("/proc/self/fd")
    if not fd_root.is_dir():
        return None
    try:
        return sum(1 for _entry in fd_root.iterdir())
    except OSError:
        return None


def _workspace_usage(workspace: Path) -> tuple[int, int]:
    if not workspace.exists():
        return 0, 0
    files = 0
    bytes_total = 0
    for entry in workspace.rglob("*"):
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            files += 1
            bytes_total += entry.stat().st_size
        except FileNotFoundError:
            # A test/runtime may atomically replace a transient file between listing and stat.
            continue
    return files, bytes_total


def capture_resource_sample(
    *,
    checkpoint: str,
    work_units: int,
    workspace: str | Path,
) -> ResourceSample:
    """Capture bounded process/workspace observations without changing runtime authority."""

    traced_memory = tracemalloc.get_traced_memory()[0] if tracemalloc.is_tracing() else None
    file_count, workspace_bytes = _workspace_usage(Path(workspace))
    return ResourceSample(
        checkpoint=checkpoint,
        work_units=work_units,
        traced_memory_bytes=traced_memory,
        thread_count=threading.active_count(),
        open_fd_count=_open_fd_count(),
        workspace_file_count=file_count,
        workspace_bytes=workspace_bytes,
    )


def _trend(signal: str, samples: Sequence[ResourceSample]) -> ResourceTrend | None:
    values = [getattr(sample, signal) for sample in samples]
    if any(value is None for value in values):
        return None
    integers = [int(value) for value in values]
    first = integers[0]
    last = integers[-1]
    work_delta = samples[-1].work_units - samples[0].work_units
    if work_delta <= 0:
        raise ValueError("qualification window must advance work_units")
    with localcontext() as context:
        context.prec = 28
        slope = Decimal(last - first) / Decimal(work_delta)
    positive_steps = [
        max(0, current - previous)
        for previous, current in zip(integers, integers[1:], strict=False)
    ]
    return ResourceTrend(
        signal=signal,
        sample_count=len(samples),
        first=first,
        last=last,
        minimum=min(integers),
        maximum=max(integers),
        net_growth=last - first,
        span=max(integers) - min(integers),
        max_positive_step=max(positive_steps, default=0),
        slope_per_work_unit=str(slope),
        monotonic_nondecreasing=all(
            current >= previous
            for previous, current in zip(integers, integers[1:], strict=False)
        ),
    )


def qualify_resource_samples(
    samples: Sequence[ResourceSample],
    *,
    warmup_samples: int,
    limits: Mapping[str, ResourceLimit],
) -> ResourceQualification:
    """Qualify a repeated-work resource series against explicit workload-specific bounds.

    A caller must declare at least one bound before this function can return PASS. Signals
    that are unavailable on the current platform remain UNKNOWN and fail closed when the
    caller requires a bound for them. Observed-but-unbounded signals and the exact declared
    limits/rationales are included in the result so a partial qualification cannot be
    misread as a whole-process leak-free claim.
    """

    if type(warmup_samples) is not int or warmup_samples < 0:
        raise ValueError("warmup_samples must be a non-negative integer")
    if not samples:
        raise ValueError("at least one resource sample is required")
    if warmup_samples >= len(samples) - 1:
        raise ValueError("at least two post-warmup samples are required")
    if len({sample.checkpoint for sample in samples}) != len(samples):
        raise ValueError("resource sample checkpoints must be unique")
    for previous, current in zip(samples, samples[1:], strict=False):
        if current.work_units <= previous.work_units:
            raise ValueError("resource samples must have strictly increasing work_units")
    unknown_limit_names = sorted(set(limits) - _SUPPORTED_SIGNALS)
    if unknown_limit_names:
        raise ValueError(f"unsupported resource signal(s): {', '.join(unknown_limit_names)}")
    for name, limit in limits.items():
        if not isinstance(limit, ResourceLimit):
            raise TypeError(f"limit for {name} must be ResourceLimit")

    window = samples[warmup_samples:]
    trends: list[ResourceTrend] = []
    failures: list[str] = []
    unsupported: list[str] = []
    unbounded: list[str] = []

    for signal in sorted(_SUPPORTED_SIGNALS):
        observed = _trend(signal, window)
        if observed is None:
            unsupported.append(signal)
            if signal in limits:
                failures.append(f"required signal {signal} is unavailable on this platform")
            continue
        trends.append(observed)
        limit = limits.get(signal)
        if limit is None:
            unbounded.append(signal)
            continue
        if observed.net_growth > limit.max_net_growth:
            failures.append(
                f"{signal} net growth {observed.net_growth} exceeds {limit.max_net_growth}"
            )
        if observed.span > limit.max_span:
            failures.append(f"{signal} span {observed.span} exceeds {limit.max_span}")

    if not limits:
        status = "UNQUALIFIED"
    elif failures:
        status = "FAIL"
    else:
        status = "PASS"

    return ResourceQualification(
        status=status,
        warmup_samples=warmup_samples,
        failures=tuple(failures),
        unsupported_signals=tuple(unsupported),
        unbounded_observed_signals=tuple(unbounded),
        declared_limits=tuple(sorted(limits.items())),
        trends=tuple(trends),
    )
