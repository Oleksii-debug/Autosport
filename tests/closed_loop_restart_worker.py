"""Two-process harness for closed-loop restart acceptance.

This file is a test-only launcher. Each invocation owns exactly one phase and exits;
the pytest parent invokes phase1 and phase2 as distinct Python interpreter processes.
"""

from __future__ import annotations

from pathlib import Path
import sys

from test_closed_loop_acceptance import _phase_one, _phase_two


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: closed_loop_restart_worker.py <phase1|phase2> <workspace>")
    phase = sys.argv[1]
    workspace = Path(sys.argv[2])
    if phase == "phase1":
        _phase_one(workspace)
        return 0
    if phase == "phase2":
        _phase_two(workspace)
        return 0
    raise SystemExit(f"unknown phase: {phase}")


if __name__ == "__main__":
    raise SystemExit(main())
