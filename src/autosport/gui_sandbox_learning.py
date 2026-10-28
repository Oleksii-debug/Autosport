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
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
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

    def start(self, workspace: Path, *, resume: bool = False) -> bool:
        if not isinstance(workspace, Path) or not workspace.is_absolute():
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._status = "Запускається симуляція котирувань; реальних ставок немає."
            self._stop.clear()
        try:
            threading.Thread(target=self._run, args=(workspace, resume),
                             name="autosport-sandbox-learning", daemon=False).start()
        except Exception:
            with self._lock:
                self._busy = False
                self._status = "Не вдалося запустити симуляцію."
            return False
        return True

    def request_stop(self) -> None:
        self._stop.set()

    def _run(self, workspace: Path, resume: bool) -> None:
        try:
            root = workspace / "sandbox-learning"
            root.mkdir(parents=True, exist_ok=True)
            def progress(event: dict[str, object]) -> None:
                with self._lock:
                    self._status = (
                        "Симуляція зміни котирувань: знімків "
                        + str(event["network_snapshots"])
                        + ", помилок джерела " + str(event["network_failures"])
                        + ". Це не результати матчів і не доказ прибутку."
                    )

            # One lock covers selection, creation, resumption and the full run;
            # separate desktop processes cannot concurrently mutate a checkpoint.
            with WorkspaceEconomicLock(root):
                if resume:
                    marker = strict_json_loads((root / "latest-session.json").read_text("utf-8"))
                    if type(marker) is not dict or set(marker) != {"session"}:
                        raise ValueError("invalid session marker")
                    name = marker["session"]
                    if type(name) is not str or not name.startswith("session-") or Path(name).name != name:
                        raise ValueError("invalid session name")
                    directory = root / name
                    if directory.is_symlink() or not directory.is_dir():
                        raise ValueError("invalid session directory")
                    campaign = BlindPaperSandboxCampaign.load(directory)
                    if campaign.plan.campaign_id != name:
                        raise ValueError("session identity mismatch")
                    cutoff = datetime.fromisoformat(campaign.plan.cutoff_at.replace("Z", "+00:00"))
                    remaining = int((cutoff - datetime.now(timezone.utc)).total_seconds()) - 1
                    if remaining < 1:
                        raise ValueError("session cutoff expired")
                    duration = min(600, remaining)
                else:
                    directory = Path(tempfile.mkdtemp(prefix="session-", dir=root))
                    started = datetime.now(timezone.utc)
                    plan = BlindPaperCampaignPlan(
                        campaign_id=directory.name,
                        started_at=started.isoformat(timespec="microseconds"),
                        cutoff_at=(started + timedelta(hours=24)).isoformat(timespec="microseconds"),
                    )
                    campaign = BlindPaperSandboxCampaign(plan)
                    campaign.save(directory)
                    atomic_write_json(root / "latest-session.json", {"session": directory.name})
                    duration = 600
                summary = run_network_endurance(
                    campaign, workspace=directory, duration_seconds=duration,
                    poll_interval_seconds=30, progress=progress, stop_event=self._stop,
                )
            prefix = "Симуляцію зупинено" if self._stop.is_set() else "Симуляцію завершено"
            change = Decimal(str(summary["final_balance"])) - Decimal(str(summary["initial_bankroll"]))
            counters = summary["counters"]
            actions = counters["SINGLE"] + counters["PARLAY_2"]
            status = (
                prefix + "; знімків: " + str(summary["network_snapshots_segment"])
                + ", помилок джерела: " + str(summary["network_failures_segment"])
                + "; умовних дій: " + str(actions) + ", очікувань: " + str(counters["WAIT"])
                + "; умовна зміна балансу: " + str(change) + " грн. "
                + "Дані віртуальні, прибуток не підтверджено."
            )
        except Exception:
            status = ("Не вдалося відновити або завершити симуляцію. Перевірте стан, "
                      "термін збереженого сеансу та доступ до робочої папки.")
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
