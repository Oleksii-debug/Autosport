"""Fail-closed Betfair prospective commission applicability evidence.

Authenticated Betfair fee inputs are necessary but not sufficient proof of the
prospective commission tariff that applies to an account/market.  This module issues
only a product-owned negative prerequisite: applicability remains UNPROVEN until the
missing tariff/event/additional-charge/terminal-net-win authorities exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from hashlib import sha256
import json
from threading import RLock
from weakref import ReferenceType, ref

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
_PROVIDER_RULE_SOURCES = (
    _PROVIDER_COMMISSION_SOURCE,
    _PROVIDER_CHARGES_SOURCE,
    _PROVIDER_MBR_SOURCE,
)


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fee_input_payload(
    observation: BetfairExecutionFeeInputsObservation,
) -> dict[str, object]:
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
        "account_source_payload_sha256": (
            observation.account_evidence.source_payload_sha256
        ),
        "market_observed_at": observation.market_evidence.observed_at,
        "market_source_payload_sha256": (
            observation.market_evidence.source_payload_sha256
        ),
    }


def _fee_input_sha256(
    payload_builder,
    canonical_json,
    hash_constructor,
    observation: BetfairExecutionFeeInputsObservation,
) -> str:
    return hash_constructor(canonical_json(payload_builder(observation))).hexdigest()


_FEE_INPUT_SHA256_CAPABILITY = partial(
    _fee_input_sha256,
    _fee_input_payload,
    _canonical_json,
    sha256,
)


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class BetfairCommissionApplicabilityAssessment:
    """Product-issued negative prerequisite for prospective commission truth."""

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
            "prospective_commission_amount_authorized": (
                self.prospective_commission_amount_authorized
            ),
            "complete_execution_fee_cost_authorized": (
                self.complete_execution_fee_cost_authorized
            ),
            "provider_write_authorized": self.provider_write_authorized,
            "real_money_execution_authorized": self.real_money_execution_authorized,
            "assessment_id": self.assessment_id,
        }


def _assessment_payload(
    fee_input_hasher,
    schema,
    schema_version,
    unproven_status,
    canonical_reasons,
    ruleset_id,
    provider_rule_sources,
    observation: BetfairExecutionFeeInputsObservation,
) -> dict[str, object]:
    return {
        "schema": schema,
        "schema_version": schema_version,
        "venue_id": observation.venue_id,
        "account_id": observation.account_id,
        "market_id": observation.market_id,
        "currency_code": observation.currency_code,
        "region": observation.region,
        "fee_input_sha256": fee_input_hasher(observation),
        "account_source_payload_sha256": (
            observation.account_evidence.source_payload_sha256
        ),
        "market_source_payload_sha256": (
            observation.market_evidence.source_payload_sha256
        ),
        "status": unproven_status.value,
        "reasons": [reason.value for reason in canonical_reasons],
        "ruleset_id": ruleset_id,
        "provider_rule_sources": list(provider_rule_sources),
        "prospective_commission_amount_authorized": False,
        "complete_execution_fee_cost_authorized": False,
        "provider_write_authorized": False,
        "real_money_execution_authorized": False,
    }


_ASSESSMENT_PAYLOAD_CAPABILITY = partial(
    _assessment_payload,
    _FEE_INPUT_SHA256_CAPABILITY,
    _SCHEMA,
    _SCHEMA_VERSION,
    BetfairCommissionApplicabilityStatus.UNPROVEN,
    _CANONICAL_REASONS,
    _RULESET_ID,
    _PROVIDER_RULE_SOURCES,
)


def _issue_assessment(
    payload_builder,
    canonical_json,
    hash_constructor,
    assessment_type,
    unproven_status,
    canonical_reasons,
    ruleset_id,
    provider_rule_sources,
    register_issued,
    observation: BetfairExecutionFeeInputsObservation,
) -> BetfairCommissionApplicabilityAssessment:
    """Compatibility helper; production issuance captures a private registrar."""

    payload = payload_builder(observation)
    assessment_id = hash_constructor(canonical_json(payload)).hexdigest()
    assessment = object.__new__(assessment_type)
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
    object.__setattr__(assessment, "status", unproven_status)
    object.__setattr__(assessment, "reasons", canonical_reasons)
    object.__setattr__(assessment, "ruleset_id", ruleset_id)
    object.__setattr__(assessment, "provider_rule_sources", provider_rule_sources)
    object.__setattr__(assessment, "prospective_commission_amount_authorized", False)
    object.__setattr__(assessment, "complete_execution_fee_cost_authorized", False)
    object.__setattr__(assessment, "provider_write_authorized", False)
    object.__setattr__(assessment, "real_money_execution_authorized", False)
    object.__setattr__(assessment, "assessment_id", assessment_id)
    register_issued(assessment)
    return assessment


def _assessment_identity_payload(
    schema,
    schema_version,
    assessment: BetfairCommissionApplicabilityAssessment,
) -> dict[str, object]:
    return {
        "schema": schema,
        "schema_version": schema_version,
        "venue_id": assessment.venue_id,
        "account_id": assessment.account_id,
        "market_id": assessment.market_id,
        "currency_code": assessment.currency_code,
        "region": assessment.region,
        "fee_input_sha256": assessment.fee_input_sha256,
        "account_source_payload_sha256": assessment.account_source_payload_sha256,
        "market_source_payload_sha256": assessment.market_source_payload_sha256,
        "status": assessment.status.value,
        "reasons": [reason.value for reason in assessment.reasons],
        "ruleset_id": assessment.ruleset_id,
        "provider_rule_sources": list(assessment.provider_rule_sources),
        "prospective_commission_amount_authorized": (
            assessment.prospective_commission_amount_authorized
        ),
        "complete_execution_fee_cost_authorized": (
            assessment.complete_execution_fee_cost_authorized
        ),
        "provider_write_authorized": assessment.provider_write_authorized,
        "real_money_execution_authorized": assessment.real_money_execution_authorized,
    }


# Compatibility names deliberately carry no product authority. Older focused tests may
# rebind or mutate them, but canonical issuance and verification below own a separate
# closure-local registry and lock that are not reachable through these module names.
_ISSUED_BY_ID: dict[
    int,
    tuple[ReferenceType[BetfairCommissionApplicabilityAssessment], str],
] = {}
_ISSUE_LOCK = RLock()


def _register_issued(_assessment: BetfairCommissionApplicabilityAssessment) -> None:
    """Non-authoritative compatibility hook; production does not call this."""

    raise BetfairCommissionApplicabilityError(
        "module-level assessment registration is not product authority"
    )


def _build_product_boundary():
    # Capture the exact authority graph once. Module-global rebinding after import
    # cannot substitute provider reads, identity builders, issuance state or verifier
    # roots. Registry state is intentionally closure-local.
    fee_input_reader = read_betfair_execution_fee_inputs
    payload_builder = _ASSESSMENT_PAYLOAD_CAPABILITY
    canonical_json = _canonical_json
    hash_constructor = sha256
    assessment_type = BetfairCommissionApplicabilityAssessment
    error_type = BetfairCommissionApplicabilityError
    unproven_status = BetfairCommissionApplicabilityStatus.UNPROVEN
    canonical_reasons = _CANONICAL_REASONS
    ruleset_id = _RULESET_ID
    provider_rule_sources = _PROVIDER_RULE_SOURCES
    schema = _SCHEMA
    schema_version = _SCHEMA_VERSION
    identity_payload_builder = _assessment_identity_payload

    issued_by_id: dict[
        int,
        tuple[ReferenceType[BetfairCommissionApplicabilityAssessment], str],
    ] = {}
    issue_lock = RLock()

    def register_issued(
        assessment: BetfairCommissionApplicabilityAssessment,
    ) -> None:
        object_id = id(assessment)
        issued_assessment_id = assessment.assessment_id

        def cleanup(
            reference: ReferenceType[BetfairCommissionApplicabilityAssessment],
        ) -> None:
            with issue_lock:
                record = issued_by_id.get(object_id)
                if record is not None and record[0] is reference:
                    issued_by_id.pop(object_id, None)

        reference = ref(assessment, cleanup)
        with issue_lock:
            issued_by_id[object_id] = (reference, issued_assessment_id)

    issue_assessment = partial(
        _issue_assessment,
        payload_builder,
        canonical_json,
        hash_constructor,
        assessment_type,
        unproven_status,
        canonical_reasons,
        ruleset_id,
        provider_rule_sources,
        register_issued,
    )

    def assess(
        client: BetfairReadOnlyClient,
        *,
        market_id: str,
    ) -> BetfairCommissionApplicabilityAssessment:
        observation = fee_input_reader(client, market_id=market_id)
        return issue_assessment(observation)

    def require_product(
        assessment: BetfairCommissionApplicabilityAssessment,
    ) -> BetfairCommissionApplicabilityAssessment:
        if type(assessment) is not assessment_type:
            raise error_type(
                "assessment must be exact BetfairCommissionApplicabilityAssessment"
            )
        with issue_lock:
            record = issued_by_id.get(id(assessment))
            if record is None or record[0]() is not assessment:
                raise error_type("assessment is not product-issued in this process")
            issued_assessment_id = record[1]

        if assessment.assessment_id != issued_assessment_id:
            raise error_type("assessment changed after product issuance")
        if (
            assessment.status is not unproven_status
            or assessment.reasons != canonical_reasons
            or assessment.ruleset_id != ruleset_id
            or assessment.provider_rule_sources != provider_rule_sources
            or assessment.prospective_commission_amount_authorized is not False
            or assessment.complete_execution_fee_cost_authorized is not False
            or assessment.provider_write_authorized is not False
            or assessment.real_money_execution_authorized is not False
        ):
            raise error_type(
                "assessment exceeds the canonical fail-closed applicability boundary"
            )

        payload = identity_payload_builder(schema, schema_version, assessment)
        expected_id = hash_constructor(canonical_json(payload)).hexdigest()
        if assessment.assessment_id != expected_id:
            raise error_type("assessment identity is inconsistent")
        return assessment

    return assess, require_product, issue_assessment


(
    assess_betfair_commission_applicability,
    require_product_betfair_commission_applicability,
    _ISSUE_ASSESSMENT_CAPABILITY,
) = _build_product_boundary()

assess_betfair_commission_applicability.__name__ = (
    "assess_betfair_commission_applicability"
)
assess_betfair_commission_applicability.__qualname__ = (
    "assess_betfair_commission_applicability"
)
assess_betfair_commission_applicability.__doc__ = (
    "Re-read canonical authenticated fee inputs and issue bounded UNPROVEN truth."
)

require_product_betfair_commission_applicability.__name__ = (
    "require_product_betfair_commission_applicability"
)
require_product_betfair_commission_applicability.__qualname__ = (
    "require_product_betfair_commission_applicability"
)
require_product_betfair_commission_applicability.__doc__ = (
    "Require exact closure-local product issuance and unchanged fail-closed identity."
)
