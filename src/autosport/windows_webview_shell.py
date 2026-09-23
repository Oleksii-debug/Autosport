from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

from .calculation_manual import ManualCalculationEvidence, ManualCalculationService
from .dataset import ReplayDataset, load_dataset
from .dataset_worker import OneShotDatasetValidationWorker
from .gui_evidence_export import OneShotEvidenceExportWorker, resolve_evidence_output_destination
from .live_observation import OneShotObservationWorker, observe_workspace_once
from .localization import text
from .operator_source_config import OperatorSourceSelection, OperatorSourceSelectionState
from .operator_source_registry import (
    OperatorSourceRegistryError,
    ProductSourceRegistryEntry,
    list_product_source_entries,
    resolve_product_source_entry,
)
from .operator_source_store import OperatorSourceConfigStore, OperatorSourceStoreError
from .owner_economic_authority import (
    INITIAL_OWNER_FORM_DEFAULTS,
    OWNER_ECONOMIC_FORM_FIELDS,
    OwnerEconomicAuthorityError,
    OwnerEconomicAuthorityService,
    OwnerEconomicReviewSnapshot,
)
from .parlayapi_provider import ParlayApiTableTennisProvider
from .paths import default_webview_storage_path, default_workspace
from .product_gui_worker import ProductGuiMessage, ProductGuiWorker
from .recovery_worker import OneShotRecoveryWorker, recover_workspace_once
from .replay_worker import OneShotReplayWorker, run_workspace_dataset_once, workspace_for_strategy
from .research_strategy import RESEARCH_STRATEGY_ID, ResearchStrategyPlan
from .session import AutosportSession
from .strategies import available_strategies, strategy_spec, validate_strategy_configuration
from .ui_model import (
    evaluation_lines,
    observation_quote_lines,
    observation_summary,
    result_summary,
    ticket_lines,
)
from .windows_surface_contract import (
    DEFAULT_SURFACE_KEY,
    SURFACE_BY_KEY,
    SURFACES,
    load_surface_selection,
    save_surface_selection,
    surface_detail_lines,
)


WEB_SHELL_DIRNAME = "windows_web"
WEB_SHELL_INDEX = "index.html"
_ALLOWED_SPEEDS = {0.0, 1.0, 10.0, 100.0, 1000.0}
_ALLOWED_LIVE_MODES = {"public_preview", "api_key"}
_PRODUCT_SOURCE_FACTORY_ENV = "AUTOSPORT_PRODUCT_SOURCE_FACTORY"
_PRODUCT_SOURCE_CONFIG_FILENAME = "operator-source.json"
_PRODUCT_SOURCE_LABELS_UK = {
    "parlayapi-table-tennis": "ParlayAPI — настільний теніс",
}
_PRODUCT_POLL_SECONDS = 30.0
_REQUEST_REPLAY_LIMIT = 256
_WEBVIEW2_ENVIRONMENT_OVERRIDES = (
    "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",
    "WEBVIEW2_USER_DATA_FOLDER",
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
    "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE",
    "WEBVIEW2_CHANNEL_SEARCH_KIND",
    "WEBVIEW2_RELEASE_CHANNELS",
    "WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER",
    "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER",
)
_PYWEBVIEW_RELEASE_SETTINGS = (
    "WEBVIEW2_RUNTIME_PATH",
    "REMOTE_DEBUGGING_PORT",
)

if set(_PRODUCT_SOURCE_LABELS_UK) != {
    entry.source_id for entry in list_product_source_entries()
}:
    raise RuntimeError("operator source registry is missing a Ukrainian product label")

_MANUAL_OPERATION_KEYS = {
    "odds_conversion": "ui.windows.manual_calculation.operation.odds_conversion",
    "implied_probability": "ui.windows.manual_calculation.operation.implied_probability",
    "multiplicative_devig": "ui.windows.manual_calculation.operation.multiplicative_devig",
    "expected_return": "ui.windows.manual_calculation.operation.expected_return",
    "paper_payout": "ui.windows.manual_calculation.operation.paper_payout",
    "fractional_kelly": "ui.windows.manual_calculation.operation.fractional_kelly",
    "maximum_drawdown": "ui.windows.manual_calculation.operation.maximum_drawdown",
}
_CANONICAL_MESSAGE_KEYS = {
    "decimal odds are the supplied analysis input": "ui.windows.manual_calculation.message.decimal_odds_supplied",
    "American output is the unrounded mathematical conversion; bookmaker display conventions may round it": "ui.windows.manual_calculation.message.american_unrounded",
    "American output is the unrounded mathematical conversion in the deterministic decimal context": "ui.windows.manual_calculation.message.american_decimal_context",
    "bookmaker display conventions may round American odds": "ui.windows.manual_calculation.message.american_display_rounding",
    "division is rounded in the deterministic decimal context": "ui.windows.manual_calculation.message.division_rounding",
    "all selections belong to one supplied market": "ui.windows.manual_calculation.message.one_market",
    "multiplicative normalization is a modelling method, not objective fair value": "ui.windows.manual_calculation.message.devig_model",
    "probability is supplied by the caller and is not inferred by this calculator": "ui.windows.manual_calculation.message.probability_caller",
    "paper-only calculation; no real-money execution authority": "ui.windows.manual_calculation.message.paper_only",
    "probability is a caller-supplied research assumption": "ui.windows.manual_calculation.message.probability_research",
    "single-position Kelly formula; dependence with other positions is not modelled": "ui.windows.manual_calculation.message.kelly_single_position",
    "paper research only; result is not execution authority": "ui.windows.manual_calculation.message.paper_research",
    "Kelly division is rounded in the deterministic decimal context": "ui.windows.manual_calculation.message.kelly_rounding",
    "balances are supplied in chronological order": "ui.windows.manual_calculation.message.balances_chronological",
    "drawdown fraction division is rounded in the deterministic decimal context": "ui.windows.manual_calculation.message.drawdown_rounding",
}


class WindowsWebViewUnavailable(RuntimeError):
    pass


class WindowsWebBridgeTrustError(RuntimeError):
    pass


def _reject_webview2_environment_overrides() -> None:
    active = [
        name
        for name in _WEBVIEW2_ENVIRONMENT_OVERRIDES
        if (value := os.environ.get(name)) is not None and value.strip()
    ]
    if active:
        raise WindowsWebViewUnavailable(
            "Автоспорт заблокував зовнішнє перевизначення WebView2: "
            + ", ".join(active)
            + "."
        )


def _reject_pywebview_release_settings(webview: object) -> None:
    settings = getattr(webview, "settings", None)
    if not isinstance(settings, dict):
        raise WindowsWebViewUnavailable(
            "Автоспорт не може підтвердити безпечні параметри pywebview."
        )
    active = [
        name
        for name in _PYWEBVIEW_RELEASE_SETTINGS
        if settings.get(name) not in (None, "")
    ]
    if active:
        raise WindowsWebViewUnavailable(
            "Автоспорт заблокував некваліфікований параметр pywebview: "
            + ", ".join(active)
            + "."
        )


def web_shell_index_path() -> Path:
    return Path(__file__).resolve().with_name(WEB_SHELL_DIRNAME) / WEB_SHELL_INDEX


def _safe_exception_text(exc: BaseException) -> str:
    """Project only bounded exception type; raw detail may contain secrets or paths."""

    try:
        name = type.__getattribute__(type(exc), "__name__")
    except BaseException:
        name = "BaseException"
    if (
        type(name) is not str
        or not name
        or len(name) > 64
        or not name.isascii()
        or not name.replace("_", "a").isalnum()
        or not (name[0].isalpha() or name[0] == "_")
    ):
        name = "BaseException"
    return text("ui.error.exception.message_unavailable", exception_type=name)


