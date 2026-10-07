from __future__ import annotations

import json
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="requires the Windows Tk/UIA runtime")
def test_windows_shell_accessibility_runtime_matches_packaged_audit(tmp_path) -> None:
    """Keep the packaged shell accessibility failure diagnosable in normal CI.

    The release executable installs the Windows layout wrapper before invoking
    ``run_accessibility_audit``. Reproduce that exact ordering here so a failed
    shell control reports its structured audit failures through pytest instead
    of being reduced to a generic packaged-process exit code.
    """

    from autosport.accessibility_audit import run_accessibility_audit
    from autosport.windows_layout import install_compact_windows_layout

    install_compact_windows_layout()
    report_path = tmp_path / "accessibility-audit.json"
    exit_code = run_accessibility_audit(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert exit_code == 0, report
    assert report["status"] == "PASS", report
    assert report["human_tested"] is False
    assert report["nvda_verified"] is False
    assert report["real_money_execution"] is False
