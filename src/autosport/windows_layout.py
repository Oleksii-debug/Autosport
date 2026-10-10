from __future__ import annotations

import tkinter as tk
from typing import Any

from tkinter import messagebox, ttk

import tk_uia

from .localization import require_keys, text
from .windows_manual_calculation import (
    WORKBENCH_AUTOMATION_IDS,
    show_manual_calculation_workbench,
)
from .owner_economic_authority import (
    INITIAL_OWNER_FORM_DEFAULTS,
    OWNER_ECONOMIC_FORM_FIELDS,
    OwnerEconomicAuthorityError,
    OwnerEconomicReviewSnapshot,
    OwnerEconomicAuthorityService,
)
from .replay_worker import workspace_for_strategy
from .research_strategy import RESEARCH_STRATEGY_ID
from .windows_surface_contract import (
    SURFACE_BY_KEY,
    SURFACES,
    load_surface_selection,
    save_surface_selection,
    surface_detail_lines,
)

# Keep every critical surface mapped inside the canonical 1080x860 Windows
# window. Scrolling surfaces retain their full content, while the Windows-only
# wrapper removes excess vertical chrome so the final execution log remains
# mapped after the product shell is inserted.
_SURFACE_HEIGHTS = {
    # #579 added the compact owner-authority panel and #585 added a dedicated
    # readonly UIA log mirror. A Tk packer can keep later managed children
    # logically packed but physically unmapped when the vertical cavity is
    # exhausted; tk-uia reports that exact state as UNMAPPED_SINCE_ANNOTATED.
    # Reclaim one additional row from each scrollable summary surface so the
    # log mirror and two-row execution history both stay mapped at 1080x860.
    "live_quotes": 1,
    "tickets": 2,
    "evaluation": 1,
    "log": 2,
}
_SECTION_LABEL_PADY = (4, 1)
_ROOT_FRAME_PADDING = 8
_LOG_MIRROR_PADY = (0, 2)
WINDOWS_SHELL_DETAILS_VISIBLE_ROWS = 1

WINDOWS_SHELL_AUTOMATION_IDS = {
    "navigation": 301,
    "state": 302,
    "open": 303,
    "details": 304,
    "owner_economic_open": 305,
    "owner_economic_status": 306,
    "owner_economic_readback": 307,
    "owner_economic_dialog_readback": 308,
}

MANUAL_CALCULATION_WORKBENCH_LOCALIZATION_KEYS = frozenset(
    {
        "ui.windows.manual_calculation.frame.title",
        "ui.windows.manual_calculation.button.open",
        "ui.windows.manual_calculation.accessibility.open.name",
        "ui.windows.manual_calculation.accessibility.open.description",
        "ui.windows.manual_calculation.dialog.title",
        "ui.windows.manual_calculation.dialog.description",
        "ui.windows.manual_calculation.operation.label",
        "ui.windows.manual_calculation.input.label",
        "ui.windows.manual_calculation.result.heading",
        "ui.windows.manual_calculation.result.operation",
        "ui.windows.manual_calculation.result.method",
        "ui.windows.manual_calculation.result.classification",
        "ui.windows.manual_calculation.result.classification.exact",
        "ui.windows.manual_calculation.result.classification.approximate_decimal",
        "ui.windows.manual_calculation.result.inputs",
        "ui.windows.manual_calculation.result.outputs",
        "ui.windows.manual_calculation.result.unit",
        "ui.windows.manual_calculation.result.assumptions",
        "ui.windows.manual_calculation.result.warnings",
        "ui.windows.manual_calculation.result.none",
        "ui.windows.manual_calculation.result.input_hash",
        "ui.windows.manual_calculation.result.result_hash",
        "ui.windows.manual_calculation.result.evidence_hash",
        "ui.windows.manual_calculation.result.service_version",
        "ui.windows.manual_calculation.result.input_mode",
        "ui.windows.manual_calculation.result.real_money_execution",
        "ui.windows.manual_calculation.result.real_money_false",
        "ui.windows.manual_calculation.result.canonical_json",
        "ui.windows.manual_calculation.error.unlocalized_evidence",
        "ui.windows.manual_calculation.message.decimal_odds_supplied",
        "ui.windows.manual_calculation.message.american_unrounded",
        "ui.windows.manual_calculation.message.american_decimal_context",
        "ui.windows.manual_calculation.message.american_display_rounding",
        "ui.windows.manual_calculation.message.division_rounding",
        "ui.windows.manual_calculation.message.one_market",
        "ui.windows.manual_calculation.message.devig_model",
        "ui.windows.manual_calculation.message.probability_caller",
        "ui.windows.manual_calculation.message.paper_only",
        "ui.windows.manual_calculation.message.probability_research",
        "ui.windows.manual_calculation.message.kelly_single_position",
        "ui.windows.manual_calculation.message.paper_research",
        "ui.windows.manual_calculation.message.kelly_rounding",
        "ui.windows.manual_calculation.message.balances_chronological",
        "ui.windows.manual_calculation.message.drawdown_rounding",
        "ui.windows.manual_calculation.calculate",
        "ui.windows.manual_calculation.clear",
        "ui.windows.manual_calculation.close",
        "ui.windows.manual_calculation.status.ready",
        "ui.windows.manual_calculation.status.success",
        "ui.windows.manual_calculation.status.error",
        "ui.windows.manual_calculation.status.cleared",
        "ui.windows.manual_calculation.error.nonempty",
        "ui.windows.manual_calculation.uia.input.name",
        "ui.windows.manual_calculation.uia.input.description",
    }
)
require_keys(MANUAL_CALCULATION_WORKBENCH_LOCALIZATION_KEYS)

OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS = {
    "readback": 308,
    "goal_id": 309,
    "bankroll_id": 310,
    "currency": 311,
    "max_stake_fraction": 312,
    "max_stake_amount": 313,
    "max_session_loss_fraction": 314,
    "max_day_loss_fraction": 315,
    "max_drawdown_fraction": 316,
    "max_capital_at_risk_fraction": 317,
    "max_risk_of_ruin": 318,
    "max_quote_age_seconds": 319,
    "minimum_data_quality": 320,
    "max_concurrent_positions": 321,
    "max_parlay_legs": 322,
    "automation_level": 323,
    "blocked_sports": 324,
    "blocked_providers": 325,
    "blocked_markets": 326,
    "emergency_stop": 327,
    "create": 328,
    "close": 329,
}

WINDOWS_SHELL_LOCALIZATION_KEYS = frozenset(
    {
        "ui.windows.shell.frame.title",
        "ui.windows.shell.screen.label",
        "ui.windows.shell.button.open",
        "ui.windows.shell.state.active",
        "ui.windows.shell.state.disabled",
        "ui.windows.shell.state.presentation",
        "ui.windows.shell.accessibility.navigation.name",
        "ui.windows.shell.accessibility.navigation.description",
        "ui.windows.shell.accessibility.state.name",
        "ui.windows.shell.accessibility.state.description",
        "ui.windows.shell.accessibility.open.name",
        "ui.windows.shell.accessibility.open.description",
        "ui.windows.shell.accessibility.details.name",
        "ui.windows.shell.accessibility.details.description",
        "ui.windows.owner_authority.frame.title",
        "ui.windows.owner_authority.button.open",
        "ui.windows.owner_authority.button.create",
        "ui.windows.owner_authority.button.close",
        "ui.windows.owner_authority.dialog.title",
        "ui.windows.owner_authority.dialog.form.title",
        "ui.windows.owner_authority.dialog.review.title",
        "ui.windows.owner_authority.dialog.review.body",
        "ui.windows.owner_authority.button.review",
        "ui.windows.owner_authority.button.confirm",
        "ui.windows.owner_authority.accessibility.confirm.description",
        "ui.windows.owner_authority.review.prompt",
        "ui.windows.owner_authority.error.review_stale",
        "ui.windows.owner_authority.accessibility.open.name",
        "ui.windows.owner_authority.accessibility.open.description",
        "ui.windows.owner_authority.accessibility.status.name",
        "ui.windows.owner_authority.accessibility.status.description",
        "ui.windows.owner_authority.accessibility.readback.name",
        "ui.windows.owner_authority.accessibility.readback.description",
        "ui.windows.owner_authority.accessibility.create.name",
        "ui.windows.owner_authority.accessibility.create.description",
        "ui.windows.owner_authority.state.absent",
        "ui.windows.owner_authority.state.valid",
        "ui.windows.owner_authority.state.corrupt",
        "ui.windows.owner_authority.boundary.initial_only",
        "ui.windows.owner_authority.boundary.read_only",
        "ui.windows.owner_authority.boundary.corrupt",
        "ui.windows.owner_authority.error.strategy_blocked",
        "ui.windows.owner_authority.error.busy",
        "ui.windows.owner_authority.error.cancelled",
    }
)
require_keys(WINDOWS_SHELL_LOCALIZATION_KEYS)


