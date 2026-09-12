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
    from autosport.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
