from __future__ import annotations

from pathlib import Path

import pytest

import autosport.run_registry as run_registry
from autosport.run_registry import RunRegistry


def test_first_open_rejects_redirected_zero_byte_decision_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / "decisions.jsonl"
    ledger_path.write_bytes(b"")
    redirected_path = tmp_path / "redirected-empty-ledger"
    redirected_path.write_bytes(b"")

    real_open = run_registry._open_read_only_descriptor
    ledger_open_calls = 0

    def redirect_first_ledger_descriptor(path: Path) -> int:
        nonlocal ledger_open_calls
        if path == ledger_path:
            ledger_open_calls += 1
            if ledger_open_calls == 1:
                return real_open(redirected_path)
        return real_open(path)

    monkeypatch.setattr(
        run_registry,
        "_open_read_only_descriptor",
        redirect_first_ledger_descriptor,
    )

    registry_path = tmp_path / "run_registry.json"
    with pytest.raises(ValueError, match="missing while durable run history exists"):
        RunRegistry.initialize_pristine(registry_path)

    assert ledger_open_calls >= 2
    assert not registry_path.exists()
    assert ledger_path.read_bytes() == b""
    assert redirected_path.read_bytes() == b""
