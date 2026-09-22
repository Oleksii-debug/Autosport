from datetime import date

from autosport.campaign_evidence_checklist import (
    CampaignDayEvidence,
    CampaignEvidenceChecklist,
    CampaignEvidenceRecord,
    EvidenceState,
    assess_campaign_evidence,
)


def _verified(key: str, evidence_ref: str) -> CampaignEvidenceRecord:
    return CampaignEvidenceRecord(
        key=key,
        state=EvidenceState.VERIFIED,
        evidence_ref=evidence_ref,
    )


def test_caller_authored_refs_and_physical_flags_cannot_mint_handoff_readiness() -> None:
    daily_key = "live_data"
    campaign_key = "restart_recovery"
    checklist = CampaignEvidenceChecklist(
        campaign_id="caller-authored-campaign",
        start_day=date(2026, 9, 18),
        end_day=date(2026, 9, 20),
        required_daily_keys=(daily_key,),
        required_campaign_keys=(campaign_key,),
        day_evidence=tuple(
            CampaignDayEvidence(
                day=day_value,
                records=(
                    _verified(
                        daily_key,
                        f"artifact://nonexistent/caller/day-{day_value.isoformat()}",
                    ),
                ),
            )
            for day_value in (
                date(2026, 9, 18),
                date(2026, 9, 19),
                date(2026, 9, 20),
            )
        ),
        campaign_evidence=(
            _verified(
                campaign_key,
                "artifact://nonexistent/caller/restart-recovery",
            ),
        ),
        human_tested=True,
        human_test_evidence_ref="artifact://nonexistent/caller/human",
        nvda_verified=True,
        nvda_evidence_ref="artifact://nonexistent/caller/nvda",
    )

    assessment = assess_campaign_evidence(checklist)

    # A caller can construct every value above without resolving any product-owned
    # campaign, machine-evidence, human-test, or physical-NVDA authority. The
    # checklist may remain useful as a diagnostic DTO, but it must not turn those
    # self-authored assertions into a positive product handoff verdict.
    assert assessment.pre_handoff_ready is False
    assert assessment.grants_execution_authority is False
    assert assessment.proves_real_money_execution is False
    assert assessment.proves_whole_product_complete is False


def test_caller_selected_required_schema_cannot_define_product_completeness() -> None:
    checklist = CampaignEvidenceChecklist(
        campaign_id="caller-shrunk-requirements",
        start_day=date(2026, 9, 18),
        end_day=date(2026, 9, 19),
        required_daily_keys=("caller_only_daily_key",),
        required_campaign_keys=("caller_only_campaign_key",),
        day_evidence=(
            CampaignDayEvidence(
                day=date(2026, 9, 18),
                records=(
                    _verified(
                        "caller_only_daily_key",
                        "artifact://caller/day-1",
                    ),
                ),
            ),
            CampaignDayEvidence(
                day=date(2026, 9, 19),
                records=(
                    _verified(
                        "caller_only_daily_key",
                        "artifact://caller/day-2",
                    ),
                ),
            ),
        ),
        campaign_evidence=(
            _verified(
                "caller_only_campaign_key",
                "artifact://caller/campaign",
            ),
        ),
        human_tested=True,
        human_test_evidence_ref="artifact://caller/human",
        nvda_verified=True,
        nvda_evidence_ref="artifact://caller/nvda",
    )

    assessment = assess_campaign_evidence(checklist)

    # Required evidence classes are product policy, not caller policy. Until the
    # canonical product schema and evidence origins are independently re-resolved,
    # narrowing the caller checklist must not authorize positive handoff.
    assert assessment.pre_handoff_ready is False
