from __future__ import annotations

import http.client
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import autosport.trusted_runtime_code_profile as runtime_profile
import test_betfair_supervised_execution as provider_tests
from autosport.betfair_supervised_execution import (
    BetfairSupervisedExecutionError,
    execute_betfair_supervised_action,
)
from autosport.execution_stop_authority import ExecutionStopAuthority
from autosport.real_execution_ledger import AttemptState


_FACTORY_SPEC = "autosport.product_source:create_parlay_product_source"
_PROVIDER_SOURCE_ID = "parlayapi:table_tennis"


@pytest.fixture(autouse=True)
def _fixed_supervised_decision_clock(monkeypatch) -> None:
    """Keep inherited #1212 approval/quote evidence inside its causal decision cut."""

    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: provider_tests.RESERVED_AT,
    )


class _ProfileRuntime:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.manifest = SimpleNamespace(source_id=_PROVIDER_SOURCE_ID)


class _BlockingTransport(provider_tests._Transport):
    def __init__(self) -> None:
        super().__init__(lambda request: provider_tests._response(request))
        self.entered = Event()
        self.release = Event()

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("test did not release admitted provider call")
        return super().post(
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )


def _arm_stop(workspace: Path) -> None:
    authority = ExecutionStopAuthority(workspace / "execution-stop.jsonl")
    stopped = authority.initialize_stopped(
        operator_id="test-owner",
        reason="safe test baseline",
        command_id="trusted-runtime-stop-init",
    )
    authority.arm(
        operator_id="test-owner",
        reason="explicit supervised test authority",
        confirmation_id="trusted-runtime-stop-confirmation",
        expected_revision=stopped.revision,
        command_id="trusted-runtime-stop-arm",
    )


def _issue_profile(monkeypatch, workspace: Path):
    monkeypatch.setattr(runtime_profile, "AutonomousProductRuntime", _ProfileRuntime)
    runtime = _ProfileRuntime(workspace.resolve())
    runtime_profile._register_started_product_runtime_origin(
        runtime,
        source_factory=_FACTORY_SPEC,
        expected_provider_source_id=_PROVIDER_SOURCE_ID,
    )
    profile = runtime_profile.issue_trusted_runtime_code_profile(runtime)
    return runtime, profile


def _prepared_call(workspace: Path, transport):
    profile, bound, approval, ledger, action, goal_store = provider_tests._prepared(
        str(workspace)
    )
    client = provider_tests._enabled_client(
        profile,
        transport,
        store=goal_store,
    )
    return profile, bound, approval, ledger, action, client


def _execute(prepared, *, attempt_id: str):
    profile, bound, approval, ledger, action, client = prepared
    return execute_betfair_supervised_action(
        ledger,
        bound,
        approval,
        action_id=action.action_id,
        attempt_id=attempt_id,
        profile=profile,
        client=client,
        clock=lambda: provider_tests.SUBMITTED_AT,
    )


def test_missing_trusted_runtime_profile_denies_before_submitted_or_provider_io(
    tmp_path: Path,
) -> None:
    _arm_stop(tmp_path)
    transport = provider_tests._Transport(
        lambda request: provider_tests._response(request)
    )
    prepared = _prepared_call(tmp_path, transport)
    ledger = prepared[3]

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="trusted RUNNING product runtime profile is required",
    ):
        _execute(prepared, attempt_id="attempt-missing-runtime-profile")

    assert transport.calls == []
    assert ledger.attempt_state("attempt-missing-runtime-profile") is AttemptState.RESERVED


def test_revoked_trusted_runtime_profile_denies_before_submitted_or_provider_io(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _arm_stop(tmp_path)
    runtime, trusted = _issue_profile(monkeypatch, tmp_path)
    assert runtime_profile.revoke_trusted_runtime_code_profile(trusted) is True
    transport = provider_tests._Transport(
        lambda request: provider_tests._response(request)
    )
    prepared = _prepared_call(tmp_path, transport)
    ledger = prepared[3]
    try:
        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="trusted RUNNING product runtime profile is required",
        ):
            _execute(prepared, attempt_id="attempt-revoked-runtime-profile")
        assert transport.calls == []
        assert (
            ledger.attempt_state("attempt-revoked-runtime-profile")
            is AttemptState.RESERVED
        )
    finally:
        runtime_profile._clear_started_product_runtime_origin(runtime)


def test_other_workspace_profile_cannot_authorize_provider_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "execution"
    other = tmp_path / "other-runtime"
    workspace.mkdir()
    other.mkdir()
    _arm_stop(workspace)
    runtime, trusted = _issue_profile(monkeypatch, other)
    transport = provider_tests._Transport(
        lambda request: provider_tests._response(request)
    )
    prepared = _prepared_call(workspace, transport)
    ledger = prepared[3]
    try:
        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="trusted RUNNING product runtime profile is required",
        ):
            _execute(prepared, attempt_id="attempt-wrong-runtime-workspace")
        assert transport.calls == []
        assert (
            ledger.attempt_state("attempt-wrong-runtime-workspace")
            is AttemptState.RESERVED
        )
    finally:
        runtime_profile.revoke_trusted_runtime_code_profile(trusted)
        runtime_profile._clear_started_product_runtime_origin(runtime)


def test_runtime_profile_resolver_rebind_fails_closed_before_provider_io(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _arm_stop(tmp_path)
    runtime, trusted = _issue_profile(monkeypatch, tmp_path)
    transport = provider_tests._Transport(
        lambda request: provider_tests._response(request)
    )
    prepared = _prepared_call(tmp_path, transport)
    ledger = prepared[3]
    monkeypatch.setattr(
        runtime_profile,
        "require_authoritative_trusted_runtime_code_profile",
        lambda value, *, workspace=None: value,
    )
    try:
        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="trusted runtime profile authority changed",
        ):
            _execute(prepared, attempt_id="attempt-runtime-authority-rebound")
        assert transport.calls == []
        assert (
            ledger.attempt_state("attempt-runtime-authority-rebound")
            is AttemptState.RESERVED
        )
    finally:
        runtime_profile.revoke_trusted_runtime_code_profile(trusted)
        runtime_profile._clear_started_product_runtime_origin(runtime)


def test_admitted_provider_write_linearizes_against_runtime_profile_revocation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(http.client, "HTTPSConnection", provider_tests._TestHTTPSConnection)
    _arm_stop(tmp_path)
    runtime, trusted = _issue_profile(monkeypatch, tmp_path)
    transport = _BlockingTransport()
    prepared = _prepared_call(tmp_path, transport)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            write_future = pool.submit(
                _execute,
                prepared,
                attempt_id="attempt-runtime-profile-race",
            )
            assert transport.entered.wait(timeout=5)
            revoke_future = pool.submit(
                runtime_profile.revoke_trusted_runtime_code_profile,
                trusted,
            )
            assert not revoke_future.done()

            transport.release.set()
            result = write_future.result(timeout=5)
            revoked = revoke_future.result(timeout=5)

        assert result.attempt_id == "attempt-runtime-profile-race"
        assert len(transport.calls) == 1
        assert revoked is True
        assert runtime_profile.is_authoritative_trusted_runtime_code_profile(trusted) is False
    finally:
        transport.release.set()
        runtime_profile.revoke_trusted_runtime_code_profile(trusted)
        runtime_profile._clear_started_product_runtime_origin(runtime)
        provider_tests._ACTIVE_WRITE_TRANSPORT = None
