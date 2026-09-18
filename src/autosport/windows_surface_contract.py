from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .localization import text

SurfacePhase = Literal["active", "visible-disabled", "presentation-only"]


@dataclass(frozen=True, slots=True)
class WindowsSurfaceSpec:
    key: str
    title_uk: str
    purpose_uk: str
    primary_task_uk: str
    controls_uk: str
    focus_entry_uk: str
    focus_exit_uk: str
    accessibility_uk: str
    transient_states_uk: str
    confirmation_uk: str
    authority_uk: str
    persistence_uk: str
    phase: SurfacePhase
    target_widget: str | None
    blocked_reason_uk: str | None = None


def _surface_message(surface_key: str, field: str) -> str:
    return text(f"ui.windows.surface.{surface_key}.{field}")


def _localized_surface(
    key: str,
    phase: SurfacePhase,
    target_widget: str | None,
    *,
    blocked: bool = False,
) -> WindowsSurfaceSpec:
    return WindowsSurfaceSpec(
        key=key,
        title_uk=_surface_message(key, "title"),
        purpose_uk=_surface_message(key, "purpose"),
        primary_task_uk=_surface_message(key, "primary_task"),
        controls_uk=_surface_message(key, "controls"),
        focus_entry_uk=_surface_message(key, "focus_entry"),
        focus_exit_uk=_surface_message(key, "focus_exit"),
        accessibility_uk=_surface_message(key, "accessibility"),
        transient_states_uk=_surface_message(key, "states"),
        confirmation_uk=_surface_message(key, "confirmation"),
        authority_uk=_surface_message(key, "authority"),
        persistence_uk=_surface_message(key, "persistence"),
        phase=phase,
        target_widget=target_widget,
        blocked_reason_uk=_surface_message(key, "blocked_reason") if blocked else None,
    )


# The shape remains language-neutral here. All product-owned presentation content
# for these surfaces is resolved through the central localization API above.
SURFACES: Final[tuple[WindowsSurfaceSpec, ...]] = (
    _localized_surface("home_dashboard", "active", "strategy"),
    _localized_surface("market_mirror", "active", "live_mode"),
    _localized_surface("research_agents", "active", "strategy"),
    _localized_surface("opportunities", "visible-disabled", None, blocked=True),
    _localized_surface("portfolio", "active", "evaluation"),
    _localized_surface("paper_bank", "active", "bank_summary"),
    _localized_surface("tickets_positions", "active", "tickets"),
    _localized_surface("evaluation_learning", "active", "evaluation"),
    _localized_surface("bookmakers_accounts", "visible-disabled", None, blocked=True),
    _localized_surface("history_results", "active", "log"),
    _localized_surface("settings", "visible-disabled", None, blocked=True),
    _localized_surface("diagnostics_recovery", "active", "repair_button"),
    _localized_surface("help_about", "presentation-only", None),
)

DEFAULT_SURFACE_KEY: Final[str] = SURFACES[0].key
SURFACE_BY_KEY: Final[dict[str, WindowsSurfaceSpec]] = {surface.key: surface for surface in SURFACES}
SHELL_STATE_FILENAME: Final[str] = "windows-shell-state-v1.json"


def shell_state_path(workspace: str | Path) -> Path:
    return Path(workspace) / SHELL_STATE_FILENAME


def load_surface_selection(workspace: str | Path) -> str:
    """Read presentation-only shell state; corruption fails soft to Home."""
    path = shell_state_path(workspace)
    try:
        raw = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError):
        return DEFAULT_SURFACE_KEY
    key = raw.get("surface_key") if isinstance(raw, dict) else None
    return key if isinstance(key, str) and key in SURFACE_BY_KEY else DEFAULT_SURFACE_KEY


def save_surface_selection(workspace: str | Path, surface_key: str) -> bool:
    """Atomically persist only UI navigation state; never raise into domain flow."""
    if surface_key not in SURFACE_BY_KEY:
        return False
    path = shell_state_path(workspace)
    try:
        atomic_write_json(path, {"version": 1, "surface_key": surface_key})
    except (OSError, UnicodeError, TypeError, ValueError):
        return False
    return True


def surface_detail_lines(surface: WindowsSurfaceSpec) -> tuple[str, ...]:
    phase = {
        "active": text("ui.windows.surface.phase.active"),
        "visible-disabled": text("ui.windows.surface.phase.disabled"),
        "presentation-only": text("ui.windows.surface.phase.presentation"),
    }[surface.phase]
    lines = (
        phase,
        f"{text('ui.windows.surface.detail.purpose')}: {surface.purpose_uk}",
        f"{text('ui.windows.surface.detail.primary_task')}: {surface.primary_task_uk}",
        f"{text('ui.windows.surface.detail.controls')}: {surface.controls_uk}",
        f"{text('ui.windows.surface.detail.focus_entry')}: {surface.focus_entry_uk}",
        f"{text('ui.windows.surface.detail.focus_exit')}: {surface.focus_exit_uk}",
        f"{text('ui.windows.surface.detail.accessibility')}: {surface.accessibility_uk}",
        f"{text('ui.windows.surface.detail.states')}: {surface.transient_states_uk}",
        f"{text('ui.windows.surface.detail.confirmation')}: {surface.confirmation_uk}",
        f"{text('ui.windows.surface.detail.truth_boundary')}: {surface.authority_uk}",
        f"{text('ui.windows.surface.detail.restart')}: {surface.persistence_uk}",
    )
    if surface.blocked_reason_uk:
        return lines + (
            f"{text('ui.windows.surface.detail.blocked_reason')}: {surface.blocked_reason_uk}",
        )
    return lines
