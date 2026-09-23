from __future__ import annotations

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
    # MB_OK | MB_ICONERROR. Native MessageBox is keyboard-operable and exposed
    # through standard Windows accessibility rather than a custom visual surface.
    ctypes.windll.user32.MessageBoxW(None, message, title, 0x00000010)


def _probe_workspace_writable(workspace: Path) -> None:
    """Fail before GUI construction when durable workspace storage is not writable."""

    import tempfile

    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=workspace,
        prefix=".autosport-write-probe-",
        suffix=".tmp",
        delete=True,
    ) as probe:
        probe.write(b"autosport workspace write probe\n")
        probe.flush()


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


def _run_interactive_gui() -> int:
    # Validate durable workspace identity before importing/constructing the GUI.
    # `default_workspace()` remains the canonical path resolver. This packaged
    # boundary also requires its resolved result to be absolute so the current
    # main path implementation cannot silently make durable identity depend on CWD.
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

    from autosport.windows_gui import main as gui_main

    return gui_main()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # Packaged machine/audit invocations must fail closed. Previously any
    # unknown flag silently fell through into the interactive GUI, which could
    # turn a typo in CI/release automation into a hung or misleading run.
    if args:
        expected_arity = _MACHINE_MODE_ARITY.get(args[0])
        if expected_arity is None or len(args) != expected_arity:
            return 2

    # Install before importing any audit module or entering the normal GUI path.
    # This guarantees that packaged release audits inspect the same compact
    # layout that users receive rather than an audit-only geometry variant.
    from autosport.windows_layout import install_compact_windows_layout

    install_compact_windows_layout()
    if args and args[0] == "--diagnostic-output":
        from autosport.diagnostic import run_machine_diagnostic

        return run_machine_diagnostic(args[1])
    if args and args[0] == "--accessibility-audit-output":
        from autosport.accessibility_audit import run_accessibility_audit

        return run_accessibility_audit(args[1])
    if args and args[0] == "--keyboard-audit-output":
        from autosport.keyboard_audit import run_keyboard_audit

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
