from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .dataset import load_dataset
from .replay import ReplayStopRequested, ReplayStopToken, replay_stop_scope
from .research_strategy import ResearchStrategyPlan
from .session import AutosportSession, SessionResult
from .strategies import experiment_strategy_id


ReplayTask = Callable[[], SessionResult]


@dataclass(frozen=True, slots=True)
class ReplayWorkerMessage:
    result: SessionResult | None = None
    error: str | None = None
    stopped: bool = False

    def __post_init__(self) -> None:
        terminal_count = int(self.result is not None) + int(self.error is not None) + int(self.stopped)
        if terminal_count != 1:
            raise ValueError("worker message must contain exactly one terminal outcome")


def _terminal_error(exc: BaseException) -> str:
    """Render a caught failure without trusting arbitrary exception metadata."""

    try:
        # Bypass a custom metaclass __getattribute__: even exception type-name
        # lookup must not be able to defeat terminal publication after the
        # single-flight slot has been acquired.
        exception_type = str.__str__(type.__getattribute__(type(exc), "__name__"))
    except BaseException:
        exception_type = "BaseException"
    try:
        # ``str(exc)`` may legally return a str subclass with hostile overridden
        # methods such as __format__. Detach through the trusted base str method
        # before any formatting/concatenation touches the rendered detail.
        detail = str.__str__(str(exc))
    except BaseException:
        return exception_type + ": exception details unavailable"
    return exception_type + ": " + detail


class OneShotReplayWorker:
    """Run one economic replay away from Tk without abandoning it on process shutdown."""

    def __init__(self) -> None:
        self._messages: queue.Queue[ReplayWorkerMessage] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._busy = False
        self._thread: threading.Thread | None = None
        self._stop_token: ReplayStopToken | None = None
        self._accepted_stop_token: ReplayStopToken | None = None
        self._stopped_pending = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def stop_available(self) -> bool:
        with self._lock:
            token = self._stop_token
            return bool(self._busy and token is not None and token.accepting)

    @property
    def stopped_pending(self) -> bool:
        """Allow the UI to intercept only STOP terminals without consuming others."""

        with self._lock:
            return self._stopped_pending

    def request_stop(self) -> bool:
        """Request cooperative STOP only while the replay can still honor it."""

        with self._lock:
            if not self._busy or self._stop_token is None:
                return False
            token = self._stop_token
            accepted = token.request()
            if accepted:
                self._accepted_stop_token = token
            return accepted

    def start(self, task: ReplayTask) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._stop_token = ReplayStopToken()
            self._accepted_stop_token = None
            self._stopped_pending = False
            stop_token = self._stop_token
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
                args=(task, start_gate, cancelled, stop_token),
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
        with self._lock:
            token = self._stop_token
            self._stop_token = None
            self._accepted_stop_token = None
        if token is not None:
            token.disarm()
        self._messages.put(ReplayWorkerMessage(error=_terminal_error(exc)))

    def _release_unstarted_slot(self) -> None:
        self._thread = None
        with self._lock:
            token = self._stop_token
            self._stop_token = None
            self._accepted_stop_token = None
            self._busy = False
        if token is not None:
            token.disarm()

    def _run_when_committed(
        self,
        task: ReplayTask,
        start_gate: threading.Event,
        cancelled: threading.Event,
        stop_token: ReplayStopToken,
    ) -> None:
        start_gate.wait()
        if cancelled.is_set():
            stop_token.disarm()
            return
        self._run(task, stop_token)

    def _run(self, task: ReplayTask, stop_token: ReplayStopToken) -> None:
        try:
            with replay_stop_scope(stop_token):
                message = ReplayWorkerMessage(result=task())
        except ReplayStopRequested as exc:
            # Close the exact token before classifying the exception so a forged
            # task exception cannot be retroactively laundered into operator STOP
            # by a request that arrives only after the exception has escaped task().
            stop_token.disarm()
            with self._lock:
                accepted_stop = self._accepted_stop_token is stop_token
            if accepted_stop:
                message = ReplayWorkerMessage(stopped=True)
            else:
                message = ReplayWorkerMessage(error=_terminal_error(exc))
        except BaseException as exc:
            # SystemExit/KeyboardInterrupt raised inside this detached background
            # thread do not provide a GUI terminal outcome by themselves. Publish
            # one so poll() clears the single-flight state instead of leaving the
            # application permanently busy after the worker thread has died.
            message = ReplayWorkerMessage(error=_terminal_error(exc))
        finally:
            stop_token.disarm()
        with self._lock:
            if self._stop_token is stop_token:
                self._stop_token = None
            if self._accepted_stop_token is stop_token:
                self._accepted_stop_token = None
            self._stopped_pending = message.stopped
        self._messages.put(message)

    def poll(self) -> ReplayWorkerMessage | None:
        try:
            message = self._messages.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy = False
            self._stop_token = None
            self._accepted_stop_token = None
            self._stopped_pending = False
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
