from __future__ import annotations

from datetime import timedelta

import pytest

from autosport.campaign_cost_evidence import derive_campaign_economics
from autosport.campaign_economic_authority import FinalizedCampaignAuthority
from autosport.scientific_registry import ScientificRegistry
from test_campaign_denomination_runtime import (
    _fixture_authority_with_goal,
    _goal,
    _remove_persisted_binding_for_test,
)


def test_transitive_denomination_dispatch_monkeypatches_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, authority = _fixture_authority_with_goal(_goal())
    try:
        issued = authority.denomination_binding()
        assert issued is not None
        _remove_persisted_binding_for_test(authority)

        attacks = (
            (
                "_persisted_payload",
                lambda self: dict(issued.canonical_payload),
            ),
            (
                "_derive_binding",
                lambda self, *, available_at: issued,
            ),
        )
        for method_name, replacement in attacks:
            with monkeypatch.context() as isolated:
                isolated.setattr(
                    FinalizedCampaignAuthority,
                    method_name,
                    replacement,
                )
                version = derive_campaign_economics(
                    campaign=authority,
                    costs=(),
                    as_of=issued.available_at + timedelta(microseconds=1),
                )
                assert version.denomination_binding is None
                assert (
                    "MISSING_CAMPAIGN_CURRENCY_AUTHORITY"
                    in version.incomplete_reasons
                )
    finally:
        fixture.doCleanups()

def test_scientific_registry_read_rebinding_cannot_supply_denomination_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, authority = _fixture_authority_with_goal(_goal())
    try:
        issued = authority.denomination_binding()
        assert issued is not None
        registry = authority._registry()
        forged_state = registry._read()
        assert (
            forged_state["campaign_denomination_bindings"][
                authority._binding_key()
            ]
            == dict(issued.canonical_payload)
        )

        _remove_persisted_binding_for_test(authority)

        attacker_reads = 0

        def attacker_read(self):
            nonlocal attacker_reads
            attacker_reads += 1
            return forged_state

        monkeypatch.setattr(ScientificRegistry, "_read", attacker_read)

        version = derive_campaign_economics(
            campaign=authority,
            costs=(),
            as_of=issued.available_at + timedelta(microseconds=1),
        )

        # Projection validation can encounter the forged state, but the
        # denomination boundary must independently revalidate the executable
        # ScientificRegistry read authority before consuming persisted binding
        # bytes. The forged reader therefore cannot become denomination authority.
        assert attacker_reads > 0
        assert version.denomination_binding is None
        assert "MISSING_CAMPAIGN_CURRENCY_AUTHORITY" in version.incomplete_reasons
    finally:
        fixture.doCleanups()