def compact_surface_heights(app: Any) -> None:
    """Apply the Windows product vertical budget without weakening UIA gates."""
    for name, height in _SURFACE_HEIGHTS.items():
        getattr(app, name).configure(height=height)
    for name in ("tickets_label", "evaluation_label", "log_label"):
        getattr(app, name).pack_configure(pady=_SECTION_LABEL_PADY)
    frame = next(iter(app.winfo_children()), None)
    if frame is None:
        raise RuntimeError("Autosport root frame is missing")
    # Keep content rows intact and reclaim chrome instead. The dedicated
    # automation_id=202 readonly mirror needs a real mapped Entry rectangle;
    # otherwise tk-uia correctly reports UNMAPPED_SINCE_ANNOTATED.
    frame.configure(padding=_ROOT_FRAME_PADDING)
    app.log_accessible.pack_configure(pady=_LOG_MIRROR_PADY)


def _surface_target_widget(app: Any, surface_key: str) -> Any | None:
    """Return only a target that the current product shell actually exposes."""
    target_name = SURFACE_BY_KEY[surface_key].target_widget
    if target_name is None:
        return None
    return getattr(app, target_name, None)


def refresh_windows_shell_open_availability(app: Any) -> None:
    """Refresh Open authority after late-bound Windows controls are installed."""
    surface_key = app.shell_surface_key.get()
    app.shell_open_button.configure(
        state=("normal" if _surface_target_widget(app, surface_key) is not None else "disabled")
    )


def _focus_surface_target(app: Any, surface_key: str) -> None:
    target = _surface_target_widget(app, surface_key)
    if target is None:
        app.shell_details.focus_set()
        return
    target.focus_set()


def _render_shell_surface(app: Any, surface_key: str, *, persist: bool) -> None:
    surface = SURFACE_BY_KEY[surface_key]
    app.shell_surface_key.set(surface.key)
    app.shell_surface_display.set(surface.title_uk)
    app.shell_surface_state.set(
        {
            "active": text("ui.windows.shell.state.active"),
            "visible-disabled": text(
                "ui.windows.shell.state.disabled", reason=surface.blocked_reason_uk or ""
            ),
            "presentation-only": text("ui.windows.shell.state.presentation"),
        }[surface.phase]
    )
    app.shell_details.delete(0, "end")
    for line in surface_detail_lines(surface):
        app.shell_details.insert("end", line)
    # A declared target is not enough: the packaged app must expose that
    # widget before the generic Open action can truthfully promise reachability.
    # This keeps presentation-only/future surfaces fail-closed instead of
    # silently focusing an unrelated control.
    refresh_windows_shell_open_availability(app)
    if persist:
        save_surface_selection(app.workspace, surface.key)


def _on_shell_selected(app: Any, _event: object | None = None) -> None:
    title = app.shell_surface_display.get()
    surface = next((item for item in SURFACES if item.title_uk == title), SURFACES[0])
    _render_shell_surface(app, surface.key, persist=True)


def _cycle_shell_surface(app: Any, delta: int) -> None:
    current = app.shell_surface_key.get()
    keys = [surface.key for surface in SURFACES]
    try:
        index = keys.index(current)
    except ValueError:
        index = 0
    surface = SURFACE_BY_KEY[keys[(index + delta) % len(keys)]]
    _render_shell_surface(app, surface.key, persist=True)
    app.shell_navigation.focus_set()


def _owner_economic_workspace(app: Any) -> tuple[Any | None, str | None]:
    """Resolve the exact non-baseline economic workspace without writing state."""

    try:
        strategy_id, research_plan = app._selected_replay_configuration()
    except Exception:
        return None, text("ui.windows.owner_authority.error.strategy_blocked")
    # The observe-only control strategy cannot exercise an EconomicGoalContract,
    # while baseline-v1 is explicitly rejected by the session. Restrict initial
    # owner authority to the one currently proven goal-aware paper strategy.
    if strategy_id != RESEARCH_STRATEGY_ID:
        return None, text("ui.windows.owner_authority.error.strategy_blocked")
    return workspace_for_strategy(app.workspace, strategy_id, research_plan), None


