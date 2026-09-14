from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .dataset import load_dataset
from .research_strategy import ResearchStrategyPlan
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
        # be killed with the process at an arbitrary point, so keep it non-daemon
        # and let the GUI refuse close until the terminal worker message arrives.
        try:
            thread = threading.Thread(
                target=self._run,
                args=(task,),
                name="autosport-paper-replay",
                daemon=False,
            )
            self._thread = thread
            thread.start()
        except RuntimeError:
            # CPython reports OS/runtime inability to start a new thread as
            # RuntimeError. No economic task ran, so restore idle single-flight
            # state and let the existing False start result keep the GUI fail-closed.
            self._thread = None
            with self._lock:
                self._busy = False
            return False
        return True

    def _run(self, task: ReplayTask) -> None:
        try:
            message = ReplayWorkerMessage(result=task())
        except Exception as exc:
            message = ReplayWorkerMessage(error=f"{type(exc).__name__}: {exc}")
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
