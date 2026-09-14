from __future__ import annotations

import sys


_MACHINE_MODE_ARITY = {
    "--diagnostic-output": 2,
    "--accessibility-audit-output": 2,
    "--keyboard-audit-output": 2,
    "--restart-recovery-audit-output": 2,
    "--restart-recovery-stage-child": 3,
    "--restart-recovery-recover-child": 3,
    "--research-demo-audit-output": 3,
}

_STARTUP_ERROR_TITLE = "Автоспорт — помилка запуску"
_MB_OK = 0x00000000
_MB_ICONERROR = 0x00000010
_MB_SETFOREGROUND = 0x00010000


def _startup_error_message(error: Exception) -> str:
    detail = str(error).strip() or type(error).__name__
    return (
        "Автоспорт не вдалося запустити.\n\n"
        f"{detail}\n\n"
        "Перевірте конфігурацію або стан запуску та повторіть спробу."
    )


def _present_interactive_startup_error(error: Exception) -> None:
    """Best-effort accessible feedback for a windowed interactive startup failure."""

    message = _startup_error_message(error)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                message,
                _STARTUP_ERROR_TITLE,
                _MB_OK | _MB_ICONERROR | _MB_SETFOREGROUND,
            )
            return
        except Exception:
            # Presentation failure must not replace the original startup failure.
            # stderr remains useful for source/dev launches even though the shipped
            # --windowed executable normally has no attached console.
            pass
    try:
        print(message, file=sys.stderr)
    except Exception:
        # Startup is already failing. Never let diagnostic presentation turn the
        # original product failure into a second unhandled exception.
        pass


def _install_compact_layout() -> None:
    # Import lazily so malformed packaged arguments can fail before GUI/layout import.
    from autosport.windows_layout import install_compact_windows_layout

    install_compact_windows_layout()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # Packaged machine/audit invocations must fail closed. Previously any
    # unknown flag silently fell through into the interactive GUI, which could
    # turn a typo in CI/release automation into a hung or misleading run.
    if args:
        expected_arity = _MACHINE_MODE_ARITY.get(args[0])
        if expected_arity is None or len(args) != expected_arity:
            return 2

    # The shipped executable is windowed, so an uncaught interactive startup
    # exception can otherwise terminate without deterministic user feedback.
    # Keep this boundary strictly on the no-argument interactive path: machine
    # and audit modes retain their existing exception/exit semantics.
    if not args:
        try:
            _install_compact_layout()
            from autosport.windows_gui import main as gui_main

            return gui_main()
        except Exception as error:
            _present_interactive_startup_error(error)
            return 1

    # Valid packaged machine/audit invocations inspect the same compact layout
    # users receive. Their failures remain visible to automation and are not
    # converted into an interactive message-box result.
    _install_compact_layout()
    if args[0] == "--diagnostic-output":
        from autosport.diagnostic import run_machine_diagnostic

        return run_machine_diagnostic(args[1])
    if args[0] == "--accessibility-audit-output":
        from autosport.accessibility_audit import run_accessibility_audit

        return run_accessibility_audit(args[1])
    if args[0] == "--keyboard-audit-output":
        from autosport.keyboard_audit import run_keyboard_audit

        return run_keyboard_audit(args[1])
    if args[0] == "--restart-recovery-audit-output":
        from autosport.process_recovery_audit import run_packaged_restart_recovery_audit

        return run_packaged_restart_recovery_audit(args[1])
    if args[0] == "--restart-recovery-stage-child":
        from autosport.process_recovery_audit import run_process_kill_stage_child

        return run_process_kill_stage_child(args[1], args[2])
    if args[0] == "--restart-recovery-recover-child":
        from autosport.process_recovery_audit import run_process_kill_recovery_child

        return run_process_kill_recovery_child(args[1], args[2])
    if args[0] == "--research-demo-audit-output":
        from autosport.research_demo_audit import run_research_demo_audit

        return run_research_demo_audit(args[1], args[2])
    raise AssertionError("validated packaged mode was not dispatched")


if __name__ == "__main__":
    raise SystemExit(main())
