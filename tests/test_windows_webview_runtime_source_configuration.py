from __future__ import annotations

from types import SimpleNamespace

from autosport.windows_webview_emergency_stop import EmergencyStopWebController
from autosport.windows_webview_shell import AutosportWebController


class _EmergencyStopProbe:
    def status(self):
        return SimpleNamespace(
            message_uk="Аварійний STOP доступний.",
            available=True,
            execution_blocked=False,
            mode="clear",
            revision=0,
            integrity_confirmed=True,
            initialized=True,
        )


def _controller() -> EmergencyStopWebController:
    controller = EmergencyStopWebController.__new__(EmergencyStopWebController)
    controller._emergency_stop = _EmergencyStopProbe()
    return controller


def _base_state(*, running: bool = False, can_start: bool = True) -> dict[str, object]:
    return {
        "product_runtime": {
            "status": "Тривалий імітаційний режим не запущено.",
            "running": running,
            "can_start": can_start,
            "can_stop": running,
        }
    }


def test_packaged_runtime_start_is_disabled_when_source_factory_is_missing(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", raising=False)
    monkeypatch.setattr(
        AutosportWebController,
        "state",
        lambda _self: _base_state(),
    )

    state = _controller().state()
    runtime = state["product_runtime"]

    assert runtime["can_start"] is False
    assert "Запуск недоступний" in runtime["status"]
    assert "AUTOSPORT_PRODUCT_SOURCE_FACTORY не задано" in runtime["status"]


def test_packaged_runtime_start_is_disabled_for_ambiguous_source_factory(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PRODUCT_SOURCE_FACTORY",
        " provider.module:factory ",
    )
    monkeypatch.setattr(
        AutosportWebController,
        "state",
        lambda _self: _base_state(),
    )

    state = _controller().state()
    runtime = state["product_runtime"]

    assert runtime["can_start"] is False
    assert "неоднозначний формат" in runtime["status"]
    assert "provider.module:factory" not in runtime["status"]


def test_packaged_runtime_start_remains_actionable_for_trimmed_source_factory(
    monkeypatch,
) -> None:
    configured = "provider.module:factory"
    monkeypatch.setenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", configured)
    monkeypatch.setattr(
        AutosportWebController,
        "state",
        lambda _self: _base_state(),
    )

    state = _controller().state()
    runtime = state["product_runtime"]

    assert runtime["can_start"] is True
    assert runtime["status"] == "Тривалий імітаційний режим не запущено."
    assert configured not in runtime["status"]


def test_running_runtime_keeps_lifecycle_status_if_configuration_changes(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", raising=False)
    monkeypatch.setattr(
        AutosportWebController,
        "state",
        lambda _self: _base_state(running=True, can_start=False),
    )

    state = _controller().state()
    runtime = state["product_runtime"]

    assert runtime["can_start"] is False
    assert runtime["status"] == "Тривалий імітаційний режим не запущено."
