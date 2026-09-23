from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

import autosport.betdaq_account_readonly as account_module
import autosport.betdaq_heartbeat_safety as heartbeat_module
from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqCredentials,
)
from autosport.betdaq_heartbeat_safety import (
    BetdaqHeartbeatSafetyController,
    BetdaqHeartbeatSafetyError,
    BetdaqHeartbeatSafetyStore,
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


class _ProviderQueue:
    def __init__(self, payloads: list[bytes | BaseException]) -> None:
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, request, *, timeout: float):
        del timeout
        self.requests.append(request)
        if not self.payloads:
            raise AssertionError("unexpected provider call")
        item = self.payloads.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _Response(item)


class _Clock:
    def __init__(self) -> None:
        self.second = 0

    def __call__(self) -> datetime:
        value = datetime(2026, 9, 23, 0, 0, self.second, tzinfo=timezone.utc)
        self.second += 1
        return value


def _soap(
    method: str,
    *,
    code: int | None = 0,
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
    if code is not None:
        ET.SubElement(
            result,
            f"{{{EXTERNAL}}}ReturnStatus",
            {"Code": str(code), "Description": "provider-status"},
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
        confirmation_id="confirm-arm-1",
        expected_revision=initial.revision,
        command_id="arm-1",
    )
    return stop


def _client(clock: _Clock) -> BetdaqAccountReadOnlyClient:
    return BetdaqAccountReadOnlyClient(
        BetdaqCredentials(
            username="alice",
            password="secret-password",
            application_identifier="app-secret",
        ),
        clock=clock,
    )


def _controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _ProviderQueue,
    *,
    clock: _Clock | None = None,
    stop: ExecutionStopAuthority | None = None,
) -> tuple[BetdaqHeartbeatSafetyController, ExecutionStopAuthority, _Clock]:
    resolved_clock = clock or _Clock()
    monkeypatch.setattr(account_module, "urlopen", provider)
    resolved_stop = stop or _armed_stop(tmp_path / "stop.jsonl")
    controller = BetdaqHeartbeatSafetyController(
        account_client=_client(resolved_clock),
        stop_authority=resolved_stop,
        state_path=tmp_path / "heartbeat.json",
    )
    return controller, resolved_stop, resolved_clock


def test_register_uses_exact_secure_contract_and_never_mints_write_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProviderQueue([_soap("RegisterHeartbeat")])
    controller, _, _ = _controller(tmp_path, monkeypatch, provider)
    try:
        event = controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.CANCEL_ORDERS,
        )
        assert event.state is HeartbeatState.ACTIVE
        assert event.threshold_ms == 6000
        assert event.registered_action is HeartbeatAction.CANCEL_ORDERS
        assert event.execution_write_authorized is False
        assert event.real_money_execution_authorized is False
        assert event.cross_session_equivalence_proven is False
        assert event.external_pulse_masking_possible is True

        request = provider.requests[0]
        assert request.full_url == (
            "https://api.betdaq.com/v2.0/Secure/SecureService.asmx"
        )
        assert request.get_method() == "POST"
        root = ET.fromstring(request.data)
        node = root.find(
            f".//{{{EXTERNAL}}}registerHeartbeatRequest"
        )
        assert node is not None
        assert node.attrib == {
            "ThresholdMs": "6000",
            "HeartbeatAction": "1",
        }
    finally:
        controller.close()


def test_normal_pulse_is_causal_active_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProviderQueue(
        [
            _soap("RegisterHeartbeat"),
            _soap(
                "Pulse",
                performed_at="2026-09-23T00:00:02Z",
                action=0,
            ),
        ]
    )
    controller, _, _ = _controller(tmp_path, monkeypatch, provider)
    try:
        registered = controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.SUSPEND_ORDERS,
        )
        pulsed = controller.pulse()
        assert pulsed.generation_id == registered.generation_id
        assert pulsed.state is HeartbeatState.ACTIVE
        assert pulsed.provider_performed_at == "2026-09-23T00:00:02Z"
        assert pulsed.performed_action is None
        assert pulsed.reconciliation_required is False
        assert controller.status().provider_registration_active is True
        assert controller.status().exclusive_account_heartbeat_owner_proven is False
    finally:
        controller.close()


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (HeartbeatAction.CANCEL_ORDERS, 1),
        (HeartbeatAction.SUSPEND_ORDERS, 2),
        (HeartbeatAction.SUSPEND_PUNTER, 3),
    ],
)
def test_provider_safety_action_forces_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: HeartbeatAction,
    code: int,
) -> None:
    provider = _ProviderQueue(
        [
            _soap("RegisterHeartbeat"),
            _soap(
                "Pulse",
                performed_at="2026-09-23T00:00:02Z",
                action=code,
            ),
        ]
    )
    controller, _, _ = _controller(tmp_path, monkeypatch, provider)
    try:
        controller.register(threshold_ms=6000, action=action)
        event = controller.pulse()
        assert event.state is HeartbeatState.RECONCILIATION_REQUIRED
        assert event.performed_action is action
        assert event.reconciliation_required is True
        assert controller.status().provider_registration_active is True
    finally:
        controller.close()