def _owner_economic_write_blocker(app: Any, workspace: Any | None = None) -> str | None:
    if bool(app.__dict__.get("_closing")):
        return text("ui.windows.owner_authority.error.busy")
    if bool(getattr(app, "_dataset_busy", False)):
        return text("ui.windows.owner_authority.error.busy")
    for worker_name in ("replay_worker", "live_worker", "recovery_worker"):
        worker = app.__dict__.get(worker_name)
        if worker is not None and bool(getattr(worker, "busy", False)):
            return text("ui.windows.owner_authority.error.busy")
    if workspace is not None:
        # WindowsAutosportApp owns the canonical per-workspace quarantine
        # authority. Call the class method directly so headless Tk fixtures do
        # not fall through tkinter.Misc.__getattr__. The base-GUI set remains a
        # compatibility fence for non-Windows/headless callers.
        recovery_checker = getattr(type(app), "_workspace_requires_recovery", None)
        if callable(recovery_checker):
            try:
                if bool(recovery_checker(app, workspace)):
                    return text("ui.windows.owner_authority.error.busy")
            except Exception:
                return text("ui.windows.owner_authority.error.busy")
        else:
            required = app.__dict__.get("_recovery_required_workspaces", ())
            try:
                if workspace in required:
                    return text("ui.windows.owner_authority.error.busy")
            except Exception:
                return text("ui.windows.owner_authority.error.busy")
    return None


def _owner_economic_service(app: Any) -> tuple[OwnerEconomicAuthorityService | None, str | None]:
    workspace, blocked = _owner_economic_workspace(app)
    if workspace is None:
        return None, blocked
    return OwnerEconomicAuthorityService(workspace), None


def _persist_initial_owner_economic_contract(
    app: Any,
    *,
    bound_workspace: Any,
    service: OwnerEconomicAuthorityService,
    values: dict[str, str],
    emergency_stop: bool,
) -> Any:
    """Revalidate exact workspace/quarantine immediately before durable creation."""

    current_workspace, _ = _owner_economic_workspace(app)
    blocker = _owner_economic_write_blocker(app, bound_workspace)
    if current_workspace != bound_workspace or blocker is not None:
        raise OwnerEconomicAuthorityError(
            blocker or text("ui.windows.owner_authority.error.busy")
        )
    return service.initialize_from_form(
        values,
        emergency_stop=emergency_stop,
        confirmed=True,
    )


def refresh_owner_economic_authority_surface(app: Any) -> None:
    """Refresh the root readback without granting a write or touching persistence."""

    service, blocked = _owner_economic_service(app)
    app.owner_economic_authority_readback.delete(0, "end")
    if service is None:
        app.owner_economic_authority_status.set(blocked or text("ui.windows.owner_authority.state.corrupt"))
        app.owner_economic_authority_readback.insert("end", app.owner_economic_authority_status.get())
        return
    view = service.read_view()
    app.owner_economic_authority_status.set(view.summary_uk)
    for line in view.lines_uk:
        app.owner_economic_authority_readback.insert("end", line)



def _owner_economic_dialog_close_handler(app: Any, dialog: Any) -> Any:
    """Destroy the owner dialog and restore keyboard focus to its canonical F9 opener."""

    def close() -> None:
        dialog.destroy()
        opener = getattr(app, "owner_economic_authority_button", None)
        focus_set = getattr(opener, "focus_set", None)
        if not callable(focus_set):
            return
        after_idle = getattr(app, "after_idle", None)
        if callable(after_idle):
            after_idle(focus_set)
        else:
            # Minimal/headless fixtures may not expose an event-loop scheduler.
            focus_set()

    protocol = getattr(dialog, "protocol", None)
    if callable(protocol):
        protocol("WM_DELETE_WINDOW", close)
    return close

