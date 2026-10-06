from __future__ import annotations

import os
from pathlib import Path

import pytest

import autosport.incident_risk_store as incident_risk_store
from autosport.incident_risk_store import IncidentRiskStoreError


def test_post_read_path_replacement_requires_same_file_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "incident_model_risk_store.json"
    replacement = tmp_path / "replacement.json"
    payload = b'{"probe":"original"}'
    path.write_bytes(payload)
    replacement.write_bytes(payload)

    real_read = incident_risk_store.os.read
    replaced = False

    def replace_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = real_read(descriptor, size)
        if chunk and not replaced:
            replaced = True
            os.replace(replacement, path)
        return chunk

    monkeypatch.setattr(incident_risk_store.os, "read", replace_after_first_read)
    monkeypatch.setattr(
        incident_risk_store,
        "_stable_stat_metadata",
        lambda _left, _right: True,
    )

    with pytest.raises(IncidentRiskStoreError, match="path changed during read"):
        incident_risk_store._read_stable_store_text(path)

    assert replaced
