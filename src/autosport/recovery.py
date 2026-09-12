from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .run_registry import RunRegistry


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    reconciled_keys: tuple[str, ...]
    unresolved_without_summary: tuple[str, ...]


def reconcile_late_crashes(workspace: str | Path) -> RecoveryReport:
    """Repair only runs that already have durable hash-matched completion evidence."""

    root = Path(workspace)
    registry_path = root / "run_registry.json"
    if not registry_path.is_file():
        return RecoveryReport((), ())
    registry = RunRegistry(registry_path)
    paper_book_path = root / "paper_book.json"
    reconciled: list[str] = []
    unresolved: list[str] = []
    for key, item in registry.in_progress():
        result_path = root / f"run-{item['run_id']}.json"
        if not result_path.is_file():
            unresolved.append(key)
            continue
        registry.reconcile_completed_summary(key, result_path, paper_book_path)
        reconciled.append(key)
    return RecoveryReport(tuple(reconciled), tuple(unresolved))
