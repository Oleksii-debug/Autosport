from datetime import date

import pytest

from autosport.campaign_evidence_checklist import (
    CampaignDayEvidence,
    CampaignEvidenceChecklist,
    CampaignEvidenceRecord,
    EvidenceState,
    assess_campaign_evidence,
)


def record(
    key: str,
    state: EvidenceState = EvidenceState.VERIFIED,
    evidence_ref: str | None = None,
) -> CampaignEvidenceRecord:
    return CampaignEvidenceRecord(
        key=key,
        state=state,
        evidence_ref=evidence_ref or f"artifact://{key}",
    )


def day(day_value: date, *keys: str) -> CampaignDayEvidence:
    return CampaignDayEvidence(
        day=day_value,
        records=tuple(record(key) for key in keys),
    )


def checklist(
    *,
    day_evidence: tuple[CampaignDayEvidence, ...],
    campaign_evidence: tuple[CampaignEvidenceRecord, ...],
    human_tested: bool = False,
    human_test_evidence_ref: str | None = None,
    nvda_verified: bool = False,
    nvda_evidence_ref: str | None = None,
) -> CampaignEvidenceChecklist:
    return CampaignEvidenceChecklist(
        campaign_id="paper-campaign-001",
        start_day=date(2026, 9, 18),
        end_day=date(2026, 9, 20),
        required_daily_keys=("live_data", "decision_ledger"),
        required_campaign_keys=("restart_recovery", "settlement_reconciliation"),
        day_evidence=day_evidence,
        campaign_evidence=campaign_evidence,
        human_tested=human_tested,
        human_test_evidence_ref=human_test_evidence_ref,
        nvda_verified=nvda_verified,
        nvda_evidence_ref=nvda_evidence_ref,
    )


def complete_days() -> tuple[CampaignDayEvidence, ...]:
    return (
        day(date(2026, 9, 18), "live_data", "decision_ledger"),
        day(date(2026, 9, 19), "live_data", "decision_ledger"),
        day(date(2026, 9, 20), "live_data", "decision_ledger"),
    )


def complete_campaign() -> tuple[CampaignEvidenceRecord, ...]:
    return (record("restart_recovery"), record("settlement_reconciliation"))


def test_machine_complete_does_not_infer_human_or_nvda_truth() -> None:
    assessment = assess_campaign_evidence(
        checklist(day_evidence=complete_days(), campaign_evidence=complete_campaign())
    )

    assert assessment.machine_evidence_complete
    assert assessment.human_tested is False
    assert assessment.nvda_verified is False
    assert assessment.physical_accessibility_evidence_complete is False
    assert assessment.pre_handoff_ready is False
    assert assessment.grants_execution_authority is False
    assert assessment.proves_real_money_execution is False
    assert assessment.proves_whole_product_complete is False


def test_positive_physical_truth_without_evidence_refs_is_not_handoff_ready() -> None:
    assessment = assess_campaign_evidence(
        checklist(
            day_evidence=complete_days(),
            campaign_evidence=complete_campaign(),
            human_tested=True,
            nvda_verified=True,
        )
    )

    assert assessment.machine_evidence_complete
    assert assessment.human_tested
    assert assessment.nvda_verified
    assert assessment.human_evidence_complete is False
    assert assessment.nvda_evidence_complete is False
    assert assessment.physical_accessibility_evidence_complete is False
    assert assessment.pre_handoff_ready is False


def test_caller_human_and_nvda_refs_remain_diagnostic_not_handoff_authority() -> None:
    assessment = assess_campaign_evidence(
        checklist(
            day_evidence=complete_days(),
            campaign_evidence=complete_campaign(),
            human_tested=True,
            human_test_evidence_ref="artifact://human-windows-walkthrough",
            nvda_verified=True,
            nvda_evidence_ref="artifact://nvda-session",
        )
    )

    assert assessment.machine_evidence_complete
    assert assessment.human_evidence_complete
    assert assessment.nvda_evidence_complete
    assert assessment.physical_accessibility_evidence_complete
    assert assessment.positive_authority_verified is False
    assert assessment.pre_handoff_ready is False
    assert assessment.proves_real_money_execution is False
    assert assessment.proves_whole_product_complete is False


def test_missing_day_is_explicit_and_cannot_disappear_from_completeness() -> None:
    assessment = assess_campaign_evidence(
        checklist(
            day_evidence=(
                day(date(2026, 9, 18), "live_data", "decision_ledger"),
                day(date(2026, 9, 20), "live_data", "decision_ledger"),
            ),
            campaign_evidence=complete_campaign(),
            human_tested=True,
            human_test_evidence_ref="artifact://human",
            nvda_verified=True,
            nvda_evidence_ref="artifact://nvda",
        )
    )

    assert assessment.missing_days == (date(2026, 9, 19),)
    assert [(item.day, item.key) for item in assessment.missing_daily_evidence] == [
        (date(2026, 9, 19), "decision_ledger"),
        (date(2026, 9, 19), "live_data"),
    ]
    assert assessment.machine_evidence_complete is False
    assert assessment.pre_handoff_ready is False


