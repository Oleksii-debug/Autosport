"""Plan 6 section 6B: packaged operator reaches canonical read-only signed update diagnostics.

The operator command must not introduce a second signature verifier, an installer
capability, or an implicit success when the pinned request fails validation.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from unittest.mock import patch

import autosport.data_tools_entry as entry


def test_windows_data_tool_help_exposes_signed_diagnostic_without_install_claim() -> None:
    output = StringIO()
    with redirect_stdout(output):
        assert entry.main(["--help"]) == 0
    text = output.getvalue()
    assert "diagnose-update <local-request.json>" in text
    assert "signed offline Windows update preflight" in text
    assert "never installs" in text
    assert "install-release" in text  # Existing canonical lifecycle preserved.


def test_packaged_entry_delegates_both_backend_results_without_changing_argv() -> None:
    request = "C:/Operator/Update Request.json"
    for exit_code in (0, 2, 3):
        with patch("autosport.windows_update_cli.main", return_value=exit_code) as signed:
            assert entry.main(["diagnose-update", request]) == exit_code
            signed.assert_called_once_with([request])


def test_invalid_local_request_fails_closed_without_installation(tmp_path: Path) -> None:
    request = tmp_path / "operator-update-request.json"
    request.write_text('{"unknown":"request"}\n', encoding="utf-8")
    original_bytes = request.read_bytes()
    output = StringIO()
    with redirect_stdout(output):
        code = entry.main(["diagnose-update", str(request)])
    assert code == 2
    report = json.loads(output.getvalue())
    assert report == {
        "status": "DIAGNOSTIC_REJECTED",
        "install_authorized": False,
        "independent_trust_root_verified": False,
        "human_tested": False,
        "nvda_verified": False,
    }
    assert request.read_bytes() == original_bytes
    assert sorted(path.name for path in tmp_path.iterdir()) == [request.name]


def test_windows_build_checks_both_packaged_and_freshly_extracted_diagnostic() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )
    for prefix, label in (
        ("$dataExe", "Packaged"),
        ("$extractedDataExe", "Fresh-extracted"),
    ):
        assert f"& {prefix} diagnose-update --help | Out-Null" in script
        assert f'{label} Autosport-Data.exe diagnose-update --help exited' in script
