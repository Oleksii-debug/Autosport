from __future__ import annotations

import sys


_MACHINE_MODE_ARITY = {
    "--diagnostic-output": 2,
    "--accessibility-audit-output": 2,
    "--keyboard-audit-output": 2,
    "--restart-recovery-audit-output": 2,
    "--process-recovery-audit-output": 2,
    "--process-recovery-crash-worker": 3,
    "--process-recovery-recover-worker": 3,
    "--research-demo-audit-output": 3,
}


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
        from autosport.restart_recovery_audit import run_restart_recovery_audit

        return run_restart_recovery_audit(args[1])
    if args and args[0] == "--process-recovery-audit-output":
        from autosport.process_recovery_audit import run_process_recovery_audit

        return run_process_recovery_audit(args[1])
    if args and args[0] == "--process-recovery-crash-worker":
        from autosport.process_recovery_audit import run_process_recovery_crash_worker

        return run_process_recovery_crash_worker(args[1], args[2])
    if args and args[0] == "--process-recovery-recover-worker":
        from autosport.process_recovery_audit import run_process_recovery_recover_worker

        return run_process_recovery_recover_worker(args[1], args[2])
    if args and args[0] == "--research-demo-audit-output":
        from autosport.research_demo_audit import run_research_demo_audit

        return run_research_demo_audit(args[1], args[2])
    from autosport.windows_gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
