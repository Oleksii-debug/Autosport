from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .recovery import RecoveryReport, reconcile_late_crashes
from .research_strategy import ResearchStrategyPlan
from .session import AutosportSession
from .ui_model import ticket_lines


@dataclass(frozen=True, slots=True)
class RecoverySessionSnapshot:
    balance: str
    committed_stake: str
    strategy_id: str
    workspace: str
    ticket_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecoveryWorkerResult:
    report: RecoveryReport | None
    snapshot: RecoverySessionSnapshot | None
    recovery_error: str | None = None
    reopen_error: str | None = None

    def __post_init__(self) -> None:
        if (self.report is None) == (self.recovery_error is None):
            raise ValueError("recovery result must contain exactly one of report or recovery_error")


RecoveryTask = Callable[[], RecoveryWorkerResult]


@dataclass(frozen=True, slots=True)
class RecoveryWorkerMessage:
    result: RecoveryWorkerResult | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error is None):
            raise ValueError("worker message must contain exactly one of result or error")


class OneShotRecoveryWorker:
    """Run crash reconciliation and session verification away from Tk/UIA.

    Recovery mutates durable economic state under ``WorkspaceEconomicLock`` and must
    not be abandoned during process shutdown. The worker therefore mirrors the
    replay worker's non-daemon lifecycle; the GUI blocks close until a terminal
    message has been consumed.
    """

    def __init__(self) -> None:
        self._messages: queue.Queue[RecoveryWorkerMessage] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._busy = False
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(self, task: RecoveryTask) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self._thread = threading.Thread(
            target=self._run,
            args=(task,),
            name="autosport-workspace-recovery",
            daemon=False,
        )
        self._thread.start()
        return True

    def _run(self, task: RecoveryTask) -> None:
        try:
            message = RecoveryWorkerMessage(result=task())
        except Exception as exc:
            message = RecoveryWorkerMessage(error=f"{type(exc).__name__}: {exc}")
        self._messages.put(message)

    def poll(self) -> RecoveryWorkerMessage | None:
        try:
            message = self._messages.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy = False
        return message


def recover_workspace_once(
    workspace: str | Path,
    strategy_id: str,
    research_plan: ResearchStrategyPlan | None = None,
    *,
    initial_bankroll: str = "10000",
) -> RecoveryWorkerResult:
    """Reconcile then verify reopen while keeping SQLite ownership on this thread.

    ``AutosportSession`` owns a SQLite connection whose default thread affinity must
    not cross into Tk. Capture only immutable display state and close the session on
    this worker thread before returning to the GUI.
    """

    root = Path(workspace)
    report: RecoveryReport | None = None
    recovery_error: str | None = None
    try:
        report = reconcile_late_crashes(root)
    except Exception as exc:
        recovery_error = f"{type(exc).__name__}: {exc}"

    snapshot: RecoverySessionSnapshot | None = None
    reopen_error: str | None = None
    session: AutosportSession | None = None
    try:
        session = AutosportSession(
            root,
            initial_bankroll,
            strategy_id=strategy_id,
            research_plan=research_plan,
        )
        snapshot = RecoverySessionSnapshot(
            balance=str(session.book.balance),
            committed_stake=str(session.book.committed_stake),
            strategy_id=session.strategy_id,
            workspace=str(session.workspace),
            ticket_lines=tuple(ticket_lines(session)),
        )
    except Exception as exc:
        reopen_error = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as exc:
                if reopen_error is None:
                    reopen_error = f"session close failed: {type(exc).__name__}: {exc}"

    return RecoveryWorkerResult(
        report=report,
        snapshot=snapshot,
        recovery_error=recovery_error,
        reopen_error=reopen_error,
    )
