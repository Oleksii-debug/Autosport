from __future__ import annotations

import sys
import types
from pathlib import Path

import autosport.paths as paths
import autosport.product_workspace_initialization as workspace_initialization
import autosport.windows_entry as windows_entry


def test_packaged_startup_establishes_workspace_identity_before_gui(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "дані Autosport з пробілами").resolve()
    events: list[tuple[str, Path]] = []

    monkeypatch.setattr(paths, "default_workspace", lambda: workspace)
    monkeypatch.setattr(
        windows_entry,
        "_probe_workspace_writable",
        lambda observed: events.append(("probe", observed)),
    )

    def initialize(observed: Path):
        events.append(("identity", observed))
        return object()

    # Support either a function-local import or a module-level import in the
    # eventual production composition without prescribing its source layout.
    monkeypatch.setattr(
        workspace_initialization,
        "initialize_product_workspace",
        initialize,
    )
    monkeypatch.setattr(
        windows_entry,
        "initialize_product_workspace",
        initialize,
        raising=False,
    )

    fake_gui = types.ModuleType("autosport.windows_gui")

    def gui_main() -> int:
        events.append(("gui", workspace))
        return 0

    fake_gui.main = gui_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "autosport.windows_gui", fake_gui)
    monkeypatch.setattr(windows_entry, "gui_main", gui_main, raising=False)

    assert windows_entry._run_interactive_gui() == 0
    assert events == [
        ("probe", workspace),
        ("identity", workspace),
        ("gui", workspace),
    ]
