"""A3 Plan-6/6Б: request descriptors must close without hiding cancellation.

These portable negative tests exercise all three request descriptor boundaries.
They do not authorize installation or simulate physical Windows/NTFS evidence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from autosport import windows_update_cli as cli


def _request(path: Path) -> None:
    path.write_text(json.dumps({key: "" for key in cli._FIELDS}), encoding="utf-8")


def _faulty_close(monkeypatch):
    real_close = os.close
    closed: list[int] = []

    def close_with_native_failure(fd: int) -> None:
        real_close(fd)
        closed.append(fd)
        raise RuntimeError("PRIVATE_PATH_SHOULD_NOT_LEAK")

    monkeypatch.setattr(cli.os, "close", close_with_native_failure)
    return closed


def test_read_request_close_failure_rejects_without_private_diagnostic(tmp_path, monkeypatch):
    path = tmp_path / "запит з пробілами.json"
    _request(path)
    with monkeypatch.context() as patch:
        closed = _faulty_close(patch)
        with pytest.raises(cli.UpdateCliError) as error:
            cli._read_request(path)
    assert closed
    assert "PRIVATE_PATH" not in str(error.value)
    assert str(path) not in str(error.value)


def test_request_witness_close_failure_rejects_without_private_diagnostic(tmp_path, monkeypatch):
    path = tmp_path / "секретний шлях.json"
    _request(path)
    with monkeypatch.context() as patch:
        closed = _faulty_close(patch)
        with pytest.raises(cli.UpdateCliError) as error:
            cli._request_witness(path)
    assert closed
    assert "PRIVATE_PATH" not in str(error.value)
    assert str(path) not in str(error.value)


def test_read_request_preserves_keyboard_interrupt_when_close_fails(tmp_path, monkeypatch):
    path = tmp_path / "приватний запит.json"
    _request(path)
    with monkeypatch.context() as patch:
        closed = _faulty_close(patch)
        patch.setattr(cli, "_native_change_time_100ns", lambda fd: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            cli._read_request(path)
    assert closed


def test_read_request_preserves_system_exit_when_close_fails(tmp_path, monkeypatch):
    path = tmp_path / "приватний запит.json"
    _request(path)
    with monkeypatch.context() as patch:
        closed = _faulty_close(patch)
        patch.setattr(cli, "_native_change_time_100ns", lambda fd: (_ for _ in ()).throw(SystemExit(3)))
        with pytest.raises(SystemExit) as error:
            cli._read_request(path)
    assert error.value.code == 3
    assert closed


def test_run_never_reports_success_if_request_descriptor_close_fails(tmp_path, monkeypatch):
    path = tmp_path / "оператор.json"
    _request(path)
    with monkeypatch.context() as patch:
        closed = _faulty_close(patch)
        code, report = cli.run(path)
    assert closed
    assert code == 2
    assert report["status"] == "DIAGNOSTIC_REJECTED"
    assert report["install_authorized"] is False
    assert "PRIVATE_PATH" not in str(report)


def test_postparse_descriptor_close_failure_rejects_after_valid_parse(tmp_path, monkeypatch):
    path = tmp_path / "запит.json"
    _request(path)
    real_close = os.close
    closed: list[int] = []

    def fail_second_close(fd: int) -> None:
        real_close(fd)
        closed.append(fd)
        if len(closed) == 2:
            raise ValueError("PRIVATE_PATH_POSTPARSE")

    with monkeypatch.context() as patch:
        patch.setattr(cli.os, "close", fail_second_close)
        with pytest.raises(cli.UpdateCliError) as error:
            cli._read_request(path)
    assert len(closed) == 2
    assert "PRIVATE_PATH" not in str(error.value)


def test_request_witness_preserves_cancellation_during_native_close_error(tmp_path, monkeypatch):
    path = tmp_path / "запит.json"
    _request(path)
    with monkeypatch.context() as patch:
        closed = _faulty_close(patch)
        patch.setattr(cli, "_native_change_time_100ns", lambda fd: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            cli._request_witness(path)
    assert closed


def test_valid_request_recovers_after_prior_descriptor_close_failure(tmp_path, monkeypatch):
    path = tmp_path / "запит.json"
    _request(path)
    with monkeypatch.context() as patch:
        _faulty_close(patch)
        with pytest.raises(cli.UpdateCliError):
            cli._read_request(path)
    parsed, witness = cli._read_request(path)
    assert set(parsed) == cli._FIELDS
    assert witness == cli._request_witness(path)
