from __future__ import annotations

import pytest

from autosport.account_readback_degradation import AccountReadbackDegradationEvidence


def test_negative_readback_evidence_cannot_override_hard_false_authority() -> None:
    """Ordinary subclassing must not turn a negative DTO into positive authority."""

    with pytest.raises(
        TypeError,
        match="AccountReadbackDegradationEvidence is a final negative-authority DTO",
    ):

        class ForgedPositiveEvidence(AccountReadbackDegradationEvidence):
            @property
            def freshness_proven(self) -> bool:
                return True

            @property
            def may_authorize_provider_failover(self) -> bool:
                return True

            @property
            def may_authorize_new_stake(self) -> bool:
                return True
