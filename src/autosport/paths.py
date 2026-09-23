from __future__ import annotations

import os
from pathlib import Path


def default_workspace() -> Path:
    override = os.environ.get("AUTOSPORT_WORKSPACE")
    if override is not None and override.strip():
        try:
            override_path = Path(override).expanduser()
        except RuntimeError as exc:
            raise ValueError(
                "AUTOSPORT_WORKSPACE home expansion could not be resolved; "
                "configure an absolute workspace path"
            ) from exc
        if not override_path.is_absolute():
            raise ValueError(
                "AUTOSPORT_WORKSPACE must be an absolute path so durable workspace identity "
                "does not depend on the process working directory"
            )
        return override_path
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        local_app_data_path = Path(local_app_data)
        if not local_app_data_path.is_absolute():
            raise ValueError(
                "LOCALAPPDATA must be an absolute path so durable workspace identity "
                "does not depend on the process working directory"
            )
        return local_app_data_path / "Autosport" / "workspace"
    try:
        home = Path.home()
    except RuntimeError as exc:
        raise ValueError(
            "home directory could not be resolved; configure an absolute AUTOSPORT_WORKSPACE"
        ) from exc
    if not home.is_absolute():
        raise ValueError(
            "home directory must be an absolute path so durable workspace identity "
            "does not depend on the process working directory"
        )
    return home / ".autosport" / "workspace"
