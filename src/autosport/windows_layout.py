from __future__ import annotations

from typing import Any

# Keep every critical surface mapped inside the canonical 1080x860 Windows
# window. Listboxes remain scrollable, so reducing visible rows does not remove
# content or keyboard access.
_SURFACE_HEIGHTS = {
    "live_quotes": 4,
    "tickets": 5,
    "evaluation": 4,
    "log": 5,
}


def compact_surface_heights(app: Any) -> None:
    """Apply the Windows V1 vertical budget without weakening UIA gates."""
    for name, height in _SURFACE_HEIGHTS.items():
        getattr(app, name).configure(height=height)


def install_compact_windows_layout() -> None:
    """Install the compact build wrapper before any packaged AutosportApp exists.

    The packaged entrypoint calls this before normal GUI startup and before the
    accessibility/keyboard audit entrypoints. The same layout is therefore
    audited that Windows users actually receive; this is not an audit-only
    resize.
    """
    from .gui import AutosportApp

    if getattr(AutosportApp, "_compact_windows_layout_installed", False):
        return

    original_build = AutosportApp._build

    def build_with_windows_budget(self) -> None:
        original_build(self)
        compact_surface_heights(self)

    AutosportApp._build = build_with_windows_budget
    AutosportApp._compact_windows_layout_installed = True
