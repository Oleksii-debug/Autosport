from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
import os

import pytest

from autosport.campaign_economic_authority import (
    CampaignEconomicAuthorityError,
    FinalizedCampaignAuthority,
)
from autosport.integrity import atomic_write_json
from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
from autosport.workspace_lock import WorkspaceEconomicLock
from test_campaign_denomination_runtime import (
    _fixture_authority_with_goal,
    _goal,
    _remove_persisted_binding_for_test,
)


def test_caller_written_exact_shape_binding_without_matching_issuance_witness_rejects() -> None:
    fixture, authority = _fixture_authority_with_goal(_goal())
    try:
        issued = authority.denomination_binding()
        assert issued is not None
        registry = authority._registry()
        key = authority._binding_key()
        forged = replace(
            issued,
            available_at=issued.available_at + timedelta(seconds=1),
            binding_id="",
        )

        with WorkspaceEconomicLock(registry.path.parent):
            state = registry._read()
            bindings = state.get("campaign_denomination_bindings")
            assert type(bindings) is dict
            bindings[key] = dict(forged.canonical_payload)
            atomic_write_json(registry.path, state)

        with pytest.raises(
            CampaignEconomicAuthorityError,
            match="not backed by its issuance witness|timestamp is not issuance-witnessed",
        ):
            authority.denomination_binding()
    finally:
        fixture.doCleanups()


def test_witness_before_registry_crash_prefix_is_fail_closed_then_recoverable() -> None:
    fixture, authority = _fixture_authority_with_goal(_goal())
    try:
        issued = authority.denomination_binding()
        assert issued is not None
        _remove_persisted_binding_for_test(authority)

        assert authority.denomination_binding() is None
        assert authority.issue_denomination_binding() == issued
        assert authority.denomination_binding() == issued
    finally:
        fixture.doCleanups()


def test_supported_witness_root_redirect_after_cache_loss_reuses_pinned_ancestry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_a = tmp_path / "denomination-authority-a"
    root_b = tmp_path / "denomination-authority-b"
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", str(root_a))

    fixture, authority = _fixture_authority_with_goal(_goal())
    try:
        issued = authority.denomination_binding()
        assert issued is not None
        assert list(root_a.glob("*.campaign-denomination-issuance.monotonic-witness.jsonl"))

        # Model the supported crash-prefix state already exercised above: the
        # independent issuance ancestry survives while the co-located cache row does
        # not. A later supported configuration change must not create ancestry B.
        _remove_persisted_binding_for_test(authority)
        assert authority.denomination_binding() is None
        monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", str(root_b))

        restarted = FinalizedCampaignAuthority(authority._campaign)
        assert restarted.issue_denomination_binding() == issued
        assert restarted.denomination_binding() == issued
        assert not list(
            root_b.glob("*.campaign-denomination-issuance.monotonic-witness.jsonl")
        )
    finally:
        fixture.doCleanups()


def test_denomination_registry_extensions_reject_rollback_to_preissuance_image(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "denomination-authority"
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", str(root))

    fixture, authority = _fixture_authority_with_goal(_goal())
    try:
        assert authority.denomination_binding() is not None
        registry = authority._registry()
        current = registry._read()
        assert "campaign_denomination_bindings" in current
        assert "campaign_denomination_witness_root" in current

        # Reconstruct the exact canonical pre-denomination ScientificRegistry image.
        # Without extension-aware monotonic publication this old image matches the
        # stale machine-authority tip and is silently accepted after direct rollback.
        rolled_back = dict(current)
        rolled_back.pop("campaign_denomination_bindings")
        rolled_back.pop("campaign_denomination_witness_root")
        encoded = (
            json.dumps(
                rolled_back,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        with registry.path.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

        with pytest.raises(
            MonotonicAuthorityRollbackError,
            match="observed workspace state does not match latest committed authority state",
        ):
            registry._read()
    finally:
        fixture.doCleanups()
