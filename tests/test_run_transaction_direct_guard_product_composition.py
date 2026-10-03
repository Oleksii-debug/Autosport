from __future__ import annotations

import subprocess
import sys


def test_normal_product_import_installs_run_transaction_direct_dispatch_guard() -> None:
    """Production composition must not depend on a focused test importing the guard."""

    script = r'''
import autosport
import autosport.run_transaction as run_transaction

stage = run_transaction.RunTransaction._stage_paper_book_snapshot
promotion = run_transaction.RunTransaction._promote_paper_book_snapshot
for label, function in (("stage", stage), ("promotion", promotion)):
    freevars = set(function.__code__.co_freevars)
    missing = {"frozen_globals_items", "require_bindings"} - freevars
    if missing:
        raise SystemExit(
            f"{label} missing production direct-dispatch guard composition: {sorted(missing)}"
        )
'''

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
