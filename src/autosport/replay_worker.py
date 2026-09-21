from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .dataset import load_dataset
from .research_strategy import ResearchStrategyPlan
from .secret_redaction import safe_exception_text
from .session import AutosportSession, SessionResult
from .strategies import experiment_strategy_id


ReplayTask = Callable[[], SessionResult]


@dataclass(frozen=True, slots=True)
class ReplayWorkerMessage:
    result: SessionResult | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error is None):
            raise ValueError("worker message must contain exactly one of result or error")


def _terminal_error(exc: BaseException) -> str:
    """Render a terminal worker failure through the product redaction boundary."""

    return safe_exception_text(exc)

class OneShotReplayWorker:
    """Run one economic replay away from Tk without abandoning it on process shutdown."""

    def __init__(self) -> None:
        self._messages: queue.Queue[ReplayWorkerMessage] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._busy = False
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(self, task: ReplayTask) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        # Economic replay may be inside PRECOMMIT/promotion. A daemon thread could
        # be killed with the process at an arbitrary point, so keep it non-daemon.
        # The helper additionally waits behind a start-commit gate: Thread.start()
        # can create the OS thread and still raise to its caller, and an economic
        # task must not run until startup has returned successfully and committed.
        try:
            start_gate = threading.Event()
            cancelled = threading.Event()
            thread = threading.Thread(
                target=self._run_when_committed,
                args=(task, start_gate, cancelled),
                name="autosport-paper-replay",
                daemon=False,
            )
        except BaseException as exc:
            if isinstance(exc, Exception):
                self._publish_setup_failure(exc)
                return True
            self._release_unstarted_slot()
            raise

        self._thread = thread
        task_committed = False
        try:
            thread.start()
            # Commit before opening the worker gate. If process-control flow lands
            # after this assignment, the task is already authorized and must be
            # released exactly once rather than rolled back as an unstarted request.
            task_committed = True
            start_gate.set()
        except BaseException as exc:
            if task_committed:
                start_gate.set()
                if isinstance(exc, Exception):
                    return True
                raise

            # Thread.start() may have created a real helper before raising. Cancel
            # before opening the gate so that helper exits without invoking task()
            # and can never publish a competing terminal task result.
            cancelled.set()
            start_gate.set()
            if isinstance(exc, Exception):
                self._publish_setup_failure(exc)
                return True
            self._release_unstarted_slot()
            raise
        return True

    def _publish_setup_failure(self, exc: Exception) -> None:
        # Preserve #302's established caller contract: a request which won the slot
        # returns True for ordinary setup failure and publishes exactly one terminal
        # error; poll() is what restores idle. False remains "already busy" only.
        self._thread = None
        self._messages.put(ReplayWorkerMessage(error=_terminal_error(exc)))

    def _release_unstarted_slot(self) -> None:
        self._thread = None
        with self._lock:
            self._busy = False

    def _run_when_committed(
        self,
        task: ReplayTask,
        start_gate: threading.Event,
        cancelled: threading.Event,
    ) -> None:
        start_gate.wait()
        if cancelled.is_set():
            return
        self._run(task)

    def _run(self, task: ReplayTask) -> None:
        try:
            message = ReplayWorkerMessage(result=task())
        except BaseException as exc:
            # SystemExit/KeyboardInterrupt raised inside this detached background
            # thread do not provide a GUI terminal outcome by themselves. Publish
            # one so poll() clears the single-flight state instead of leaving the
            # application permanently busy after the worker thread has died.
            message = ReplayWorkerMessage(error=_terminal_error(exc))
        self._messages.put(message)

    def poll(self) -> ReplayWorkerMessage | None:
        try:
            message = self._messages.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy = False
        return message


def workspace_for_strategy(
    workspace_root: str | Path,
    strategy_id: str,
    research_plan: ResearchStrategyPlan | None = None,
) -> Path:
    """Resolve deterministic economic storage for one executable strategy identity.

    Preserve the legacy baseline workspace so existing V1 state does not move.
    Other strategies live below ``strategies/``. Research strategy identity already
    includes the canonical plan hash, so different plans cannot inherit each other's
    PaperBook, ledger or registry state.
    """

    root = Path(workspace_root)
    identity = experiment_strategy_id(strategy_id, research_plan)
    if identity == "baseline-v1":
        return root
    return root / "strategies" / identity.replace("@", "-")


def run_workspace_dataset_once(
    workspace: str | Path,
    dataset_path: str | Path,
    *,
    initial_bankroll: str = "10000",
    speed: float = 0.0,
    strategy_id: str = "baseline-v1",
    research_plan: ResearchStrategyPlan | None = None,
) -> SessionResult:
    """Own all replay-session resources on the calling worker thread.

    ``workspace`` is the exact economic workspace chosen by the caller. Windows GUI
    callers resolve it once with :func:`workspace_for_strategy`; keeping this helper
    literal prevents accidental nested ``strategies/<identity>`` directories.
    """

    dataset = load_dataset(dataset_path)
    session = AutosportSession(
        workspace,
        initial_bankroll,
        strategy_id=strategy_id,
        research_plan=research_plan,
    )
    try:
        return session.run_dataset(dataset, speed=speed)
    finally:
        session.close()
