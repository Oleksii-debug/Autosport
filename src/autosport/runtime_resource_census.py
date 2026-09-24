"""Deterministic read-only census and probes for Autosport-owned runtime resources.

This qualification helper observes process-local worker ownership and closed-workspace
handle behavior only. It does not start, stop, authorize, or otherwise control product
work, and it is not durable runtime authority.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from enum import Enum
from hashlib import sha256
import json
import shutil
import sys
import threading
from pathlib import Path
from typing import Final


AUTOSPORT_THREAD_PREFIX: Final = "autosport-"


class ThreadCensusStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class OwnedThreadRecord:
    """One live Autosport-owned thread observed in the current process."""

    name: str
    daemon: bool
    ident: int | None
    native_id: int | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OwnedThreadSnapshot:
    """Immutable point-in-time process-local thread census."""

    records: tuple[OwnedThreadRecord, ...]
    issues: tuple[str, ...] = ()

    @property
    def integrity_issues(self) -> tuple[str, ...]:
        """Return stored and record-derived evidence-integrity issues."""

        issues = set(self.issues)
        for record in self.records:
            if type(record) is not OwnedThreadRecord:
                issues.add("invalid_owned_thread_record_type")
                continue
            if record.ident is None:
                issues.add(f"live_owned_thread_missing_ident:{record.name}")
        return tuple(sorted(issues))

    @property
    def complete(self) -> bool:
        return not self.integrity_issues

    @property
    def owned_thread_count(self) -> int:
        return len(self.records)

    @property
    def counts(self) -> tuple[tuple[str, bool, int], ...]:
        if not self.complete:
            return ()
        counts = Counter((record.name, record.daemon) for record in self.records)
        return tuple(
            sorted(
                (name, daemon, count)
                for (name, daemon), count in counts.items()
            )
        )

    def to_dict(self) -> dict[str, object]:
        records: list[dict[str, object]] = []
        for record in self.records:
            if type(record) is OwnedThreadRecord:
                records.append(record.to_dict())
        return {
            "owned_thread_count": self.owned_thread_count,
            "owned_threads": records,
            "integrity_issues": list(self.integrity_issues),
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True, order=True)
class OwnedThreadGrowth:
    """Positive live-thread cardinality growth versus a quiescent baseline."""

    name: str
    daemon: bool
    baseline_count: int
    current_count: int

    @property
    def delta(self) -> int:
        return self.current_count - self.baseline_count

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "daemon": self.daemon,
            "baseline_count": self.baseline_count,
            "current_count": self.current_count,
            "delta": self.delta,
        }


@dataclass(frozen=True, slots=True)
class ThreadCensusComparison:
    """Fail-honest comparison of two process-local thread snapshots."""

    status: ThreadCensusStatus
    growth: tuple[OwnedThreadGrowth, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceHandleProbe:
    """Closed-workspace move/delete evidence, with Windows semantics explicit."""

    operation: str
    status: str
    platform: str
    windows_handle_semantics: bool
    error_type: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in {"MOVE_ROUND_TRIP", "MOVE_DELETE"}:
            raise ValueError("unsupported workspace handle probe operation")
        if self.status not in {"PASS", "FAIL"}:
            raise ValueError("workspace handle probe status must be PASS or FAIL")
        if self.status == "PASS" and self.error_type is not None:
            raise ValueError("PASS workspace probe cannot carry an error type")
        if self.status == "FAIL" and not self.error_type:
            raise ValueError("FAIL workspace probe must carry an error type")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def capture_owned_thread_snapshot() -> OwnedThreadSnapshot:
    """Capture all currently live threads whose canonical name begins ``autosport-``."""

    records: list[OwnedThreadRecord] = []
    issues: list[str] = []
    for thread in threading.enumerate():
        if not thread.is_alive() or not thread.name.startswith(AUTOSPORT_THREAD_PREFIX):
            continue
        ident = thread.ident
        native_id = getattr(thread, "native_id", None)
        if ident is None:
            issues.append(f"live_owned_thread_missing_ident:{thread.name}")
        records.append(
            OwnedThreadRecord(
                name=thread.name,
                daemon=bool(thread.daemon),
                ident=ident,
                native_id=native_id if isinstance(native_id, int) else None,
            )
        )
    records.sort(
        key=lambda record: (
            record.name,
            record.daemon,
            record.ident is None,
            -1 if record.ident is None else record.ident,
            record.native_id is None,
            -1 if record.native_id is None else record.native_id,
        )
    )
    return OwnedThreadSnapshot(
        records=tuple(records),
        issues=tuple(sorted(set(issues))),
    )


def compare_owned_thread_snapshots(
    baseline: OwnedThreadSnapshot,
    current: OwnedThreadSnapshot,
) -> ThreadCensusComparison:
    """Compare quiescent snapshots without treating missing evidence as PASS."""

    if type(baseline) is not OwnedThreadSnapshot or type(current) is not OwnedThreadSnapshot:
        raise TypeError("thread census comparison requires exact OwnedThreadSnapshot values")

    issues = tuple(sorted(set(baseline.integrity_issues + current.integrity_issues)))
    if issues:
        return ThreadCensusComparison(
            status=ThreadCensusStatus.INCONCLUSIVE,
            growth=(),
            issues=issues,
        )

    baseline_counts = Counter(
        (record.name, record.daemon) for record in baseline.records
    )
    current_counts = Counter(
        (record.name, record.daemon) for record in current.records
    )
    growth = tuple(
        OwnedThreadGrowth(
            name=name,
            daemon=daemon,
            baseline_count=baseline_counts[(name, daemon)],
            current_count=current_count,
        )
        for (name, daemon), current_count in sorted(current_counts.items())
        if current_count > baseline_counts[(name, daemon)]
    )
    return ThreadCensusComparison(
        status=ThreadCensusStatus.FAIL if growth else ThreadCensusStatus.PASS,
        growth=growth,
        issues=(),
    )


def _probe_result(operation: str, exc: OSError | None) -> WorkspaceHandleProbe:
    return WorkspaceHandleProbe(
        operation=operation,
        status="PASS" if exc is None else "FAIL",
        platform=sys.platform,
        windows_handle_semantics=sys.platform == "win32",
        error_type=None if exc is None else type(exc).__name__,
    )


def probe_workspace_move_round_trip(workspace: str | Path) -> WorkspaceHandleProbe:
    """Rename a closed workspace away and back, restoring it on recoverable failure."""

    root = Path(workspace)
    target = root.with_name(f".{root.name}.autosport-resource-probe")
    if not root.is_dir():
        return _probe_result("MOVE_ROUND_TRIP", FileNotFoundError())
    if target.exists():
        return _probe_result("MOVE_ROUND_TRIP", FileExistsError())

    moved = False
    try:
        root.rename(target)
        moved = True
        target.rename(root)
        moved = False
    except OSError as exc:
        if moved and target.exists() and not root.exists():
            try:
                target.rename(root)
            except OSError:
                pass
        return _probe_result("MOVE_ROUND_TRIP", exc)
    return _probe_result("MOVE_ROUND_TRIP", None)


def probe_disposable_workspace_move_delete(workspace: str | Path) -> WorkspaceHandleProbe:
    """Rename then remove a disposable closed workspace."""

    root = Path(workspace)
    target = root.with_name(f".{root.name}.autosport-resource-delete-probe")
    if not root.is_dir():
        return _probe_result("MOVE_DELETE", FileNotFoundError())
    if target.exists():
        return _probe_result("MOVE_DELETE", FileExistsError())

    try:
        root.rename(target)
        shutil.rmtree(target)
    except OSError as exc:
        return _probe_result("MOVE_DELETE", exc)
    return _probe_result("MOVE_DELETE", None)


def stable_evidence_sha256(payload: dict[str, object]) -> str:
    """Hash one canonical JSON evidence image."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