def test_failed_evidence_is_distinct_from_missing_evidence() -> None:
    failed_day = CampaignDayEvidence(
        day=date(2026, 9, 19),
        records=(
            record("live_data", EvidenceState.FAILED, "artifact://live-data-failure"),
            record("decision_ledger"),
        ),
    )
    assessment = assess_campaign_evidence(
        checklist(
            day_evidence=(complete_days()[0], failed_day, complete_days()[2]),
            campaign_evidence=(
                record("restart_recovery"),
                record(
                    "settlement_reconciliation",
                    EvidenceState.FAILED,
                    "artifact://reconciliation-failure",
                ),
            ),
        )
    )

    assert assessment.missing_daily_evidence == ()
    assert [
        (item.day, item.key, item.evidence_ref)
        for item in assessment.failed_daily_evidence
    ] == [
        (date(2026, 9, 19), "live_data", "artifact://live-data-failure")
    ]
    assert assessment.missing_campaign_evidence == ()
    assert [
        (item.key, item.evidence_ref) for item in assessment.failed_campaign_evidence
    ] == [
        ("settlement_reconciliation", "artifact://reconciliation-failure")
    ]
    assert assessment.machine_evidence_complete is False


def test_evidence_id_is_deterministic_across_input_order() -> None:
    first = assess_campaign_evidence(
        checklist(
            day_evidence=complete_days(),
            campaign_evidence=complete_campaign(),
        )
    )
    second = assess_campaign_evidence(
        CampaignEvidenceChecklist(
            campaign_id="paper-campaign-001",
            start_day=date(2026, 9, 18),
            end_day=date(2026, 9, 20),
            required_daily_keys=("decision_ledger", "live_data"),
            required_campaign_keys=(
                "settlement_reconciliation",
                "restart_recovery",
            ),
            day_evidence=tuple(
                CampaignDayEvidence(item.day, tuple(reversed(item.records)))
                for item in reversed(complete_days())
            ),
            campaign_evidence=tuple(reversed(complete_campaign())),
        )
    )

    assert first.evidence_id == second.evidence_id
    assert first.expected_days == second.expected_days
    assert first.machine_evidence_complete == second.machine_evidence_complete


def test_missing_campaign_requirement_is_reported() -> None:
    assessment = assess_campaign_evidence(
        checklist(
            day_evidence=complete_days(),
            campaign_evidence=(record("restart_recovery"),),
        )
    )

    assert assessment.missing_campaign_evidence == ("settlement_reconciliation",)
    assert assessment.failed_campaign_evidence == ()
    assert assessment.machine_evidence_complete is False


def test_checklist_rejects_ambiguous_or_non_multi_day_contracts() -> None:
    base = dict(
        campaign_id="paper-campaign-001",
        start_day=date(2026, 9, 18),
        end_day=date(2026, 9, 20),
        required_daily_keys=("live_data",),
        required_campaign_keys=("restart_recovery",),
        day_evidence=(),
        campaign_evidence=(),
    )

    with pytest.raises(ValueError, match="at least two"):
        CampaignEvidenceChecklist(**{**base, "end_day": date(2026, 9, 18)})
    with pytest.raises(ValueError, match="unique keys"):
        CampaignEvidenceChecklist(
            **{**base, "required_daily_keys": ("live_data", "live_data")}
        )
    with pytest.raises(ValueError, match="disjoint"):
        CampaignEvidenceChecklist(
            **{**base, "required_campaign_keys": ("live_data",)}
        )
    with pytest.raises(ValueError, match="bool"):
        CampaignEvidenceChecklist(**{**base, "human_tested": 1})
    with pytest.raises(ValueError, match="human_test_evidence_ref"):
        CampaignEvidenceChecklist(
            **{**base, "human_test_evidence_ref": " human-artifact "}
        )


def test_day_evidence_rejects_duplicates_and_out_of_window_days() -> None:
    duplicate_records = CampaignDayEvidence(
        day=date(2026, 9, 18),
        records=(record("live_data"), record("decision_ledger")),
    )
    with pytest.raises(ValueError, match="unique days"):
        checklist(
            day_evidence=(duplicate_records, duplicate_records),
            campaign_evidence=complete_campaign(),
        )

    with pytest.raises(ValueError, match="outside"):
        checklist(
            day_evidence=(day(date(2026, 9, 21), "live_data"),),
            campaign_evidence=complete_campaign(),
        )

    with pytest.raises(ValueError, match="unique"):
        CampaignDayEvidence(
            day=date(2026, 9, 18),
            records=(record("live_data"), record("live_data")),
        )


def test_records_fail_closed_on_noncanonical_identity_and_evidence_ref() -> None:
    with pytest.raises(ValueError, match="key"):
        record(" live_data")
    with pytest.raises(ValueError, match="evidence_ref"):
        record("live_data", evidence_ref=" ")
    with pytest.raises(ValueError, match="state"):
        CampaignEvidenceRecord(
            key="live_data",
            state="verified",  # type: ignore[arg-type]
            evidence_ref="artifact://live-data",
        )
