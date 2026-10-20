"""Unexpected backend attribute failures are a sanitized diagnostic rejection.

Malformed plugin/crypto/verifier objects must not print a traceback containing
an operator's private path, and must not grant update/install authority.
"""
from __future__ import annotations

import json

import pytest

from autosport import windows_update_cli as cli


def _request(tmp_path):
    data = {
        "package_path": "оновлення.zip", "expected_package_sha256": "a" * 64,
        "installed_manifest_path": "встановлена.json",
        "expected_installed_manifest_sha256": "b" * 64,
        "signature_hex": "c" * 128, "candidate_version": "1.2.3",
        "candidate_schema": 2, "expected_source_sha": "d" * 40,
        "supported_schema_migrations": [],
    }
    path = tmp_path / "запит.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("failure", [AttributeError, KeyError, ImportError, SyntaxError])
def test_backend_exception_is_sanitized_at_cli_boundary(tmp_path, monkeypatch, capsys, failure):
    path = _request(tmp_path)

    def broken_backend(**_):
        raise failure(f"private path {tmp_path}\\credential")

    monkeypatch.setattr(cli, "preflight_signed_windows_update", broken_backend)
    code = cli.main([str(path)])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err == ""
    assert str(tmp_path) not in captured.out
    assert json.loads(captured.out)["status"] == "DIAGNOSTIC_REJECTED"
    assert json.loads(captured.out)["install_authorized"] is False


def test_preflight_attribute_error_from_package_boundary_is_redacted(tmp_path, monkeypatch):
    from autosport import windows_update_preflight as preflight

    def broken_snapshot(*_args, **_kwargs):
        raise AttributeError(f"private package path {tmp_path}\\session")

    monkeypatch.setattr(preflight, "pinned_windows_update_package", broken_snapshot)
    with pytest.raises(preflight.WindowsUpdatePreflightError) as caught:
        preflight.preflight_signed_windows_update(
            package_path=tmp_path / "candidate.zip",
            expected_package_sha256="a" * 64,
            installed_manifest_path=tmp_path / "state.json",
            expected_installed_manifest_sha256="b" * 64,
            signature=b"x" * 64,
            candidate_version="1.2.3",
            candidate_schema=2,
            expected_source_sha="c" * 40,
            supported_schema_migrations=(),
        )
    assert str(tmp_path) not in str(caught.value)
    assert str(caught.value) == "Windows update preflight rejected"
