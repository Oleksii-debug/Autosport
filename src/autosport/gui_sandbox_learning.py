"""Isolated, bounded, explicitly simulated learning for the desktop operator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import queue
import tempfile
import threading

from .paper_live_sandbox_campaign import (
    BlindPaperCampaignPlan, BlindPaperSandboxCampaign, run_network_endurance,
)
from .workspace_lock import WorkspaceEconomicLock


class SandboxLearningWorker:
    """Own one simulation until its durable terminal checkpoint is saved."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._stop = threading.Event()
        self._terminal: queue.Queue[str] = queue.Queue(maxsize=1)
        self._status = "Симуляцію навчання ще не запускали."

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def start(self, workspace: Path) -> bool:
        if not isinstance(workspace, Path) or not workspace.is_absolute():
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._status = "Запускається симуляція котирувань; реальних ставок немає."
            self._stop.clear()
        try:
            threading.Thread(target=self._run, args=(workspace,),
                             name="autosport-sandbox-learning", daemon=False).start()
        except Exception:
            with self._lock:
                self._busy = False
                self._status = "Не вдалося запустити симуляцію."
            return False
        return True

    def request_stop(self) -> None:
        self._stop.set()

    def _run(self, workspace: Path) -> None:
        try:
            root = workspace / "sandbox-learning"
            root.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="session-", dir=root))
            started = datetime.now(timezone.utc)
            plan = BlindPaperCampaignPlan(
                campaign_id=directory.name,
                started_at=started.isoformat(timespec="microseconds"),
                cutoff_at=(started + timedelta(minutes=15)).isoformat(timespec="microseconds"),
            )
            campaign = BlindPaperSandboxCampaign(plan)

            def progress(event: dict[str, object]) -> None:
                with self._lock:
                    self._status = (
                        "Симуляція зміни котирувань: знімків "
                        + str(event["network_snapshots"])
                        + ", помилок джерела " + str(event["network_failures"])
                        + ". Це не результати матчів і не доказ прибутку."
                    )

            with WorkspaceEconomicLock(directory):
                summary = run_network_endurance(
                    campaign, workspace=directory, duration_seconds=600,
                    poll_interval_seconds=30, progress=progress, stop_event=self._stop,
                )
            prefix = "Симуляцію зупинено" if self._stop.is_set() else "Симуляцію завершено"
            change = Decimal(str(summary["final_balance"])) - Decimal(str(summary["initial_bankroll"]))
            status = (
                prefix + "; знімків: " + str(summary["network_snapshots_segment"])
                + ", помилок джерела: " + str(summary["network_failures_segment"])
                + "; умовна зміна балансу: " + str(change) + " грн. "
                + "Дані віртуальні, прибуток не підтверджено."
            )
        except Exception:
            status = "Симуляція не завершилась; перевірте доступ до джерела та робочої папки."
        self._terminal.put(status)

    def poll(self) -> str | None:
        try:
            status = self._terminal.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._status = status
            self._busy = False
        return status