def _show_owner_economic_dialog(app: Any) -> None:
    """Present one keyboard-first owner contract workflow; display never writes."""

    dialog = tk.Toplevel(app)
    dialog.title(text("ui.windows.owner_authority.dialog.title"))
    dialog.transient(app)
    dialog.geometry("1080x760")
    dialog.minsize(820, 620)
    body = ttk.Frame(dialog, padding=12)
    body.pack(fill="both", expand=True)
    readback = tk.Listbox(body, height=18, takefocus=True)
    readback.pack(fill="both", expand=True)
    tk_uia.set_acc_name(readback, text("ui.windows.owner_authority.accessibility.readback.name"))
    tk_uia.set_acc_description(readback, text("ui.windows.owner_authority.accessibility.readback.description"))
    tk_uia.set_automation_id(
        readback,
        OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["readback"],
    )

    close_dialog = _owner_economic_dialog_close_handler(app, dialog)

    def add_close_button(parent: Any) -> Any:
        close_button = ttk.Button(
            parent,
            text=text("ui.windows.owner_authority.button.close"),
            command=close_dialog,
            takefocus=True,
        )
        tk_uia.set_acc_name(close_button, text("ui.windows.owner_authority.button.close"))
        tk_uia.set_automation_id(
            close_button,
            OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["close"],
        )
        close_button.pack(anchor="e", pady=(8, 0))
        return close_button

    bound_workspace, blocked = _owner_economic_workspace(app)
    if bound_workspace is None:
        readback.insert("end", blocked or text("ui.windows.owner_authority.state.corrupt"))
        add_close_button(body)
        readback.focus_set()
        return

    service = OwnerEconomicAuthorityService(bound_workspace)
    view = service.read_view()
    for line in view.lines_uk:
        readback.insert("end", line)
    if not view.can_initialize:
        add_close_button(body)
        readback.focus_set()
        return

    form = ttk.LabelFrame(body, text=text("ui.windows.owner_authority.dialog.form.title"), padding=8)
    form.pack(fill="x", pady=(8, 0))
    form_values: dict[str, tk.StringVar] = {}
    for index, field in enumerate(OWNER_ECONOMIC_FORM_FIELDS):
        row, column = divmod(index, 2)
        column *= 2
        ttk.Label(form, text=text(f"ui.windows.owner_authority.field.{field}")).grid(
            row=row, column=column, sticky="w", padx=(0, 4), pady=2
        )
        variable = tk.StringVar(value=INITIAL_OWNER_FORM_DEFAULTS[field])
        form_values[field] = variable
        entry = ttk.Entry(form, textvariable=variable, width=24, takefocus=True)
        entry.grid(row=row, column=column + 1, sticky="ew", padx=(0, 12), pady=2)
        tk_uia.set_acc_name(entry, text(f"ui.windows.owner_authority.field.{field}"))
        tk_uia.set_automation_id(
            entry,
            OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS[field],
        )

    emergency_stop = tk.BooleanVar(value=False)
    emergency_stop_control = ttk.Checkbutton(
        form,
        text=text("ui.windows.owner_authority.field.emergency_stop"),
        variable=emergency_stop,
        takefocus=True,
    )
    emergency_stop_control.grid(
        row=(len(OWNER_ECONOMIC_FORM_FIELDS) + 1) // 2,
        column=0,
        columnspan=2,
        sticky="w",
        pady=(4, 0),
    )
    tk_uia.set_acc_name(
        emergency_stop_control,
        text("ui.windows.owner_authority.field.emergency_stop"),
    )
    tk_uia.set_automation_id(
        emergency_stop_control,
        OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["emergency_stop"],
    )

    reviewed: OwnerEconomicReviewSnapshot | None = None

    def invalidate_review(*_args: object) -> None:
        nonlocal reviewed
        if reviewed is None:
            return
        reviewed = None
        create_button.configure(text=text("ui.windows.owner_authority.button.review"))
        tk_uia.set_acc_name(create_button, text("ui.windows.owner_authority.button.review"))
        tk_uia.set_acc_description(
            create_button, text("ui.windows.owner_authority.accessibility.create.description")
        )
        readback.delete(0, "end")
        for line in service.read_view().lines_uk:
            readback.insert("end", line)

    def create_initial_contract() -> None:
        nonlocal reviewed
        current_workspace, _ = _owner_economic_workspace(app)
        blocker = _owner_economic_write_blocker(app, bound_workspace)
        if current_workspace != bound_workspace:
            blocker = text("ui.windows.owner_authority.error.busy")
        if blocker is not None:
            # A temporary quarantine or context switch must consume the
            # earlier review even if the owner later returns to this workspace.
            invalidate_review()
            messagebox.showerror(text("ui.windows.owner_authority.dialog.title"), blocker, parent=dialog)
            return
        values = {field: variable.get() for field, variable in form_values.items()}
        try:
            if not service.read_view().can_initialize:
                raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.not_absent"))
            if reviewed is None:
                # Return focus to the selectable list. A modal prompt here would
                # prevent a keyboard/NVDA user from reading the reviewed limits.
                reviewed = OwnerEconomicReviewSnapshot.from_form(
                    values, emergency_stop=emergency_stop.get()
                )
                readback.delete(0, "end")
                readback.insert("end", text("ui.windows.owner_authority.review.prompt"))
                for line in reviewed.lines_uk:
                    readback.insert("end", line)
                create_button.configure(text=text("ui.windows.owner_authority.button.confirm"))
                tk_uia.set_acc_name(create_button, text("ui.windows.owner_authority.button.confirm"))
                tk_uia.set_acc_description(
                    create_button,
                    text("ui.windows.owner_authority.accessibility.confirm.description"),
                )
                readback.selection_set(0)
                readback.activate(0)
                readback.focus_set()
                return
            if not reviewed.still_matches(values, emergency_stop=emergency_stop.get()):
                invalidate_review()
                raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.review_stale"))
            # Re-resolve both workspace identity and recovery quarantine at the
            # irreversible boundary, then persist only the exact reviewed form.
            persisted = _persist_initial_owner_economic_contract(
                app,
                bound_workspace=bound_workspace,
                service=service,
                values=reviewed.form_values(),
                emergency_stop=reviewed.emergency_stop,
            )
        except OwnerEconomicAuthorityError as exc:
            # A concurrent change after review can reject persistence. Never
            # reuse the previous approval once that boundary was crossed.
            invalidate_review()
            messagebox.showerror(text("ui.windows.owner_authority.dialog.title"), str(exc), parent=dialog)
            refresh_owner_economic_authority_surface(app)
            return
        readback.delete(0, "end")
        for line in persisted.lines_uk:
            readback.insert("end", line)
        refresh_owner_economic_authority_surface(app)
        reviewed = None
        create_button.configure(state="disabled")

    button_row = ttk.Frame(body)
    button_row.pack(fill="x", pady=(8, 0))
    initial_write_blocker = _owner_economic_write_blocker(app, bound_workspace)
    create_button = ttk.Button(
        button_row,
        text=text("ui.windows.owner_authority.button.review"),
        command=create_initial_contract,
        takefocus=True,
        state=("disabled" if initial_write_blocker is not None else "normal"),
    )
    create_button.pack(side="left")
    tk_uia.set_acc_name(create_button, text("ui.windows.owner_authority.button.review"))
    tk_uia.set_acc_description(create_button, text("ui.windows.owner_authority.accessibility.create.description"))
    tk_uia.set_automation_id(
        create_button,
        OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["create"],
    )
    close_button = ttk.Button(
        button_row,
        text=text("ui.windows.owner_authority.button.close"),
        command=close_dialog,
        takefocus=True,
    )
    close_button.pack(side="right")
    tk_uia.set_acc_name(close_button, text("ui.windows.owner_authority.button.close"))
    tk_uia.set_automation_id(
        close_button,
        OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["close"],
    )
    for variable in (*form_values.values(), emergency_stop):
        variable.trace_add("write", invalidate_review)
    dialog.bind(
        "<Control-Return>",
        lambda _event: create_button.focus_set() if reviewed is not None else None,
    )
    readback.focus_set()


