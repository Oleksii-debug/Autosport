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

def _valid_report_kwargs() -> dict[str, object]:
    return {
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


def test_direct_report_construction_cannot_claim_complete_with_blockers() -> None:
    kwargs = _valid_report_kwargs()
    kwargs["blockers"] = ("contradictory blocker",)

    with pytest.raises(
        ValueError,
        match="complete campaign qualification cannot carry blockers",
    ):
        CampaignQualificationReport(**kwargs)


def test_direct_report_construction_cannot_claim_blocked_without_blockers() -> None:
    kwargs = _valid_report_kwargs()
    kwargs["state"] = QualificationState.BLOCKED

    with pytest.raises(
        ValueError,
        match="blocked campaign qualification must carry at least one blocker",
    ):
        CampaignQualificationReport(**kwargs)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("identity_sha256", "not-a-digest"),
        ("expected_terminal_bundle_sha256", "B" * 64),
        ("episode_sha256s", ()),
        ("episode_sha256s", ("not-a-digest",)),
        ("qualification_sha256", "d" * 63),
        ("blockers", ["not", "a", "tuple"]),
    ),
)
def test_direct_report_construction_rejects_noncanonical_structural_fields(
    field: str,
    invalid_value: object,
) -> None:
    kwargs = _valid_report_kwargs()
    kwargs[field] = invalid_value

    with pytest.raises((TypeError, ValueError)):
        CampaignQualificationReport(**kwargs)

