from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from autosport.windows_entry import _workspace_access_error_message


def test_workspace_access_error_redacts_sensitive_environment_value(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "дані Autosport з пробілами"
    secret = "autosport-test-secret-9f0f3a"
    failure = OSError(f"permission denied while opening provider token={secret}")

    with patch.dict(
        os.environ,
        {"AUTOSPORT_PARLAYAPI_KEY": secret},
        clear=False,
    ):
        rendered = _workspace_access_error_message(workspace, failure)

    assert secret not in rendered
    assert str(workspace) in rendered
    assert "OSError" in rendered


def test_workspace_access_error_preserves_non_secret_diagnostic_context(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "Autosport workspace"
    rendered = _workspace_access_error_message(
        workspace,
        PermissionError("access denied by filesystem policy"),
    )

    assert str(workspace) in rendered
    assert "PermissionError" in rendered
    assert "access denied by filesystem policy" in rendered
