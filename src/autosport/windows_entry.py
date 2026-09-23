from __future__ import annotations

import os
import sys
from pathlib import Path


_MACHINE_MODE_ARITY = {
    "--diagnostic-output": 2,
    "--accessibility-audit-output": 2,
    "--keyboard-audit-output": 2,
    "--restart-recovery-audit-output": 2,
    "--restart-recovery-stage-child": 3,
    "--restart-recovery-recover-child": 3,
    "--research-demo-audit-output": 3,
}
_WEBVIEW2_STARTUP_ERROR = (
    "Автоспорт не може відкрити доступний інтерфейс WebView2. "
    "Перевірте наявність Microsoft Edge WebView2 Runtime."
)


def _show_workspace_configuration_error(detail: str) -> None:
    """Show an accessible native Windows error before any interactive GUI state opens."""

    import ctypes

    title = "Автоспорт — помилка конфігурації workspace"
    message = (
        "Автоспорт не відкрив interactive workspace через недійсну конфігурацію.\n\n"
        f"{detail}\n\n"
        "Вкажіть абсолютний шлях у AUTOSPORT_WORKSPACE або виправте LOCALAPPDATA, "
        "потім перезапустіть Автоспорт. Economic і live state не змінено."
    )
    ctypes.windll.user32.MessageBoxW(None, message, title, 0x00000010)


def _probe_workspace_writable(workspace: Path) -> None:
    """Fail before GUI construction when canonical durable publication is unavailable."""

    import tempfile

    from autosport.integrity import durable_path_lock

    payload = b"autosport workspace atomic publish probe\n"
    source: Path | None = None
    destination: Path | None = None
    lock_path: Path | None = None

    workspace.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=workspace,
            prefix=".autosport-write-probe-source-",
            suffix=".tmp",
            delete=False,
        ) as probe:
            source = Path(probe.name)
            probe.write(payload)
            probe.flush()
            os.fsync(probe.fileno())

        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=workspace,
            prefix=".autosport-write-probe-destination-",
            suffix=".tmp",
            delete=False,
        ) as published_probe:
            destination = Path(published_probe.name)
        lock_path = destination.with_name(f".{destination.name}.lock")

        with durable_path_lock(destination):
            os.replace(source, destination)
            source = None

        if destination.read_bytes() != payload:
            raise OSError("workspace atomic replace did not publish expected probe bytes")
    finally:
        for candidate in (source, destination, lock_path):
            if candidate is None:
                continue
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


def _workspace_access_error_message(workspace: Path, exc: OSError) -> str:
    detail = " ".join(str(exc).splitlines()).strip() or "невідома помилка файлової системи"
    return (
        "Автоспорт не може підготувати workspace для запису.\n\n"
        f"Workspace: {workspace}\n"
        f"Помилка: {type(exc).__name__}: {detail}\n\n"
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


def _run_interactive_gui() -> int:
    from autosport.paths import default_workspace

    try:
        workspace = default_workspace()
        if not workspace.is_absolute():
            raise ValueError(
                "Resolved Autosport workspace must be an absolute path; "
                "configure an absolute AUTOSPORT_WORKSPACE or LOCALAPPDATA value"
            )
    except ValueError as exc:
        _show_workspace_configuration_error(str(exc))
        return 2

    try:
        _probe_workspace_writable(workspace)
    except OSError as exc:
        _show_workspace_access_error(workspace, exc)
        return 2

    try:
        from autosport.webview2_runtime_preflight import probe_webview2_runtime

        runtime_preflight = probe_webview2_runtime()
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
        _show_startup_error(_WEBVIEW2_STARTUP_ERROR + "\n\n" + str(exc))
        return 3


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args:
        expected_arity = _MACHINE_MODE_ARITY.get(args[0])
        if expected_arity is None or len(args) != expected_arity:
            return 2

    if args and args[0] == "--diagnostic-output":
        from autosport.diagnostic import run_machine_diagnostic

        return run_machine_diagnostic(args[1])
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
