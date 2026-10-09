from __future__ import annotations

import sys
from pathlib import Path


_MACHINE_MODE_ARITY = {
    "--diagnostic-output": 2,
    "--first-run-storage-audit-output": 2,
    "--accessibility-audit-output": 2,
    "--keyboard-audit-output": 2,
    "--restart-recovery-audit-output": 2,
    "--restart-recovery-stage-child": 3,
    "--restart-recovery-recover-child": 3,
    "--research-demo-audit-output": 3,
}
_WEBVIEW2_STARTUP_ERROR = (
    "Автоспорт не може відкрити доступний інтерфейс WebView2. "
    "Перевірте наявність Microsoft Edge WebView2 Runtime і доступ до локального "
    "сховища WebView2 у профілі вашого користувача."
)
_WEBVIEW2_STORAGE_ERROR = (
    "Автоспорт не може підготувати локальне сховище WebView2 для вашого профілю. "
    "Перевірте, що LOCALAPPDATA вказує на абсолютну папку вашого користувача, "
    "доступну для запису, і перезапустіть Автоспорт. Права адміністратора не потрібні."
)
_WORKSPACE_INSTANCE_BUSY_ERROR = (
    "Автоспорт уже відкритий для цього workspace в іншому процесі. "
    "Закрийте інший екземпляр Автоспорту та повторіть запуск. "
    "Economic і live state не змінено."
)
_WORKSPACE_INSTANCE_LOCK_ERROR = (
    "Автоспорт не може підтвердити одноосібний доступ до interactive workspace. "
    "Перевірте доступ до папки workspace та повторіть запуск. "
    "Economic і live state не змінено."
)


def _show_workspace_configuration_error(detail: str) -> None:
    """Show an accessible native Windows error before any interactive GUI state opens."""

    import ctypes

    from autosport.localization import text

    title = text("ui.windows.workspace_configuration.title")
    from autosport.secret_redaction import redact_operator_text

    message = text("ui.windows.workspace_configuration.message", detail=redact_operator_text(detail))
    # MB_OK | MB_ICONERROR. Native MessageBox is keyboard-operable and exposed
    # through standard Windows accessibility rather than a custom visual surface.
    ctypes.windll.user32.MessageBoxW(None, message, title, 0x00000010)


def _probe_workspace_writable(workspace: Path) -> None:
    """Fail before GUI construction when canonical durable publication is unavailable."""

    from autosport.storage_preflight import probe_workspace_writable

    probe_workspace_writable(workspace)


def _workspace_access_error_message(workspace: Path, exc: OSError) -> str:
    from autosport.localization import text
    from autosport.secret_redaction import redact_operator_text, safe_exception_detail

    detail = " ".join(safe_exception_detail(
        exc, unavailable_detail=text("ui.windows.workspace_access.unknown_error")
    ).splitlines()).strip() or text(
        "ui.windows.workspace_access.unknown_error"
    )
    return text(
        "ui.windows.workspace_access.message",
        workspace=redact_operator_text(str(workspace)),
        error_type=OSError.__name__,
        error_detail=detail,
    )


def _show_workspace_access_error(workspace: Path, exc: OSError) -> None:
    """Show an actionable native error when first-run durable storage cannot open."""

    import ctypes

    from autosport.localization import text

    title = text("ui.windows.workspace_access.title")
    ctypes.windll.user32.MessageBoxW(
        None,
        _workspace_access_error_message(workspace, exc),
        title,
        0x00000010,
    )


