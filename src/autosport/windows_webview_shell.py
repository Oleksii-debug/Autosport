from __future__ import annotations

import json
import os
import sys
import threading
import tempfile
import weakref
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .calculation_manual import ManualCalculationEvidence, ManualCalculationService
from .causal_collector import GapState, SyncState
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
from .product_gui_worker import (
    ProductGuiEconomicSnapshot,
    ProductGuiMessage,
    ProductGuiWorker,
)
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
from .webview2_release_environment import (
    WEBVIEW2_ENVIRONMENT_OVERRIDES as _WEBVIEW2_ENVIRONMENT_OVERRIDES,
    active_webview2_environment_overrides,
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
_PYWEBVIEW_RELEASE_SETTINGS = (
    "WEBVIEW2_RUNTIME_PATH",
    "REMOTE_DEBUGGING_PORT",
)
_WEBVIEW2_RUNTIME_WITNESS_FILENAME = "webview2-runtime-witness.json"
_WEBVIEW2_RUNTIME_VERSION_MAX_LENGTH = 256

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
    """Bounded Windows shell startup failure with operator-safe reason class."""

    def __init__(self, message: str, *, reason: str = "runtime") -> None:
        super().__init__(message)
        self.reason = reason


class WindowsWebBridgeTrustError(RuntimeError):
    pass


def _reject_webview2_environment_overrides() -> None:
    active = active_webview2_environment_overrides()
    if active:
        raise WindowsWebViewUnavailable(
            "Автоспорт заблокував зовнішнє перевизначення WebView2: "
            + ", ".join(active)
            + "."
        )


def _reject_pywebview_release_settings(webview: object) -> None:
    settings = getattr(webview, "settings", None)
    if not isinstance(settings, Mapping):
        raise WindowsWebViewUnavailable(
            "Автоспорт не може підтвердити безпечні параметри pywebview."
        )
    missing = [name for name in _PYWEBVIEW_RELEASE_SETTINGS if name not in settings]
    if missing:
        raise WindowsWebViewUnavailable(
            "Автоспорт не може підтвердити параметри pywebview: "
            + ", ".join(missing)
            + "."
        )
    active = [
        name
        for name in _PYWEBVIEW_RELEASE_SETTINGS
        if settings[name] not in (None, "")
    ]
    if active:
        raise WindowsWebViewUnavailable(
            "Автоспорт заблокував некваліфікований параметр pywebview: "
            + ", ".join(active)
            + "."
        )


def _probe_webview_storage_writable(storage_path: Path) -> None:
    """Verify the canonical per-user UDF root before constructing the WebView.

    The shared host-process probe only proves that Autosport can create and durably
    write within its chosen root. It is deliberately not treated as proof that
    WebView2 child processes have sufficient LowIL/AppContainer access; actual
    WebView startup remains the final runtime gate for that stronger condition.
    """

    from .storage_preflight import probe_webview_storage_writable

    try:
        probe_webview_storage_writable(storage_path)
    except OSError as exc:
        raise WindowsWebViewUnavailable(
            "Autosport canonical WebView storage is not writable",
            reason="storage",
        ) from exc


def _observed_webview2_browser_version(window: object) -> str:
    """Read the runtime identity from the actual native CoreWebView2 instance."""

    try:
        native = getattr(window, "native")
        native_webview = getattr(native, "webview")
        core_webview = getattr(native_webview, "CoreWebView2")
        environment = getattr(core_webview, "Environment")
        value = getattr(environment, "BrowserVersionString")
    except Exception as exc:
        raise WindowsWebViewUnavailable(
            "Autosport could not observe the launched WebView2 runtime identity"
        ) from exc
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _WEBVIEW2_RUNTIME_VERSION_MAX_LENGTH
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise WindowsWebViewUnavailable(
            "Autosport observed an invalid launched WebView2 runtime identity"
        )
    return value


def _write_webview2_runtime_witness(path: Path, browser_version: str) -> None:
    """Atomically persist one bounded witness from the actual native WebView2 host."""

    payload = {
        "schema_version": 1,
        "renderer": "edgechromium",
        "browser_version_string": browser_version,
        "observation_source": "native_core_webview2_environment",
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "whole_product_complete": False,
    }
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def web_shell_index_path() -> Path:
    return Path(__file__).resolve().with_name(WEB_SHELL_DIRNAME) / WEB_SHELL_INDEX


def _require_packaged_launch_document(
    window: object,
    *,
    expected_original_url: str,
) -> str:
    """Bind bridge eligibility to pywebview's exact packaged local-page resolution."""

    try:
        original_url = getattr(window, "original_url")
        real_url = getattr(window, "real_url")
    except Exception as exc:
        raise WindowsWebBridgeTrustError(
            "The WebView bridge cannot verify packaged launch identity"
        ) from exc
    if (
        type(original_url) is not str
        or original_url != expected_original_url
        or type(real_url) is not str
        or not real_url
        or real_url.strip() != real_url
        or any(ord(char) < 0x20 for char in real_url)
    ):
        raise WindowsWebBridgeTrustError(
            "The WebView bridge rejected packaged launch identity drift"
        )
    try:
        parsed = urlsplit(real_url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise WindowsWebBridgeTrustError(
            "The WebView bridge observed an invalid packaged launch URL"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or port < 1
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
        or not parsed.path.endswith("/" + Path(expected_original_url).name)
    ):
        raise WindowsWebBridgeTrustError(
            "The WebView bridge refused a non-local packaged launch URL"
        )
    return real_url


def _safe_exception_text(exc: BaseException) -> str:
    """Project a secret-safe localized operator error without Python type leakage."""

    del exc
    return text("ui.windows.error.internal_hidden")


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
        self._product_runtime_identity: tuple[Path, str, str] | None = None
        self.product_runtime_source_status = (
            "Стан зовнішнього джерела ще не підтверджено канонічним циклом."
        )
        self._product_runtime_source_provider_unavailable: bool | None = None
        self._product_runtime_source_attention_required: bool | None = None
        self._product_runtime_source_last_success_at: str | None = None
        self._product_runtime_economic_snapshot: ProductGuiEconomicSnapshot | None = None
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
            "can_configure": not getattr(self, "_closing", False) and not self._busy(),
        }

    def _selected_configuration(self) -> tuple[str, ResearchStrategyPlan | None]:
        validate_strategy_configuration(self.strategy_id, self.research_plan)
        return self.strategy_id, self.research_plan

    def _product_runtime_target_workspace(self) -> Path:
        strategy_id, plan = self._selected_configuration()
        return Path(workspace_for_strategy(self.workspace, strategy_id, plan))

    def _product_runtime_can_start(self, *, source_ready: bool | None = None) -> bool:
        if getattr(self, "_closing", False) or self._busy():
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

    def _product_runtime_stop_requested(self) -> bool:
        try:
            value = self.product_worker.stop_requested
        except AttributeError:
            # Compatibility for bounded test/presentation doubles only. The
            # canonical ProductGuiWorker always exposes stop_requested.
            return False
        if type(value) is not bool:
            return True
        return value

    def _product_runtime_identity_projection(self) -> dict[str, str]:
        identity = getattr(self, "_product_runtime_identity", None)
        if identity is None:
            return {"workspace": "", "session_id": "", "source_id": ""}
        workspace, session_id, source_id = identity
        return {
            "workspace": str(workspace),
            "session_id": session_id,
            "source_id": source_id,
        }

    def _quarantine_product_runtime_truth(self, workspace: Path) -> None:
        resolved_workspace = Path(workspace)
        self._recovery_required_workspaces.add(resolved_workspace)
        self._product_runtime_economic_snapshot = None
        self.bank = text("ui.status.bank.quarantined", workspace=resolved_workspace)
        self.tickets = [text("ui.status.tickets.startup_failure")]
        self.evaluation = [
            "Економічна проєкція тривалої симуляції недоступна; "
            "потрібне канонічне відновлення робочої області."
        ]
        self.product_runtime_status = text(
            "ui.windows.product_runtime.error.recovery_required"
        )
        if self.product_worker.busy:
            self.product_worker.request_stop("runtime_error")
        self._fail(self.product_runtime_status)

    def _reject_product_runtime_identity(self, workspace: Path) -> None:
        self._quarantine_product_runtime_truth(workspace)

    def _bind_product_runtime_identity(
        self,
        *,
        workspace: Path,
        session_id: object,
        source_id: object,
    ) -> bool:
        resolved_workspace = Path(workspace)
        if (
            type(session_id) is not str
            or not session_id
            or session_id.strip() != session_id
            or type(source_id) is not str
            or not source_id
            or source_id.strip() != source_id
        ):
            self._reject_product_runtime_identity(resolved_workspace)
            return False

        candidate = (resolved_workspace, session_id, source_id)
        existing = getattr(self, "_product_runtime_identity", None)
        if (
            existing is not None
            and existing[0] == resolved_workspace
            and existing != candidate
        ):
            self._reject_product_runtime_identity(resolved_workspace)
            return False
        self._product_runtime_identity = candidate
        return True

    def _product_runtime_source_projection(self) -> dict[str, Any]:
        return {
            "status": getattr(
                self,
                "product_runtime_source_status",
                "Стан зовнішнього джерела ще не підтверджено канонічним циклом.",
            ),
            "provider_unavailable": getattr(
                self,
                "_product_runtime_source_provider_unavailable",
                None,
            ),
            "attention_required": getattr(
                self,
                "_product_runtime_source_attention_required",
                None,
            ),
            "last_success_at": getattr(
                self,
                "_product_runtime_source_last_success_at",
                None,
            )
            or "",
        }

    def _apply_product_runtime_source_projection(
        self,
        *,
        provider_unavailable: object,
        gap_states: object,
        sync_states: object,
        full_refresh_required: object,
        invalidation_backlog: object,
        last_success_at: object,
    ) -> bool:
        if (
            type(provider_unavailable) is not bool
            or type(gap_states) is not tuple
            or type(sync_states) is not tuple
            or type(full_refresh_required) is not bool
            or type(invalidation_backlog) is not bool
            or (
                last_success_at is not None
                and (
                    type(last_success_at) is not str
                    or not last_success_at
                    or last_success_at.strip() != last_success_at
                )
            )
        ):
            self._quarantine_product_runtime_truth(Path(self._active_workspace))
            return False

        try:
            canonical_gap_states = tuple(GapState(value) for value in gap_states)
            canonical_sync_states = tuple(SyncState(value) for value in sync_states)
        except (TypeError, ValueError):
            self._quarantine_product_runtime_truth(Path(self._active_workspace))
            return False

        gap_attention = any(
            value in {GapState.DETECTED, GapState.CURSOR_RESET}
            for value in canonical_gap_states
        )
        sync_attention = any(
            value
            in {
                SyncState.GAP_DETECTED,
                SyncState.CURSOR_RESET,
                SyncState.EPOCH_CHANGED,
                SyncState.RETRY_REQUIRED,
            }
            for value in canonical_sync_states
        )
        has_observation = bool(
            provider_unavailable
            or canonical_gap_states
            or canonical_sync_states
            or full_refresh_required
            or invalidation_backlog
            or last_success_at is not None
        )
        attention_required = bool(
            provider_unavailable
            or gap_attention
            or sync_attention
            or full_refresh_required
            or invalidation_backlog
        )

        self._product_runtime_source_provider_unavailable = provider_unavailable
        self._product_runtime_source_attention_required = (
            attention_required if has_observation else None
        )
        self._product_runtime_source_last_success_at = last_success_at

        if provider_unavailable:
            self.product_runtime_source_status = (
                "Зовнішнє джерело недоступне в останньому канонічному циклі. "
                "Порожній результат цього циклу не вважається здоровою стрічкою."
            )
        elif attention_required:
            self.product_runtime_source_status = (
                "Останній канонічний цикл має прогалину, повторну синхронізацію "
                "або чергу оновлення. Дані потребують уваги."
            )
        elif not has_observation:
            self.product_runtime_source_status = (
                "Стан зовнішнього джерела ще не підтверджено канонічним циклом."
            )
        else:
            self.product_runtime_source_status = (
                "Останній канонічний цикл не повідомляє про недоступність, "
                "невирішену прогалину або чергу оновлення."
            )
        return True

    def _project_product_runtime_source_tick(self, tick: object) -> bool:
        try:
            return self._apply_product_runtime_source_projection(
                provider_unavailable=tick.source_provider_unavailable,
                gap_states=tick.source_gap_states,
                sync_states=tick.source_sync_states,
                full_refresh_required=tick.full_refresh_required,
                invalidation_backlog=tick.invalidation_backlog,
                last_success_at=tick.last_success_at,
            )
        except AttributeError:
            self._quarantine_product_runtime_truth(Path(self._active_workspace))
            return False

    def _project_product_runtime_source_status(self, status: object) -> bool:
        try:
            provider_unavailable = status.source_provider_unavailable
            source_gap_state = status.source_gap_state
            source_sync_state = status.source_sync_state
            projection_backlog = status.source_state_projection_backlog
            invalidation_pending_count = status.invalidation_pending_count
            full_refresh_required = status.invalidation_full_refresh_required
            last_success_at = status.source_last_success_at
        except AttributeError:
            self._quarantine_product_runtime_truth(Path(self._active_workspace))
            return False

        if (
            type(projection_backlog) is not bool
            or isinstance(invalidation_pending_count, bool)
            or not isinstance(invalidation_pending_count, int)
            or invalidation_pending_count < 0
            or (source_gap_state is None) != (source_sync_state is None)
        ):
            self._quarantine_product_runtime_truth(Path(self._active_workspace))
            return False

        gap_states = () if source_gap_state is None else (source_gap_state,)
        sync_states = () if source_sync_state is None else (source_sync_state,)
        return self._apply_product_runtime_source_projection(
            provider_unavailable=provider_unavailable,
            gap_states=gap_states,
            sync_states=sync_states,
            full_refresh_required=full_refresh_required,
            invalidation_backlog=bool(
                projection_backlog
                or invalidation_pending_count > 0
                or full_refresh_required
            ),
            last_success_at=last_success_at,
        )

    def _product_runtime_economic_projection(self) -> dict[str, object]:
        snapshot = getattr(self, "_product_runtime_economic_snapshot", None)
        if snapshot is None:
            return {
                "available": False,
                "cycle_index": None,
                "cycle_last_success_at": None,
                "paper_book_sha256": None,
                "balance": None,
                "committed_stake": None,
                "ticket_count": None,
                "open_ticket_count": None,
                "portfolio_mode": None,
                "portfolio_scenario_count": None,
                "portfolio_worst_case": None,
                "portfolio_best_case": None,
                "portfolio_mean_case": None,
                "paper_risk": {
                    "available": False,
                    "includes_live_execution_exposure": False,
                    "live_execution_headroom_authoritative": False,
                    "risk_of_ruin_upper_bound": None,
                },
                "risk_policy_evaluated": False,
                "real_money_authorized": False,
            }
        risk = snapshot.paper_risk
        risk_projection: dict[str, object]
        if risk is None:
            risk_projection = {
                "available": False,
                "includes_live_execution_exposure": False,
                "live_execution_headroom_authoritative": False,
                "risk_of_ruin_upper_bound": None,
            }
        else:
            risk_projection = {
                "available": True,
                "scope": risk.scope,
                "goal_id": risk.goal_id,
                "goal_revision": risk.goal_revision,
                "goal_contract_sha256": risk.goal_contract_sha256,
                "portfolio_risk_state_sha256": risk.portfolio_risk_state_sha256,
                "bankroll_id": risk.bankroll_id,
                "currency": risk.currency,
                "current_equity": str(risk.current_equity),
                "peak_equity": str(risk.peak_equity),
                "committed_stake": str(risk.committed_stake),
                "realized_gross_loss": str(risk.realized_gross_loss),
                "turnover": str(risk.turnover),
                "current_drawdown_amount": str(risk.current_drawdown_amount),
                "historical_max_drawdown_amount": str(
                    risk.historical_max_drawdown_amount
                ),
                "historical_max_drawdown_fraction": (
                    None
                    if risk.historical_max_drawdown_fraction is None
                    else str(risk.historical_max_drawdown_fraction)
                ),
                "drawdown_loss_room": str(risk.drawdown_loss_room),
                "max_drawdown_fraction": str(risk.max_drawdown_fraction),
                "risk_of_ruin_limit": str(risk.risk_of_ruin_limit),
                "risk_of_ruin_status": risk.risk_of_ruin_status,
                "includes_live_execution_exposure": False,
                "live_execution_headroom_authoritative": False,
                "risk_of_ruin_upper_bound": None,
            }
        return {
            "available": True,
            "cycle_index": snapshot.cycle_index,
            "cycle_last_success_at": snapshot.cycle_last_success_at,
            "paper_book_sha256": snapshot.paper_book_sha256,
            "balance": str(snapshot.balance),
            "committed_stake": str(snapshot.committed_stake),
            "ticket_count": len(snapshot.tickets),
            "open_ticket_count": sum(
                1 for ticket in snapshot.tickets if ticket.status == "open"
            ),
            "portfolio_mode": snapshot.portfolio_mode,
            "portfolio_scenario_count": snapshot.portfolio_scenario_count,
            "portfolio_worst_case": str(snapshot.portfolio_worst_case),
            "portfolio_best_case": str(snapshot.portfolio_best_case),
            "portfolio_mean_case": str(snapshot.portfolio_mean_case),
            "paper_risk": risk_projection,
            "risk_policy_evaluated": False,
            "real_money_authorized": False,
        }

    def _apply_product_runtime_economic_snapshot(
        self,
        snapshot: object,
        *,
        cycle_index: object,
        session_id: object,
        source_id: object,
    ) -> bool:
        workspace = Path(self._active_workspace)
        canonical_workspace = workspace.resolve(strict=False)
        identity = getattr(self, "_product_runtime_identity", None)
        if (
            type(snapshot) is not ProductGuiEconomicSnapshot
            or type(cycle_index) is not int
            or isinstance(cycle_index, bool)
            or cycle_index < 0
            or type(session_id) is not str
            or type(source_id) is not str
            or identity is None
            or identity[1:] != (session_id, source_id)
            or Path(identity[0]).resolve(strict=False) != canonical_workspace
            or snapshot.workspace != canonical_workspace
            or snapshot.session_id != session_id
            or snapshot.source_id != source_id
            or snapshot.cycle_index != cycle_index
        ):
            self._quarantine_product_runtime_truth(workspace)
            return False

        previous = getattr(self, "_product_runtime_economic_snapshot", None)
        if (
            previous is not None
            and previous.workspace == snapshot.workspace
            and previous.session_id == snapshot.session_id
            and snapshot.cycle_index < previous.cycle_index
        ):
            self._quarantine_product_runtime_truth(workspace)
            return False

        self._product_runtime_economic_snapshot = snapshot
        self.bank = text(
            "ui.status.bank.current",
            balance=snapshot.balance,
            committed_stake=snapshot.committed_stake,
            strategy_id=self.strategy_id,
            workspace=snapshot.workspace,
        )
        self.tickets = [
            text(
                "ui.ticket.row",
                status=ticket.status.upper(),
                stake=ticket.stake,
                odds=ticket.combined_odds,
                payout=ticket.payout,
                legs=", ".join(ticket.legs),
            )
            for ticket in snapshot.tickets
        ] or [text("ui.ticket.empty")]

        cycle_success = (
            snapshot.cycle_last_success_at
            or "успішний цикл ще не підтверджено"
        )
        book_identity = snapshot.paper_book_sha256 or "відсутній durable PaperBook"
        self.evaluation = [
            (
                "Економічний знімок тривалої симуляції отримано після "
                f"циклу {snapshot.cycle_index}; останній успішний цикл: "
                f"{cycle_success}; точний PaperBook: {book_identity}."
            ),
            (
                "Поточний PAPER-портфель: "
                f"режим {snapshot.portfolio_mode}; "
                f"сценаріїв {snapshot.portfolio_scenario_count}; "
                f"найгірший P&L {snapshot.portfolio_worst_case}; "
                f"найкращий {snapshot.portfolio_best_case}; "
                f"середній {snapshot.portfolio_mean_case}."
            ),
            (
                "Відкрите PAPER-зобов'язання: "
                f"{snapshot.committed_stake}. "
                "Цей знімок не надає реального грошового дозволу."
            ),
        ]
        risk = snapshot.paper_risk
        if risk is None:
            self.evaluation.append(
                "PAPER-звіт ризику недоступний: канонічна ціль власника "
                "не ініціалізована. Висновок політики ризику не вигадується."
            )
        else:
            self.evaluation.append(
                "Канонічний PAPER-звіт ризику: "
                f"ціль {risk.goal_id} ревізія {risk.goal_revision}; "
                f"поточний капітал {risk.current_equity}; "
                f"поточна просадка {risk.current_drawdown_amount}; "
                f"історична максимальна просадка "
                f"{risk.historical_max_drawdown_amount}; "
                f"додатковий простір втрати за drawdown-лімітом "
                f"{risk.drawdown_loss_room}; статус risk-of-ruin "
                f"{risk.risk_of_ruin_status}. "
                "Жива execution-експозиція не включена, а live headroom "
                "не є авторитетним."
            )
        return True

    def _refresh_economic_projection(self) -> None:
        # This path reopens durable state only when no live product-runtime message
        # owns presentation freshness (startup/replay/recovery/terminal STOP). Any
        # prior runtime snapshot is therefore retired before the durable reopen.
        self._product_runtime_economic_snapshot = None
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
        workspace = Path(
            workspace_for_strategy(self.workspace, strategy_id, plan)
        )
        self._active_workspace = workspace
        if workspace in self._recovery_required_workspaces:
            self.bank = text("ui.status.bank.quarantined", workspace=workspace)
            self.tickets = [text("ui.status.tickets.startup_failure")]
            self.status = text("ui.status.startup.recovery_required")
            return
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
        if workspace in self._recovery_required_workspaces:
            self._owner_review = None
            self.owner_review_lines = []
            self.owner_state = "blocked"
            self.owner_summary = text(
                "ui.windows.owner_authority.error.recovery_required"
            )
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
                # STARTED proves lifecycle/source identity only. The prior run's
                # economic snapshot is not current for this run until a TICK carries
                # a newly bound snapshot.
                self._product_runtime_economic_snapshot = None
                if not self._bind_product_runtime_identity(
                    workspace=Path(self._active_workspace),
                    session_id=product_message.status.session_id,
                    source_id=product_message.status.source_id,
                ):
                    continue
                if not self._project_product_runtime_source_status(
                    product_message.status
                ):
                    continue
                self.product_runtime_status = (
                    "Тривалий імітаційний режим активний: "
                    f"джерело {product_message.status.source_id}; "
                    f"циклів {product_message.status.cycles_completed}."
                )
                self._ok(self.product_runtime_status)
            elif product_message.kind == "TICK" and product_message.tick is not None:
                if not self._bind_product_runtime_identity(
                    workspace=Path(self._active_workspace),
                    session_id=product_message.tick.session_id,
                    source_id=product_message.tick.source_id,
                ):
                    continue
                if not self._project_product_runtime_source_tick(product_message.tick):
                    continue
                if not self._apply_product_runtime_economic_snapshot(
                    getattr(product_message, "economic", None),
                    cycle_index=product_message.tick.cycle_index,
                    session_id=product_message.tick.session_id,
                    source_id=product_message.tick.source_id,
                ):
                    continue
                self.product_runtime_status = (
                    "Тривалий імітаційний режим: завершено цикл "
                    f"{product_message.tick.cycle_index}; "
                    f"зафіксовано змін {len(product_message.tick.committed_delta_ids)}; "
                    f"завершено розрахунків {len(product_message.tick.settled_ticket_ids)}."
                )
                self.status = self.product_runtime_status
            elif product_message.kind == "STOPPED" and product_message.status is not None:
                if not self._bind_product_runtime_identity(
                    workspace=Path(self._active_workspace),
                    session_id=product_message.status.session_id,
                    source_id=product_message.status.source_id,
                ):
                    continue
                if not self._project_product_runtime_source_status(
                    product_message.status
                ):
                    continue
                runtime_workspace = Path(self._active_workspace)
                if runtime_workspace in self._recovery_required_workspaces:
                    self._quarantine_product_runtime_truth(runtime_workspace)
                    continue
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
                # The live runtime portfolio snapshot has been retired. Do not
                # present it beside the newly reopened durable bank/ticket state.
                self.evaluation = [text("ui.status.evaluation.empty")]
            elif product_message.kind == "ERROR":
                self._quarantine_product_runtime_truth(
                    Path(self._active_workspace)
                )

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
            runtime_identity = self._product_runtime_identity_projection()
            runtime_source = self._product_runtime_source_projection()
            runtime_economic = self._product_runtime_economic_projection()
            runtime_stop_requested = self._product_runtime_stop_requested()
            return {
                "status": self.status,
                "last_error": self._bridge_validation_error or self.last_error,
                "closing": getattr(self, "_closing", False),
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
                    "can_stop": bool(
                        self.product_worker.busy and not runtime_stop_requested
                    ),
                    "stop_requested": runtime_stop_requested,
                    "workspace": runtime_identity["workspace"],
                    "session_id": runtime_identity["session_id"],
                    "source_id": runtime_identity["source_id"],
                    "source_status": runtime_source["status"],
                    "source_provider_unavailable": runtime_source[
                        "provider_unavailable"
                    ],
                    "source_attention_required": runtime_source[
                        "attention_required"
                    ],
                    "source_last_success_at": runtime_source["last_success_at"],
                    "economic_snapshot": runtime_economic,
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
        try:
            workspace = self._product_runtime_target_workspace()
        except Exception:
            return self._fail(text("ui.status.evidence_export.destination_invalid"))
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
                expected_source_id=source_entry.expected_provider_source_id,
                initial_bankroll="10000",
                poll_seconds=_PRODUCT_POLL_SECONDS,
            )
        except Exception:
            return self._fail(
                "Тривалий імітаційний режим не запущено: "
                "внутрішня перевірка конфігурації відхилила запуск."
            )
        if not started:
            return self._fail("Тривалий імітаційний режим уже запущено.")
        existing_identity = getattr(self, "_product_runtime_identity", None)
        if existing_identity is not None and existing_identity[0] != Path(workspace):
            self._product_runtime_identity = None
        self._product_runtime_economic_snapshot = None
        self._active_workspace = workspace
        self.product_runtime_status = "Запускається канонічний тривалий імітаційний режим…"
        return self._ok(self.product_runtime_status, focus_id="product-runtime-status")

    def _action_product_runtime_stop(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.product_worker.busy:
            return self._fail("Тривалий імітаційний режим зараз не виконується.")
        if self._product_runtime_stop_requested():
            return self._fail(
                "Команду STOP уже прийнято; очікується безпечне завершення."
            )
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
        self._poll_workers()
        if self._busy():
            return self._fail(text("ui.windows.owner_authority.error.busy"))
        workspace = self._owner_workspace()
        if workspace is None:
            return self._fail(text("ui.windows.owner_authority.error.strategy_blocked"))
        if workspace in self._recovery_required_workspaces:
            self._owner_review = None
            self.owner_review_lines = []
            return self._fail(
                text("ui.windows.owner_authority.error.recovery_required")
            )
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
        self._poll_workers()
        if self._busy():
            return self._fail(text("ui.windows.owner_authority.error.busy"))
        confirmed = payload.get("confirmed")
        values = payload.get("values")
        emergency_stop = payload.get("emergency_stop")
        workspace = self._owner_workspace()
        if workspace is not None and workspace in self._recovery_required_workspaces:
            self._owner_review = None
            self.owner_review_lines = []
            return self._fail(
                text("ui.windows.owner_authority.error.recovery_required")
            )
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

    @staticmethod
    def _consume_joined_product_terminal(worker: Any) -> str | None:
        """Consume terminal product truth after its non-daemon thread has joined."""

        if not worker.busy:
            return None
        poll = getattr(worker, "poll", None)
        if not callable(poll):
            raise RuntimeError("product runtime worker does not expose terminal polling")

        terminal_kind: str | None = None
        while worker.busy:
            message = poll()
            if message is None:
                raise RuntimeError(
                    "product runtime worker joined without publishing terminal state"
                )
            kind = getattr(message, "kind", None)
            if kind in {"STOPPED", "ERROR"}:
                terminal_kind = kind

        if terminal_kind is None:
            raise RuntimeError(
                "product runtime worker ended without terminal lifecycle state"
            )
        return terminal_kind

    def close(self) -> None:
        with self._lock:
            if getattr(self, "_close_complete", False):
                return
            if self._closing:
                raise RuntimeError("Autosport close is already in progress")
            self._closing = True
            self._bridge_validation_error = ""
            self.last_error = ""
            self.status = (
                "Автоспорт завершує роботу. "
                "Дочекайтеся безпечного завершення фонових операцій."
            )
            self._append_log(self.status)

        try:
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
            if not callable(join_product):
                if self.product_worker.busy:
                    raise RuntimeError(
                        "product runtime worker does not expose terminal join"
                    )
            elif join_product() is not True:
                raise RuntimeError(
                    "product runtime worker did not reach terminal thread state"
                )

            terminal_kind = self._consume_joined_product_terminal(self.product_worker)
            if terminal_kind == "ERROR":
                self._quarantine_product_runtime_truth(Path(self._active_workspace))
                raise RuntimeError(
                    "product runtime terminated with error during close"
                )
        except BaseException as exc:
            with self._lock:
                # A process-control interruption (for example KeyboardInterrupt or
                # SystemExit) must still propagate, but it cannot strand the
                # controller in an in-progress close state if the process remains
                # alive and the operator/runtime later retries safe teardown.
                self._closing = False
                if isinstance(exc, Exception):
                    self._fail(
                        "Не вдалося безпечно завершити фонову роботу. "
                        "Вікно залишено відкритим; перевірте стан і повторіть завершення."
                    )
            raise

        with self._lock:
            self._close_complete = True


_WEB_CONTROLLER_AUTHORITY_CLASS = AutosportWebController
_WEB_CONTROLLER_AUTHORITY_METHOD_NAMES = ("close", "dispatch", "state")
_WEB_CONTROLLER_BASE_METHOD_WITNESSES = tuple(
    (
        name,
        getattr(AutosportWebController, name),
        getattr(getattr(AutosportWebController, name), "__code__", None),
    )
    for name in _WEB_CONTROLLER_AUTHORITY_METHOD_NAMES
)
_WEB_BRIDGE_CONTROLLER_REGISTRY = weakref.WeakKeyDictionary()
_WEB_BRIDGE_CONTROLLER_REGISTRY_LOCK = threading.RLock()
_WEB_EMERGENCY_CONTROLLER_AUTHORITY_CLASS: type[AutosportWebController] | None = None
_WEB_EMERGENCY_CONTROLLER_AUTHORITY_LOCK = threading.RLock()


def _register_emergency_stop_controller_authority(
    controller_type: type[AutosportWebController],
) -> None:
    """Register the one packaged emergency controller class exactly once."""

    global _WEB_EMERGENCY_CONTROLLER_AUTHORITY_CLASS
    if (
        not isinstance(controller_type, type)
        or not issubclass(controller_type, _WEB_CONTROLLER_AUTHORITY_CLASS)
        or controller_type is _WEB_CONTROLLER_AUTHORITY_CLASS
    ):
        raise WindowsWebBridgeTrustError(
            "The WebView bridge refused an invalid emergency controller authority"
        )
    with _WEB_EMERGENCY_CONTROLLER_AUTHORITY_LOCK:
        registered = _WEB_EMERGENCY_CONTROLLER_AUTHORITY_CLASS
        if registered is None:
            _WEB_EMERGENCY_CONTROLLER_AUTHORITY_CLASS = controller_type
            return
        if registered is not controller_type:
            raise WindowsWebBridgeTrustError(
                "The WebView bridge emergency controller authority changed"
            )


def _canonical_product_controller_type(
    controller: object,
    *,
    _base_type=_WEB_CONTROLLER_AUTHORITY_CLASS,
    _names=_WEB_CONTROLLER_AUTHORITY_METHOD_NAMES,
    _base_witnesses=_WEB_CONTROLLER_BASE_METHOD_WITNESSES,
):
    """Resolve the only product controller classes authorized for WebView launch."""

    if not isinstance(controller, _base_type):
        return None
    if (
        AutosportWebController is not _base_type
        or _WEB_CONTROLLER_AUTHORITY_CLASS is not _base_type
        or _WEB_CONTROLLER_AUTHORITY_METHOD_NAMES is not _names
        or _WEB_CONTROLLER_BASE_METHOD_WITNESSES is not _base_witnesses
    ):
        raise WindowsWebBridgeTrustError(
            "The WebView bridge canonical controller class authority changed"
        )
    for name, expected, expected_code in _base_witnesses:
        current = getattr(_base_type, name, None)
        if (
            current is not expected
            or getattr(current, "__code__", None) is not expected_code
        ):
            raise WindowsWebBridgeTrustError(
                "The WebView bridge canonical base controller method authority changed"
            )
    if type(controller) is _base_type:
        return _base_type

    # The packaged Windows path intentionally extends the base controller with one
    # durable emergency-STOP lane. Import lazily to avoid the module cycle:
    # windows_webview_emergency_stop imports this module to define that subclass.
    from .windows_webview_emergency_stop import EmergencyStopWebController

    registered_emergency = _WEB_EMERGENCY_CONTROLLER_AUTHORITY_CLASS
    if (
        registered_emergency is None
        or EmergencyStopWebController is not registered_emergency
    ):
        raise WindowsWebBridgeTrustError(
            "The WebView bridge emergency controller authority changed"
        )
    if type(controller) is not registered_emergency:
        raise WindowsWebBridgeTrustError(
            "The WebView bridge refused a non-canonical controller subclass"
        )
    return registered_emergency


class AutosportWebBridge:
    """Minimal pywebview API bound to one trusted launch document."""

    def __init__(self, controller: AutosportWebController | None = None) -> None:
        resolved_controller = controller or AutosportWebController()
        controller_type = _canonical_product_controller_type(resolved_controller)
        controller_operations = tuple(
            (name, getattr(resolved_controller, name, None))
            for name in _WEB_CONTROLLER_AUTHORITY_METHOD_NAMES
        )
        if any(not callable(operation) for _name, operation in controller_operations):
            raise WindowsWebBridgeTrustError(
                "The WebView bridge controller does not expose its required operations"
            )

        trust_lock = threading.RLock()
        controller_record = (
            resolved_controller,
            controller_type,
            controller_operations,
            getattr(resolved_controller, "workspace", None)
            if controller_type is not None
            else None,
            trust_lock,
        )
        object.__setattr__(self, "_controller", resolved_controller)
        object.__setattr__(self, "_controller_witness", resolved_controller)
        object.__setattr__(self, "_controller_record_witness", controller_record)
        object.__setattr__(self, "_trust_lock", trust_lock)
        self._trusted_window: object | None = None
        self._trusted_url: str | None = None
        self._trust_revoked = False
        self._host_shutdown = False
        with _WEB_BRIDGE_CONTROLLER_REGISTRY_LOCK:
            _WEB_BRIDGE_CONTROLLER_REGISTRY[self] = controller_record

    def __setattr__(self, name: str, value: object) -> None:
        if name in {
            "_controller",
            "_controller_witness",
            "_controller_record_witness",
            "_trust_lock",
        } and hasattr(
            self, "_controller_witness"
        ):
            raise WindowsWebBridgeTrustError(
                "The WebView bridge controller authority is immutable"
            )
        object.__setattr__(self, name, value)

    def _registered_controller_record_locked(
        self,
        *,
        _base_type=_WEB_CONTROLLER_AUTHORITY_CLASS,
        _names=_WEB_CONTROLLER_AUTHORITY_METHOD_NAMES,
        _base_witnesses=_WEB_CONTROLLER_BASE_METHOD_WITNESSES,
        _registry=_WEB_BRIDGE_CONTROLLER_REGISTRY,
        _registry_lock=_WEB_BRIDGE_CONTROLLER_REGISTRY_LOCK,
    ) -> tuple[
        object,
        object | None,
        tuple[tuple[str, object], ...],
        object,
        object,
    ]:
        """Return and revalidate the construction-time controller authority record."""

        if (
            AutosportWebController is not _base_type
            or _WEB_CONTROLLER_AUTHORITY_CLASS is not _base_type
            or _WEB_CONTROLLER_AUTHORITY_METHOD_NAMES is not _names
            or _WEB_CONTROLLER_BASE_METHOD_WITNESSES is not _base_witnesses
            or _WEB_BRIDGE_CONTROLLER_REGISTRY is not _registry
            or _WEB_BRIDGE_CONTROLLER_REGISTRY_LOCK is not _registry_lock
        ):
            self._trust_revoked = True
            raise WindowsWebBridgeTrustError(
                "The WebView bridge controller registry authority changed"
            )
        with _registry_lock:
            record = _registry.get(self)
        if (
            not isinstance(record, tuple)
            or len(record) != 5
            or not isinstance(record[2], tuple)
        ):
            self._trust_revoked = True
            raise WindowsWebBridgeTrustError(
                "The WebView bridge lost its construction-time controller authority"
            )
        if record is not getattr(self, "_controller_record_witness", None):
            self._trust_revoked = True
            raise WindowsWebBridgeTrustError(
                "The WebView bridge construction-time controller authority record changed"
            )
        (
            controller,
            controller_type,
            operations,
            workspace_witness,
            trust_lock_witness,
        ) = record
        if self._trust_lock is not trust_lock_witness:
            self._trust_revoked = True
            raise WindowsWebBridgeTrustError(
                "The WebView bridge trust-lock authority changed"
            )
        if self._controller is not controller or self._controller_witness is not controller:
            self._trust_revoked = True
            raise WindowsWebBridgeTrustError(
                "The WebView bridge controller authority changed"
            )

        if controller_type is not None:
            if (
                getattr(controller, "workspace", None) is not workspace_witness
            ):
                self._trust_revoked = True
                raise WindowsWebBridgeTrustError(
                    "The WebView bridge canonical controller workspace authority changed"
                )
            for name, expected, expected_code in _base_witnesses:
                current_base = getattr(_base_type, name, None)
                if (
                    current_base is not expected
                    or getattr(current_base, "__code__", None) is not expected_code
                ):
                    self._trust_revoked = True
                    raise WindowsWebBridgeTrustError(
                        "The WebView bridge canonical base controller method authority changed"
                    )
            if type(controller) is not controller_type:
                self._trust_revoked = True
                raise WindowsWebBridgeTrustError(
                    "The WebView bridge canonical controller type changed"
                )
            if controller_type is not _base_type:
                from .windows_webview_emergency_stop import EmergencyStopWebController

                if (
                    EmergencyStopWebController is not controller_type
                    or _WEB_EMERGENCY_CONTROLLER_AUTHORITY_CLASS is not controller_type
                ):
                    self._trust_revoked = True
                    raise WindowsWebBridgeTrustError(
                        "The WebView bridge emergency controller authority changed"
                    )
            if tuple(name for name, _operation in operations) != _names:
                self._trust_revoked = True
                raise WindowsWebBridgeTrustError(
                    "The WebView bridge canonical controller operation set changed"
                )
            for name, captured in operations:
                current = getattr(controller, name, None)
                if (
                    not callable(current)
                    or getattr(current, "__func__", None)
                    is not getattr(captured, "__func__", None)
                    or getattr(
                        getattr(current, "__func__", current),
                        "__code__",
                        None,
                    )
                    is not getattr(
                        getattr(captured, "__func__", captured),
                        "__code__",
                        None,
                    )
                ):
                    self._trust_revoked = True
                    raise WindowsWebBridgeTrustError(
                        "The WebView bridge refused a rebound canonical controller method"
                    )

        return (
            controller,
            controller_type,
            operations,
            workspace_witness,
            trust_lock_witness,
        )

    def _registered_controller_locked(self) -> object:
        (
            controller,
            _controller_type,
            _operations,
            _workspace_witness,
            _trust_lock_witness,
        ) = self._registered_controller_record_locked()
        return controller

    def _assert_canonical_controller_surface_locked(
        self,
        controller: object,
    ) -> None:
        """Revalidate the exact construction-time controller surface."""

        registered = self._registered_controller_locked()
        if registered is not controller:
            self._trust_revoked = True
            raise WindowsWebBridgeTrustError(
                "The WebView bridge controller authority changed"
            )

    def _controller_operation_locked(self, controller: object, name: str):
        """Capture one construction-time operation before releasing the trust lock."""

        (
            registered,
            controller_type,
            operations,
            _workspace_witness,
            _trust_lock_witness,
        ) = self._registered_controller_record_locked()
        if registered is not controller:
            self._trust_revoked = True
            raise WindowsWebBridgeTrustError(
                "The WebView bridge controller authority changed"
            )
        if controller_type is not None:
            for method_name, operation in operations:
                if method_name == name:
                    return operation
            self._trust_revoked = True
            raise WindowsWebBridgeTrustError(
                "The WebView bridge requested an unknown canonical controller method"
            )

        operation = getattr(controller, name, None)
        if not callable(operation):
            self._trust_revoked = True
            raise WindowsWebBridgeTrustError(
                "The WebView bridge controller does not expose the required operation"
            )
        return operation

    def _trusted_controller_locked(self) -> AutosportWebController:
        controller = self._registered_controller_locked()
        self._assert_canonical_controller_surface_locked(controller)
        self._assert_trusted_session_locked()
        if self._controller is not controller:
            self._trust_revoked = True
            raise WindowsWebBridgeTrustError(
                "The WebView bridge controller authority changed during trust proof"
            )
        self._assert_canonical_controller_surface_locked(controller)
        return controller

    def _trusted_controller_operation_locked(self, name: str):
        controller = self._trusted_controller_locked()
        return controller, self._controller_operation_locked(controller, name)

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

    def _runtime_witness_path(
        self,
        *,
        _controller_type=_WEB_CONTROLLER_AUTHORITY_CLASS,
    ) -> Path | None:
        """Return a witness path only from the immutable controller authority."""

        with self._trust_lock:
            try:
                controller = self._registered_controller_locked()
            except WindowsWebBridgeTrustError as exc:
                raise WindowsWebBridgeTrustError(
                    "The WebView bridge controller authority changed before runtime witness binding"
                ) from exc
            self._assert_canonical_controller_surface_locked(controller)
            if not isinstance(controller, _controller_type):
                return None
            workspace = getattr(controller, "workspace", None)
            if self._controller is not controller:
                self._trust_revoked = True
                raise WindowsWebBridgeTrustError(
                    "The WebView bridge controller authority changed during runtime witness binding"
                )
            self._assert_canonical_controller_surface_locked(controller)
            if not isinstance(workspace, Path) or not workspace.is_absolute():
                raise WindowsWebBridgeTrustError(
                    "The canonical WebView controller has no absolute workspace identity"
                )
            return workspace / _WEBVIEW2_RUNTIME_WITNESS_FILENAME

    def _revoke_trust(self) -> None:
        with self._trust_lock:
            self._trust_revoked = True

    def _bind_trusted_window(
        self,
        window: object,
        *,
        expected_url: str | None = None,
    ) -> None:
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
            if expected_url is not None and current_url != expected_url:
                self._trust_revoked = True
                raise WindowsWebBridgeTrustError(
                    "The WebView bridge rejected the initial packaged document URL"
                )
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
            _controller, dispatch = self._trusted_controller_operation_locked(
                "dispatch"
            )
        return dispatch(raw)

    def get_state(self) -> dict[str, Any]:
        # State reads may wait for the ordinary controller lock. They must not
        # monopolize the document-trust lock while doing so, otherwise a trusted
        # emergency STOP call could be delayed behind a polling request.
        with self._trust_lock:
            _controller, state = self._trusted_controller_operation_locked("state")
        return {"ok": True, "state": state()}

    def close(self) -> None:
        # Validate the initiating document under the trust lock, then release it
        # while canonical teardown waits. This mirrors dispatch/get_state: a long
        # backend operation must not monopolize the document-trust lane or delay
        # safety/state traffic from the still-visible trusted window.
        with self._trust_lock:
            controller, close = self._trusted_controller_operation_locked("close")
        close()
        with self._trust_lock:
            if self._controller is not controller:
                self._trust_revoked = True
                raise WindowsWebBridgeTrustError(
                    "The WebView bridge controller authority changed during close"
                )
            self._assert_canonical_controller_surface_locked(controller)
            self._host_shutdown = True
            self._trust_revoked = True

    def _close_from_host(self) -> None:
        """Host-only finalizer; intentionally not exposed through pywebview API."""

        with self._trust_lock:
            if self._host_shutdown:
                return
            controller = self._registered_controller_locked()
            close = self._controller_operation_locked(controller, "close")
        close()
        with self._trust_lock:
            if self._controller is not controller:
                self._trust_revoked = True
                raise WindowsWebBridgeTrustError(
                    "The WebView bridge controller authority changed during host close"
                )
            self._assert_canonical_controller_surface_locked(controller)
            self._host_shutdown = True
            self._trust_revoked = True


_WEB_BRIDGE_PUBLIC_METHOD_NAMES = ("close", "dispatch", "get_state")
_WEB_BRIDGE_PUBLIC_METHOD_WITNESSES = tuple(
    (
        name,
        AutosportWebBridge.__dict__[name],
        getattr(AutosportWebBridge.__dict__[name], "__code__", None),
    )
    for name in _WEB_BRIDGE_PUBLIC_METHOD_NAMES
)
_WEB_BRIDGE_HOST_METHOD_NAMES = (
    "_bind_trusted_window",
    "_close_from_host",
    "_registered_controller_record_locked",
    "_revoke_trust",
    "_runtime_witness_path",
)
_WEB_BRIDGE_HOST_METHOD_WITNESSES = tuple(
    (
        name,
        AutosportWebBridge.__dict__[name],
        getattr(AutosportWebBridge.__dict__[name], "__code__", None),
    )
    for name in _WEB_BRIDGE_HOST_METHOD_NAMES
)


def _require_canonical_web_bridge_surface(
    api: object,
    *,
    _names=_WEB_BRIDGE_PUBLIC_METHOD_NAMES,
    _witnesses=_WEB_BRIDGE_PUBLIC_METHOD_WITNESSES,
    _host_names=_WEB_BRIDGE_HOST_METHOD_NAMES,
    _host_witnesses=_WEB_BRIDGE_HOST_METHOD_WITNESSES,
) -> AutosportWebBridge:
    """Reject any JS API object whose callable surface can exceed the product bridge."""

    if (
        type(api) is not AutosportWebBridge
        or _WEB_BRIDGE_PUBLIC_METHOD_NAMES is not _names
        or _WEB_BRIDGE_PUBLIC_METHOD_WITNESSES is not _witnesses
        or _WEB_BRIDGE_HOST_METHOD_NAMES is not _host_names
        or _WEB_BRIDGE_HOST_METHOD_WITNESSES is not _host_witnesses
    ):
        raise WindowsWebViewUnavailable(
            "Autosport refused a non-canonical WebView bridge surface"
        )
    try:
        public_callables = tuple(
            sorted(
                name
                for name in dir(api)
                if not name.startswith("_") and callable(getattr(api, name))
            )
        )
    except Exception as exc:
        raise WindowsWebViewUnavailable(
            "Autosport could not verify the WebView bridge surface"
        ) from exc
    if public_callables != tuple(sorted(_names)):
        raise WindowsWebViewUnavailable(
            "Autosport refused an expanded WebView bridge surface"
        )
    for name, expected, expected_code in _witnesses:
        current = AutosportWebBridge.__dict__.get(name)
        bound = getattr(api, name, None)
        if (
            current is not expected
            or getattr(current, "__code__", None) is not expected_code
            or getattr(bound, "__func__", None) is not expected
        ):
            raise WindowsWebViewUnavailable(
                "Autosport refused a rebound WebView bridge method"
            )
    for name, expected, expected_code in _host_witnesses:
        current = AutosportWebBridge.__dict__.get(name)
        bound = getattr(api, name, None)
        if (
            current is not expected
            or getattr(current, "__code__", None) is not expected_code
            or getattr(bound, "__func__", None) is not expected
        ):
            raise WindowsWebViewUnavailable(
                "Autosport refused a rebound WebView bridge host method"
            )
    return api


def _require_canonical_web_bridge_controller(
    api: AutosportWebBridge,
) -> AutosportWebBridge:
    """Reject test/dummy controller authority at the privileged host boundary."""

    with api._trust_lock:
        try:
            (
                _controller,
                controller_type,
                _operations,
                _workspace_witness,
                _trust_lock_witness,
            ) = api._registered_controller_record_locked()
        except WindowsWebBridgeTrustError as exc:
            raise WindowsWebViewUnavailable(
                "Autosport could not verify the canonical WebView controller authority"
            ) from exc
    if controller_type is None:
        raise WindowsWebViewUnavailable(
            "Autosport refused a non-canonical WebView controller authority"
        )
    return api


def _default_web_bridge() -> AutosportWebBridge:
    """Build the same emergency-STOP-capable bridge used by the packaged entry."""

    from .windows_webview_emergency_stop import EmergencyStopWebController

    return AutosportWebBridge(EmergencyStopWebController())


def launch_windows_shell(
    bridge: AutosportWebBridge | None = None,
    *,
    title: str | None = None,
    storage_path: Path | None = None,
) -> int:
    _reject_webview2_environment_overrides()

    try:
        storage_path = (
            default_webview_storage_path()
            if storage_path is None
            else Path(storage_path)
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise WindowsWebViewUnavailable(
            "Autosport could not resolve its canonical WebView storage path"
        ) from exc
    if not storage_path.is_absolute():
        raise WindowsWebViewUnavailable(
            "Autosport WebView storage path must be absolute"
        )
    _probe_webview_storage_writable(storage_path)

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

    api = _require_canonical_web_bridge_surface(
        _default_web_bridge() if bridge is None else bridge
    )
    api = _require_canonical_web_bridge_controller(api)
    bind_trusted_window = api._bind_trusted_window
    close_from_host = api._close_from_host
    revoke_trust = api._revoke_trust
    runtime_witness_path_for_controller = api._runtime_witness_path
    expected_original_url = str(asset)
    canonical_bridge = True
    required_renderer = "edgechromium"
    renderer_observed = False
    expected_real_url: str | None = None
    trusted_document_observed = not canonical_bridge
    trusted_document_violation = False
    runtime_browser_version: str | None = None
    runtime_identity_violation = False
    runtime_witness_published = False
    runtime_witness_path: Path | None = None
    if canonical_bridge:
        try:
            runtime_witness_path = runtime_witness_path_for_controller()
        except WindowsWebBridgeTrustError as exc:
            raise WindowsWebViewUnavailable(
                "Autosport could not bind the WebView2 runtime witness workspace"
            ) from exc
        if runtime_witness_path is not None:
            try:
                runtime_witness_path.unlink(missing_ok=True)
            except OSError as exc:
                raise WindowsWebViewUnavailable(
                    "Autosport could not invalidate the previous WebView2 runtime witness"
                ) from exc
    window: object | None = None

    def verify_initialized_renderer(renderer: object) -> bool:
        nonlocal expected_real_url, renderer_observed, trusted_document_violation
        if type(renderer) is not str or renderer != required_renderer:
            if canonical_bridge:
                revoke_trust()
            return False
        renderer_observed = True
        if canonical_bridge:
            if window is None:
                revoke_trust()
                trusted_document_violation = True
                return False
            try:
                expected_real_url = _require_packaged_launch_document(
                    window,
                    expected_original_url=expected_original_url,
                )
            except WindowsWebBridgeTrustError:
                revoke_trust()
                trusted_document_violation = True
                return False
        return True

    def bind_trusted_document() -> bool | None:
        nonlocal runtime_browser_version, runtime_identity_violation
        nonlocal runtime_witness_published
        nonlocal trusted_document_observed, trusted_document_violation
        if not canonical_bridge:
            return None
        if not renderer_observed or window is None:
            revoke_trust()
            trusted_document_violation = True
            return False
        if runtime_witness_path is not None:
            try:
                observed_version = _observed_webview2_browser_version(window)
            except WindowsWebViewUnavailable:
                revoke_trust()
                runtime_identity_violation = True
                trusted_document_violation = True
                return False
            if runtime_browser_version is None:
                runtime_browser_version = observed_version
            elif runtime_browser_version != observed_version:
                revoke_trust()
                runtime_identity_violation = True
                trusted_document_violation = True
                return False
        try:
            current_resolved_url = _require_packaged_launch_document(
                window,
                expected_original_url=expected_original_url,
            )
            if (
                expected_real_url is None
                or current_resolved_url != expected_real_url
            ):
                raise WindowsWebBridgeTrustError(
                    "The WebView bridge rejected packaged URL retargeting"
                )
            bind_trusted_window(window, expected_url=expected_real_url)
        except WindowsWebBridgeTrustError:
            revoke_trust()
            trusted_document_violation = True
            return False

        # before_load is the last synchronous host boundary before pywebview
        # injects the privileged Python API into this exact document. Publish the
        # actual native runtime identity here, while the qualified window is live,
        # rather than after webview.start() returns (which normally means the
        # native window has already closed). A crash/kill during a physical NVDA
        # session must not erase the only exact-runtime binding evidence.
        if runtime_witness_path is not None and not runtime_witness_published:
            assert runtime_browser_version is not None
            try:
                _write_webview2_runtime_witness(
                    runtime_witness_path,
                    runtime_browser_version,
                )
            except OSError:
                revoke_trust()
                runtime_identity_violation = True
                trusted_document_violation = True
                return False
            runtime_witness_published = True

        trusted_document_observed = True
        return None

    close_teardown_lock = threading.Lock()
    close_teardown_thread: threading.Thread | None = None
    close_teardown_succeeded = False

    def run_close_teardown() -> None:
        nonlocal close_teardown_thread, close_teardown_succeeded
        try:
            close_from_host()
        except Exception:
            # The controller publishes bounded operator-safe failure state and
            # resets its retry fence. Leave the window open for state readback and
            # a later operator close retry.
            pass
        else:
            with close_teardown_lock:
                close_teardown_succeeded = True
            current_window = window
            if current_window is not None:
                try:
                    # This runs after the blocking FormClosing callback returned.
                    # pywebview marshals destroy() onto the native UI thread; the
                    # second closing callback observes success and allows close.
                    current_window.destroy()
                except Exception:
                    # Canonical backend teardown already completed. If native
                    # destruction fails, keep the inert window available for one
                    # ordinary close retry rather than reviving backend authority.
                    pass
        finally:
            with close_teardown_lock:
                close_teardown_thread = None

    def close_trusted_window_safely() -> bool:
        """Veto native close until canonical teardown completes off the UI thread."""

        nonlocal close_teardown_thread
        if not canonical_bridge:
            return True
        thread_start_failed = False
        with close_teardown_lock:
            if close_teardown_succeeded:
                return True
            if close_teardown_thread is not None:
                return False
            try:
                candidate = threading.Thread(
                    target=run_close_teardown,
                    name="autosport-webview-safe-close",
                    daemon=False,
                )
                close_teardown_thread = candidate
                candidate.start()
            except Exception:
                close_teardown_thread = None
                thread_start_failed = True

        if thread_start_failed:
            # Thread creation/start failure must never turn an exception in the
            # pywebview event callback into implicit permission to close. Fall
            # back to the older synchronous safety path: block the native close
            # until canonical teardown succeeds, or veto it on any failure.
            try:
                close_from_host()
            except Exception:
                return False
            with close_teardown_lock:
                close_teardown_succeeded = True
            return True

        # pywebview's blocking closing event treats False as cancellation. The
        # semantic window therefore stays present while teardown runs.
        return False

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
            # closing is blocking on WinForms. The first close is vetoed quickly;
            # canonical teardown runs off the UI thread and destroys the window
            # only after STOP/worker terminalization succeeds.
            window.events.closing += close_trusted_window_safely
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
        if runtime_witness_path is not None and (
            runtime_browser_version is None
            or runtime_identity_violation
            or not runtime_witness_published
        ):
            raise WindowsWebViewUnavailable(
                "The Autosport semantic shell has no actual WebView2 runtime witness"
            )
    except WindowsWebViewUnavailable:
        try:
            close_from_host()
        except BaseException:
            # Never let teardown replace the already-bounded primary launch failure.
            pass
        raise
    except Exception as exc:
        try:
            close_from_host()
        except BaseException:
            # Preserve the actual startup/runtime failure as the causal root.
            pass
        raise WindowsWebViewUnavailable(
            "Microsoft Edge WebView2 could not start the Autosport semantic shell"
        ) from exc
    except BaseException:
        try:
            close_from_host()
        except BaseException:
            pass
        raise

    try:
        close_from_host()
    except Exception as exc:
        raise WindowsWebViewUnavailable(
            "Autosport could not safely finalize the semantic shell"
        ) from exc
    return 0


def main() -> int:
    return launch_windows_shell()


if __name__ == "__main__":
    raise SystemExit(main())