def test_provider_restart_code_462_revokes_positive_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProviderQueue(
        [
            _soap("RegisterHeartbeat"),
            _soap("Pulse", code=462),
        ]
    )
    controller, _, _ = _controller(tmp_path, monkeypatch, provider)
    try:
        controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.CANCEL_ORDERS,
        )
        event = controller.pulse()
        assert event.state is HeartbeatState.LOST_PROVIDER_REGISTRATION
        assert event.provider_return_code == 462
        assert event.reconciliation_required is True
        assert controller.status().provider_registration_active is False
        with pytest.raises(
            BetdaqHeartbeatSafetyError,
            match="not currently proven",
        ):
            controller.pulse()
    finally:
        controller.close()


def test_stop_fence_revokes_and_prevents_reregister_or_pulse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProviderQueue([_soap("RegisterHeartbeat")])
    controller, stop, _ = _controller(tmp_path, monkeypatch, provider)
    try:
        controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.CANCEL_ORDERS,
        )
        current = stop.current()
        stop.stop(
            operator_id="operator",
            reason="irreversible operator stop",
            expected_revision=current.revision,
            command_id="stop-2",
        )
        status = controller.status()
        assert status.event is not None
        assert status.event.state is HeartbeatState.REVOKED
        assert status.reconciliation_required is True
        assert status.provider_registration_active is False
        with pytest.raises(BetdaqHeartbeatSafetyError, match="STOP"):
            controller.register(
                threshold_ms=6000,
                action=HeartbeatAction.CANCEL_ORDERS,
            )
        with pytest.raises(BetdaqHeartbeatSafetyError, match="STOP"):
            controller.pulse()
        assert len(provider.requests) == 1
    finally:
        controller.close()


def test_change_registration_creates_new_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProviderQueue(
        [
            _soap("RegisterHeartbeat"),
            _soap("ChangeHeartbeatRegistration"),
        ]
    )
    controller, _, _ = _controller(tmp_path, monkeypatch, provider)
    try:
        first = controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.CANCEL_ORDERS,
        )
        second = controller.change_registration(
            threshold_ms=7000,
            action=HeartbeatAction.SUSPEND_ORDERS,
        )
        assert second.generation_id != first.generation_id
        assert second.predecessor_generation_id == first.generation_id
        assert second.threshold_ms == 7000
        assert second.registered_action is HeartbeatAction.SUSPEND_ORDERS
    finally:
        controller.close()


def test_unknown_or_conflicting_pulse_action_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProviderQueue(
        [
            _soap("RegisterHeartbeat"),
            _soap(
                "Pulse",
                performed_at="2026-09-23T00:00:02Z",
                action=2,
            ),
        ]
    )
    controller, _, _ = _controller(tmp_path, monkeypatch, provider)
    try:
        controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.CANCEL_ORDERS,
        )
        event = controller.pulse()
        assert event.state is HeartbeatState.DEGRADED_UNKNOWN
        assert event.reconciliation_required is True
        assert controller.status().provider_registration_active is False
    finally:
        controller.close()


def test_identical_provider_pulse_is_idempotent_but_conflict_is_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse = _soap(
        "Pulse",
        performed_at="2026-09-23T00:00:02Z",
        action=0,
    )
    provider = _ProviderQueue(
        [
            _soap("RegisterHeartbeat"),
            pulse,
            pulse,
            _soap(
                "Pulse",
                performed_at="2026-09-23T00:00:02Z",
                action=1,
            ),
        ]
    )
    controller, _, _ = _controller(tmp_path, monkeypatch, provider)
    try:
        controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.CANCEL_ORDERS,
        )
        first = controller.pulse()
        duplicate = controller.pulse()
        assert duplicate.event_sha256 == first.event_sha256
        assert len(BetdaqHeartbeatSafetyStore(
            tmp_path / "heartbeat.json"
        ).history()) == 2

        conflict = controller.pulse()
        assert conflict.state is HeartbeatState.DEGRADED_UNKNOWN
        assert conflict.reconciliation_required is True
    finally:
        controller.close()


