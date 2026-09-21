from __future__ import annotations

from datetime import timedelta

import pytest

from autosport.campaign_cost_evidence import derive_campaign_economics
from autosport.campaign_economic_authority import FinalizedCampaignAuthority
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
