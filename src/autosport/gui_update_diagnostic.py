"""Bounded, nonblocking operator diagnostic over the canonical signed-update preflight.

It never installs, downloads, modifies a release, or grants an independent trust
root. All user-visible results are static Ukrainian text, not backend exceptions.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import queue
import threading

from .windows_update_cli import run as run_canonical_update_diagnostic


@dataclass(frozen=True, slots=True)
class UpdateDiagnosticResult:
    status: str


class OneShotUpdateDiagnosticWorker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._queue: queue.Queue[UpdateDiagnosticResult] = queue.Queue(maxsize=1)

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(self, request: str) -> bool:
        if (
            type(request) is not str or not request or request != request.strip()
            or len(request) > 4096 or not Path(request).is_absolute()
            or "\x00" in request
        ):
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        try:
            thread = threading.Thread(
                target=self._run,
                args=(request,),
                name="autosport-update-diagnostic",
                daemon=False,
            )
            thread.start()
        except Exception:
            with self._lock:
                self._busy = False
            return False
        return True

    def _run(self, request: str) -> None:
        try:
            code, report = run_canonical_update_diagnostic(request)
            if (
                code == 0 and type(report) is dict
                and report.get("install_authorized") is False
                and report.get("independent_trust_root_verified") is False
                and report.get("status") == "DIAGNOSTIC_PASS_UNTRUSTED_ROOT"
            ):
                status = (
                    "Діагностика пропонує підготовку оновлення, але "
                    "незалежний корінь довіри не підтверджений. "
                    "Встановлення НЕ дозволено."
                )
            elif (
                code == 0 and type(report) is dict
                and report.get("install_authorized") is False
                and report.get("independent_trust_root_verified") is False
                and report.get("status") == "DIAGNOSTIC_NO_CHANGE_UNTRUSTED_ROOT"
            ):
                status = (
                    "Порівняння версій: змін немає. "
                    "Незалежний корінь довіри не підтверджений. "
                    "Встановлення НЕ дозволено."
                )
            else:
                status = (
                    "Діагностику оновлення відхилено. "
                    "Жодне встановлення або зміну даних не виконано."
                )
        except Exception:
            status = (
                "Діагностика завершилась помилкою. "
                "Жодне встановлення або зміну даних не виконано."
            )
        # The UI consumes one immutable, bounded result and clears the busy flag.
        self._queue.put(UpdateDiagnosticResult(status=status))

    def poll(self) -> UpdateDiagnosticResult | None:
        try:
            result = self._queue.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy = False
        return result