def install_owner_economic_authority_surface(app: Any, frame: Any) -> None:
    """Install the compact active Settings target in the packaged Windows shell."""

    # Keep the Settings authority surface inside the same proven 1080x860
    # vertical budget as the canonical product shell. The button and readonly
    # status are peers, not a semantic hierarchy, so render them on one row;
    # the exact selectable readback remains a dedicated row below.
    panel = ttk.LabelFrame(frame, text=text("ui.windows.owner_authority.frame.title"), padding=(8, 2))
    first = frame.winfo_children()[0] if frame.winfo_children() else None
    if first is None:
        panel.pack(fill="x", pady=(0, 2))
    else:
        panel.pack(fill="x", pady=(0, 2), before=first)
    app.owner_economic_authority_status = tk.StringVar()
    owner_row = ttk.Frame(panel)
    owner_row.pack(fill="x")
    app.owner_economic_authority_button = ttk.Button(
        owner_row,
        text=text("ui.windows.owner_authority.button.open"),
        command=lambda: _show_owner_economic_dialog(app),
        takefocus=True,
    )
    app.owner_economic_authority_button.pack(side="left", padx=(0, 4))
    app.owner_economic_authority_state = ttk.Entry(
        owner_row,
        textvariable=app.owner_economic_authority_status,
        state="readonly",
        takefocus=True,
    )
    app.owner_economic_authority_state.pack(side="left", fill="x", expand=True)
    app.owner_economic_authority_readback = tk.Listbox(panel, height=1, takefocus=True)
    app.owner_economic_authority_readback.pack(fill="x", pady=(1, 0))
    app.bind("<F9>", lambda _event: app.owner_economic_authority_button.focus_set())
    refresh_owner_economic_authority_surface(app)


