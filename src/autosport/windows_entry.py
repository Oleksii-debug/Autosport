from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--diagnostic-output":
        if len(args) != 2:
            return 2
        from autosport.diagnostic import run_machine_diagnostic

        return run_machine_diagnostic(args[1])
    if args and args[0] == "--accessibility-audit-output":
        if len(args) != 2:
            return 2
        from autosport.accessibility_audit import run_accessibility_audit

        return run_accessibility_audit(args[1])
    if args and args[0] == "--keyboard-audit-output":
        if len(args) != 2:
            return 2
        from autosport.keyboard_audit import run_keyboard_audit

        return run_keyboard_audit(args[1])
    if args and args[0] == "--restart-recovery-audit-output":
        if len(args) != 2:
            return 2
        from autosport.restart_recovery_audit import run_restart_recovery_audit

        return run_restart_recovery_audit(args[1])
    if args and args[0] == "--product-journey-audit-output":
        if (
            len(args) != 6
            or args[2] != "--dataset-root"
            or args[4] != "--research-plan"
        ):
            return 2
        from autosport.product_journey_audit import run_product_journey_audit

        return run_product_journey_audit(args[1], args[3], args[5])
    from autosport.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
