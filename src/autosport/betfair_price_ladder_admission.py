"""Fail-closed Betfair market-specific LIMIT price-ladder admission.

This module proves only whether one exact Decimal price lies on the provider
market's canonically acquired price ladder. It grants no stake, funds, liquidity,
provider-write, fill, settlement, profitability, or real-money authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from weakref import ref

from .betfair_account_readonly import (
    BetfairMarketPriceLadderObservation,
    assert_market_price_ladder_authoritative,
    market_price_ladder_acquisition_started_at,
)


class BetfairPriceLadderAdmissionState(str, Enum):
    UNKNOWN_UNPROVEN = "UNKNOWN_UNPROVEN"
    PRICE_LADDER_ADMISSIBLE = "PRICE_LADDER_ADMISSIBLE"
    PRICE_LADDER_INVALID = "PRICE_LADDER_INVALID"
    UNSUPPORTED_LADDER_SEMANTICS = "UNSUPPORTED_LADDER_SEMANTICS"


BETFAIR_PRICE_LADDER_ADMISSION_TRUST_BOUNDARY = (
    "trusted-process-api-provenance-v1"
)


_CLASSIC_BANDS: tuple[tuple[Decimal, Decimal, Decimal], ...] = (
    (Decimal("1.01"), Decimal("2.00"), Decimal("0.01")),
    (Decimal("2.00"), Decimal("3.00"), Decimal("0.02")),
    (Decimal("3.00"), Decimal("4.00"), Decimal("0.05")),
    (Decimal("4.00"), Decimal("6.00"), Decimal("0.10")),
    (Decimal("6.00"), Decimal("10.00"), Decimal("0.20")),
    (Decimal("10.00"), Decimal("20.00"), Decimal("0.50")),
    (Decimal("20.00"), Decimal("30.00"), Decimal("1.00")),
    (Decimal("30.00"), Decimal("50.00"), Decimal("2.00")),
    (Decimal("50.00"), Decimal("100.00"), Decimal("5.00")),
    (Decimal("100.00"), Decimal("1000.00"), Decimal("10.00")),
)


def _build_result_meta():
    sealed: set[type] = set()
    protected = frozenset({"admissible"})

    class _ResultMeta(type):
        def __setattr__(cls, name: str, value: object) -> None:
            if cls in sealed and name in protected:
                raise TypeError(
                    "Betfair price-ladder authority surface is sealed: "
                    + name
                )
            super().__setattr__(name, value)

        def __delattr__(cls, name: str) -> None:
            if cls in sealed and name in protected:
                raise TypeError(
                    "Betfair price-ladder authority surface is sealed: "
                    + name
                )
            super().__delattr__(name)

        @classmethod
        def seal(mcls, cls: type) -> None:
            sealed.add(cls)

    return _ResultMeta


_ResultMeta = _build_result_meta()
del _build_result_meta


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairPriceLadderAdmission(metaclass=_ResultMeta):
    state: BetfairPriceLadderAdmissionState
    evidence_digest: str
    reasons: tuple[str, ...]
    provider_id: str
    account_id: str
    market_id: str
    price: Decimal
    ladder_type: str
    acquisition_started_at: datetime
    response_received_at: datetime
    decision_at: datetime
    source_payload_sha256: str

def _exact_price(value: object) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ValueError("price must be a positive finite exact Decimal")
    return value


def _exact_positive_age(value: object) -> timedelta:
    if type(value) is not timedelta or value <= timedelta(0):
        raise ValueError("max_evidence_age must be a positive exact timedelta")
    return value


def _receipt_timestamp(value: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("receipt timestamp must be non-empty trimmed text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("receipt timestamp must be ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("receipt timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _on_grid(price: Decimal, lower: Decimal, step: Decimal) -> bool:
    """Return exact tick membership without consulting mutable Decimal context.

    Decimal arithmetic operators obey the process/thread decimal Context. Price
    admissibility is provider truth and must not change because unrelated code
    lowered precision or changed traps. as_integer_ratio() is exact and
    context-independent, so perform the divisibility proof over integers.
    """

    price_num, price_den = price.as_integer_ratio()
    lower_num, lower_den = lower.as_integer_ratio()
    step_num, step_den = step.as_integer_ratio()
    if step_num <= 0:
        raise ValueError("price ladder step must be positive")
    delta_num = price_num * lower_den - lower_num * price_den
    delta_den = price_den * lower_den
    return (delta_num * step_den) % (delta_den * step_num) == 0


def _classic_admissible(price: Decimal) -> bool:
    if price < Decimal("1.01") or price > Decimal("1000.00"):
        return False
    for lower, upper, step in _CLASSIC_BANDS:
        if lower <= price <= upper:
            return _on_grid(price, lower, step)
    return False


def _finest_admissible(price: Decimal) -> bool:
    return (
        Decimal("1.01") <= price <= Decimal("1000.00")
        and _on_grid(price, Decimal("1.01"), Decimal("0.01"))
    )


def _canonical_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("evidence Decimal must be finite and exact")
    sign, digits, exponent = value.as_tuple()
    canonical_digits = list(digits)
    canonical_exponent = exponent
    while len(canonical_digits) > 1 and canonical_digits[-1] == 0:
        canonical_digits.pop()
        canonical_exponent += 1
    if not canonical_digits or all(digit == 0 for digit in canonical_digits):
        return "0e0"
    prefix = "-" if sign else ""
    return (
        prefix
        + "".join(str(digit) for digit in canonical_digits)
        + "e"
        + str(canonical_exponent)
    )


def _timestamp(value: datetime) -> str:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("evidence timestamp must be exact timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


def _result_fingerprint(result: BetfairPriceLadderAdmission) -> str:
    return _digest(
        {
            "schema": "autosport.betfair-price-ladder-admission-result.v1",
            "state": result.state.value,
            "evidence_digest": result.evidence_digest,
            "reasons": list(result.reasons),
            "provider_id": result.provider_id,
            "account_id": result.account_id,
            "market_id": result.market_id,
            "price": _canonical_decimal(result.price),
            "ladder_type": result.ladder_type,
            "acquisition_started_at": _timestamp(
                result.acquisition_started_at
            ),
            "response_received_at": _timestamp(result.response_received_at),
            "decision_at": _timestamp(result.decision_at),
            "source_payload_sha256": result.source_payload_sha256,
        }
    )


def _assess_unsealed(
    observation: BetfairMarketPriceLadderObservation,
    price: Decimal,
    *,
    max_evidence_age: timedelta,
) -> BetfairPriceLadderAdmission:
    if type(observation) is not BetfairMarketPriceLadderObservation:
        raise TypeError(
            "observation must be exact BetfairMarketPriceLadderObservation"
        )
    exact_price = _exact_price(price)
    max_age = _exact_positive_age(max_evidence_age)

    acquisition_started_at = market_price_ladder_acquisition_started_at(
        observation
    ).astimezone(timezone.utc)
    decision_at = assert_market_price_ladder_authoritative(
        observation
    ).astimezone(timezone.utc)
    response_received_at = _receipt_timestamp(
        observation.evidence.observed_at
    )

    reasons: list[str] = []
    if observation.venue_id != "betfair":
        reasons.append("PROVIDER_ID_MISMATCH")
    if response_received_at < acquisition_started_at:
        reasons.append("RESPONSE_BEFORE_ACQUISITION")
    if response_received_at > decision_at:
        reasons.append("RESPONSE_AFTER_DECISION")
    if acquisition_started_at > decision_at:
        reasons.append("FUTURE_MARKET_DESCRIPTION")
    if decision_at - acquisition_started_at > max_age:
        reasons.append("STALE_MARKET_DESCRIPTION")

    ladder_type = observation.ladder_type
    mechanically_admissible = False
    if ladder_type == "CLASSIC":
        mechanically_admissible = _classic_admissible(exact_price)
    elif ladder_type == "FINEST":
        mechanically_admissible = _finest_admissible(exact_price)
    elif ladder_type == "LINE_RANGE":
        reasons.append("LINE_RANGE_EXECUTION_SEMANTICS_UNBOUND")
    else:
        reasons.append("UNSUPPORTED_PRICE_LADDER_TYPE")

    unsupported_reasons = {
        "LINE_RANGE_EXECUTION_SEMANTICS_UNBOUND",
        "UNSUPPORTED_PRICE_LADDER_TYPE",
    }
    authority_reasons = [
        reason for reason in reasons if reason not in unsupported_reasons
    ]
    if authority_reasons:
        state = BetfairPriceLadderAdmissionState.UNKNOWN_UNPROVEN
    elif any(reason in unsupported_reasons for reason in reasons):
        state = (
            BetfairPriceLadderAdmissionState.UNSUPPORTED_LADDER_SEMANTICS
        )
    elif mechanically_admissible:
        state = BetfairPriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    else:
        state = BetfairPriceLadderAdmissionState.PRICE_LADDER_INVALID

    evidence_digest = _digest(
        {
            "schema": "autosport.betfair-price-ladder-admission-evidence.v1",
            "provider_id": observation.venue_id,
            "account_id": observation.account_id,
            "market_id": observation.market_id,
            "price": _canonical_decimal(exact_price),
            "ladder_type": observation.ladder_type,
            "line_range_min": _canonical_decimal(
                observation.line_range_min
            ),
            "line_range_max": _canonical_decimal(
                observation.line_range_max
            ),
            "line_range_interval": _canonical_decimal(
                observation.line_range_interval
            ),
            "line_range_unit": observation.line_range_unit,
            "request_scope_sha256": observation.request_scope_sha256,
            "source_payload_sha256": (
                observation.evidence.source_payload_sha256
            ),
            "acquisition_started_at": _timestamp(
                acquisition_started_at
            ),
            "response_received_at": _timestamp(response_received_at),
            "decision_at": _timestamp(decision_at),
            "max_evidence_age_microseconds": (
                max_age.days * 86_400_000_000
                + max_age.seconds * 1_000_000
                + max_age.microseconds
            ),
            "state": state.value,
            "reasons": reasons,
        }
    )

    return BetfairPriceLadderAdmission(
        state=state,
        evidence_digest=evidence_digest,
        reasons=tuple(reasons),
        provider_id=observation.venue_id,
        account_id=observation.account_id,
        market_id=observation.market_id,
        price=exact_price,
        ladder_type=observation.ladder_type,
        acquisition_started_at=acquisition_started_at,
        response_received_at=response_received_at,
        decision_at=decision_at,
        source_payload_sha256=observation.evidence.source_payload_sha256,
    )


def _install_price_ladder_admission_authority():
    issued: dict[
        int,
        tuple[
            object,
            str,
            BetfairMarketPriceLadderObservation,
            timedelta,
        ],
    ] = {}
    raw_assess = _assess_unsealed
    raw_assess_code = raw_assess.__code__
    fingerprint = _result_fingerprint
    fingerprint_code = fingerprint.__code__
    result_type = BetfairPriceLadderAdmission
    observation_type = BetfairMarketPriceLadderObservation
    acquisition = market_price_ladder_acquisition_started_at
    acquisition_code = acquisition.__code__
    assert_authoritative = assert_market_price_ladder_authoritative
    assert_authoritative_code = assert_authoritative.__code__
    classic = _classic_admissible
    classic_code = classic.__code__
    finest = _finest_admissible
    finest_code = finest.__code__
    receipt_timestamp = _receipt_timestamp
    receipt_timestamp_code = receipt_timestamp.__code__
    digest = _digest
    digest_code = digest.__code__
    exact_price = _exact_price
    exact_price_code = exact_price.__code__
    exact_positive_age = _exact_positive_age
    exact_positive_age_code = exact_positive_age.__code__
    canonical_decimal = _canonical_decimal
    canonical_decimal_code = canonical_decimal.__code__
    timestamp = _timestamp
    timestamp_code = timestamp.__code__
    on_grid = _on_grid
    on_grid_code = on_grid.__code__
    state_type = BetfairPriceLadderAdmissionState
    decimal_type = Decimal
    timedelta_type = timedelta
    datetime_type = datetime
    timezone_module = timezone
    json_module = json
    sha256_function = sha256
    weakref_ref = ref
    classic_bands = _CLASSIC_BANDS
    exact_type = type

    def canonical_dispatch_intact() -> bool:
        return (
            _assess_unsealed is raw_assess
            and raw_assess.__code__ is raw_assess_code
            and _result_fingerprint is fingerprint
            and fingerprint.__code__ is fingerprint_code
            and BetfairPriceLadderAdmission is result_type
            and BetfairMarketPriceLadderObservation is observation_type
            and market_price_ladder_acquisition_started_at is acquisition
            and acquisition.__code__ is acquisition_code
            and assert_market_price_ladder_authoritative
            is assert_authoritative
            and assert_authoritative.__code__ is assert_authoritative_code
            and _classic_admissible is classic
            and classic.__code__ is classic_code
            and _finest_admissible is finest
            and finest.__code__ is finest_code
            and _receipt_timestamp is receipt_timestamp
            and receipt_timestamp.__code__ is receipt_timestamp_code
            and _digest is digest
            and digest.__code__ is digest_code
            and _exact_price is exact_price
            and exact_price.__code__ is exact_price_code
            and _exact_positive_age is exact_positive_age
            and exact_positive_age.__code__ is exact_positive_age_code
            and _canonical_decimal is canonical_decimal
            and canonical_decimal.__code__ is canonical_decimal_code
            and _timestamp is timestamp
            and timestamp.__code__ is timestamp_code
            and _on_grid is on_grid
            and on_grid.__code__ is on_grid_code
            and BetfairPriceLadderAdmissionState is state_type
            and Decimal is decimal_type
            and timedelta is timedelta_type
            and datetime is datetime_type
            and timezone is timezone_module
            and json is json_module
            and sha256 is sha256_function
            and ref is weakref_ref
            and _CLASSIC_BANDS is classic_bands
        )

    def assess(
        observation: BetfairMarketPriceLadderObservation,
        price: Decimal,
        *,
        max_evidence_age: timedelta,
    ) -> BetfairPriceLadderAdmission:
        # The exact-ingress contract must not depend on mutable builtins.type.
        # Otherwise a Decimal subclass can spoof exact-type admission and
        # override arithmetic methods used by the mechanical tick-grid proof.
        if exact_type(observation) is not observation_type:
            raise TypeError(
                "observation must be exact BetfairMarketPriceLadderObservation"
            )
        if (
            exact_type(price) is not decimal_type
            or not price.is_finite()
            or price <= 0
        ):
            raise ValueError("price must be a positive finite exact Decimal")
        if (
            exact_type(max_evidence_age) is not timedelta_type
            or max_evidence_age <= timedelta_type(0)
        ):
            raise ValueError(
                "max_evidence_age must be a positive exact timedelta"
            )
        if not canonical_dispatch_intact():
            raise RuntimeError(
                "canonical Betfair price-ladder assessor changed"
            )
        max_age = exact_positive_age(max_evidence_age)
        result = raw_assess(
            observation,
            price,
            max_evidence_age=max_age,
        )
        if exact_type(result) is not result_type:
            raise TypeError("price-ladder assessor returned invalid result type")
        # Close the final issuance race against a concurrently observed
        # incompatible MarketDescription revision. The result registry also keeps
        # the exact provider receipt so later positive-property checks can
        # revalidate its current definition generation instead of turning a
        # historical admissible state into timeless authority.
        acquisition(observation)
        result_id = id(result)
        result_fingerprint = fingerprint(result)

        def forget(current: object, *, key: int = result_id) -> None:
            existing = issued.get(key)
            if existing is not None and existing[0] is current:
                issued.pop(key, None)

        issued[result_id] = (
            ref(result, forget),
            result_fingerprint,
            observation,
            max_age,
        )
        return result

    def is_authoritative(result: BetfairPriceLadderAdmission) -> bool:
        if not canonical_dispatch_intact() or exact_type(result) is not result_type:
            return False
        current = issued.get(id(result))
        if current is None or current[0]() is not result:
            return False
        try:
            # A positive assessment is a time-bounded witness, not timeless
            # authority. Re-read the canonical product decision clock on every
            # authority check so a cached result cannot stay admissible after the
            # exact max_evidence_age budget used to issue it has elapsed.
            current_decision_at = assert_authoritative(current[2]).astimezone(
                timezone_module.utc
            )
            if (
                current_decision_at < result.decision_at
                or current_decision_at < result.response_received_at
                or current_decision_at < result.acquisition_started_at
                or current_decision_at - result.acquisition_started_at
                > current[3]
            ):
                return False
            return current[1] == fingerprint(result)
        except Exception:
            return False

    def admissible(result: BetfairPriceLadderAdmission) -> bool:
        return (
            exact_type(result) is result_type
            and result.state is state_type.PRICE_LADDER_ADMISSIBLE
            and is_authoritative(result)
        )

    return assess, property(admissible)


(
    assess_betfair_price_ladder_admission,
    _price_ladder_admissible_property,
) = _install_price_ladder_admission_authority()
BetfairPriceLadderAdmission.admissible = _price_ladder_admissible_property
_ResultMeta.seal(BetfairPriceLadderAdmission)
del _ResultMeta
del _price_ladder_admissible_property
del _install_price_ladder_admission_authority
