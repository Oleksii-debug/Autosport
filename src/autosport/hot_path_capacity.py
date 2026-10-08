"""Bounded, source-bound summary of five-stage performance observations.

This is an observational projection over existing HotPathReport evidence; it never
runs the callbacks, grants decision authority, or qualifies target hardware.
Reports created from fixtures remain fixtures, even when every window is OK.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .hot_path_latency import STAGES, HotPathError, HotPathReport

MAX_CAPACITY_WINDOWS = 2000
_WAIT_REASONS = ("BACKLOG", "STALE_SOURCE", "TOTAL_BUDGET", "STAGE_BUDGET")


def _p95(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[(95 * len(ordered) + 99) // 100 - 1]


@dataclass(frozen=True, slots=True)
class HotPathCapacityEvidence:
    source_sha: str
    window_count: int
    complete_count: int
    wait_count: int
    max_backlog: int
    max_elapsed_ns: int
    p95_elapsed_ns: int | None
    stage_p95_ns: tuple[tuple[str, int], ...]
    wait_reasons: tuple[tuple[str, int], ...]
    windows_sha256: str
    evidence_class: str = "observational_source_or_fixture"
    execution_authority: bool = False
    target_machine_acceptance: bool = False

    def __post_init__(self) -> None:
        if type(self.source_sha) is not str or len(self.source_sha) != 40 or any(c not in "0123456789abcdef" for c in self.source_sha):
            raise HotPathError("invalid capacity source revision")
        if (
            type(self.evidence_class) is not str
            or self.evidence_class != "observational_source_or_fixture"
            or self.execution_authority is not False
            or self.target_machine_acceptance is not False
        ):
            raise HotPathError("capacity observations grant no authority")
        for field in ("window_count", "complete_count", "wait_count", "max_backlog", "max_elapsed_ns"):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise HotPathError("invalid capacity count or latency")
        if not (0 < self.window_count <= MAX_CAPACITY_WINDOWS and self.window_count == self.complete_count + self.wait_count):
            raise HotPathError("inconsistent capacity window counts")
        if (self.p95_elapsed_ns is None) != (self.complete_count == 0):
            raise HotPathError("inconsistent complete-window latency")
        if self.p95_elapsed_ns is not None and (type(self.p95_elapsed_ns) is not int or not 0 <= self.p95_elapsed_ns <= self.max_elapsed_ns):
            raise HotPathError("invalid capacity percentile")
        if type(self.stage_p95_ns) is not tuple or len(self.stage_p95_ns) != (len(STAGES) if self.complete_count else 0):
            raise HotPathError("incomplete capacity stage percentiles")
        for i, pair in enumerate(self.stage_p95_ns):
            if type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str or pair[0] != STAGES[i] or type(pair[1]) is not int or pair[1] < 0:
                raise HotPathError("invalid capacity stage percentile")
        if type(self.wait_reasons) is not tuple or len(self.wait_reasons) != len(_WAIT_REASONS):
            raise HotPathError("invalid capacity wait reasons")
        for i, pair in enumerate(self.wait_reasons):
            if type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str or pair[0] != _WAIT_REASONS[i] or type(pair[1]) is not int or pair[1] < 0:
                raise HotPathError("invalid capacity wait count")
        if sum(count for _, count in self.wait_reasons) != self.wait_count:
            raise HotPathError("inconsistent capacity wait reasons")
        if type(self.windows_sha256) is not str or len(self.windows_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.windows_sha256):
            raise HotPathError("invalid capacity evidence identity")


def summarize_hot_path_windows(
    reports: tuple[HotPathReport, ...], *, expected_source_sha: str
) -> HotPathCapacityEvidence:
    """Deterministically summarize at most 2000 detached canonical reports.

    This function does not call the live pipeline, hold callbacks, persist raw
    provider data, or infer readiness. Input is exact tuple to rule out infinite
    iterators and adversarial collection protocols at the ingestion boundary.
    """
    if type(reports) is not tuple or not 1 <= len(reports) <= MAX_CAPACITY_WINDOWS:
        raise HotPathError("capacity windows require a bounded nonempty tuple")
    if type(expected_source_sha) is not str or len(expected_source_sha) != 40 or any(c not in "0123456789abcdef" for c in expected_source_sha):
        raise HotPathError("invalid expected capacity source revision")

    completed: list[HotPathReport] = []
    max_backlog = 0
    max_elapsed = 0
    counts = {reason: 0 for reason in _WAIT_REASONS}
    canonical_windows: list[tuple[object, ...]] = []
    for item in reports:
        if type(item) is not HotPathReport:
            raise HotPathError("capacity observation must be an exact HotPathReport")
        # Snapshot each field exactly once. Revalidation catches a frozen-object
        # bypass via object.__setattr__ before it can enter a report digest.
        fields = (
            item.source_sha, item.disposition, item.reason,
            item.stage_latencies_ns, item.total_elapsed_ns, item.backlog,
            item.execution_authority, item.target_machine_acceptance,
        )
        observation = HotPathReport(*fields)
        if observation.source_sha != expected_source_sha:
            raise HotPathError("capacity windows mix incompatible source revisions")
        if observation.disposition == "OK":
            completed.append(observation)
        else:
            counts[observation.reason] += 1
        max_backlog = max(max_backlog, observation.backlog)
        max_elapsed = max(max_elapsed, observation.total_elapsed_ns)
        canonical_windows.append((
            observation.disposition, observation.reason,
            observation.stage_latencies_ns, observation.total_elapsed_ns,
            observation.backlog,
        ))

    encoded = json.dumps(canonical_windows, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return HotPathCapacityEvidence(
        source_sha=expected_source_sha,
        window_count=len(reports),
        complete_count=len(completed),
        wait_count=len(reports) - len(completed),
        max_backlog=max_backlog,
        max_elapsed_ns=max_elapsed,
        p95_elapsed_ns=_p95([r.total_elapsed_ns for r in completed]) if completed else None,
        stage_p95_ns=tuple(
            (stage, _p95([r.stage_latencies_ns[index][1] for r in completed]))
            for index, stage in enumerate(STAGES)
        ) if completed else (),
        wait_reasons=tuple(counts.items()),
        windows_sha256=hashlib.sha256(encoded).hexdigest(),
    )
