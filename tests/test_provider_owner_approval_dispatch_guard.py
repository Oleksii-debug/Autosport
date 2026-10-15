from __future__ import annotations

from pathlib import Path

import pytest

import autosport.provider_owner_approval as approval_module
from autosport.provider_owner_approval import OwnerApprovalStore


AUTHORITY_ID = "a" * 64
APPROVAL_SHA = "b" * 64
REFERENCE = "urn:autosport:owner-approval:dispatch-guard"


def _store(tmp_path: Path) -> OwnerApprovalStore:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    return OwnerApprovalStore(
        workspace / "provider-owner-approvals.json",
        authority_root=(tmp_path / "authority").resolve(),
    )


def _resolve(store: OwnerApprovalStore):
    return store.resolve(
        governance_authority_id=AUTHORITY_ID,
        provider_id="betfair",
        service_id="exchange-api",
        owner_approval_reference=REFERENCE,
        owner_approval_sha256=APPROVAL_SHA,
        as_of="2026-09-25T12:00:00Z",
    )


def test_resolve_rejects_resolve_state_rebind_before_forged_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    forged_called = False

    def forged_resolve_state(self, *args, **kwargs):
        nonlocal forged_called
        forged_called = True
        raise AssertionError("forged positive resolver must not run")

    monkeypatch.setattr(OwnerApprovalStore, "_resolve_state", forged_resolve_state)

    with pytest.raises(RuntimeError, match="store dispatch '_resolve_state' changed"):
        _resolve(store)

    assert forged_called is False


def test_resolve_rejects_read_rebind_before_forged_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    forged_called = False

    def forged_read(self):
        nonlocal forged_called
        forged_called = True
        return {
            "schema_version": 1,
            "generation": 0,
            "events": [],
            "state_sha256": None,
        }

    monkeypatch.setattr(OwnerApprovalStore, "_read_locked", forged_read)

    with pytest.raises(RuntimeError, match="store dispatch '_read_locked' changed"):
        _resolve(store)

    assert forged_called is False


def test_resolve_rejects_resolution_issuer_rebind_before_forged_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    forged_called = False

    def forged_resolution(**kwargs):
        nonlocal forged_called
        forged_called = True
        raise AssertionError("forged resolution issuer must not run")

    monkeypatch.setattr(approval_module, "_resolution", forged_resolution)

    with pytest.raises(RuntimeError, match="dependency '_resolution' changed"):
        _resolve(store)

    assert forged_called is False
