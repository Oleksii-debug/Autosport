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
    _localized_surface("settings", "active", "owner_economic_authority_button"),
    _localized_surface("manual_calculation", "active", "manual_calculation_button"),
    _localized_surface("diagnostics_recovery", "active", "repair_button"),
    _localized_surface("help_about", "active", "shell_open_button"),
)

DEFAULT_SURFACE_KEY: Final[str] = SURFACES[0].key
SURFACE_BY_KEY: Final[dict[str, WindowsSurfaceSpec]] = {surface.key: surface for surface in SURFACES}
SHELL_STATE_FILENAME: Final[str] = "windows-shell-state-v1.json"
_MAX_SHELL_STATE_BYTES: Final[int] = 4096


def shell_state_path(workspace: str | Path) -> Path:
    return Path(workspace) / SHELL_STATE_FILENAME


def load_surface_selection(workspace: str | Path) -> str:
    """Read presentation-only shell state; corruption fails soft to Home."""
    path = shell_state_path(workspace)
    try:
        # This untrusted, persisted UI preference never needs a large document.
        # Read only a fixed prefix so a corrupted state file cannot exhaust
        # memory before the JSON parser or schema check is reached.
        with path.open("rb") as stream:
            payload = stream.read(_MAX_SHELL_STATE_BYTES + 1)
        if len(payload) > _MAX_SHELL_STATE_BYTES:
            return DEFAULT_SURFACE_KEY
        raw = strict_json_loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError):
        return DEFAULT_SURFACE_KEY
    # The stored schema is versioned. Boolean 1 and future versions must not
    # silently restore an unrelated surface after a format change.
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        return DEFAULT_SURFACE_KEY
    key = raw.get("surface_key")
    return key if type(key) is str and key in SURFACE_BY_KEY else DEFAULT_SURFACE_KEY


def save_surface_selection(workspace: str | Path, surface_key: str) -> bool:
    """Atomically persist only UI navigation state; never raise into domain flow."""
    # Input may be passed through an untrusted bridge before runtime type checks.
    if type(surface_key) is not str or surface_key not in SURFACE_BY_KEY:
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
