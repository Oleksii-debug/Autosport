from __future__ import annotations

import json
import threading
from pathlib import Path

from autosport.operator_source_config import OperatorSourceSelectionState
from autosport.windows_webview_shell import AutosportWebController


_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ID = "parlayapi-table-tennis"
_FACTORY_SPEC = "autosport.product_source:create_parlay_product_source"


class _IdleWorker:
    busy = False

    def poll(self):
        return None


class _FakeProductWorker(_IdleWorker):
    def __init__(self) -> None:
        self.busy = False
        self.start_calls: list[dict[str, object]] = []

    def start(self, **kwargs: object) -> bool:
        self.start_calls.append(dict(kwargs))
        self.busy = True
        return True

    def request_stop(self, _reason: str = "operator_stop") -> bool:
        return self.busy


def _controller(tmp_path: Path) -> AutosportWebController:
    controller = AutosportWebController.__new__(AutosportWebController)
    controller._lock = threading.RLock()
    controller.workspace = tmp_path
    controller._active_workspace = tmp_path
    controller._recovery_required_workspaces = set()
    controller.strategy_id = "baseline-v1"
    controller.research_plan = None
    controller.dataset_worker = _IdleWorker()
    controller.replay_worker = _IdleWorker()
    controller.live_worker = _IdleWorker()
    controller.recovery_worker = _IdleWorker()
    controller.evidence_export_worker = _IdleWorker()
    controller.product_worker = _FakeProductWorker()
    controller.product_runtime_status = "Тривалий імітаційний режим не запущено."
    controller.status = ""
    controller.last_error = ""
    controller.log = []
    return controller


def test_fresh_workspace_is_configuration_required_without_env_syntax(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", raising=False)
    controller = _controller(tmp_path)

    selection, entry = controller._resolve_operator_source()
    projection = controller._product_source_projection(selection, entry)

    assert selection.state is OperatorSourceSelectionState.CONFIGURATION_REQUIRED
    assert entry is None
    assert projection["state"] == "configuration_required"
    assert projection["selected_id"] == ""
    assert projection["choices"] == [
        {"id": _SOURCE_ID, "label": "ParlayAPI — настільний теніс"}
    ]
    assert projection["can_configure"] is True
    assert "Виберіть підтримуване джерело" in projection["status"]
    assert "AUTOSPORT_PRODUCT_SOURCE_FACTORY" not in projection["status"]
    assert controller._product_runtime_can_start(source_ready=False) is False


def test_operator_selection_persists_and_reopens_exactly(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", raising=False)
    controller = _controller(tmp_path)

    result = controller._action_product_source_configure({"source_id": _SOURCE_ID})
    assert result["status"] == "completed"
    assert result["focus_id"] == "product-source-status"

    reopened = _controller(tmp_path)
    selection, entry = reopened._resolve_operator_source()
    assert selection.state is OperatorSourceSelectionState.CONFIGURED
    assert entry is not None
    assert entry.source_id == _SOURCE_ID
    assert entry.factory_spec == _FACTORY_SPEC
    assert selection.runtime_authorized is False
    assert entry.runtime_authorized is False


def test_unknown_operator_source_fails_without_persisting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", raising=False)
    controller = _controller(tmp_path)

    result = controller._action_product_source_configure(
        {"source_id": "unknown-source"}
    )

    assert result["status"] == "rejected"
    assert controller._operator_source_store().read() is None


def test_source_configuration_mutation_is_blocked_while_runtime_busy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", raising=False)
    controller = _controller(tmp_path)
    first = controller._action_product_source_configure({"source_id": _SOURCE_ID})
    assert first["status"] == "completed"
    before = controller._operator_source_store().path.read_bytes()
    controller.product_worker.busy = True

    blocked = controller._action_product_source_configure({"source_id": _SOURCE_ID})

    assert blocked["status"] == "rejected"
    assert controller._operator_source_store().path.read_bytes() == before


def test_runtime_start_rereads_persisted_source_and_uses_closed_registry_factory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", raising=False)
    controller = _controller(tmp_path)
    assert (
        controller._action_product_source_configure({"source_id": _SOURCE_ID})[
            "status"
        ]
        == "completed"
    )

    started = controller._action_product_runtime_start({})

    assert started["status"] == "completed"
    worker = controller.product_worker
    assert isinstance(worker, _FakeProductWorker)
    assert worker.start_calls == [
        {
            "workspace": tmp_path,
            "source_factory": _FACTORY_SPEC,
            "initial_bankroll": "10000",
            "poll_seconds": 30.0,
        }
    ]


def test_exact_registered_factory_env_is_admin_override_not_free_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", _FACTORY_SPEC)
    controller = _controller(tmp_path)

    selection, entry = controller._resolve_operator_source()

    assert selection.state is OperatorSourceSelectionState.ADMIN_OVERRIDE
    assert selection.source_id == _SOURCE_ID
    assert entry is not None
    assert entry.source_id == _SOURCE_ID
    assert controller._operator_source_store().read() is None


def test_unregistered_or_ambiguous_factory_env_fails_closed_without_echo(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw = " provider.module:factory "
    monkeypatch.setenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", raw)
    controller = _controller(tmp_path)

    selection, entry = controller._resolve_operator_source()
    result = controller._action_product_runtime_start({})

    assert selection.state is OperatorSourceSelectionState.INVALID
    assert selection.source_id is None
    assert entry is None
    assert result["status"] == "rejected"
    assert raw not in result["message"]
    assert controller.product_worker.start_calls == []


def test_persisted_and_admin_disagreement_is_explicit_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    controller = _controller(tmp_path)
    controller._operator_source_store().write_source_id("unknown-source")
    monkeypatch.setenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", _FACTORY_SPEC)

    selection, entry = controller._resolve_operator_source()

    assert selection.state is OperatorSourceSelectionState.CONFLICT
    assert selection.source_id is None
    assert entry is None
    assert "конфліктує" in controller._product_source_status(selection, entry)


def test_tampered_persisted_selection_blocks_start_before_worker_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", raising=False)
    controller = _controller(tmp_path)
    assert (
        controller._action_product_source_configure({"source_id": _SOURCE_ID})[
            "status"
        ]
        == "completed"
    )
    path = controller._operator_source_store().path
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source_id"] = "unknown-source"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = controller._action_product_runtime_start({})

    assert result["status"] == "rejected"
    assert controller.product_worker.start_calls == []
    assert _FACTORY_SPEC not in result["message"]


def test_packaged_html_exposes_closed_keyboard_source_configuration() -> None:
    html = (_ROOT / "src" / "autosport" / "windows_web" / "index.html").read_text(
        encoding="utf-8"
    )
    app = (_ROOT / "src" / "autosport" / "windows_web" / "app.js").read_text(
        encoding="utf-8"
    )
    emergency = (
        _ROOT / "src" / "autosport" / "windows_webview_emergency_stop.py"
    ).read_text(encoding="utf-8")

    assert 'id="product-source-select"' in html
    assert 'id="product-source-save"' in html
    assert 'id="product-source-status"' in html
    assert "Джерело даних для тривалої симуляційної роботи" in html
    assert "AUTOSPORT_PRODUCT_SOURCE_FACTORY" not in html
    assert 'dispatch("product_source.configure"' in app
    assert "productSource.choices" in app
    assert "productSource.can_configure" in app
    assert "_product_runtime_source_configuration_error" not in emergency
    assert "AUTOSPORT_PRODUCT_SOURCE_FACTORY" not in emergency