def install_manual_calculation_workbench_surface(app: Any, frame: Any) -> None:
    """Install the single active manual-calculation Windows workbench target."""
    panel = ttk.LabelFrame(
        frame,
        text=text("ui.windows.manual_calculation.frame.title"),
        padding=(8, 2),
    )
    first = frame.winfo_children()[0] if frame.winfo_children() else None
    if first is None:
        panel.pack(fill="x", pady=(0, 2))
    else:
        panel.pack(fill="x", pady=(0, 2), before=first)
    app.manual_calculation_button = ttk.Button(
        panel,
        text=text("ui.windows.manual_calculation.button.open"),
        command=lambda: show_manual_calculation_workbench(app),
        takefocus=True,
    )
    app.manual_calculation_button.pack(fill="x")
    app.bind(
        "<F10>",
        lambda _event: app.manual_calculation_button.focus_set(),
    )


def install_windows_product_shell(app: Any) -> None:
    """Install the truthful keyboard-first product navigator in the existing Tk shell."""
    children = app.winfo_children()
    frame = children[0] if children else None
    if frame is None:
        raise RuntimeError("Autosport root frame is missing")

    shell = ttk.LabelFrame(frame, text=text("ui.windows.shell.frame.title"), padding=(8, 4))
    first = frame.winfo_children()[0] if frame.winfo_children() else None
    if first is None:
        shell.pack(fill="x", pady=(0, 4))
    else:
        shell.pack(fill="x", pady=(0, 4), before=first)

    app.shell_surface_key = tk.StringVar()
    app.shell_surface_display = tk.StringVar()
    app.shell_surface_state = tk.StringVar()

    nav_row = ttk.Frame(shell)
    nav_row.pack(fill="x")
    ttk.Label(nav_row, text=text("ui.windows.shell.screen.label")).pack(side="left", padx=(0, 4))
    app.shell_navigation = ttk.Combobox(
        nav_row,
        textvariable=app.shell_surface_display,
        values=[surface.title_uk for surface in SURFACES],
        state="readonly",
        width=38,
        takefocus=True,
    )
    app.shell_navigation.pack(side="left", fill="x", expand=True, padx=(0, 8))
    app.shell_navigation.bind("<<ComboboxSelected>>", lambda event: _on_shell_selected(app, event))

    app.shell_open_button = ttk.Button(
        nav_row,
        text=text("ui.windows.shell.button.open"),
        command=lambda: _focus_surface_target(app, app.shell_surface_key.get()),
        takefocus=True,
    )
    app.shell_open_button.pack(side="left")

    app.shell_state = ttk.Entry(
        shell,
        textvariable=app.shell_surface_state,
        state="readonly",
        takefocus=True,
    )
    app.shell_state.pack(fill="x", pady=(4, 2))

    app.shell_details = tk.Listbox(
        shell,
        height=WINDOWS_SHELL_DETAILS_VISIBLE_ROWS,
        takefocus=True,
    )
    app.shell_details.pack(fill="x")

    app.bind("<F2>", lambda _event: app.shell_navigation.focus_set())
    app.bind("<Control-Alt-Left>", lambda _event: _cycle_shell_surface(app, -1))
    app.bind("<Control-Alt-Right>", lambda _event: _cycle_shell_surface(app, 1))

    _render_shell_surface(app, load_surface_selection(app.workspace), persist=False)


