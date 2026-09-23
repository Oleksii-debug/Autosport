from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .execution_stop_authority import (
    ExecutionAuthorityMode,
    ExecutionAuthorityState,
    ExecutionStopAuthority,
    ExecutionStopStateError,
)


EXECUTION_STOP_JOURNAL_FILENAME = "execution-stop.jsonl"
WINDOWS_EMERGENCY_STOP_OPERATOR_ID = "windows-operator"
WINDOWS_EMERGENCY_STOP_REASON = "windows-emergency-stop"


def execution_stop_path(workspace: str | Path) -> Path:
    """Return the one product STOP-authority journal inside the canonical workspace."""

    return Path(workspace) / EXECUTION_STOP_JOURNAL_FILENAME


@dataclass(frozen=True, slots=True)
class EmergencyStopResult:
    stopped: bool
    already_stopped: bool
    revision: int | None
    command_id: str | None
    message_uk: str
    message_en: str
    runtime_cooperation_verified: bool = False
    error: str | None = None

    @property
    def accessible_message(self) -> str:
        return f"{self.message_uk} / {self.message_en}"


class WindowsEmergencyStopBridge:
    """Bind a Windows emergency action to the canonical durable STOP authority.

    The bridge deliberately proves only durable execution admission STOP. It does
    not claim that an already-running worker, provider feed, or process has drained.
    A per-instance lock collapses duplicate UI activations; expected revisions and
    a confirming re-read preserve the same property across concurrent processes.
    """

    def __init__(self, authority: ExecutionStopAuthority | None) -> None:
        self._authority = authority
        self._lock = threading.RLock()

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> WindowsEmergencyStopBridge:
        return cls(ExecutionStopAuthority(execution_stop_path(workspace)))

    @staticmethod
    def _failure(detail: str) -> EmergencyStopResult:
        return EmergencyStopResult(
            stopped=False,
            already_stopped=False,
            revision=None,
            command_id=None,
            message_uk=(
                "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. "
                "Нові виконання мають залишатися заблокованими; перевірте журнал STOP."
            ),
            message_en=(
                "EMERGENCY STOP WAS NOT CONFIRMED. "
                "New execution must remain blocked; inspect the STOP journal."
            ),
            error=detail,
        )

    @staticmethod
    def _success(
        state: ExecutionAuthorityState,
        *,
        already_stopped: bool,
    ) -> EmergencyStopResult:
        status_uk = "вже був активний" if already_stopped else "активовано"
        status_en = "was already active" if already_stopped else "activated"
        return EmergencyStopResult(
            stopped=True,
            already_stopped=already_stopped,
            revision=state.revision,
            command_id=state.command_id,
            message_uk=(
                f"Аварійний STOP {status_uk}; підтверджено durable revision "
                f"{state.revision}. Це не є доказом зупинки вже запущеного worker/feed."
            ),
            message_en=(
                f"Emergency STOP {status_en}; durable revision {state.revision} "
                "confirmed. This does not prove an already-running worker/feed drained."
            ),
        )

    @staticmethod
    def _safe_error(_exc: Exception) -> str:
        # Emergency STOP failure detail is projected into operator status/log/UIA
        # surfaces. Never stringify an arbitrary exception here: provider, path,
        # transport, or library diagnostics may contain credentials or secrets.
        return "EMERGENCY_STOP_INTERNAL_ERROR"

    def _confirmed_stopped(
        self,
        candidate: ExecutionAuthorityState,
        *,
        already_stopped: bool,
    ) -> EmergencyStopResult:
        authority = self._authority
        if authority is None:
            return self._failure("execution STOP authority is unavailable")
        confirmed = authority.current()
        if confirmed.mode is not ExecutionAuthorityMode.STOPPED:
            return self._failure(
                f"authoritative mode after STOP is {confirmed.mode.value}, not STOPPED"
            )
        # A later concurrent STOP is still authoritative STOP. Never report the
        # stale candidate revision as the confirmed durable truth.
        return self._success(confirmed, already_stopped=already_stopped)

    def activate(self) -> EmergencyStopResult:
        with self._lock:
            authority = self._authority
            if authority is None:
                return self._failure("execution STOP authority is unavailable")

            try:
                try:
                    current = authority.current()
                except ExecutionStopStateError:
                    # A never-initialized canonical workspace is safe to initialize
                    # directly into STOPPED. Any partial/corrupt pair must remain a
                    # failure rather than being overwritten or inferred.
                    if authority.path.exists() or authority.anchor_path.exists():
                        raise
                    try:
                        initialized = authority.initialize_stopped(
                            operator_id=WINDOWS_EMERGENCY_STOP_OPERATOR_ID,
                            reason=WINDOWS_EMERGENCY_STOP_REASON,
                        )
                    except ExecutionStopStateError:
                        # Another process may have won the initialization race.
                        raced = authority.current()
                        if raced.mode is not ExecutionAuthorityMode.STOPPED:
                            raise
                        return self._confirmed_stopped(
                            raced,
                            already_stopped=True,
                        )
                    return self._confirmed_stopped(
                        initialized,
                        already_stopped=False,
                    )

                if current.mode is ExecutionAuthorityMode.STOPPED:
                    return self._confirmed_stopped(
                        current,
                        already_stopped=True,
                    )

                try:
                    stopped = authority.stop(
                        operator_id=WINDOWS_EMERGENCY_STOP_OPERATOR_ID,
                        reason=WINDOWS_EMERGENCY_STOP_REASON,
                        expected_revision=current.revision,
                    )
                except ExecutionStopStateError:
                    # Cross-process idempotence: a competing STOP that committed
                    # first is success only after an authoritative STOPPED re-read.
                    raced = authority.current()
                    if raced.mode is not ExecutionAuthorityMode.STOPPED:
                        raise
                    return self._confirmed_stopped(
                        raced,
                        already_stopped=True,
                    )
                return self._confirmed_stopped(
                    stopped,
                    already_stopped=False,
                )
            except Exception as exc:
                return self._failure(self._safe_error(exc))
