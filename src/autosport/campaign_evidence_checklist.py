from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum


class EvidenceState(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"


def _require_canonical_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty canonical string")
    return value


def _require_optional_canonical_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _require_canonical_text(value, label)


def _require_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a bool")
    return value


def _require_key_tuple(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise ValueError(f"{label} must be a non-empty tuple")
    keys = tuple(_require_canonical_text(item, f"{label} item") for item in value)
    if len(keys) != len(set(keys)):
        raise ValueError(f"{label} must contain unique keys")
    return keys


@dataclass(frozen=True, slots=True)
class CampaignEvidenceRecord:
    key: str
    state: EvidenceState
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_canonical_text(self.key, "key")
        if not isinstance(self.state, EvidenceState):
            raise ValueError("state must be an EvidenceState")
        _require_canonical_text(self.evidence_ref, "evidence_ref")


@dataclass(frozen=True, slots=True)
class CampaignDayEvidence:
    day: date
    records: tuple[CampaignEvidenceRecord, ...]

    def __post_init__(self) -> None:
        if type(self.day) is not date:
            raise ValueError("day must be a date")
        if type(self.records) is not tuple:
            raise ValueError("records must be a tuple")
        if not all(isinstance(record, CampaignEvidenceRecord) for record in self.records):
            raise ValueError("records must contain CampaignEvidenceRecord values")
        keys = [record.key for record in self.records]
        if len(keys) != len(set(keys)):
            raise ValueError("daily evidence keys must be unique")


@dataclass(frozen=True, slots=True)
class CampaignEvidenceChecklist:
    campaign_id: str
    start_day: date
    end_day: date
    required_daily_keys: tuple[str, ...]
    required_campaign_keys: tuple[str, ...]
    day_evidence: tuple[CampaignDayEvidence, ...]
    campaign_evidence: tuple[CampaignEvidenceRecord, ...]
    human_tested: bool = False
    human_test_evidence_ref: str | None = None
    nvda_verified: bool = False
    nvda_evidence_ref: str | None = None

    def __post_init__(self) -> None:
        _require_canonical_text(self.campaign_id, "campaign_id")
        if type(self.start_day) is not date or type(self.end_day) is not date:
            raise ValueError("start_day and end_day must be dates")
        if self.end_day <= self.start_day:
            raise ValueError("a multi-day campaign must span at least two calendar days")
        _require_key_tuple(self.required_daily_keys, "required_daily_keys")
        _require_key_tuple(self.required_campaign_keys, "required_campaign_keys")
        if set(self.required_daily_keys) & set(self.required_campaign_keys):
            raise ValueError("daily and campaign evidence keys must be disjoint")
        if type(self.day_evidence) is not tuple:
            raise ValueError("day_evidence must be a tuple")
        if not all(isinstance(item, CampaignDayEvidence) for item in self.day_evidence):
            raise ValueError("day_evidence must contain CampaignDayEvidence values")
        days = [item.day for item in self.day_evidence]
        if len(days) != len(set(days)):
            raise ValueError("day_evidence must contain unique days")
        if any(day < self.start_day or day > self.end_day for day in days):
            raise ValueError("day_evidence cannot fall outside the campaign window")
        if type(self.campaign_evidence) is not tuple:
            raise ValueError("campaign_evidence must be a tuple")
        if not all(
            isinstance(item, CampaignEvidenceRecord) for item in self.campaign_evidence
        ):
            raise ValueError(
                "campaign_evidence must contain CampaignEvidenceRecord values"
            )
        campaign_keys = [item.key for item in self.campaign_evidence]
        if len(campaign_keys) != len(set(campaign_keys)):
            raise ValueError("campaign evidence keys must be unique")
        _require_bool(self.human_tested, "human_tested")
        _require_optional_canonical_text(
            self.human_test_evidence_ref, "human_test_evidence_ref"
        )
        _require_bool(self.nvda_verified, "nvda_verified")
        _require_optional_canonical_text(
            self.nvda_evidence_ref, "nvda_evidence_ref"
        )


@dataclass(frozen=True, slots=True, order=True)
class MissingDailyEvidence:
    day: date
    key: str


@dataclass(frozen=True, slots=True, order=True)
class FailedDailyEvidence:
    day: date
    key: str
    evidence_ref: str


@dataclass(frozen=True, slots=True, order=True)
class FailedCampaignEvidence:
    key: str
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class CampaignEvidenceAssessment:
    evidence_id: str
    expected_days: tuple[date, ...]
    missing_days: tuple[date, ...]
    missing_daily_evidence: tuple[MissingDailyEvidence, ...]
    failed_daily_evidence: tuple[FailedDailyEvidence, ...]
    missing_campaign_evidence: tuple[str, ...]
    failed_campaign_evidence: tuple[FailedCampaignEvidence, ...]
    human_tested: bool
    human_test_evidence_ref: str | None
    nvda_verified: bool
    nvda_evidence_ref: str | None

    @property
    def machine_evidence_complete(self) -> bool:
        return not (
            self.missing_days
            or self.missing_daily_evidence
            or self.failed_daily_evidence
            or self.missing_campaign_evidence
            or self.failed_campaign_evidence
        )

    @property
    def human_evidence_complete(self) -> bool:
        return self.human_tested and self.human_test_evidence_ref is not None

    @property
    def nvda_evidence_complete(self) -> bool:
        return self.nvda_verified and self.nvda_evidence_ref is not None

    @property
    def physical_accessibility_evidence_complete(self) -> bool:
        return self.human_evidence_complete and self.nvda_evidence_complete

    @property
    def positive_authority_verified(self) -> bool:
        """Whether product-owned authorities independently prove positive handoff."""
        return False

    @property
    def pre_handoff_ready(self) -> bool:
        # This checklist is caller-constructible diagnostic evidence. Required-key
        # selection, evidence refs, and HUMAN/NVDA claim fields are not themselves
        # product-owned authority and therefore cannot issue a positive handoff.
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    @property
    def proves_real_money_execution(self) -> bool:
        return False

    @property
    def proves_whole_product_complete(self) -> bool:
        return False


def _expected_days(start_day: date, end_day: date) -> tuple[date, ...]:
    return tuple(
        start_day + timedelta(days=offset)
        for offset in range((end_day - start_day).days + 1)
    )


def _evidence_id(
    checklist: CampaignEvidenceChecklist,
    expected_days: tuple[date, ...],
) -> str:
    payload = {
        "campaign_id": checklist.campaign_id,
        "start_day": checklist.start_day.isoformat(),
        "end_day": checklist.end_day.isoformat(),
        "required_daily_keys": sorted(checklist.required_daily_keys),
        "required_campaign_keys": sorted(checklist.required_campaign_keys),
        "day_evidence": [
            {
                "day": item.day.isoformat(),
                "records": [
                    {
                        "key": record.key,
                        "state": record.state.value,
                        "evidence_ref": record.evidence_ref,
                    }
                    for record in sorted(item.records, key=lambda record: record.key)
                ],
            }
            for item in sorted(checklist.day_evidence, key=lambda item: item.day)
        ],
        "campaign_evidence": [
            {
                "key": record.key,
                "state": record.state.value,
                "evidence_ref": record.evidence_ref,
            }
            for record in sorted(checklist.campaign_evidence, key=lambda record: record.key)
        ],
        "human_tested": checklist.human_tested,
        "human_test_evidence_ref": checklist.human_test_evidence_ref,
        "nvda_verified": checklist.nvda_verified,
        "nvda_evidence_ref": checklist.nvda_evidence_ref,
        "expected_days": [day.isoformat() for day in expected_days],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def assess_campaign_evidence(
    checklist: CampaignEvidenceChecklist,
) -> CampaignEvidenceAssessment:
    """Assess deterministic pre-handoff evidence for a multi-day PAPER campaign.

    Missing evidence is represented by absence, failed evidence by an explicit FAILED
    record. HUMAN_TESTED/NVDA_VERIFIED fields and evidence refs are caller-supplied
    diagnostic claims only; they do not become product-owned positive authority here.
    The assessment grants no execution or handoff authority and cannot prove real-money
    execution or whole-product completion.
    """

    if not isinstance(checklist, CampaignEvidenceChecklist):
        raise ValueError("checklist must be a CampaignEvidenceChecklist")

    expected_days = _expected_days(checklist.start_day, checklist.end_day)
    by_day = {item.day: item for item in checklist.day_evidence}
    missing_days = tuple(day for day in expected_days if day not in by_day)

    missing_daily: list[MissingDailyEvidence] = []
    failed_daily: list[FailedDailyEvidence] = []
    required_daily = tuple(sorted(checklist.required_daily_keys))
    for day in expected_days:
        daily = by_day.get(day)
        records = {} if daily is None else {record.key: record for record in daily.records}
        for key in required_daily:
            record = records.get(key)
            if record is None:
                missing_daily.append(MissingDailyEvidence(day, key))
            elif record.state is EvidenceState.FAILED:
                failed_daily.append(FailedDailyEvidence(day, key, record.evidence_ref))

    campaign_records = {record.key: record for record in checklist.campaign_evidence}
    missing_campaign = tuple(
        key
        for key in sorted(checklist.required_campaign_keys)
        if key not in campaign_records
    )
    failed_campaign = tuple(
        FailedCampaignEvidence(key, campaign_records[key].evidence_ref)
        for key in sorted(checklist.required_campaign_keys)
        if key in campaign_records
        and campaign_records[key].state is EvidenceState.FAILED
    )

    return CampaignEvidenceAssessment(
        evidence_id=_evidence_id(checklist, expected_days),
        expected_days=expected_days,
        missing_days=missing_days,
        missing_daily_evidence=tuple(missing_daily),
        failed_daily_evidence=tuple(failed_daily),
        missing_campaign_evidence=missing_campaign,
        failed_campaign_evidence=failed_campaign,
        human_tested=checklist.human_tested,
        human_test_evidence_ref=checklist.human_test_evidence_ref,
        nvda_verified=checklist.nvda_verified,
        nvda_evidence_ref=checklist.nvda_evidence_ref,
    )