def configure_windows_product_shell_accessibility(app: Any) -> None:
    """Attach stable Windows UIA metadata after the canonical Tk/UIA bridge is enabled."""
    tk_uia.set_acc_name(app.shell_navigation, text("ui.windows.shell.accessibility.navigation.name"))
    tk_uia.set_acc_description(
        app.shell_navigation,
        text("ui.windows.shell.accessibility.navigation.description"),
    )
    tk_uia.set_automation_id(app.shell_navigation, WINDOWS_SHELL_AUTOMATION_IDS["navigation"])
    tk_uia.set_acc_name(app.shell_state, text("ui.windows.shell.accessibility.state.name"))
    tk_uia.set_acc_description(
        app.shell_state,
        text("ui.windows.shell.accessibility.state.description"),
    )
    tk_uia.set_automation_id(app.shell_state, WINDOWS_SHELL_AUTOMATION_IDS["state"])
    tk_uia.set_acc_name(app.shell_open_button, text("ui.windows.shell.accessibility.open.name"))
    tk_uia.set_acc_description(
        app.shell_open_button,
        text("ui.windows.shell.accessibility.open.description"),
    )
    tk_uia.set_automation_id(app.shell_open_button, WINDOWS_SHELL_AUTOMATION_IDS["open"])
    tk_uia.set_acc_name(app.shell_details, text("ui.windows.shell.accessibility.details.name"))
    tk_uia.set_acc_description(
        app.shell_details,
        text("ui.windows.shell.accessibility.details.description"),
    )
    tk_uia.set_automation_id(app.shell_details, WINDOWS_SHELL_AUTOMATION_IDS["details"])
    tk_uia.set_acc_name(
        app.manual_calculation_button,
        text("ui.windows.manual_calculation.accessibility.open.name"),
    )
    tk_uia.set_acc_description(
        app.manual_calculation_button,
        text("ui.windows.manual_calculation.accessibility.open.description"),
    )
    tk_uia.set_automation_id(
        app.manual_calculation_button,
        WORKBENCH_AUTOMATION_IDS["open"],
    )
    tk_uia.set_acc_name(
        app.owner_economic_authority_button,
        text("ui.windows.owner_authority.accessibility.open.name"),
    )
    tk_uia.set_acc_description(
        app.owner_economic_authority_button,
        text("ui.windows.owner_authority.accessibility.open.description"),
    )
    tk_uia.set_automation_id(
        app.owner_economic_authority_button,
        WINDOWS_SHELL_AUTOMATION_IDS["owner_economic_open"],
    )
    tk_uia.set_acc_name(
        app.owner_economic_authority_state,
        text("ui.windows.owner_authority.accessibility.status.name"),
    )
    tk_uia.set_acc_description(
        app.owner_economic_authority_state,
        text("ui.windows.owner_authority.accessibility.status.description"),
    )
    tk_uia.set_automation_id(
        app.owner_economic_authority_state,
        WINDOWS_SHELL_AUTOMATION_IDS["owner_economic_status"],
    )
    tk_uia.set_acc_name(
        app.owner_economic_authority_readback,
        text("ui.windows.owner_authority.accessibility.readback.name"),
    )
    tk_uia.set_acc_description(
        app.owner_economic_authority_readback,
        text("ui.windows.owner_authority.accessibility.readback.description"),
    )
    tk_uia.set_automation_id(
        app.owner_economic_authority_readback,
        WINDOWS_SHELL_AUTOMATION_IDS["owner_economic_readback"],
    )


def install_compact_windows_layout() -> None:
    """Install the Windows build wrapper before any packaged AutosportApp exists.

    The packaged entrypoint calls this before normal GUI startup and before the
    accessibility/keyboard audit entrypoints. The same layout is therefore
    audited that Windows users actually receive rather than an audit-only
    resize.
    """
    from .gui import AutosportApp

    if getattr(AutosportApp, "_compact_windows_layout_installed", False):
        return

    original_build = AutosportApp._build
    original_configure_accessibility = AutosportApp._configure_accessibility

    def build_with_windows_budget(self) -> None:
        original_build(self)
        compact_surface_heights(self)
        install_windows_product_shell(self)
        frame = next(iter(self.winfo_children()), None)
        if frame is None:
            raise RuntimeError("Autosport root frame is missing")
        install_manual_calculation_workbench_surface(self, frame)
        install_owner_economic_authority_surface(self, frame)

    def configure_with_windows_shell(self) -> None:
        original_configure_accessibility(self)
        configure_windows_product_shell_accessibility(self)
        # WindowsAutosportApp creates its read-only bank_summary only after the
        # patched base _build() has installed and restored the shell selection.
        # Re-check Open authority here, after the full subclass build returned
        # and before user interaction begins, without re-persisting selection.
        refresh_windows_shell_open_availability(self)

    AutosportApp._build = build_with_windows_budget
    AutosportApp._configure_accessibility = configure_with_windows_shell
    AutosportApp._compact_windows_layout_installed = True
