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

    # ValueError text is implementation diagnostics, not bounded operator copy.  The
    # recovery instruction below is complete without announcing Python-origin text.
    del detail
    title = "Автоспорт — помилка конфігурації workspace"
    message = (
        "Автоспорт не відкрив interactive workspace через недійсну конфігурацію.\n\n"
        "Вкажіть абсолютний шлях у AUTOSPORT_WORKSPACE, окремий від сховища WebView2, "
        "або виправте LOCALAPPDATA, потім перезапустіть Автоспорт. "
        "Economic і live state не змінено."
    )
    ctypes.windll.user32.MessageBoxW(None, message, title, 0x00000010)


def _probe_workspace_writable(workspace: Path) -> None:
    """Fail before GUI construction when canonical durable publication is unavailable."""

    from autosport.storage_preflight import probe_workspace_writable

    probe_workspace_writable(workspace)


def _workspace_access_error_message(workspace: Path, exc: OSError) -> str:
    # The exception remains useful to the internal caller for failure classification,
    # but raw filesystem detail and Python exception class names are not operator UI.
    # They may contain account names, host paths, locale-dependent OS text or other
    # implementation detail that should not be announced by the native dialog/NVDA.
    del exc
    return (
        "Автоспорт не може підготувати workspace для запису.\n\n"
        f"Workspace: {workspace}\n\n"
        "Вкажіть AUTOSPORT_WORKSPACE як абсолютний шлях до папки вашого користувача, "
        "доступної для запису, і перезапустіть Автоспорт. "
        "Права адміністратора не потрібні. Economic і live state не змінено."
    )


def _show_workspace_access_error(workspace: Path, exc: OSError) -> None:
    """Show an actionable native error when first-run durable storage cannot open."""

    import ctypes

    title = "Автоспорт — workspace недоступний для запису"
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


def _run_owned_interactive_gui(workspace: Path) -> int:
    """Run the interactive WebView stack while caller holds workspace ownership."""

    from autosport.webview2_release_environment import (
        active_webview2_environment_overrides,
    )

    if active_webview2_environment_overrides():
        _show_startup_error(_WEBVIEW2_STARTUP_ERROR)
        return 3

    try:
        from autosport.webview2_runtime_deployment import ensure_webview2_runtime

        runtime_preflight = ensure_webview2_runtime()
    except Exception:
        _show_startup_error(_WEBVIEW2_STARTUP_ERROR)
        return 3

    if runtime_preflight.available is not True:
        _show_startup_error(_WEBVIEW2_STARTUP_ERROR)
        return 3

    from autosport.windows_webview_emergency_stop import EmergencyStopWebController
    from autosport.windows_webview_shell import (
        AutosportWebBridge,
        WindowsWebViewUnavailable,
        launch_windows_shell,
    )

    try:
        controller = EmergencyStopWebController(workspace)
        return launch_windows_shell(AutosportWebBridge(controller))
    except WindowsWebViewUnavailable as exc:
        if getattr(exc, "reason", None) == "storage":
            _show_startup_error(_WEBVIEW2_STORAGE_ERROR)
            return 2
        _show_startup_error(_WEBVIEW2_STARTUP_ERROR)
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
        with WorkspaceInteractiveLock(workspace):
            return _run_owned_interactive_gui(workspace)
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
