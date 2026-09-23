from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

import autosport.betdaq_account_readonly as account_module
from autosport.betdaq_account_readonly import BetdaqAccountReadOnlyClient, BetdaqCredentials
from autosport.betdaq_heartbeat_safety import (
    BetdaqHeartbeatSafetyController,
    HeartbeatAction,
    HeartbeatState,
)
from autosport.execution_stop_authority import ExecutionStopAuthority


EXTERNAL = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb

    def read(self) -> bytes:
        return self.payload


class _Clock:
    def __init__(self) -> None:
        self.second = 0

    def __call__(self) -> datetime:
        value = datetime(2026, 9, 23, 11, 41, self.second, tzinfo=timezone.utc)
        self.second += 1
        return value


def _soap(
    method: str,
    *,
    performed_at: str | None = None,
    action: int | None = None,
) -> bytes:
    envelope = ET.Element(f"{{{SOAP}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP}}}Body")
    response = ET.SubElement(body, f"{{{EXTERNAL}}}{method}Response")
    result = ET.SubElement(response, f"{{{EXTERNAL}}}{method}Result")
    if performed_at is not None:
        result.set("PerformedAt", performed_at)
    if action is not None:
        result.set("HeartbeatAction", str(action))
    ET.SubElement(
        result,
        f"{{{EXTERNAL}}}ReturnStatus",
        {"Code": "0", "Description": "success"},
    )
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def _armed_stop(path: Path) -> ExecutionStopAuthority:
    stop = ExecutionStopAuthority(path)
    initial = stop.initialize_stopped(
        operator_id="test",
        reason="safe default",
        command_id="initial-stop",
    )
    stop.arm(
        operator_id="test",
        reason="explicit test arm",
        confirmation_id="confirm-arm",
        expected_revision=initial.revision,
        command_id="arm",
    )
    return stop


class _StopDuringProviderCall:
    def __init__(
        self,
        payloads: list[bytes],
        *,
        stop: ExecutionStopAuthority,
        stop_on_call: int,
        rearm_after_stop: bool = False,
    ) -> None:
        self.payloads = list(payloads)
        self.stop = stop
        self.stop_on_call = stop_on_call
        self.rearm_after_stop = rearm_after_stop
        self.calls = 0

    def __call__(self, request, *, timeout: float):
        del request, timeout
        self.calls += 1
        if self.calls == self.stop_on_call:
            stopped = self.stop.stop(
                operator_id="test",
                reason="irreversible STOP while heartbeat provider call is in flight",
                command_id=f"race-stop-{self.calls}",
            )
            if self.rearm_after_stop:
                self.stop.arm(
                    operator_id="test",
                    reason="explicit re-arm after in-flight STOP",
                    confirmation_id=f"race-rearm-confirm-{self.calls}",
                    expected_revision=stopped.revision,
                    command_id=f"race-rearm-{self.calls}",
                )
        if not self.payloads:
            raise AssertionError("unexpected provider call")
        return _Response(self.payloads.pop(0))


def _controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _StopDuringProviderCall,
    stop: ExecutionStopAuthority,
) -> BetdaqHeartbeatSafetyController:
    monkeypatch.setattr(account_module, "urlopen", provider)
    client = BetdaqAccountReadOnlyClient(
        BetdaqCredentials(
            username="alice",
            password="secret-password",
            application_identifier="app-secret",
        ),
        clock=_Clock(),
    )
    return BetdaqHeartbeatSafetyController(
        account_client=client,
        stop_authority=stop,
        state_path=tmp_path / "heartbeat.json",
    )


def test_register_success_cannot_publish_active_after_concurrent_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = _armed_stop(tmp_path / "stop.jsonl")
    provider = _StopDuringProviderCall(
        [_soap("RegisterHeartbeat")],
        stop=stop,
        stop_on_call=1,
    )
    controller = _controller(tmp_path, monkeypatch, provider, stop)
    try:
        event = controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.CANCEL_ORDERS,
        )
        assert event.state is HeartbeatState.REVOKED
        assert event.operation == "RegisterHeartbeat:POST_CALL_STOP_FENCE"
        assert event.provider_return_code == 0
        assert event.reconciliation_required is True
        assert controller.status().provider_registration_active is False
    finally:
        controller.close()


def test_pulse_success_cannot_republish_active_after_concurrent_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = _armed_stop(tmp_path / "stop.jsonl")
    provider = _StopDuringProviderCall(
        [
            _soap("RegisterHeartbeat"),
            _soap(
                "Pulse",
                performed_at="2026-09-23T11:41:03Z",
                action=0,
            ),
        ],
        stop=stop,
        stop_on_call=2,
    )
    controller = _controller(tmp_path, monkeypatch, provider, stop)
    try:
        registered = controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.SUSPEND_ORDERS,
        )
        assert registered.state is HeartbeatState.ACTIVE

        event = controller.pulse()
        assert event.state is HeartbeatState.REVOKED
        assert event.operation == "Pulse:POST_CALL_STOP_FENCE"
        assert event.provider_return_code == 0
        assert event.provider_performed_at == "2026-09-23T11:41:03Z"
        assert event.reconciliation_required is True
        assert controller.status().provider_registration_active is False
    finally:
        controller.close()

def test_change_registration_success_cannot_publish_active_after_concurrent_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = _armed_stop(tmp_path / "stop.jsonl")
    provider = _StopDuringProviderCall(
        [
            _soap("RegisterHeartbeat"),
            _soap("ChangeHeartbeatRegistration"),
        ],
        stop=stop,
        stop_on_call=2,
    )
    controller = _controller(tmp_path, monkeypatch, provider, stop)
    try:
        registered = controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.CANCEL_ORDERS,
        )
        assert registered.state is HeartbeatState.ACTIVE

        event = controller.change_registration(
            threshold_ms=7000,
            action=HeartbeatAction.SUSPEND_ORDERS,
        )
        assert event.state is HeartbeatState.REVOKED
        assert (
            event.operation
            == "ChangeHeartbeatRegistration:POST_CALL_STOP_FENCE"
        )
        assert event.provider_return_code == 0
        assert event.reconciliation_required is True
        assert controller.status().provider_registration_active is False
    finally:
        controller.close()


def test_stop_then_rearm_during_pulse_cannot_restore_positive_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = _armed_stop(tmp_path / "stop.jsonl")
    provider = _StopDuringProviderCall(
        [
            _soap("RegisterHeartbeat"),
            _soap(
                "Pulse",
                performed_at="2026-09-23T11:41:03Z",
                action=0,
            ),
        ],
        stop=stop,
        stop_on_call=2,
        rearm_after_stop=True,
    )
    controller = _controller(tmp_path, monkeypatch, provider, stop)
    try:
        registered = controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.SUSPEND_ORDERS,
        )
        assert registered.state is HeartbeatState.ACTIVE
        assert stop.current().revision == 2

        event = controller.pulse()

        assert stop.current().mode.value == "ARMED"
        assert stop.current().revision == 4
        assert event.state is HeartbeatState.REVOKED
        assert event.operation == "Pulse:POST_CALL_STOP_FENCE"
        assert event.provider_return_code == 0
        assert event.provider_performed_at == "2026-09-23T11:41:03Z"
        assert event.reconciliation_required is True
        assert controller.status().provider_registration_active is False
    finally:
        controller.close()

