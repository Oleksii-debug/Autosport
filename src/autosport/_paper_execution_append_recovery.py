from __future__ import annotations

from pathlib import Path

from .paper import PaperBook
from .paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
)


_ORIGINAL_EXECUTE = PaperExecutionAdoptionRuntime.execute
_LIVE_DECISION_PREFIX = "live-"
_PRE_ACTION_BOOK_FILE_NAME = "live_decision_pre_action_book.json"


def _load_live_pre_action_book(runtime: PaperExecutionAdoptionRuntime) -> PaperBook | None:
    """Return the exact durable pre-action witness for a persistent live decision."""
    path = Path(runtime.paper_book_path).parent / _PRE_ACTION_BOOK_FILE_NAME
    if not path.exists():
        return None
    try:
        return PaperBook.load(path)
    except (OSError, TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "live recovery pre-action PaperBook is unreadable"
        ) from exc


def _guarded_execute(
    self: PaperExecutionAdoptionRuntime,
    *,
    prepared,
    trigger_id: str,
    started_at: str,
    materialize_exposure: bool,
    observations=None,
    evidence_registry=None,
    suspended_action_ids: frozenset[str] = frozenset(),
):
    """Fence APPEND_PENDING replay against unrelated durable PaperBook drift.

    PersistentLiveDecisionLoop writes an exact pre-action PaperBook witness before
    publishing its PENDING cursor. A restart may therefore observe either that
    exact book or the exact post-action book explained by the already-durable #623
    run. Anything else is unrelated economic mutation and must fail closed before
    execution is resumed or live progress can be promoted to COMMITTED.

    The live decision trigger prefix is intentionally narrow so legacy PAPER paths
    that do not own this recovery witness keep their existing behavior.
    """
    if type(trigger_id) is str and trigger_id.startswith(_LIVE_DECISION_PREFIX):
        pre_action_book = _load_live_pre_action_book(self)
        if pre_action_book is not None:
            self.assert_recoverable_book_state(
                pre_action_book=pre_action_book,
                prepared=prepared,
                trigger_id=trigger_id,
                started_at=started_at,
                materialize_exposure=materialize_exposure,
            )

    return _ORIGINAL_EXECUTE(
        self,
        prepared=prepared,
        trigger_id=trigger_id,
        started_at=started_at,
        materialize_exposure=materialize_exposure,
        observations=observations,
        evidence_registry=evidence_registry,
        suspended_action_ids=suspended_action_ids,
    )


def _install() -> None:
    if getattr(PaperExecutionAdoptionRuntime, "_autosport_append_recovery_installed", False):
        return
    PaperExecutionAdoptionRuntime.execute = _guarded_execute
    PaperExecutionAdoptionRuntime._autosport_append_recovery_installed = True


_install()
