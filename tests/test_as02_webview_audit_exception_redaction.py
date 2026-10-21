"""Plan 6: audit evidence may never publish raw exception strings or paths.

Static WebView2 source checks are machine evidence, not physical NVDA evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from autosport import windows_webview_audit as audit


SECRET = "AS02_PRIVATE_EXCEPTION_CANARY_20261021"


def _assert_redacted(report, expected):
    assert report["status"] == "FAIL"
    assert expected in report["failures"]
    serialized = json.dumps(report, ensure_ascii=False)
    assert SECRET not in serialized
    assert "real_money_execution" in report and report["real_money_execution"] is False
    assert "nvda_verified" in report and report["nvda_verified"] is False
    assert "human_tested" in report and report["human_tested"] is False


def test_unreadable_semantic_html_and_keyboard_assets_do_not_echo_os_errors():
    with patch.object(Path, "read_text", side_effect=PermissionError(SECRET)):
        semantic = audit.inspect_semantic_shell()
        keyboard = audit.inspect_keyboard_contract()
    _assert_redacted(semantic, "semantic shell HTML unreadable")
    _assert_redacted(keyboard, "semantic keyboard assets unreadable")


def test_missing_javascript_does_not_echo_private_exception():
    original = Path.read_text

    def guarded_read(path, *args, **kwargs):
        if path.name == "app.js":
            raise PermissionError(SECRET)
        return original(path, *args, **kwargs)

    with patch.object(Path, "read_text", guarded_read):
        semantic = audit.inspect_semantic_shell()
        keyboard = audit.inspect_keyboard_contract()
    _assert_redacted(semantic, "semantic shell JavaScript unreadable")
    _assert_redacted(keyboard, "semantic keyboard assets unreadable")


def test_emergency_stop_script_failure_does_not_echo_private_exception():
    original = Path.read_text

    def guarded_read(path, *args, **kwargs):
        if path.name == "emergency_stop.js":
            raise OSError(SECRET)
        return original(path, *args, **kwargs)

    with patch.object(Path, "read_text", guarded_read):
        keyboard = audit.inspect_keyboard_contract()
    _assert_redacted(keyboard, "emergency STOP keyboard asset unreadable")


def test_html_parser_failure_does_not_echo_private_exception():
    with patch.object(audit._SemanticShellParser, "feed", side_effect=ValueError(SECRET)):
        semantic = audit.inspect_semantic_shell()
    _assert_redacted(semantic, "semantic shell HTML parse failed")