def test_out_of_order_pulse_cannot_overwrite_newer_provider_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProviderQueue(
        [
            _soap("RegisterHeartbeat"),
            _soap(
                "Pulse",
                performed_at="2026-09-23T00:00:05Z",
                action=0,
            ),
            _soap(
                "Pulse",
                performed_at="2026-09-23T00:00:04Z",
                action=0,
            ),
        ]
    )
    controller, _, _ = _controller(tmp_path, monkeypatch, provider)
    try:
        controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.CANCEL_ORDERS,
        )
        assert controller.pulse().state is HeartbeatState.ACTIVE
        assert controller.pulse().state is HeartbeatState.DEGRADED_UNKNOWN
    finally:
        controller.close()


def test_transport_exception_never_leaks_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProviderQueue(
        [RuntimeError("secret-password app-secret alice")]
    )
    controller, _, _ = _controller(tmp_path, monkeypatch, provider)
    try:
        event = controller.register(
            threshold_ms=6000,
            action=HeartbeatAction.CANCEL_ORDERS,
        )
        assert event.state is HeartbeatState.DEGRADED_UNKNOWN
        raw = (tmp_path / "heartbeat.json").read_text(encoding="utf-8")
        assert "secret-password" not in raw
        assert "app-secret" not in raw
        assert "alice" not in raw
    finally:
        controller.close()


def test_process_lease_rejects_parallel_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProviderQueue([])
    first, _, clock = _controller(tmp_path, monkeypatch, provider)
    try:
        with pytest.raises(
            BetdaqHeartbeatSafetyError,
            match="another process owns",
        ):
            BetdaqHeartbeatSafetyController(
                account_client=_client(clock),
                stop_authority=_armed_stop(tmp_path / "other-stop.jsonl"),
                state_path=tmp_path / "heartbeat.json",
            )
    finally:
        first.close()


def test_restart_fence_never_restores_remote_active_from_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProviderQueue([_soap("RegisterHeartbeat")])
    first, stop, clock = _controller(tmp_path, monkeypatch, provider)
    registered = first.register(
        threshold_ms=6000,
        action=HeartbeatAction.CANCEL_ORDERS,
    )

    first._lease.close()
    first._closed = True
    monkeypatch.setattr(
        heartbeat_module,
        "_PROCESS_INSTANCE_ID",
        "f" * 64,
    )

    second = BetdaqHeartbeatSafetyController(
        account_client=_client(clock),
        stop_authority=stop,
        state_path=tmp_path / "heartbeat.json",
    )
    try:
        status = second.status()
        assert status.event is not None
        assert status.event.state is HeartbeatState.RESTART_FENCED
        assert status.event.generation_id == registered.generation_id
        assert status.provider_registration_active is False
        assert status.reconciliation_required is True
    finally:
        second.close()


def test_clean_deregister_is_not_reinterpreted_as_active_after_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProviderQueue(
        [
            _soap("RegisterHeartbeat"),
            _soap("DeregisterHeartbeat"),
        ]
    )
    first, stop, clock = _controller(tmp_path, monkeypatch, provider)
    first.register(
        threshold_ms=6000,
        action=HeartbeatAction.SUSPEND_ORDERS,
    )
    deregistered = first.deregister()
    assert deregistered.state is HeartbeatState.UNREGISTERED
    first.close()

    second = BetdaqHeartbeatSafetyController(
        account_client=_client(clock),
        stop_authority=stop,
        state_path=tmp_path / "heartbeat.json",
    )
    try:
        status = second.status()
        assert status.event is not None
        assert status.event.state is HeartbeatState.UNREGISTERED
        assert status.provider_registration_active is False
    finally:
        second.close()


def test_state_and_anchor_tamper_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProviderQueue([_soap("RegisterHeartbeat")])
    controller, _, _ = _controller(tmp_path, monkeypatch, provider)
    controller.register(
        threshold_ms=6000,
        action=HeartbeatAction.CANCEL_ORDERS,
    )
    controller.close()

    anchor = tmp_path / "heartbeat.json.anchor.json"
    raw = anchor.read_text(encoding="utf-8")
    anchor.write_text(raw.replace('"sequence":2', '"sequence":1'), encoding="utf-8")
    with pytest.raises(BetdaqHeartbeatSafetyError, match="anchor"):
        BetdaqHeartbeatSafetyStore(tmp_path / "heartbeat.json").history()