def _show_startup_error(message: str) -> None:
    """Show one native Windows failure message without starting the legacy Tk shell."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "Автоспорт — помилка запуску",
            0x00000010,
        )
    except Exception:
        pass



def _offer_native_emergency_stop(workspace: Path, failure_message: str) -> None:
    """Keep an explicit, keyboard-accessible STOP choice after WebView2 startup fails.

    This is not an automatic execution STOP. The native Windows dialog defaults to
    No, and a Yes response attempts only the canonical durable admission STOP.
    A failed or uncertain journal write must never be announced as confirmed.
    """

    try:
        import ctypes

        message_box = ctypes.windll.user32.MessageBoxW
        choice = message_box(
            None,
            failure_message
            + "\n\nWebView2 недоступний. Чи активувати аварійний STOP через "
            "стійкий журнал заборони нових виконань?\n"
            "Це не доводить завершення вже запущених процесів. "
            "Кнопка «Ні» вибрана за замовчуванням.",
            "Автоспорт — резервне аварійне керування",
            0x00000004 | 0x00000030 | 0x00000100,  # YESNO, warning, default NO
        )
    except Exception:
        _show_startup_error(failure_message)
        return

    if choice != 6:  # IDYES; No, Escape, close and unexpected results are inert.
        return

    try:
        from autosport.windows_emergency_stop import WindowsEmergencyStopBridge

        result = WindowsEmergencyStopBridge.for_workspace(workspace).activate()
        confirmed = result.stopped is True
        message = (
            result.message_uk
            if confirmed
            else "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. "
            "Не вважайте нові виконання заблокованими без підтвердження; "
            "перевірте стійкий журнал STOP."
        )
    except Exception:
        confirmed = False
        message = (
            "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. "
            "Доступ до стійкого журналу не підтверджено. "
            "Не вважайте нові виконання заблокованими без перевірки."
        )

    try:
        message_box(
            None,
            message,
            "Автоспорт — результат аварійного STOP",
            0x00000040 if confirmed else 0x00000010,  # information / error
        )
    except Exception:
        pass


def _run_owned_interactive_gui(workspace: Path, webview_storage: Path) -> int:
    """Run the interactive WebView stack while caller holds both storage ownership locks."""

    from autosport.webview2_release_environment import (
        active_webview2_environment_overrides,
    )

    if active_webview2_environment_overrides():
        _offer_native_emergency_stop(workspace, _WEBVIEW2_STARTUP_ERROR)
        return 3

    try:
        from autosport.webview2_runtime_deployment import ensure_webview2_runtime

        runtime_preflight = ensure_webview2_runtime()
    except Exception:
        _offer_native_emergency_stop(workspace, _WEBVIEW2_STARTUP_ERROR)
        return 3

    if runtime_preflight.available is not True:
        _offer_native_emergency_stop(workspace, _WEBVIEW2_STARTUP_ERROR)
        return 3

    # A damaged/incomplete packaged shell must still provide the native
    # keyboard-accessible STOP choice. Keep the imports inside their own
    # guard: WindowsWebViewUnavailable is undefined if shell import fails.
    try:
        from autosport.windows_webview_emergency_stop import EmergencyStopWebController
        from autosport.windows_webview_shell import (
            AutosportWebBridge,
            WindowsWebViewUnavailable,
            launch_windows_shell,
        )
    except Exception:
        _offer_native_emergency_stop(workspace, _WEBVIEW2_STARTUP_ERROR)
        return 3

    try:
        controller = EmergencyStopWebController(workspace)
        return launch_windows_shell(
            AutosportWebBridge(controller),
            storage_path=webview_storage,
        )
    except WindowsWebViewUnavailable as exc:
        if getattr(exc, "reason", None) == "storage":
            _offer_native_emergency_stop(workspace, _WEBVIEW2_STORAGE_ERROR)
            return 2
        _offer_native_emergency_stop(workspace, _WEBVIEW2_STARTUP_ERROR)
        return 3
    except Exception:
        # Construction and renderer failures can contain filesystem/secret
        # details. Do not print their text or silently bypass native STOP.
        _offer_native_emergency_stop(workspace, _WEBVIEW2_STARTUP_ERROR)
        return 3


def _run_interactive_gui() -> int:
    from autosport.paths import (
        default_webview_storage_path,
        default_workspace,
        validate_product_storage_roots,
    )

    try:
        workspace = default_workspace()
        if not workspace.is_absolute():
            raise ValueError(
                "Resolved Autosport workspace must be an absolute path; "
                "configure an absolute AUTOSPORT_WORKSPACE or LOCALAPPDATA value"
            )
        webview_storage = default_webview_storage_path()
        validate_product_storage_roots(workspace, webview_storage)
    except ValueError as exc:
        _show_workspace_configuration_error(str(exc))
        return 2

    # Establish local durable-state writability before checking the optional GUI
    # runtime dependency. A missing WebView2 runtime must not mask a damaged or
    # inaccessible canonical workspace as a dependency-only startup failure.
    try:
        _probe_workspace_writable(workspace)
    except OSError as exc:
        _show_workspace_access_error(workspace, exc)
        return 2

    from autosport.workspace_lock import (
        WorkspaceEconomicLockBusyError,
        WorkspaceEconomicLockError,
        WorkspaceInteractiveLock,
    )

    try:
        # The economic workspace and the WebView2 profile are independent mutable
        # authorities. Lock both for the full operator lifetime so either shared
        # root has deterministic single-owner behavior across processes.
        with (
            WorkspaceInteractiveLock(workspace),
            WorkspaceInteractiveLock(webview_storage.parent),
        ):
            return _run_owned_interactive_gui(workspace, webview_storage)
    except WorkspaceEconomicLockBusyError:
        _show_startup_error(_WORKSPACE_INSTANCE_BUSY_ERROR)
        return 2
    except (WorkspaceEconomicLockError, OSError):
        _show_startup_error(_WORKSPACE_INSTANCE_LOCK_ERROR)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args:
        expected_arity = _MACHINE_MODE_ARITY.get(args[0])
        if expected_arity is None or len(args) != expected_arity:
            return 2

    if args and args[0] == "--diagnostic-output":
        from autosport.diagnostic import run_machine_diagnostic

        return run_machine_diagnostic(args[1])
    if args and args[0] == "--first-run-storage-audit-output":
        from autosport.first_run_storage_audit import run_first_run_storage_audit

        return run_first_run_storage_audit(args[1])
    if args and args[0] == "--accessibility-audit-output":
        from autosport.windows_webview_audit import run_accessibility_audit

        return run_accessibility_audit(args[1])
    if args and args[0] == "--keyboard-audit-output":
        from autosport.windows_webview_audit import run_keyboard_audit

        return run_keyboard_audit(args[1])
    if args and args[0] == "--restart-recovery-audit-output":
        from autosport.process_recovery_audit import run_packaged_restart_recovery_audit

        return run_packaged_restart_recovery_audit(args[1])
    if args and args[0] == "--restart-recovery-stage-child":
        from autosport.process_recovery_audit import run_process_kill_stage_child

        return run_process_kill_stage_child(args[1], args[2])
    if args and args[0] == "--restart-recovery-recover-child":
        from autosport.process_recovery_audit import run_process_kill_recovery_child

        return run_process_kill_recovery_child(args[1], args[2])
    if args and args[0] == "--research-demo-audit-output":
        from autosport.research_demo_audit import run_research_demo_audit

        return run_research_demo_audit(args[1], args[2])
    return _run_interactive_gui()


if __name__ == "__main__":
    raise SystemExit(main())
