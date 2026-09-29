"""Fail-closed Betfair prospective commission applicability evidence.

Authenticated Betfair fee inputs are necessary but not sufficient proof of the
prospective commission tariff that applies to an account/market. This module computes
only a canonical negative prerequisite: applicability remains UNPROVEN until the
missing tariff/event/additional-charge/terminal-net-win authorities exist.

``assessment_id`` is an integrity checksum over that negative assessment. It is not an
authenticity, provenance, or process-local issuance proof. In-process Python reflection
can reach and mutate ordinary Python registries/closure cells, so this module does not
pretend such mutable state is a security boundary. Positive commission/execution
authority must come from future independently verifiable product authorities.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from hashlib import sha256
import json
from threading import RLock
from weakref import ReferenceType

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
    """Raised when commission applicability evidence is malformed or exceeds policy."""


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
        "market_source_payload_sha256": observation.market_evidence.source_payload_sha256,
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
    """Canonical fail-closed negative prerequisite for prospective commission truth.

    Instances returned by :func:`assess_betfair_commission_applicability` are derived
    from the canonical read-only provider observation path. The value itself carries no
    unforgeable provenance token; all four positive authority flags are necessarily
    false and validation never upgrades them.
    """

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
            "direct assessment construction is not canonical applicability evaluation"
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
        "market_source_payload_sha256": observation.market_evidence.source_payload_sha256,
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


def _issue_assessment(*_args: object, **_kwargs: object):
    """Compatibility rejection: no module-level bearer can mint positive authority."""

    raise BetfairCommissionApplicabilityError(
        "direct assessment issuance is not product authority"
    )


# Legacy diagnostic names intentionally carry no authority and are never consulted by
# canonical validation. Keeping them temporarily avoids turning old instrumentation
# into an accidental import break while making the absence of registry provenance
# explicit and mechanically testable.
_ISSUED_BY_ID: dict[
    int,
    tuple[ReferenceType[BetfairCommissionApplicabilityAssessment], str],
] = {}
_ISSUE_LOCK = RLock()
_ISSUE_ASSESSMENT_CAPABILITY = _issue_assessment


def _assessment_identity_payload(
    assessment: BetfairCommissionApplicabilityAssessment,
    *,
    schema: str,
    schema_version: int,
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


def _build_product_boundary():
    # Capture the exact negative-authority graph once. Assessment construction stays
    # inside assess() so the normal product path always starts with the canonical
    # provider read. Validation deliberately proves only canonical negative structure
    # and checksum consistency; it does not claim in-process issuance provenance.
    fee_input_reader = read_betfair_execution_fee_inputs
    if type(fee_input_reader) is not partial:
        raise BetfairCommissionApplicabilityError(
            "fee input reader must be exact sealed functools.partial"
        )
    fee_input_reader_function = fee_input_reader.func
    fee_input_reader_function_code = fee_input_reader_function.__code__
    fee_input_reader_function_globals = fee_input_reader_function.__globals__
    fee_input_reader_function_closure = fee_input_reader_function.__closure__
    fee_input_reader_function_closure_values = tuple(
        cell.cell_contents for cell in (fee_input_reader_function_closure or ())
    )
    fee_input_reader_args = fee_input_reader.args
    fee_input_reader_keywords = dict(fee_input_reader.keywords)

    # The sealed fee-input reader is itself a partial graph. `partial.args` identity
    # alone does not freeze the Python function objects reachable through those args:
    # their `__code__` can be replaced in-place while every outer identity remains
    # unchanged. Capture that already-canonical graph once so the applicability layer
    # cannot bind provider payload hashes to values produced by a retargeted parser.
    function_type = type(fee_input_reader_function)
    reader_partial_witness_list: list[
        tuple[object, object, object, tuple[tuple[str, object], ...]]
    ] = []
    reader_function_witness_list: list[
        tuple[object, object, object, object, tuple[object, ...]]
    ] = []
    seen_reader_dependencies: set[int] = set()

    def capture_reader_dependency(value: object) -> None:
        identity = id(value)
        if identity in seen_reader_dependencies:
            return
        if type(value) is partial:
            seen_reader_dependencies.add(identity)
            keyword_items = tuple((value.keywords or {}).items())
            reader_partial_witness_list.append(
                (value, value.func, value.args, keyword_items)
            )
            capture_reader_dependency(value.func)
            for nested in value.args:
                capture_reader_dependency(nested)
            for _key, nested in keyword_items:
                capture_reader_dependency(nested)
            return
        if type(value) is function_type:
            seen_reader_dependencies.add(identity)
            closure = value.__closure__
            closure_values = tuple(
                cell.cell_contents for cell in (closure or ())
            )
            reader_function_witness_list.append(
                (value, value.__code__, value.__globals__, closure, closure_values)
            )
            for nested in closure_values:
                capture_reader_dependency(nested)

    capture_reader_dependency(fee_input_reader)
    reader_partial_witnesses = tuple(reader_partial_witness_list)
    reader_function_witnesses = tuple(reader_function_witness_list)

    fee_payload_builder = _FEE_INPUT_SHA256_CAPABILITY
    fee_payload_function = fee_payload_builder.func
    fee_payload_function_code = fee_payload_function.__code__
    fee_payload_function_globals = fee_payload_function.__globals__
    fee_payload_args = fee_payload_builder.args
    fee_input_payload_builder = fee_payload_args[0]
    fee_input_payload_builder_code = fee_input_payload_builder.__code__
    fee_input_payload_builder_globals = fee_input_payload_builder.__globals__
    canonical_json = _canonical_json
    canonical_json_code = canonical_json.__code__
    canonical_json_globals = canonical_json.__globals__
    json_module = json
    json_dumps = json.dumps
    hash_constructor = sha256
    assessment_type = BetfairCommissionApplicabilityAssessment
    error_type = BetfairCommissionApplicabilityError
    observation_type = BetfairExecutionFeeInputsObservation
    unproven_status = BetfairCommissionApplicabilityStatus.UNPROVEN
    canonical_reasons = _CANONICAL_REASONS
    ruleset_id = _RULESET_ID
    provider_rule_sources = _PROVIDER_RULE_SOURCES
    schema = _SCHEMA
    schema_version = _SCHEMA_VERSION
    identity_payload_builder = _assessment_identity_payload
    identity_payload_builder_code = identity_payload_builder.__code__
    identity_payload_builder_globals = identity_payload_builder.__globals__

    executable_graph_witness_marker = (
        "__AUTOSPORT_BETFAIR_COMMISSION_EXECUTABLE_GRAPH_WITNESS__"
    )
    nested_reader_graph_witness_marker = (
        "__AUTOSPORT_BETFAIR_COMMISSION_NESTED_READER_GRAPH_WITNESS__"
    )

    def require_executable_authority() -> None:
        (
            reader_code_witness,
            fee_payload_code_witness,
            fee_input_payload_code_witness,
            canonical_json_code_witness,
            identity_payload_code_witness,
        ) = "__AUTOSPORT_BETFAIR_COMMISSION_EXECUTABLE_GRAPH_WITNESS__"
        (
            trusted_fee_input_reader,
            nested_partial_witnesses,
            nested_function_witnesses,
        ) = "__AUTOSPORT_BETFAIR_COMMISSION_NESTED_READER_GRAPH_WITNESS__"

        # Keep the historical expected-code/graph cells inspectable for diagnostics and
        # adversarial tests, but never use those mutable cells as the trust root.
        _ = (
            fee_input_reader_function_code,
            fee_payload_function_code,
            fee_input_payload_builder_code,
            canonical_json_code,
            identity_payload_builder_code,
            reader_partial_witnesses,
            reader_function_witnesses,
        )

        if (
            fee_input_reader is not trusted_fee_input_reader
            or type(fee_input_reader) is not partial
            or fee_input_reader.func is not fee_input_reader_function
            or fee_input_reader.args is not fee_input_reader_args
            or fee_input_reader.keywords != fee_input_reader_keywords
            or fee_input_reader_function.__code__ is not reader_code_witness
            or fee_input_reader_function.__globals__ is not fee_input_reader_function_globals
            or fee_input_reader_function.__closure__ is not fee_input_reader_function_closure
        ):
            raise error_type("fee input reader executable authority changed")
        current_closure = fee_input_reader_function.__closure__ or ()
        if len(current_closure) != len(fee_input_reader_function_closure_values):
            raise error_type("fee input reader closure authority changed")
        for cell, expected in zip(
            current_closure,
            fee_input_reader_function_closure_values,
        ):
            try:
                current = cell.cell_contents
            except ValueError as exc:
                raise error_type("fee input reader closure authority changed") from exc
            if current is not expected:
                raise error_type("fee input reader closure authority changed")

        for (
            nested,
            expected_func,
            expected_args,
            expected_keyword_items,
        ) in nested_partial_witnesses:
            if (
                type(nested) is not partial
                or nested.func is not expected_func
                or nested.args is not expected_args
            ):
                raise error_type(
                    "fee input reader nested executable authority changed"
                )
            current_keywords = nested.keywords or {}
            if len(current_keywords) != len(expected_keyword_items):
                raise error_type(
                    "fee input reader nested executable authority changed"
                )
            for key, expected in expected_keyword_items:
                if key not in current_keywords or current_keywords[key] is not expected:
                    raise error_type(
                        "fee input reader nested executable authority changed"
                    )
        for (
            nested_function,
            expected_code,
            expected_globals,
            expected_closure,
            expected_closure_values,
        ) in nested_function_witnesses:
            if (
                type(nested_function) is not function_type
                or nested_function.__code__ is not expected_code
                or nested_function.__globals__ is not expected_globals
                or nested_function.__closure__ is not expected_closure
            ):
                raise error_type(
                    "fee input reader nested executable authority changed"
                )
            current_nested_closure = nested_function.__closure__ or ()
            if len(current_nested_closure) != len(expected_closure_values):
                raise error_type(
                    "fee input reader nested executable authority changed"
                )
            for cell, expected in zip(
                current_nested_closure,
                expected_closure_values,
            ):
                try:
                    current = cell.cell_contents
                except ValueError as exc:
                    raise error_type(
                        "fee input reader nested executable authority changed"
                    ) from exc
                if current is not expected:
                    raise error_type(
                        "fee input reader nested executable authority changed"
                    )

        if (
            fee_payload_builder.func is not fee_payload_function
            or fee_payload_builder.args != fee_payload_args
            or fee_payload_function.__code__ is not fee_payload_code_witness
            or fee_payload_function.__globals__ is not fee_payload_function_globals
            or fee_input_payload_builder.__code__ is not fee_input_payload_code_witness
            or fee_input_payload_builder.__globals__ is not fee_input_payload_builder_globals
            or fee_payload_args[1] is not canonical_json
            or fee_payload_args[2] is not hash_constructor
        ):
            raise error_type("fee input identity executable authority changed")

        if (
            canonical_json.__code__ is not canonical_json_code_witness
            or canonical_json.__globals__ is not canonical_json_globals
            or canonical_json_globals.get("json") is not json_module
            or getattr(json_module, "dumps", None) is not json_dumps
            or identity_payload_builder.__code__ is not identity_payload_code_witness
            or identity_payload_builder.__globals__ is not identity_payload_builder_globals
        ):
            raise error_type("assessment identity executable authority changed")

    guard_constants = require_executable_authority.__code__.co_consts
    if sum(item == executable_graph_witness_marker for item in guard_constants) != 1:
        raise error_type("commission applicability executable graph anchor is ambiguous")
    if sum(item == nested_reader_graph_witness_marker for item in guard_constants) != 1:
        raise error_type("commission applicability nested reader graph anchor is ambiguous")
    immutable_code_witnesses = (
        fee_input_reader_function_code,
        fee_payload_function_code,
        fee_input_payload_builder_code,
        canonical_json_code,
        identity_payload_builder_code,
    )
    immutable_nested_reader_witnesses = (
        fee_input_reader,
        reader_partial_witnesses,
        reader_function_witnesses,
    )
    require_executable_authority.__code__ = (
        require_executable_authority.__code__.replace(
            co_consts=tuple(
                immutable_code_witnesses
                if item == executable_graph_witness_marker
                else immutable_nested_reader_witnesses
                if item == nested_reader_graph_witness_marker
                else item
                for item in guard_constants
            )
        )
    )

    authority_guard_marker = "__AUTOSPORT_BETFAIR_COMMISSION_AUTHORITY_GUARD_ANCHOR__"

    def assess(
        client: BetfairReadOnlyClient,
        *,
        market_id: str,
    ) -> BetfairCommissionApplicabilityAssessment:
        authority, authority_code = "__AUTOSPORT_BETFAIR_COMMISSION_AUTHORITY_GUARD_ANCHOR__"
        if authority.__code__ is not authority_code:
            raise error_type("commission applicability executable authority guard changed")
        authority()
        observation = fee_input_reader(client, market_id=market_id)
        if authority.__code__ is not authority_code:
            raise error_type("commission applicability executable authority guard changed")
        authority()
        if type(observation) is not observation_type:
            raise error_type(
                "fee inputs must be exact BetfairExecutionFeeInputsObservation"
            )

        fee_input_sha256 = fee_payload_builder(observation)
        if authority.__code__ is not authority_code:
            raise error_type("commission applicability executable authority guard changed")
        authority()
        payload = {
            "schema": schema,
            "schema_version": schema_version,
            "venue_id": observation.venue_id,
            "account_id": observation.account_id,
            "market_id": observation.market_id,
            "currency_code": observation.currency_code,
            "region": observation.region,
            "fee_input_sha256": fee_input_sha256,
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
        assessment_id = hash_constructor(canonical_json(payload)).hexdigest()
        if authority.__code__ is not authority_code:
            raise error_type("commission applicability executable authority guard changed")
        authority()
        assessment = object.__new__(assessment_type)
        for name, value in (
            ("venue_id", observation.venue_id),
            ("account_id", observation.account_id),
            ("market_id", observation.market_id),
            ("currency_code", observation.currency_code),
            ("region", observation.region),
            ("fee_input_sha256", payload["fee_input_sha256"]),
            (
                "account_source_payload_sha256",
                observation.account_evidence.source_payload_sha256,
            ),
            (
                "market_source_payload_sha256",
                observation.market_evidence.source_payload_sha256,
            ),
            ("status", unproven_status),
            ("reasons", canonical_reasons),
            ("ruleset_id", ruleset_id),
            ("provider_rule_sources", provider_rule_sources),
            ("prospective_commission_amount_authorized", False),
            ("complete_execution_fee_cost_authorized", False),
            ("provider_write_authorized", False),
            ("real_money_execution_authorized", False),
            ("assessment_id", assessment_id),
        ):
            object.__setattr__(assessment, name, value)
        return assessment

    def validate(
        assessment: BetfairCommissionApplicabilityAssessment,
    ) -> BetfairCommissionApplicabilityAssessment:
        authority, authority_code = "__AUTOSPORT_BETFAIR_COMMISSION_AUTHORITY_GUARD_ANCHOR__"
        if authority.__code__ is not authority_code:
            raise error_type("commission applicability executable authority guard changed")
        authority()
        if type(assessment) is not assessment_type:
            raise error_type(
                "assessment must be exact BetfairCommissionApplicabilityAssessment"
            )
        try:
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
            payload = identity_payload_builder(
                assessment,
                schema=schema,
                schema_version=schema_version,
            )
        except AttributeError as exc:
            raise error_type(
                "assessment is incomplete canonical fail-closed applicability evidence"
            ) from exc
        if authority.__code__ is not authority_code:
            raise error_type("commission applicability executable authority guard changed")
        authority()
        if assessment.assessment_id != hash_constructor(canonical_json(payload)).hexdigest():
            raise error_type("assessment identity is inconsistent")
        if authority.__code__ is not authority_code:
            raise error_type("commission applicability executable authority guard changed")
        authority()
        return assessment

    authority_guard_witness = (
        require_executable_authority,
        require_executable_authority.__code__,
    )
    for boundary in (assess, validate):
        constants = boundary.__code__.co_consts
        if sum(item == authority_guard_marker for item in constants) != 1:
            raise error_type("commission applicability guard anchor is ambiguous")
        boundary.__code__ = boundary.__code__.replace(
            co_consts=tuple(
                authority_guard_witness if item == authority_guard_marker else item
                for item in constants
            )
        )

    return assess, validate


(
    assess_betfair_commission_applicability,
    validate_betfair_commission_applicability_assessment,
) = _build_product_boundary()

assess_betfair_commission_applicability.__name__ = (
    "assess_betfair_commission_applicability"
)
assess_betfair_commission_applicability.__qualname__ = (
    "assess_betfair_commission_applicability"
)
validate_betfair_commission_applicability_assessment.__name__ = (
    "validate_betfair_commission_applicability_assessment"
)
validate_betfair_commission_applicability_assessment.__qualname__ = (
    "validate_betfair_commission_applicability_assessment"
)

# Deprecated compatibility alias. This performs the same canonical negative structural
# validation only; despite the historical name it does NOT establish product issuance,
# authenticity, provenance, commission amount authority, provider-write authority, or
# real-money authority. New code must use the explicit ``validate_...`` name.
require_product_betfair_commission_applicability = (
    validate_betfair_commission_applicability_assessment
)
