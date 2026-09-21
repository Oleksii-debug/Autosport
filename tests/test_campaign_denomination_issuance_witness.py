from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from autosport.campaign_economic_authority import CampaignEconomicAuthorityError
from autosport.integrity import atomic_write_json
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