def _safe_worker_error_detail(_value: object) -> str:
    """Never project worker/provider exception detail into visible or spoken UI."""

    return "деталі приховано"


def _lines_from_manual_input(raw: object) -> list[str]:
    if not isinstance(raw, str):
        raise ValueError(text("ui.windows.manual_calculation.error.nonempty"))
    lines = [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]
    if not lines:
        raise ValueError(text("ui.windows.manual_calculation.error.nonempty"))
    return lines


def _selection_odds_from_text(raw: object) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _lines_from_manual_input(raw):
        if "=" not in line:
            raise ValueError(text("ui.windows.manual_calculation.error.selection_format"))
        selection, odds = (part.strip() for part in line.split("=", 1))
        if not selection or not odds:
            raise ValueError(text("ui.windows.manual_calculation.error.selection_empty"))
        if selection in values:
            raise ValueError(
                text("ui.windows.manual_calculation.error.duplicate", selection=selection)
            )
        values[selection] = odds
    return values


def _manual_calculation(service: ManualCalculationService, operation: str, raw: object) -> ManualCalculationEvidence:
    if operation not in _MANUAL_OPERATION_KEYS:
        raise ValueError(text("ui.windows.manual_calculation.error.unknown"))
    if operation in {"odds_conversion", "implied_probability"}:
        values = _lines_from_manual_input(raw)
        if len(values) != 1:
            raise ValueError(text("ui.windows.manual_calculation.error.single"))
        return getattr(service, operation)(values[0])
    if operation == "multiplicative_devig":
        return service.multiplicative_devig(_selection_odds_from_text(raw))

    values = _lines_from_manual_input(raw)
    if operation == "expected_return":
        if len(values) != 3:
            raise ValueError(text("ui.windows.manual_calculation.error.expected_return"))
        return service.expected_return(values[0], values[1], values[2])
    if operation == "paper_payout":
        if len(values) != 2:
            raise ValueError(text("ui.windows.manual_calculation.error.paper_payout"))
        return service.paper_payout(values[0], values[1])
    if operation == "fractional_kelly":
        if len(values) != 4:
            raise ValueError(text("ui.windows.manual_calculation.error.kelly"))
        return service.fractional_kelly(
            values[0], values[1], fraction=values[2], cap=values[3]
        )
    if operation == "maximum_drawdown":
        return service.maximum_drawdown(values)
    raise ValueError(text("ui.windows.manual_calculation.error.unknown"))


def _localized_manual_message(value: str) -> str:
    key = _CANONICAL_MESSAGE_KEYS.get(value)
    return text(key) if key is not None else value


def _render_manual_evidence(evidence: ManualCalculationEvidence) -> str:
    result = evidence.result
    operation_label = text(_MANUAL_OPERATION_KEYS[result.calculation_id])
    classification = text(
        f"ui.windows.manual_calculation.result.classification.{result.classification}"
    )
    unit_label = text("ui.windows.manual_calculation.result.unit")
    none_label = text("ui.windows.manual_calculation.result.none")
    input_units = dict(result.input_units)
    output_units = dict(result.output_units)
    lines = [
        text("ui.windows.manual_calculation.result.heading"),
        f'{text("ui.windows.manual_calculation.result.operation")}: {operation_label}',
        f'{text("ui.windows.manual_calculation.result.method")}: {result.method}',
        f'{text("ui.windows.manual_calculation.result.classification")}: {classification} ({result.classification})',
        text("ui.windows.manual_calculation.result.inputs") + ":",
    ]
    if result.inputs:
        lines.extend(
            f"- {name}: {value}; {unit_label}: {input_units[name]}"
            for name, value in result.inputs
        )
    else:
        lines.append(f"- {none_label}")
    lines.append(text("ui.windows.manual_calculation.result.assumptions") + ":")
    lines.extend(f"- {_localized_manual_message(item)}" for item in result.assumptions)
    if not result.assumptions:
        lines.append(f"- {none_label}")
    lines.append(text("ui.windows.manual_calculation.result.outputs") + ":")
    if result.outputs:
        lines.extend(
            f"- {name}: {value}; {unit_label}: {output_units[name]}"
            for name, value in result.outputs
        )
    else:
        lines.append(f"- {none_label}")
    lines.append(text("ui.windows.manual_calculation.result.warnings") + ":")
    lines.extend(f"- {_localized_manual_message(item)}" for item in result.warnings)
    if not result.warnings:
        lines.append(f"- {none_label}")
    lines.extend(
        [
            f'{text("ui.windows.manual_calculation.result.input_hash")}: {result.input_hash}',
            f'{text("ui.windows.manual_calculation.result.result_hash")}: {result.result_hash}',
            f'{text("ui.windows.manual_calculation.result.evidence_hash")}: {evidence.evidence_sha256}',
            f'{text("ui.windows.manual_calculation.result.service_version")}: {evidence.service_version}',
            f'{text("ui.windows.manual_calculation.result.input_mode")}: {evidence.input_mode}',
            f'{text("ui.windows.manual_calculation.result.real_money_execution")}: '
            f'{text("ui.windows.manual_calculation.result.real_money_false")}',
            "",
            text("ui.windows.manual_calculation.result.canonical_json") + ":",
            evidence.to_text().rstrip("\n"),
        ]
    )
    return "\n".join(lines) + "\n"


