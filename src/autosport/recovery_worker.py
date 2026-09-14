from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .paper import PaperBook
from .recovery import RecoveryReport, reconcile_late_crashes
from .research_strategy import ResearchStrategyPlan
from .session import AutosportSession


@dataclass(frozen=True, slots=True)
class RecoverySessionView:
    """Thread-neutral economic state rendered by Tk after worker-owned session validation."""

    book: PaperBook
    strategy_id: str
    workspace: Path


@dataclass(frozen=True, slots=True)
class RecoveryTaskResult:
    report: RecoveryReport
    session_view: RecoverySessionView


RecoveryTask = Callable[[], RecoveryTaskResult]


@dataclass(frozen=True, slots=True)
class RecoveryWorkerMessage:
    result: RecoveryTaskResult | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error is None):
            raise ValueError("worker message must contain exactly one of result or error")


class OneShotRecoveryWorker:
    """Run one mutating workspace reconciliation away from Tk/UIA/NVDA event handling."""

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
        # Recovery can mutate transaction/registry state. Do not allow interpreter
        # shutdown to kill it at an arbitrary persistence boundary. The worker waits
        # behind a gate until Thread.start() has returned successfully, so an
        # interrupted/failed start can cancel a partially-created OS thread without
        # ever running the economic recovery task.
        start_gate = threading.Event()
        cancelled = threading.Event()
        try:
            thread = threading.Thread(
                target=self._run_when_committed,
                args=(task, start_gate, cancelled),
                name="autosport-workspace-recovery",
                daemon=False,
            )
        except BaseException as exc:
            self._release_unstarted_slot()
            if isinstance(exc, Exception):
                return False
            raise

        self._thread = thread
        task_committed = False
        try:
            thread.start()
            # Mark the request committed before releasing the worker. If a
            # process-control BaseException arrives after this assignment, the
            # handler must keep the single-flight slot occupied and ensure the
            # already-authorized task is released exactly once.
            task_committed = True
            start_gate.set()
        except BaseException as exc:
            if task_committed:
                start_gate.set()
                if isinstance(exc, Exception):
                    return True
                raise

            # Thread.start() can be interrupted after the OS thread exists but
            # before it returns to the caller. Cancel before opening the gate so
            # such a thread exits without touching recovery/economic state.
            cancelled.set()
            start_gate.set()
            self._release_unstarted_slot()
            if isinstance(exc, Exception):
                return False
            raise
        return True

    def _release_unstarted_slot(self) -> None:
        self._thread = None
        with self._lock:
            self._busy = False

    def _run_when_committed(
        self,
        task: RecoveryTask,
        start_gate: threading.Event,
        cancelled: threading.Event,
    ) -> None:
        start_gate.wait()
        if cancelled.is_set():
            return
        self._run(task)

    def _run(self, task: RecoveryTask) -> None:
        try:
            message = RecoveryWorkerMessage(result=task())
        except BaseException as exc:
            # Error rendering is itself an untrusted boundary: arbitrary exception
            # classes may implement a broken __str__. Never let that secondary
            # failure kill the worker before the terminal message reaches poll().
            try:
                error = f"{type(exc).__name__}: {exc}"
            except BaseException:
                error = "BaseException: recovery task failed; exception details unavailable"
            message = RecoveryWorkerMessage(error=error)
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
    *,
    initial_bankroll: str = "10000",
    strategy_id: str = "baseline-v1",
    research_plan: ResearchStrategyPlan | None = None,
) -> RecoveryTaskResult:
    """Reconcile and validate/reopen one strategy workspace entirely on the calling worker thread.

    SQLiteMarketStore uses sqlite's default same-thread connection contract. Therefore
    the worker-owned AutosportSession is never handed to Tk. We retain only a pure
    PaperBook/identity view after closing the worker-owned SQLite session on the same
    thread that created it.
    """

    root = Path(workspace)
    report = reconcile_late_crashes(root)
    session = AutosportSession(
        root,
        initial_bankroll,
        strategy_id=strategy_id,
        research_plan=research_plan,
    )
    try:
        view = RecoverySessionView(
            book=session.book,
            strategy_id=session.strategy_id,
            workspace=session.workspace,
        )
        return RecoveryTaskResult(report=report, session_view=view)
    finally:
        session.close()
