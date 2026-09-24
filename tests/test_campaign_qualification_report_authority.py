from __future__ import annotations

import pytest

from autosport.campaign_qualification import (
    CampaignQualificationReport,
    QualificationState,
)


@pytest.mark.parametrize(
    "positive_field",
    (
        "canonical_authority_verified",
        "promotion_authority",
        "readiness_authority",
        "real_money_execution",
        "human_tested",
        "nvda_verified",
        "whole_product_complete",
    ),
)
def test_direct_report_construction_cannot_mint_campaign_authority(
    positive_field: str,
) -> None:
    kwargs = {
        "state": QualificationState.COMPLETE_FOR_CANONICAL_RESOLUTION,
        "blockers": (),
        "identity_sha256": "a" * 64,
        "expected_terminal_bundle_sha256": "b" * 64,
        "episode_sha256s": ("c" * 64,),
        "qualification_sha256": "d" * 64,
        "canonical_authority_verified": False,
        "promotion_authority": False,
        "readiness_authority": False,
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "whole_product_complete": False,
    }
    kwargs[positive_field] = True

    with pytest.raises(
        ValueError,
        match="CampaignQualificationReport cannot carry positive authority/truth",
    ):
        CampaignQualificationReport(**kwargs)
