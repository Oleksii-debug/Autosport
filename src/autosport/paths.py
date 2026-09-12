from __future__ import annotations

import os
from pathlib import Path


def default_workspace() -> Path:
    override = os.environ.get("AUTOSPORT_WORKSPACE")
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Autosport" / "workspace"
    return Path.home() / ".autosport" / "workspace"
