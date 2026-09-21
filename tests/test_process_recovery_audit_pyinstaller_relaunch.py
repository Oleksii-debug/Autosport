from __future__ import annotations

import json
import os
import signal

import pytest

import autosport.process_recovery_audit as recovery_audit


class _StopAfterSpawn(RuntimeError):
    pass


def test_frozen_process_kill_stage_requests_independent_pyinstaller_instance(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(recovery_audit.sys, "frozen", True, raising=False)
    monkeypatch.delenv("PYINSTALLER_RESET_ENVIRONMENT", raising=False)
    captured: dict[str, object] = {}

    def fail_after_spawn(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        raise _StopAfterSpawn

    monkeypatch.setattr(recovery_audit.subprocess, "Popen", fail_after_spawn)

    with pytest.raises(_StopAfterSpawn):
        recovery_audit.audit_process_kill_relaunch(tmp_path)

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in os.environ


def test_frozen_process_kill_recovery_requests_independent_pyinstaller_instance(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(recovery_audit.sys, "frozen", True, raising=False)
    monkeypatch.delenv("PYINSTALLER_RESET_ENVIRONMENT", raising=False)
    captured: dict[str, object] = {}
    fake_stage_pid = os.getpid() + 100_000

    class FakeStage:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, *, timeout):
            self.returncode = int(signal.SIGTERM)
            return self.returncode

        def kill(self):
            self.returncode = -1

    def fake_popen(command, **kwargs):
        captured["stage_env"] = kwargs.get("env")
        ready_path = recovery_audit.Path(command[-1])
        ready_path.parent.mkdir(parents=True, exist_ok=True)
        ready_path.write_text(
            json.dumps(
                {
                    "status": recovery_audit._READY_STATUS,
                    "pid": fake_stage_pid,
                    "run_id": recovery_audit._PROCESS_RUN_ID,
                    "experiment_key": "test-experiment",
                    "transaction_phase": "precommitted",
                    "real_money_execution": False,
                }
            ),
            encoding="utf-8",
        )
        return FakeStage()

    def fail_recovery_after_spawn(command, **kwargs):
        captured["recovery_env"] = kwargs.get("env")
        raise _StopAfterSpawn

    monkeypatch.setattr(recovery_audit.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(recovery_audit.subprocess, "run", fail_recovery_after_spawn)
    monkeypatch.setattr(recovery_audit.os, "kill", lambda pid, sig: None)

    with pytest.raises(_StopAfterSpawn):
        recovery_audit.audit_process_kill_relaunch(tmp_path)

    stage_environment = captured["stage_env"]
    recovery_environment = captured["recovery_env"]
    assert isinstance(stage_environment, dict)
    assert stage_environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert isinstance(recovery_environment, dict)
    assert recovery_environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in os.environ
