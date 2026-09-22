from __future__ import annotations

import hashlib
import json
import shutil
import sys
import threading
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


_OWNED_THREAD_PREFIX = "autosport-"


@dataclass(frozen=True, slots=True)
class OwnedThreadResource:
    name: str
    daemon: bool
    ident: int | None

    def ownership_key(self) -> tuple[str, bool]:
        return (self.name, self.daemon)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RuntimeResourceCensus:
    owned_threads: tuple[OwnedThreadResource, ...]

    @property
    def owned_thread_count(self) -> int:
        return len(self.owned_threads)

    def to_dict(self) -> dict[str, object]:
        return {
            "owned_thread_count": self.owned_thread_count,
            "owned_threads": [item.to_dict() for item in self.owned_threads],
        }


@dataclass(frozen=True, slots=True)
class WorkspaceHandleProbe:
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


def capture_runtime_resource_census() -> RuntimeResourceCensus:
    """Capture only live threads with Autosport-owned names.

    Whole-process thread/handle counts are intentionally excluded: interpreter,
    runner and dependency pools are not Autosport ownership evidence.
    """

    resources = [
        OwnedThreadResource(
            name=thread.name,
            daemon=bool(thread.daemon),
            ident=thread.ident,
        )
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith(_OWNED_THREAD_PREFIX)
    ]
    resources.sort(
        key=lambda item: (
            item.name,
            item.daemon,
            -1 if item.ident is None else item.ident,
        )
    )
    return RuntimeResourceCensus(tuple(resources))


def residual_owned_threads(
    baseline: RuntimeResourceCensus,
    current: RuntimeResourceCensus,
) -> tuple[OwnedThreadResource, ...]:
    """Return current Autosport thread multiplicity beyond a warm baseline."""

    baseline_counts = Counter(item.ownership_key() for item in baseline.owned_threads)
    remaining = baseline_counts.copy()
    residual: list[OwnedThreadResource] = []
    for item in current.owned_threads:
        key = item.ownership_key()
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            residual.append(item)
    return tuple(residual)


def _error_type(exc: BaseException) -> str:
    try:
        name = type.__getattribute__(type(exc), "__name__")
    except BaseException:
        return "BaseException"
    return name if type(name) is str and name else "BaseException"


def _probe_result(operation: str, exc: BaseException | None) -> WorkspaceHandleProbe:
    return WorkspaceHandleProbe(
        operation=operation,
        status="PASS" if exc is None else "FAIL",
        platform=sys.platform,
        windows_handle_semantics=sys.platform == "win32",
        error_type=None if exc is None else _error_type(exc),
    )


def probe_workspace_move_round_trip(workspace: str | Path) -> WorkspaceHandleProbe:
    """Rename a closed workspace away and back without hiding a failed second move."""

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
    except BaseException as exc:
        if moved and target.exists() and not root.exists():
            try:
                target.rename(root)
            except BaseException:
                pass
        return _probe_result("MOVE_ROUND_TRIP", exc)
    return _probe_result("MOVE_ROUND_TRIP", None)


def probe_disposable_workspace_move_delete(workspace: str | Path) -> WorkspaceHandleProbe:
    """Rename then remove a disposable closed workspace.

    On Windows this is a direct liveness check for leaked Autosport file/database
    handles. On POSIX the operation is still checked but is not labelled as proof
    of Windows handle semantics.
    """

    root = Path(workspace)
    target = root.with_name(f".{root.name}.autosport-resource-delete-probe")
    if not root.is_dir():
        return _probe_result("MOVE_DELETE", FileNotFoundError())
    if target.exists():
        return _probe_result("MOVE_DELETE", FileExistsError())

    try:
        root.rename(target)
        shutil.rmtree(target)
    except BaseException as exc:
        return _probe_result("MOVE_DELETE", exc)
    return _probe_result("MOVE_DELETE", None)


def stable_evidence_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def owned_thread_keys(
    resources: Iterable[OwnedThreadResource],
) -> tuple[tuple[str, bool], ...]:
    return tuple(sorted(item.ownership_key() for item in resources))
