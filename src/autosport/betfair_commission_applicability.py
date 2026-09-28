"""Fail-closed Betfair prospective commission applicability evidence.

The canonical Betfair fee-input reader proves authenticated account/market inputs.  It
cannot prove which tariff regime applies to the account/market, whether a Rewards
package or Master/Sub-Account rule overrides ordinary MBR semantics, or whether
Transaction/Expert/Turnover charges apply.  This module makes that distinction a
product-owned decision-time fact instead of letting authenticated inputs be mistaken
for a complete prospective cost rule.

This is deliberately a *negative prerequisite* authority.  It never returns a
prospective commission amount, never marks execution fees complete, and grants no
provider-write or real-money authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from threading import RLock
from weakref import WeakSet

from .betfair_account_readonly import BetfairReadOnlyClient
from .betfair_execution_fee_inputs import (
    BetfairExecutionFeeInputsObservation,
    read_betfair_execution_fee_inputs,
)


_SCHEMA = "autosport.betfair_commission_applicability"
_SCHEMA_VERSION = 1
_RULESET_ID = "betfair-exchange-charges-fail-closed-2026-09-29"
_PROVIDER_COMMISSION_SOURCE = "betfair-support.exchange-commission"
_PROVIDER_CHARGES_SOURCE = "betfair-support.betfair-charges"
_PROVIDER_MBR_SOURCE = "betfair-support.market-base-rate"


class BetfairCommissionApplicabilityError(ValueError):
    """Raised when commission applicability evidence is malformed or unissued."""


class BetfairCommissionApplicabilityStatus(StrEnum):
    UNPROVEN = "UNPROVEN"


class BetfairCommissionApplicabilityReason(StrEnum):
    ACCOUNT_TARIFF_REGIME_UNPROVEN = "ACCOUNT_TARIFF_REGIME_UNPROVEN"
    EVENT_REGIME_UNPROVEN = "EVENT_REGIME_UNPROVEN"
    ADDITIONAL_ACCOUNT_CHARGES_UNPROVEN = "ADDITIONAL_ACCOUNT_CHARGES_UNPROVEN"
    TERMINAL_NET_WINNINGS_UNAVAILABLE = "TERMINAL_NET_WINNINGS_UNAVAILABLE"


_CANONICAL_REASONS = (
    BetfairCommissionApplicabilityReason.ACCOUNT_TARIFF_REGIME_UNPROVEN,
    BetfairCommissionApplicabilityReason.EVENT_REGIME_UNPROVEN,
    BetfairCommissionApplicabilityReason.ADDITIONAL_ACCOUNT_CHARGES_UNPROVEN,
    BetfairCommissionApplicabilityReason.TERMINAL_NET_WINNINGS_UNAVAILABLE,
)


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fee_input_payload(observation: BetfairExecutionFeeInputsObservation) -> dict[str, object]:
    if type(observation) is not BetfairExecutionFeeInputsObservation:
        raise BetfairCommissionApplicabilityError(
            "fee inputs must be exact BetfairExecutionFeeInputsObservation"
        )
    return {
        "venue_id": observation.venue_id,
        "account_id": observation.account_id,
        "currency_code": observation.currency_code,
        "region": observation.region,
        "market_id": observation.market_id,
        "discount_rate_percent": str(observation.discount_rate_percent),
        "market_base_rate_percent": str(observation.market_base_rate_percent),
        "discount_allowed": observation.discount_allowed,
        "regulator": observation.regulator,
        "account_observed_at": observation.account_evidence.observed_at,
        "account_source_payload_sha256": observation.account_evidence.source_payload_sha256,
        "market_observed_at": observation.market_evidence.observed_at,
        "market_source_payload_sha256": observation.market_evidence.source_payload_sha256,
    }


def _fee_input_sha256(observation: BetfairExecutionFeeInputsObservation) -> str:
    return sha256(_canonical_json(_fee_input_payload(observation))).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class BetfairCommissionApplicabilityAssessment:
    """Product-issued negative prerequisite for prospective Betfair commission truth."""

    venue_id: str
    account_id: str
    market_id: str
    currency_code: str
    region: str | None
    fee_input_sha256: str
    account_source_payload_sha256: str
    market_source_payload_sha256: str
    status: BetfairCommissionApplicabilityStatus
    reasons: tuple[BetfairCommissionApplicabilityReason, ...]
    ruleset_id: str
    provider_rule_sources: tuple[str, ...]
    prospective_commission_amount_authorized: bool
    complete_execution_fee_cost_authorized: bool
    provider_write_authorized: bool
    real_money_execution_authorized: bool
    assessment_id: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise BetfairCommissionApplicabilityError(
            "Betfair commission applicability assessments are product-issued"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "venue_id": self.venue_id,
            "account_id": self.account_id,
            "market_id": self.market_id,
            "currency_code": self.currency_code,
            "region": self.region,
            "fee_input_sha256": self.fee_input_sha256,
            "account_source_payload_sha256": self.account_source_payload_sha256,
            "market_source_payload_sha256": self.market_source_payload_sha256,
            "status": self.status.value,
            "reasons": [reason.value for reason in self.reasons],
            "ruleset_id": self.ruleset_id,
            "provider_rule_sources": list(self.provider_rule_sources),
            "prospective_commission_amount_authorized": self.prospective_commission_amount_authorized,
            "complete_execution_fee_cost_authorized": self.complete_execution_fee_cost_authorized,
            "provider_write_authorized": self.provider_write_authorized,
            "real_money_execution_authorized": self.real_money_execution_authorized,
            "assessment_id": self.assessment_id,
        }


_ISSUED: WeakSet[BetfairCommissionApplicabilityAssessment] = WeakSet()
_ISSUE_LOCK = RLock()


def _assessment_payload(
    observation: BetfairExecutionFeeInputsObservation,
) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "venue_id": observation.venue_id,
        "account_id": observation.account_id,
        "market_id": observation.market_id,
        "currency_code": observation.currency_code,
        "region": observation.region,
        "fee_input_sha256": _fee_input_sha256(observation),
        "account_source_payload_sha256": observation.account_evidence.source_payload_sha256,
        "market_source_payload_sha256": observation.market_evidence.source_payload_sha256,
        "status": BetfairCommissionApplicabilityStatus.UNPROVEN.value,
        "reasons": [reason.value for reason in _CANONICAL_REASONS],
        "ruleset_id": _RULESET_ID,
        "provider_rule_sources": [
            _PROVIDER_COMMISSION_SOURCE,
            _PROVIDER_CHARGES_SOURCE,
            _PROVIDER_MBR_SOURCE,
        ],
        "prospective_commission_amount_authorized": False,
        "complete_execution_fee_cost_authorized": False,
        "provider_write_authorized": False,
        "real_money_execution_authorized": False,
    }


def _issue_assessment(
    observation: BetfairExecutionFeeInputsObservation,
) -> BetfairCommissionApplicabilityAssessment:
    payload = _assessment_payload(observation)
    assessment_id = sha256(_canonical_json(payload)).hexdigest()
    assessment = object.__new__(BetfairCommissionApplicabilityAssessment)
    object.__setattr__(assessment, "venue_id", observation.venue_id)
    object.__setattr__(assessment, "account_id", observation.account_id)
    object.__setattr__(assessment, "market_id", observation.market_id)
    object.__setattr__(assessment, "currency_code", observation.currency_code)
    object.__setattr__(assessment, "region", observation.region)
    object.__setattr__(assessment, "fee_input_sha256", payload["fee_input_sha256"])
    object.__setattr__(
        assessment,
        "account_source_payload_sha256",
        observation.account_evidence.source_payload_sha256,
    )
    object.__setattr__(
        assessment,
        "market_source_payload_sha256",
        observation.market_evidence.source_payload_sha256,
    )
    object.__setattr__(assessment, "status", BetfairCommissionApplicabilityStatus.UNPROVEN)
    object.__setattr__(assessment, "reasons", _CANONICAL_REASONS)
    object.__setattr__(assessment, "ruleset_id", _RULESET_ID)
    object.__setattr__(
        assessment,
        "provider_rule_sources",
        (_PROVIDER_COMMISSION_SOURCE, _PROVIDER_CHARGES_SOURCE, _PROVIDER_MBR_SOURCE),
    )
    object.__setattr__(assessment, "prospective_commission_amount_authorized", False)
    object.__setattr__(assessment, "complete_execution_fee_cost_authorized", False)
    object.__setattr__(assessment, "provider_write_authorized", False)
    object.__setattr__(assessment, "real_money_execution_authorized", False)
    object.__setattr__(assessment, "assessment_id", assessment_id)
    with _ISSUE_LOCK:
        _ISSUED.add(assessment)
    return assessment


def assess_betfair_commission_applicability(
    client: BetfairReadOnlyClient,
    *,
    market_id: str,
) -> BetfairCommissionApplicabilityAssessment:
    """Re-read authenticated inputs and issue the bounded current applicability truth.

    Positive commission calculation is intentionally unavailable.  Current canonical
    provider reads do not prove account package/tariff mode, Australasian-event or
    Master/Sub-Account regime, conditional additional-charge applicability, or the
    terminal market net winnings on which commission is actually settled.
    """

    observation = read_betfair_execution_fee_inputs(client, market_id=market_id)
    return _issue_assessment(observation)


def require_product_betfair_commission_applicability(
    assessment: BetfairCommissionApplicabilityAssessment,
) -> BetfairCommissionApplicabilityAssessment:
    """Validate exact in-process issuance and fail closed on any positive widening."""

    if type(assessment) is not BetfairCommissionApplicabilityAssessment:
        raise BetfairCommissionApplicabilityError(
            "assessment must be exact BetfairCommissionApplicabilityAssessment"
        )
    with _ISSUE_LOCK:
        if assessment not in _ISSUED:
            raise BetfairCommissionApplicabilityError(
                "assessment is not product-issued in this process"
            )
    if (
        assessment.status is not BetfairCommissionApplicabilityStatus.UNPROVEN
        or assessment.reasons != _CANONICAL_REASONS
        or assessment.ruleset_id != _RULESET_ID
        or assessment.provider_rule_sources
        != (_PROVIDER_COMMISSION_SOURCE, _PROVIDER_CHARGES_SOURCE, _PROVIDER_MBR_SOURCE)
        or assessment.prospective_commission_amount_authorized is not False
        or assessment.complete_execution_fee_cost_authorized is not False
        or assessment.provider_write_authorized is not False
        or assessment.real_money_execution_authorized is not False
    ):
        raise BetfairCommissionApplicabilityError(
            "assessment exceeds the canonical fail-closed applicability boundary"
        )
    payload = assessment.to_dict()
    claimed_id = payload.pop("assessment_id")
    if claimed_id != sha256(_canonical_json(payload)).hexdigest():
        raise BetfairCommissionApplicabilityError("assessment identity is inconsistent")
    return assessment
