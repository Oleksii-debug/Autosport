from __future__ import annotations

import os
from pathlib import Path


def default_workspace() -> Path:
    override = os.environ.get("AUTOSPORT_WORKSPACE")
    if override is not None and override.strip():
        override_path = Path(override).expanduser()
        if not override_path.is_absolute():
            raise ValueError(
                "AUTOSPORT_WORKSPACE must be an absolute path so durable workspace identity "
                "does not depend on the process working directory"
            )
        return override_path
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Autosport" / "workspace"
    return Path.home() / ".autosport" / "workspace"
