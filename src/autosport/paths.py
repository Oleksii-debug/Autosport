from __future__ import annotations

import os
from enum import Enum
from pathlib import Path


class WorkspaceConfigurationReason(str, Enum):
    """Stable reasons for invalid durable workspace configuration."""

    AUTOSPORT_WORKSPACE_HOME_EXPANSION_FAILED = "autosport-workspace-home-expansion-failed"
    AUTOSPORT_WORKSPACE_NOT_ABSOLUTE = "autosport-workspace-not-absolute"
    LOCALAPPDATA_NOT_ABSOLUTE = "localappdata-not-absolute"
    HOME_RESOLUTION_FAILED = "home-resolution-failed"
    HOME_NOT_ABSOLUTE = "home-not-absolute"
    RESOLVED_WORKSPACE_NOT_ABSOLUTE = "resolved-workspace-not-absolute"


class WorkspaceConfigurationError(ValueError):
    """A machine-readable workspace configuration error with legacy text intact."""

    def __init__(self, reason: WorkspaceConfigurationReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def default_workspace() -> Path:
    override = os.environ.get("AUTOSPORT_WORKSPACE")
    if override is not None:
        try:
            override_path = Path(override).expanduser()
        except RuntimeError as exc:
            raise WorkspaceConfigurationError(
                WorkspaceConfigurationReason.AUTOSPORT_WORKSPACE_HOME_EXPANSION_FAILED,
                "AUTOSPORT_WORKSPACE home expansion could not be resolved; "
                "configure an absolute workspace path",
            ) from exc
        if not override_path.is_absolute():
            raise WorkspaceConfigurationError(
                WorkspaceConfigurationReason.AUTOSPORT_WORKSPACE_NOT_ABSOLUTE,
                "AUTOSPORT_WORKSPACE must be an absolute path so durable workspace identity "
                "does not depend on the process working directory",
            )
        return override_path
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        local_app_data_path = Path(local_app_data)
        if not local_app_data_path.is_absolute():
            raise WorkspaceConfigurationError(
                WorkspaceConfigurationReason.LOCALAPPDATA_NOT_ABSOLUTE,
                "LOCALAPPDATA must be an absolute path so durable workspace identity "
                "does not depend on the process working directory",
            )
        return local_app_data_path / "Autosport" / "workspace"
    try:
        home = Path.home()
    except RuntimeError as exc:
        raise WorkspaceConfigurationError(
            WorkspaceConfigurationReason.HOME_RESOLUTION_FAILED,
            "home directory could not be resolved; configure an absolute AUTOSPORT_WORKSPACE",
        ) from exc
    if not home.is_absolute():
        raise WorkspaceConfigurationError(
            WorkspaceConfigurationReason.HOME_NOT_ABSOLUTE,
            "home directory must be an absolute path so durable workspace identity "
            "does not depend on the process working directory",
        )
    return home / ".autosport" / "workspace"
