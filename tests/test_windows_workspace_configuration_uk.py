from __future__ import annotations

import pytest

from autosport.paths import (
    WorkspaceConfigurationError,
    WorkspaceConfigurationReason,
    default_workspace,
)
from autosport.windows_entry import (
    _workspace_configuration_error_detail,
    _workspace_configuration_error_message,
)


def test_relative_workspace_keeps_legacy_value_error_text_and_adds_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_WORKSPACE", "relative-workspace")

    with pytest.raises(WorkspaceConfigurationError) as exc_info:
        default_workspace()

    error = exc_info.value
    assert isinstance(error, ValueError)
    assert error.reason is WorkspaceConfigurationReason.AUTOSPORT_WORKSPACE_NOT_ABSOLUTE
    assert "AUTOSPORT_WORKSPACE must be an absolute path" in str(error)


def test_relative_localappdata_has_structured_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTOSPORT_WORKSPACE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", "relative-localappdata")

    with pytest.raises(WorkspaceConfigurationError) as exc_info:
        default_workspace()

    assert exc_info.value.reason is WorkspaceConfigurationReason.LOCALAPPDATA_NOT_ABSOLUTE
    assert "LOCALAPPDATA must be an absolute path" in str(exc_info.value)


@pytest.mark.parametrize(
    ("reason", "expected_detail"),
    [
        (
            WorkspaceConfigurationReason.AUTOSPORT_WORKSPACE_HOME_EXPANSION_FAILED,
            "Не вдалося розгорнути домашню теку в AUTOSPORT_WORKSPACE. Вкажіть абсолютний шлях.",
        ),
        (
            WorkspaceConfigurationReason.AUTOSPORT_WORKSPACE_NOT_ABSOLUTE,
            "Шлях у AUTOSPORT_WORKSPACE має бути абсолютним.",
        ),
        (
            WorkspaceConfigurationReason.LOCALAPPDATA_NOT_ABSOLUTE,
            "Шлях у LOCALAPPDATA має бути абсолютним.",
        ),
        (
            WorkspaceConfigurationReason.HOME_RESOLUTION_FAILED,
            "Не вдалося визначити домашню теку. Вкажіть абсолютний шлях у AUTOSPORT_WORKSPACE.",
        ),
        (
            WorkspaceConfigurationReason.HOME_NOT_ABSOLUTE,
            "Шлях до домашньої теки має бути абсолютним.",
        ),
        (
            WorkspaceConfigurationReason.RESOLVED_WORKSPACE_NOT_ABSOLUTE,
            "Визначений шлях до робочої теки має бути абсолютним.",
        ),
    ],
)
def test_windows_detail_is_ukrainian_and_does_not_echo_legacy_english(
    reason: WorkspaceConfigurationReason,
    expected_detail: str,
) -> None:
    error = WorkspaceConfigurationError(reason, "legacy English must not reach MessageBox")

    detail = _workspace_configuration_error_detail(error)

    assert detail == expected_detail
    assert "legacy English" not in detail


def test_unknown_value_error_is_fail_closed_without_echoing_internal_text() -> None:
    detail = _workspace_configuration_error_detail(
        ValueError("unexpected English internal configuration detail")
    )

    assert detail == "Некоректне налаштування шляху до робочої теки."
    assert "unexpected English" not in detail


def test_native_configuration_message_shell_has_no_mixed_english_status_text() -> None:
    message = _workspace_configuration_error_message(
        "Шлях у AUTOSPORT_WORKSPACE має бути абсолютним."
    )

    assert "робочу теку" in message
    assert "Економічний стан і стан виконання не змінено." in message
    assert "interactive workspace" not in message
    assert "Economic" not in message
    assert "live state" not in message