class AutosportWebController:
    """Headless product controller for the semantic Windows shell.

    Domain and economic authority stay in existing Autosport services. This object
    only serializes user intent, starts the existing single-flight workers, and
    publishes bounded presentation state for semantic HTML.
    """

    def __init__(self, workspace: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self.workspace = Path(default_workspace() if workspace is None else workspace)
        self._active_workspace = self.workspace
        self.dataset_path: Path | None = None
        self.research_plan_path: Path | None = None
        self.research_plan: ResearchStrategyPlan | None = None
        self.strategy_id = "baseline-v1"
        self.replay_speed = 0.0
        self.live_mode = "public_preview"
        self.dataset_worker = OneShotDatasetValidationWorker()
        self.replay_worker = OneShotReplayWorker()
        self.live_worker = OneShotObservationWorker()
        self.recovery_worker = OneShotRecoveryWorker()
        self.evidence_export_worker = OneShotEvidenceExportWorker()
        self.product_worker = ProductGuiWorker()
        self.product_runtime_status = "Тривалий імітаційний режим не запущено."
        # Request identity/replay bookkeeping is intentionally separate from the
        # ordinary controller lock. Reservations are brief; handlers never run
        # while this lock is held, so the emergency lane can preserve one global
        # request-id namespace without waiting for unrelated backend work.
        self._request_replay_lock = threading.RLock()
        self._request_identities: dict[str, str] = {}
        self._request_results: dict[str, tuple[str, dict[str, Any]]] = {}
        self._pending_dataset_path: Path | None = None
        self._recovery_required_workspaces: set[Path] = set()
        self._owner_review: tuple[Path, OwnerEconomicReviewSnapshot] | None = None
        self._closing = False
        self.status = text("ui.status.startup.ready")
        self.dataset_summary = text("ui.status.dataset.none")
        self.live_status = text("ui.status.live.never")
        self.live_quotes = [text("ui.status.live_quotes.empty")]
        self.evaluation = [text("ui.status.evaluation.empty")]
        self.tickets: list[str] = []
        self.bank = ""
        self.log: list[str] = []
        self.last_error = ""
        self._bridge_validation_error = ""
        self.manual_result = ""
        self.manual_status = text("ui.windows.manual_calculation.status.ready")
        self.owner_review_lines: list[str] = []
        selected = load_surface_selection(self.workspace)
        self.surface_key = selected if selected in SURFACE_BY_KEY else DEFAULT_SURFACE_KEY
        self._refresh_economic_projection()
        self._refresh_owner_projection()

    def _append_log(self, value: str) -> None:
        self.log.append(str(value))
        if len(self.log) > 200:
            self.log = self.log[-200:]

    def _fail(self, message: str) -> dict[str, Any]:
        self.last_error = message
        self.status = message
        self._append_log(message)
        return {"status": "rejected", "message": message}

    def _ok(self, message: str = "", *, focus_id: str | None = None) -> dict[str, Any]:
        self.last_error = ""
        if message:
            self.status = message
            self._append_log(message)
        result: dict[str, Any] = {"status": "completed", "message": message}
        if focus_id:
            result["focus_id"] = focus_id
        return result

    def _busy(self) -> bool:
        return any(
            worker.busy
            for worker in (
                self.dataset_worker,
                self.replay_worker,
                self.live_worker,
                self.recovery_worker,
                self.evidence_export_worker,
                self.product_worker,
            )
        )

    def _operator_source_store(self) -> OperatorSourceConfigStore:
        return OperatorSourceConfigStore(self.workspace / _PRODUCT_SOURCE_CONFIG_FILENAME)

    def _operator_source_admin_override_id(self) -> str | None:
        source_factory = os.environ.get(_PRODUCT_SOURCE_FACTORY_ENV)
        if source_factory is None or source_factory == "":
            return None
        if source_factory.strip() != source_factory:
            raise OperatorSourceRegistryError("admin source override format is invalid")
        matches = [
            entry.source_id
            for entry in list_product_source_entries()
            if entry.factory_spec == source_factory
        ]
        if len(matches) != 1:
            raise OperatorSourceRegistryError(
                "admin source override is not registered by this product build"
            )
        return matches[0]

    def _resolve_operator_source(
        self,
    ) -> tuple[OperatorSourceSelection, ProductSourceRegistryEntry | None]:
        try:
            admin_source_id = self._operator_source_admin_override_id()
        except OperatorSourceRegistryError:
            return (
                OperatorSourceSelection(
                    OperatorSourceSelectionState.INVALID,
                    None,
                    "admin_override_invalid",
                ),
                None,
            )

        selection = self._operator_source_store().resolve(
            admin_override_source_id=admin_source_id
        )
        if selection.source_id is None:
            return selection, None
        try:
            entry = resolve_product_source_entry(selection.source_id)
        except OperatorSourceRegistryError:
            return (
                OperatorSourceSelection(
                    OperatorSourceSelectionState.INVALID,
                    None,
                    "source_not_registered",
                ),
                None,
            )
        return selection, entry

    def _product_source_status(
        self,
        selection: OperatorSourceSelection,
        entry: ProductSourceRegistryEntry | None,
    ) -> str:
        if entry is not None:
            label = _PRODUCT_SOURCE_LABELS_UK[entry.source_id]
            if selection.state is OperatorSourceSelectionState.ADMIN_OVERRIDE:
                return f"Джерело даних задано адміністратором: {label}."
            if selection.state is OperatorSourceSelectionState.CONFIGURED:
                return f"Джерело даних збережено: {label}."
        if selection.state is OperatorSourceSelectionState.CONFIGURATION_REQUIRED:
            return (
                "Джерело даних не налаштовано. "
                "Виберіть підтримуване джерело та збережіть його."
            )
        if selection.state is OperatorSourceSelectionState.CONFLICT:
            return (
                "Збережене джерело даних конфліктує з адміністративним "
                "налаштуванням. Запуск заблоковано."
            )
        return (
            "Налаштування джерела даних недійсне або пошкоджене. "
            "Виберіть підтримуване джерело та збережіть його повторно."
        )

    def _product_source_projection(
        self,
        selection: OperatorSourceSelection,
        entry: ProductSourceRegistryEntry | None,
    ) -> dict[str, Any]:
        return {
            "state": selection.state.value,
            "selected_id": "" if entry is None else entry.source_id,
            "status": self._product_source_status(selection, entry),
            "choices": [
                {
                    "id": candidate.source_id,
                    "label": _PRODUCT_SOURCE_LABELS_UK[candidate.source_id],
                }
                for candidate in list_product_source_entries()
            ],
            "can_configure": not self._busy(),
        }

    def _selected_configuration(self) -> tuple[str, ResearchStrategyPlan | None]:
        validate_strategy_configuration(self.strategy_id, self.research_plan)
        return self.strategy_id, self.research_plan

    def _product_runtime_target_workspace(self) -> Path:
        strategy_id, plan = self._selected_configuration()
        return Path(workspace_for_strategy(self.workspace, strategy_id, plan))

    def _product_runtime_can_start(self, *, source_ready: bool | None = None) -> bool:
        if self._busy():
            return False
        if source_ready is None:
            _selection, entry = self._resolve_operator_source()
            source_ready = entry is not None
        if not source_ready:
            return False
        try:
            workspace = self._product_runtime_target_workspace()
        except Exception:
            return False
        return workspace not in self._recovery_required_workspaces

    def _refresh_economic_projection(self) -> None:
        strategy_id = self.strategy_id
        plan = self.research_plan
        try:
            validate_strategy_configuration(strategy_id, plan)
        except Exception:
            self.bank = text(
                "ui.status.bank.quarantined", workspace=self._active_workspace
            )
            self.tickets = [text("ui.status.tickets.startup_failure")]
            return
        workspace = workspace_for_strategy(self.workspace, strategy_id, plan)
        self._active_workspace = Path(workspace)
        session: AutosportSession | None = None
        try:
            session = AutosportSession(
                workspace,
                "10000",
                strategy_id=strategy_id,
                research_plan=plan,
            )
            self.bank = text(
                "ui.status.bank.current",
                balance=session.book.balance,
                committed_stake=session.book.committed_stake,
                strategy_id=session.strategy_id,
                workspace=session.workspace,
            )
            self.tickets = list(ticket_lines(session))
        except Exception as exc:
            self._recovery_required_workspaces.add(Path(workspace))
            self.bank = text("ui.status.bank.quarantined", workspace=workspace)
            self.tickets = [text("ui.status.tickets.startup_failure")]
            self.last_error = _safe_exception_text(exc)
            self.status = text("ui.status.startup.recovery_required")
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception as exc:
                    self._recovery_required_workspaces.add(Path(workspace))
                    self.last_error = _safe_exception_text(exc)
                    self.status = text("ui.status.startup.recovery_required")

    def _owner_workspace(self) -> Path | None:
        if self.strategy_id != RESEARCH_STRATEGY_ID or self.research_plan is None:
            return None
        try:
            return Path(
                workspace_for_strategy(
                    self.workspace,
                    self.strategy_id,
                    self.research_plan,
                )
            )
        except Exception:
            return None

    def _refresh_owner_projection(self) -> None:
        workspace = self._owner_workspace()
        if workspace is None:
            self.owner_state = "blocked"
            self.owner_summary = text("ui.windows.owner_authority.error.strategy_blocked")
            self.owner_lines = [self.owner_summary]
            self.owner_can_initialize = False
            return
        view = OwnerEconomicAuthorityService(workspace).read_view()
        self.owner_state = view.state
        self.owner_summary = view.summary_uk
        self.owner_lines = list(view.lines_uk)
        self.owner_can_initialize = view.can_initialize

    def _poll_workers(self) -> None:
        message = self.dataset_worker.poll()
        if message is not None:
            pending = self._pending_dataset_path
            self._pending_dataset_path = None
            if message.error is not None:
                self._fail(text("ui.error.dataset.rejected", detail=_safe_worker_error_detail(message.error)))
            elif (
                not isinstance(message.result, ReplayDataset)
                or pending is None
                or Path(message.result.root) != pending
            ):
                self._fail(text("ui.error.dataset.identity_mismatch"))
            else:
                dataset = message.result
                self.dataset_path = pending
                self.dataset_summary = text(
                    "ui.status.dataset.summary",
                    name=dataset.name,
                    sport=dataset.sport,
                    market_sha=dataset.market_sha256[:12],
                    results_sha=dataset.results_sha256[:12],
                )
                self._ok(text("ui.status.dataset.ready"))

        replay_message = self.replay_worker.poll()
        if replay_message is not None:
            if replay_message.error is not None:
                self._recovery_required_workspaces.add(Path(self._active_workspace))
                self.bank = text(
                    "ui.status.bank.quarantined", workspace=self._active_workspace
                )
                self.tickets = [text("ui.status.replay.error_ticket")]
                self.evaluation = [text("ui.evaluation.replay_failed")]
                self._fail(
                    text("ui.error.replay.worker", detail=_safe_worker_error_detail(replay_message.error))
                )
            elif replay_message.result is None:
                self._recovery_required_workspaces.add(Path(self._active_workspace))
                self.evaluation = [text("ui.evaluation.no_terminal_result")]
                self._fail(text("ui.status.replay.no_terminal_result"))
            else:
                self._refresh_economic_projection()
                self.evaluation = list(evaluation_lines(replay_message.result))
                self._ok(result_summary(replay_message.result))

        live_message = self.live_worker.poll()
        if live_message is not None:
            if live_message.error is not None:
                self.live_status = text(
                    "ui.error.live.snapshot",
                    detail=_safe_worker_error_detail(live_message.error),
                )
                self._fail(text("ui.status.live.failed"))
            elif live_message.result is None:
                self.live_status = text("ui.status.live.no_result")
            else:
                self.live_status = observation_summary(live_message.result)
                self.live_quotes = list(observation_quote_lines(live_message.result))
                self._ok(text("ui.status.live.updated"))
                self._append_log(self.live_status)

        recovery_message = self.recovery_worker.poll()
        if recovery_message is not None:
            if recovery_message.error is not None:
                self._recovery_required_workspaces.add(Path(self._active_workspace))
                self._fail(
                    text("ui.error.recovery.failure", detail=_safe_worker_error_detail(recovery_message.error))
                )
            elif recovery_message.result is None:
                self._recovery_required_workspaces.add(Path(self._active_workspace))
                self._fail(text("ui.status.recovery.no_result"))
            else:
                report = recovery_message.result.report
                workspace = Path(recovery_message.result.session_view.workspace)
                summary = text(
                    "ui.recovery.summary",
                    reconciled=len(report.reconciled_keys),
                    aborted_uncommitted=len(report.aborted_uncommitted_keys),
                    unresolved=len(report.unresolved_without_summary),
                    workspace=workspace,
                )
                if report.unresolved_without_summary:
                    self._recovery_required_workspaces.add(workspace)
                    self._fail(summary + text("ui.status.recovery.unresolved_suffix"))
                else:
                    self._recovery_required_workspaces.discard(workspace)
                    self._refresh_economic_projection()
                    self._ok(summary + text("ui.status.recovery.ready_suffix"))

        export_message = self.evidence_export_worker.poll()
        if export_message is not None:
            if export_message.error is not None:
                self._fail(
                    text(
                        "ui.error.evidence_export.failed",
                        error=_safe_worker_error_detail(export_message.error),
                    )
                )
            else:
                self._ok(text("ui.status.evidence_export.complete"))

        # Drain the current product queue before deriving actionability in state().
        # ProductGuiWorker publishes the terminal message immediately before clearing
        # busy; consuming only one message could therefore re-enable START while a
        # STOPPED/ERROR truth was still queued behind earlier STARTED/TICK messages.
        while True:
            product_message = self.product_worker.poll()
            if product_message is None:
                break
            if product_message.kind == "STARTED" and product_message.status is not None:
                self.product_runtime_status = (
                    "Тривалий імітаційний режим активний: "
                    f"джерело {product_message.status.source_id}; "
                    f"циклів {product_message.status.cycles_completed}."
                )
                self._ok(self.product_runtime_status)
            elif product_message.kind == "TICK" and product_message.tick is not None:
                self.product_runtime_status = (
                    "Тривалий імітаційний режим: завершено цикл "
                    f"{product_message.tick.cycle_index}; "
                    f"зафіксовано змін {len(product_message.tick.committed_delta_ids)}; "
                    f"завершено розрахунків {len(product_message.tick.settled_ticket_ids)}."
                )
                self.status = self.product_runtime_status
            elif product_message.kind == "STOPPED" and product_message.status is not None:
                reason = {
                    "operator_stop": "операторська зупинка",
                    "app_close": "закриття програми",
                    "runtime_error": "аварійне завершення",
                }.get(product_message.stop_reason, "зупинено")
                self.product_runtime_status = (
                    "Тривалий імітаційний режим зупинено: "
                    f"{reason}; циклів {product_message.status.cycles_completed}."
                )
                self._ok(self.product_runtime_status)
                self._refresh_economic_projection()
            elif product_message.kind == "ERROR":
                self._recovery_required_workspaces.add(Path(self._active_workspace))
                error_type = product_message.error_type or "BaseException"
                self.product_runtime_status = (
                    "Тривалий імітаційний режим завершився помилкою типу "
                    f"{error_type}. Спочатку відновіть робочу область."
                )
                self._fail(self.product_runtime_status)

        self._refresh_owner_projection()

    def _strategy_choices(self) -> list[dict[str, Any]]:
        return [
            {
                "id": spec.strategy_id,
                "label": text("ui.strategy.display", strategy_id=spec.strategy_id),
                "requires_research_plan": spec.requires_research_plan,
                "opens_paper_tickets": spec.opens_paper_tickets,
            }
            for spec in available_strategies()
        ]

    def state(self) -> dict[str, Any]:
        with self._lock:
            self._poll_workers()
            spec = strategy_spec(self.strategy_id)
            surfaces = [
                {
                    "key": item.key,
                    "title": item.title_uk,
                    "phase": item.phase,
                    "blocked_reason": item.blocked_reason_uk,
                }
                for item in SURFACES
            ]
            surface = SURFACE_BY_KEY[self.surface_key]
            source_selection, source_entry = self._resolve_operator_source()
            source_projection = self._product_source_projection(
                source_selection,
                source_entry,
            )
            return {
                "status": self.status,
                "last_error": self._bridge_validation_error or self.last_error,
                "workspace": str(self.workspace),
                "active_workspace": str(self._active_workspace),
                "bank": self.bank,
                "dataset_summary": self.dataset_summary,
                "dataset_path": "" if self.dataset_path is None else str(self.dataset_path),
                "research_plan_path": (
                    "" if self.research_plan_path is None else str(self.research_plan_path)
                ),
                "research_plan_summary": (
                    (
                        text("ui.status.research_plan.required_missing")
                        if spec.requires_research_plan
                        else (
                            text("ui.status.research_plan.baseline")
                            if self.strategy_id == "baseline-v1"
                            else text(
                                "ui.status.research_plan.not_required",
                                strategy_id=self.strategy_id,
                            )
                        )
                    )
                    if self.research_plan is None
                    else text(
                        "ui.status.research_plan.selected",
                        name=Path(self.research_plan_path).name,
                        sha256=self.research_plan.source_sha256,
                    )
                ),
                "strategy_id": self.strategy_id,
                "strategy_choices": self._strategy_choices(),
                "strategy_requires_plan": spec.requires_research_plan,
                "replay_speed": self.replay_speed,
                "live_mode": self.live_mode,
                "live_status": self.live_status,
                "live_quotes": list(self.live_quotes),
                "tickets": list(self.tickets),
                "evaluation": list(self.evaluation),
                "log": list(self.log),
                "busy": {
                    "dataset": self.dataset_worker.busy,
                    "replay": self.replay_worker.busy,
                    "live": self.live_worker.busy,
                    "recovery": self.recovery_worker.busy,
                    "evidence_export": self.evidence_export_worker.busy,
                    "product_runtime": self.product_worker.busy,
                },
                "product_source": source_projection,
                "product_runtime": {
                    "status": self.product_runtime_status,
                    "running": self.product_worker.busy,
                    "can_start": self._product_runtime_can_start(
                        source_ready=source_entry is not None
                    ),
                    "can_stop": self.product_worker.busy,
                },
                "surface_key": self.surface_key,
                "surfaces": surfaces,
                "surface_state": surface.phase,
                "surface_details": list(surface_detail_lines(surface)),
                "owner": {
                    "state": self.owner_state,
                    "summary": self.owner_summary,
                    "lines": list(self.owner_lines),
                    "can_initialize": self.owner_can_initialize,
                    "defaults": dict(INITIAL_OWNER_FORM_DEFAULTS),
                    "review_lines": list(self.owner_review_lines),
                },
                "manual": {
                    "status": self.manual_status,
                    "result": self.manual_result,
                    "operations": [
                        {"id": key, "label": text(label_key)}
                        for key, label_key in _MANUAL_OPERATION_KEYS.items()
                    ],
                },
                "truth": {
                    "real_money_execution": False,
                    "human_tested": False,
                    "nvda_verified": False,
                    "whole_product_complete": False,
                },
            }

    def _action_dataset_select(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._busy():
            return self._fail(text("ui.status.dataset.validation_busy"))
        raw = payload.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return self._fail(text("ui.status.dataset.validation_failed"))
        selected = Path(raw).expanduser().absolute()
        self._pending_dataset_path = selected

        def task() -> ReplayDataset:
            return load_dataset(selected)

        if not self.dataset_worker.start(task):
            self._pending_dataset_path = None
            return self._fail(text("ui.error.dataset.worker_not_started"))
        return self._ok(
            text("ui.status.dataset.validation_running", path=selected),
            focus_id="dataset-path",
        )

    def _action_strategy_set(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._busy():
            return self._fail(text("ui.status.strategy.dataset_busy"))
        strategy_id = payload.get("strategy_id")
        if not isinstance(strategy_id, str):
            return self._fail(text("ui.error.strategy.unknown_display", display=repr(strategy_id)))
        try:
            spec = strategy_spec(strategy_id)
        except Exception as exc:
            return self._fail(_safe_exception_text(exc))
        self.strategy_id = spec.strategy_id
        if not spec.requires_research_plan:
            self.research_plan = None
            self.research_plan_path = None
        self._owner_review = None
        self.owner_review_lines = []
        # Selecting a research strategy before binding its required causal plan is
        # a normal configuration state, not evidence of economic corruption.
        # Keep the last proven economic projection visible until the plan is bound;
        # do not quarantine or open the research workspace prematurely.
        if not spec.requires_research_plan or self.research_plan is not None:
            self._refresh_economic_projection()
        self._refresh_owner_projection()
        return self._ok(
            text("ui.status.strategy.selected", strategy_id=self.strategy_id),
            focus_id="106",
        )

    def _action_research_plan_select(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._busy():
            return self._fail(text("ui.status.research_plan.dataset_busy"))
        raw = payload.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return self._fail(text("ui.status.research_plan.validation_failed"))
        try:
            plan_path = Path(raw).expanduser().absolute()
            plan = ResearchStrategyPlan.from_path(plan_path)
            validate_strategy_configuration(self.strategy_id, plan)
        except Exception as exc:
            return self._fail(
                text("ui.error.research_plan.rejected", detail=_safe_exception_text(exc))
            )
        self.research_plan_path = plan_path
        self.research_plan = plan
        self._owner_review = None
        self.owner_review_lines = []
        self._refresh_economic_projection()
        self._refresh_owner_projection()
        return self._ok(text("ui.status.research_plan.bound", strategy_id=self.strategy_id, sha_short=plan.source_sha256[:12]), focus_id="research-plan-path")

    def _action_speed_set(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        value = payload.get("speed")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return self._fail(text("ui.status.replay.start_failed"))
        speed = float(value)
        if speed not in _ALLOWED_SPEEDS:
            return self._fail(text("ui.status.replay.start_failed"))
        if self._busy():
            return self._fail(text("ui.status.replay.already_busy"))
        self.replay_speed = speed
        return self._ok()

    def _action_live_mode_set(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        mode = payload.get("mode")
        if not isinstance(mode, str) or mode not in _ALLOWED_LIVE_MODES:
            return self._fail(text("ui.status.live.unknown_mode"))
        if self._busy():
            return self._fail(text("ui.status.live.busy"))
        self.live_mode = mode
        return self._ok()

    def _action_replay_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._busy():
            return self._fail(text("ui.status.replay.already_busy"))
        if self.dataset_path is None:
            return self._fail(text("ui.info.replay.dataset_required"))

        visible_dataset_raw = payload.get("dataset_path")
        visible_plan_raw = payload.get("research_plan_path")
        if type(visible_dataset_raw) is not str or type(visible_plan_raw) is not str:
            return self._fail(
                "Перед запуском повтору підтвердьте поточні шляхи набору даних "
                "і плану дослідження."
            )
        try:
            visible_dataset_path = (
                None
                if not visible_dataset_raw.strip()
                else Path(visible_dataset_raw).expanduser().absolute()
            )
            visible_plan_path = (
                None
                if not visible_plan_raw.strip()
                else Path(visible_plan_raw).expanduser().absolute()
            )
        except (OSError, RuntimeError):
            return self._fail(
                "Не вдалося підтвердити видимі шляхи перед запуском повтору."
            )

        if visible_dataset_path != self.dataset_path:
            return self._fail(
                "Видимий шлях набору даних не збігається з останнім "
                "перевіреним набором. Повторно виберіть і перевірте набір "
                "даних перед запуском."
            )
        bound_plan_path = getattr(self, "research_plan_path", None)
        if visible_plan_path != bound_plan_path:
            return self._fail(
                "Видимий шлях плану дослідження не збігається з прив'язаним "
                "планом. Повторно виберіть план перед запуском."
            )

        try:
            strategy_id, plan = self._selected_configuration()
            replay_workspace = Path(
                workspace_for_strategy(self.workspace, strategy_id, plan)
            )
        except Exception as exc:
            return self._fail(_safe_exception_text(exc))
        if replay_workspace in self._recovery_required_workspaces:
            return self._fail(text("ui.status.replay.quarantined"))
        dataset_path = self.dataset_path
        speed = self.replay_speed
        self._active_workspace = replay_workspace

        def task():
            return run_workspace_dataset_once(
                replay_workspace,
                dataset_path,
                initial_bankroll="10000",
                speed=speed,
                strategy_id=strategy_id,
                research_plan=plan,
            )

        if not self.replay_worker.start(task):
            return self._fail(text("ui.status.replay.start_failed"))
        self.evaluation = [text("ui.evaluation.running")]
        return self._ok(
            text("ui.status.replay.running", strategy_id=strategy_id, plan_identity=""),
            focus_id="204",
        )

    def _action_live_refresh(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._busy():
            return self._fail(text("ui.status.live.busy"))
        public_preview = self.live_mode == "public_preview"
        workspace = self.workspace

        def task():
            api_key = None if public_preview else os.environ.get("AUTOSPORT_PARLAYAPI_KEY")
            if not public_preview and not api_key:
                raise RuntimeError(text("ui.error.live.api_key_missing"))
            provider = ParlayApiTableTennisProvider(
                api_key,
                public_preview=public_preview,
            )
            return observe_workspace_once(workspace, provider, max_items=250)

        if not self.live_worker.start(task):
            return self._fail(text("ui.status.live.busy"))
        self.live_status = text("ui.status.live.running")
        return self._ok(
            text("ui.status.live.read_only_running"),
            focus_id="203",
        )

    def _action_recovery_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._busy():
            return self._fail(text("ui.status.recovery.already_busy"))
        try:
            strategy_id, plan = self._selected_configuration()
            workspace = Path(workspace_for_strategy(self.workspace, strategy_id, plan))
        except Exception as exc:
            return self._fail(
                text("ui.error.recovery.configuration", detail=_safe_exception_text(exc))
            )
        self._active_workspace = workspace
        self._recovery_required_workspaces.add(workspace)

        def task():
            return recover_workspace_once(
                workspace,
                initial_bankroll="10000",
                strategy_id=strategy_id,
                research_plan=plan,
            )

        if not self.recovery_worker.start(task):
            return self._fail(text("ui.status.recovery.start_failed"))
        self.bank = text("ui.status.bank.quarantined", workspace=workspace)
        self.tickets = [text("ui.status.recovery.in_progress_ticket")]
        return self._ok(
            text(
                "ui.status.recovery.running",
                strategy_id=strategy_id,
                plan_identity="",
            ),
            focus_id="202",
        )

    def _action_evidence_export(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._busy():
            return self._fail(text("ui.status.evidence_export.operation_busy"))
        raw = payload.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return self._fail(text("ui.status.evidence_export.destination_invalid"))
        workspace = Path(self._active_workspace)
        try:
            destination = resolve_evidence_output_destination(
                workspace,
                Path(raw).expanduser().absolute(),
            )
        except (OSError, TypeError, ValueError):
            return self._fail(text("ui.status.evidence_export.destination_invalid"))
        if not self.evidence_export_worker.start(workspace, destination):
            return self._fail(text("ui.status.evidence_export.start_failed"))
        return self._ok(
            text("ui.status.evidence_export.running"),
            focus_id="202",
        )

    def _action_product_source_configure(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self._busy():
            return self._fail(
                "Зміну джерела даних заблоковано, доки поточна операція не завершиться."
            )
        source_id = payload.get("source_id")
        try:
            entry = resolve_product_source_entry(source_id)
        except (OperatorSourceRegistryError, TypeError):
            return self._fail("Виберіть джерело даних зі списку підтримуваних.")

        try:
            admin_source_id = self._operator_source_admin_override_id()
        except OperatorSourceRegistryError:
            return self._fail(
                "Адміністративне налаштування джерела недійсне. "
                "Збереження та запуск заблоковано."
            )
        if admin_source_id is not None and admin_source_id != entry.source_id:
            return self._fail(
                "Вибране джерело конфліктує з адміністративним налаштуванням. "
                "Збереження та запуск заблоковано."
            )

        try:
            self._operator_source_store().write_source_id(entry.source_id)
        except (OperatorSourceStoreError, OSError, TypeError, ValueError):
            return self._fail("Не вдалося безпечно зберегти вибране джерело даних.")

        _selection, resolved_entry = self._resolve_operator_source()
        if resolved_entry is None or resolved_entry.source_id != entry.source_id:
            return self._fail(
                "Джерело даних збережено, але повторна перевірка конфігурації "
                "не підтвердила його. Запуск заблоковано."
            )
        return self._ok(
            f"Джерело даних збережено: {_PRODUCT_SOURCE_LABELS_UK[entry.source_id]}.",
            focus_id="product-source-status",
        )

    def _action_product_runtime_start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._busy():
            return self._fail("Спочатку завершіть поточну операцію.")
        try:
            workspace = self._product_runtime_target_workspace()
        except Exception:
            return self._fail(
                "Тривалий імітаційний режим не запущено: поточна "
                "конфігурація стратегії неповна або недійсна."
            )
        if workspace in self._recovery_required_workspaces:
            return self._fail(
                "Тривалий імітаційний режим заблоковано: спочатку відновіть робочу область."
            )

        source_selection, source_entry = self._resolve_operator_source()
        if source_entry is None:
            return self._fail(
                "Тривалий імітаційний режим не запущено: "
                + self._product_source_status(source_selection, source_entry)
            )
        source_factory = source_entry.factory_spec

        try:
            started = self.product_worker.start(
                workspace=workspace,
                source_factory=source_factory,
                initial_bankroll="10000",
                poll_seconds=_PRODUCT_POLL_SECONDS,
            )
        except Exception as exc:
            return self._fail(_safe_exception_text(exc))
        if not started:
            return self._fail("Тривалий імітаційний режим уже запущено.")
        self._active_workspace = workspace
        self.product_runtime_status = "Запускається канонічний тривалий імітаційний режим…"
        return self._ok(self.product_runtime_status, focus_id="product-runtime-status")

    def _action_product_runtime_stop(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.product_worker.busy:
            return self._fail("Тривалий імітаційний режим зараз не виконується.")
        if not self.product_worker.request_stop("operator_stop"):
            return self._fail("Не вдалося передати команду STOP.")
        self.product_runtime_status = (
            "Надіслано команду STOP; очікується безпечне завершення тривалого імітаційного режиму."
        )
        return self._ok(self.product_runtime_status, focus_id="product-runtime-status")

    def _action_surface_select(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        key = payload.get("surface_key")
        if not isinstance(key, str) or key not in SURFACE_BY_KEY:
            return self._fail("Невідомий екран продукту.")
        self.surface_key = key
        save_surface_selection(self.workspace, key)
        return self._ok("")

    def _action_owner_preview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._busy():
            return self._fail(text("ui.windows.owner_authority.error.busy"))
        workspace = self._owner_workspace()
        if workspace is None:
            return self._fail(text("ui.windows.owner_authority.error.strategy_blocked"))
        values = payload.get("values")
        emergency_stop = payload.get("emergency_stop")
        if not isinstance(values, dict) or type(emergency_stop) is not bool:
            return self._fail(text("ui.windows.owner_authority.error.review_stale"))
        if set(values) != set(OWNER_ECONOMIC_FORM_FIELDS):
            return self._fail(text("ui.windows.owner_authority.error.review_stale"))
        try:
            review = OwnerEconomicReviewSnapshot.from_form(
                values,
                emergency_stop=emergency_stop,
            )
        except OwnerEconomicAuthorityError as exc:
            return self._fail(_safe_exception_text(exc))
        self._owner_review = (workspace, review)
        self.owner_review_lines = list(review.lines_uk)
        return self._ok(text("ui.windows.owner_authority.review.prompt"), focus_id="owner-review")

    def _action_owner_initialize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._busy():
            return self._fail(text("ui.windows.owner_authority.error.busy"))
        confirmed = payload.get("confirmed")
        values = payload.get("values")
        emergency_stop = payload.get("emergency_stop")
        workspace = self._owner_workspace()
        review_pair = self._owner_review
        if (
            confirmed is not True
            or workspace is None
            or review_pair is None
            or review_pair[0] != workspace
            or not isinstance(values, dict)
            or type(emergency_stop) is not bool
            or not review_pair[1].still_matches(values, emergency_stop=emergency_stop)
        ):
            return self._fail(text("ui.windows.owner_authority.error.review_stale"))
        try:
            OwnerEconomicAuthorityService(workspace).initialize_from_form(
                values,
                emergency_stop=emergency_stop,
                confirmed=True,
            )
        except OwnerEconomicAuthorityError as exc:
            return self._fail(_safe_exception_text(exc))
        self._owner_review = None
        self.owner_review_lines = []
        self._refresh_owner_projection()
        self._refresh_economic_projection()
        return self._ok(self.owner_summary, focus_id="306")

    def _action_manual_calculate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation = payload.get("operation")
        raw = payload.get("input")
        if not isinstance(operation, str):
            return self._fail(text("ui.windows.manual_calculation.error.operation_empty"))
        try:
            evidence = _manual_calculation(
                ManualCalculationService(),
                operation,
                raw,
            )
            self.manual_result = _render_manual_evidence(evidence)
        except (TypeError, ValueError, ArithmeticError) as exc:
            self.manual_result = ""
            self.manual_status = text("ui.windows.manual_calculation.status.error")
            return self._fail(
                f"{self.manual_status}: {_safe_exception_text(exc)}"
            )
        self.manual_status = text("ui.windows.manual_calculation.status.success")
        return self._ok(self.manual_status, focus_id="334")

    def _action_manual_clear(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.manual_result = ""
        self.manual_status = text("ui.windows.manual_calculation.status.cleared")
        return self._ok(self.manual_status, focus_id="332")

    def _reserve_request_identity(
        self,
        request_id: str,
        command_identity: str,
    ) -> tuple[str | None, dict[str, Any] | None]:
        with self._request_replay_lock:
            previous_identity = self._request_identities.get(request_id)
            previous = self._request_results.get(request_id)
            if previous_identity is None:
                self._request_identities[request_id] = command_identity
            return (
                previous_identity,
                None if previous is None else dict(previous[1]),
            )

    def _release_request_identity(
        self,
        request_id: str,
        command_identity: str,
    ) -> None:
        with self._request_replay_lock:
            if (
                self._request_identities.get(request_id) == command_identity
                and request_id not in self._request_results
            ):
                del self._request_identities[request_id]

    def _store_request_result(
        self,
        request_id: str,
        command_identity: str,
        result: Mapping[str, Any],
    ) -> None:
        with self._request_replay_lock:
            if self._request_identities.get(request_id) != command_identity:
                raise RuntimeError("request identity reservation changed during dispatch")
            self._request_results[request_id] = (command_identity, dict(result))
            while len(self._request_results) > _REQUEST_REPLAY_LIMIT:
                oldest = next(iter(self._request_results))
                del self._request_results[oldest]
                self._request_identities.pop(oldest, None)

    def _reject_bridge_command(
        self,
        request_id: str,
        message: str,
    ) -> dict[str, Any]:
        """Persist one bounded bridge-validation rejection for stable UI readback.

        Bridge-shape validation occurs before domain action dispatch. These fixed,
        product-owned messages must survive the frontend's immediate get_state()
        refresh, but they must not become domain/log events or execute an action.
        """
        with self._lock:
            self._bridge_validation_error = message
        return {
            "request_id": request_id,
            "status": "rejected",
            "message": message,
        }

    def dispatch(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            return self._reject_bridge_command(
                "invalid",
                "Некоректна команда інтерфейсу.",
            )
        request_id = raw.get("request_id")
        action_id = raw.get("action_id")
        payload = raw.get("payload", {})
        if (
            type(request_id) is not str
            or not request_id
            or request_id.strip() != request_id
            or len(request_id) > 128
        ):
            return self._reject_bridge_command(
                "invalid",
                "Некоректний ідентифікатор команди.",
            )
        if type(action_id) is not str or not isinstance(payload, Mapping):
            return self._reject_bridge_command(
                request_id,
                "Некоректна команда інтерфейсу.",
            )
        try:
            command_identity = json.dumps(
                {"action_id": action_id, "payload": dict(payload)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return self._reject_bridge_command(
                request_id,
                "Некоректні дані команди інтерфейсу.",
            )
        handlers = {
            "dataset.select": self._action_dataset_select,
            "strategy.set": self._action_strategy_set,
            "research_plan.select": self._action_research_plan_select,
            "replay_speed.set": self._action_speed_set,
            "live_mode.set": self._action_live_mode_set,
            "replay.run": self._action_replay_run,
            "live.refresh": self._action_live_refresh,
            "recovery.run": self._action_recovery_run,
            "evidence.export": self._action_evidence_export,
            "product_source.configure": self._action_product_source_configure,
            "product_runtime.start": self._action_product_runtime_start,
            "product_runtime.stop": self._action_product_runtime_stop,
            "surface.select": self._action_surface_select,
            "owner.preview": self._action_owner_preview,
            "owner.initialize": self._action_owner_initialize,
            "manual.calculate": self._action_manual_calculate,
            "manual.clear": self._action_manual_clear,
        }
        handler = handlers.get(action_id)
        if handler is None:
            return self._reject_bridge_command(
                request_id,
                "Невідома команда інтерфейсу.",
            )
        with self._lock:
            self._bridge_validation_error = ""
            previous_identity, previous_result = self._reserve_request_identity(
                request_id,
                command_identity,
            )
            if previous_identity is not None:
                if previous_identity != command_identity:
                    return self._reject_bridge_command(
                        request_id,
                        "Повторний ідентифікатор належить іншій команді.",
                    )
                if previous_result is not None:
                    return previous_result
                return self._reject_bridge_command(
                    request_id,
                    "Команда з цим ідентифікатором уже виконується.",
                )

            try:
                if self._closing:
                    response = self._fail("Автоспорт завершує роботу.")
                else:
                    try:
                        response = handler(payload)
                    except BaseException as exc:
                        if not isinstance(exc, Exception):
                            raise
                        response = self._fail(_safe_exception_text(exc))
                result = {"request_id": request_id, **response}
                self._store_request_result(request_id, command_identity, result)
                return result
            except BaseException:
                self._release_request_identity(request_id, command_identity)
                raise

    @staticmethod
    def _wait_for_terminal_worker(worker: Any) -> None:
        """Wait until one committed non-daemon worker publishes terminal truth."""

        if not worker.busy:
            return
        poll = getattr(worker, "poll", None)
        if not callable(poll):
            raise RuntimeError("non-daemon worker does not expose terminal polling")
        pause = threading.Event()
        while worker.busy:
            if poll() is None:
                pause.wait(0.01)

    def close(self) -> None:
        with self._lock:
            self._closing = True

        # Request cooperative STOP first so the long-running product runtime can
        # wind down while one-shot economic workers finish their committed tasks.
        self.product_worker.request_stop("app_close")

        # These workers deliberately use daemon=False because replay/live/recovery
        # can cross durable economic boundaries.  The only operator surface must
        # therefore remain in close() until each committed task has terminalized.
        for worker in (
            self.replay_worker,
            self.live_worker,
            self.recovery_worker,
            self.evidence_export_worker,
        ):
            self._wait_for_terminal_worker(worker)

        join_product = getattr(self.product_worker, "join", None)
        if callable(join_product):
            join_product()


class AutosportWebBridge:
    """Minimal pywebview API bound to one trusted launch document."""

    def __init__(self, controller: AutosportWebController | None = None) -> None:
        self._controller = controller or AutosportWebController()
        self._trust_lock = threading.RLock()
        self._trusted_window: object | None = None
        self._trusted_url: str | None = None
        self._trust_revoked = False
        self._host_shutdown = False

    @staticmethod
    def _current_window_url(window: object) -> str:
        getter = getattr(window, "get_current_url", None)
        if not callable(getter):
            raise WindowsWebBridgeTrustError(
                "The WebView bridge cannot verify the active document URL"
            )
        try:
            value = getter()
        except Exception as exc:
            raise WindowsWebBridgeTrustError(
                "The WebView bridge could not read the active document URL"
            ) from exc
        if (
            type(value) is not str
            or not value
            or value.strip() != value
            or any(ord(char) < 0x20 for char in value)
        ):
            raise WindowsWebBridgeTrustError(
                "The WebView bridge observed an invalid active document URL"
            )
        return value

    def _revoke_trust(self) -> None:
        with self._trust_lock:
            self._trust_revoked = True

    def _bind_trusted_window(self, window: object) -> None:
        """Bind once to the exact first document seen before API injection.

        The same trusted URL may reload inside the same native window. Any other
        window or URL permanently revokes this bridge instance for the launch.
        """

        with self._trust_lock:
            if self._host_shutdown or self._trust_revoked:
                raise WindowsWebBridgeTrustError(
                    "The WebView bridge launch trust is no longer active"
                )
            current_url = self._current_window_url(window)
            if self._trusted_window is None:
                self._trusted_window = window
                self._trusted_url = current_url
                return
            if self._trusted_window is not window or self._trusted_url != current_url:
                self._trust_revoked = True
                raise WindowsWebBridgeTrustError(
                    "The WebView bridge rejected a different window or document"
                )

    def _assert_trusted_session_locked(self) -> None:
        if (
            self._host_shutdown
            or self._trust_revoked
            or self._trusted_window is None
            or self._trusted_url is None
        ):
            raise WindowsWebBridgeTrustError(
                "The WebView bridge is not bound to the trusted launch document"
            )
        try:
            current_url = self._current_window_url(self._trusted_window)
        except WindowsWebBridgeTrustError:
            self._trust_revoked = True
            raise
        if current_url != self._trusted_url:
            self._trust_revoked = True
            raise WindowsWebBridgeTrustError(
                "The WebView bridge rejected active document URL drift"
            )

    def dispatch(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        # Validate the exact bound document under the trust lock, but do not hold
        # that lock while backend work runs. Ordinary commands remain serialized
        # by AutosportWebController._lock; releasing this outer lock also keeps a
        # trusted emergency STOP from queueing behind an unrelated slow command.
        with self._trust_lock:
            self._assert_trusted_session_locked()
        return self._controller.dispatch(raw)

    def get_state(self) -> dict[str, Any]:
        # State reads may wait for the ordinary controller lock. They must not
        # monopolize the document-trust lock while doing so, otherwise a trusted
        # emergency STOP call could be delayed behind a polling request.
        with self._trust_lock:
            self._assert_trusted_session_locked()
        return {"ok": True, "state": self._controller.state()}

    def close(self) -> None:
        with self._trust_lock:
            self._assert_trusted_session_locked()
            self._host_shutdown = True
            self._trust_revoked = True
        self._controller.close()

    def _close_from_host(self) -> None:
        """Host-only finalizer; intentionally not exposed through pywebview API."""

        with self._trust_lock:
            if self._host_shutdown:
                return
            self._host_shutdown = True
            self._trust_revoked = True
        self._controller.close()


def launch_windows_shell(
    bridge: AutosportWebBridge | None = None,
    *,
    title: str | None = None,
) -> int:
    _reject_webview2_environment_overrides()

    try:
        storage_path = default_webview_storage_path()
    except (OSError, RuntimeError, ValueError) as exc:
        raise WindowsWebViewUnavailable(
            "Autosport could not resolve its canonical WebView storage path"
        ) from exc
    if not storage_path.is_absolute():
        raise WindowsWebViewUnavailable(
            "Autosport WebView storage path must be absolute"
        )

    try:
        import webview
    except Exception as exc:
        raise WindowsWebViewUnavailable(
            "pywebview is unavailable; the Windows semantic shell cannot start"
        ) from exc
    _reject_pywebview_release_settings(webview)

    asset = web_shell_index_path()
    if not asset.is_file():
        raise WindowsWebViewUnavailable(
            f"Windows semantic shell asset is missing: {asset}"
        )

    api = bridge or AutosportWebBridge()
    canonical_bridge = isinstance(api, AutosportWebBridge)
    required_renderer = "edgechromium"
    renderer_observed = False
    trusted_document_observed = not canonical_bridge
    trusted_document_violation = False
    window: object | None = None

    def verify_initialized_renderer(renderer: object) -> bool:
        nonlocal renderer_observed
        if type(renderer) is not str or renderer != required_renderer:
            if canonical_bridge:
                api._revoke_trust()
            return False
        renderer_observed = True
        return True

    def bind_trusted_document() -> None:
        nonlocal trusted_document_observed, trusted_document_violation
        if not canonical_bridge:
            return
        if not renderer_observed or window is None:
            api._revoke_trust()
            trusted_document_violation = True
            return
        try:
            api._bind_trusted_window(window)
        except WindowsWebBridgeTrustError:
            api._revoke_trust()
            trusted_document_violation = True
            return
        trusted_document_observed = True

    try:
        window = webview.create_window(
            title or text("ui.app.title"),
            str(asset),
            js_api=api,
            width=1180,
            height=820,
            min_size=(820, 680),
            text_select=True,
            zoomable=True,
        )
        window.events.initialized += verify_initialized_renderer
        if canonical_bridge:
            # before_load is synchronous and runs immediately before pywebview
            # injects window.pywebview into each document. Re-check every load so
            # a navigated document cannot inherit the privileged Python API.
            window.events.before_load += bind_trusted_document
        webview.start(
            gui=required_renderer,
            storage_path=str(storage_path),
        )
        if not renderer_observed:
            raise WindowsWebViewUnavailable(
                "The Autosport semantic shell started without an observed "
                "EdgeChromium/WebView2 renderer witness"
            )
        if canonical_bridge and (
            not trusted_document_observed or trusted_document_violation
        ):
            raise WindowsWebViewUnavailable(
                "The Autosport semantic shell lost its trusted WebView document binding"
            )
    except WindowsWebViewUnavailable:
        raise
    except Exception as exc:
        raise WindowsWebViewUnavailable(
            "Microsoft Edge WebView2 could not start the Autosport semantic shell"
        ) from exc
    finally:
        if canonical_bridge:
            api._close_from_host()
        else:
            api.close()
    return 0


def main() -> int:
    return launch_windows_shell()


if __name__ == "__main__":
    raise SystemExit(main())
