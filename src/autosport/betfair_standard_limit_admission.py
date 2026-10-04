"""Canonical fail-closed Betfair standard-LIMIT size/payout admission seam.

This module composes existing authorities; it does not create provider/session,
jurisdiction, currency, or current-constraint provenance. Current dependencies
remain deliberately hard-false for remote-provider/current-rule proof, so positive
admission is unreachable until those canonical owners are strengthened.

No result here authorizes a provider write, fill, settlement, profitability, or
real-money execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from operator import attrgetter
from threading import RLock
from weakref import ReferenceType, ref

from .betfair_account_identity import (
    BetfairAccountIdentityError,
    BetfairAuthenticatedAccountIdentity,
    require_authoritative_betfair_account_identity,
)
from .betfair_account_readonly import BetfairReadOnlyClient
from .betfair_italy_order_admission import (
    ItalianLimitAdmissionState,
    ItalianLimitInstruction,
    evaluate_italian_limit_batch,
)
from .betfair_provider_constraint_evidence import (
    BetfairConstraintResolutionState,
    BetfairProviderConstraintResolution,
)
from .betfair_session_origin import (
    BetfairAuthenticatedJurisdiction,
    BetfairLoginJurisdiction,
    BetfairSessionOriginError,
    require_authoritative_betfair_authenticated_jurisdiction,
)
from .betfair_spain_guard import (
    SpainOrderAdmission,
    assess_betfair_spain_limit_order,
)


POLICY_VERSION = 1
ORDER_FAMILY = "LIMIT_STANDARD_SIZE"
_MAX_DECIMAL_DIGITS = 64
_MAX_ABS_EXPONENT = 18


class BetfairStandardLimitAdmissionError(ValueError):
    """Malformed standard-LIMIT admission request or authority graph."""


class BetfairStandardLimitAdmissionState(str, Enum):
    UNKNOWN_UNPROVEN = "UNKNOWN_UNPROVEN"
    STANDARD_MINIMUM_SATISFIED = "STANDARD_MINIMUM_SATISFIED"
    LOWER_MINIMUM_PAYOUT_SATISFIED = "LOWER_MINIMUM_PAYOUT_SATISFIED"
    BELOW_PROVIDER_MINIMUM = "BELOW_PROVIDER_MINIMUM"
    UNSUPPORTED_JURISDICTION_ORDER_SEMANTICS = (
        "UNSUPPORTED_JURISDICTION_ORDER_SEMANTICS"
    )


@dataclass(frozen=True, slots=True)
class BetfairStandardLimitAction:
    """Exact plain standard-size LIMIT projection consumed by #1505."""

    selection_id: int
    side: str
    size: Decimal
    price: Decimal
    order_type: str = "LIMIT"
    bet_target_type: str | None = None

    def __post_init__(self) -> None:
        if type(self.selection_id) is not int or self.selection_id <= 0:
            raise BetfairStandardLimitAdmissionError(
                "selection_id must be a positive integer"
            )
        if type(self.side) is not str or self.side not in {"BACK", "LAY"}:
            raise BetfairStandardLimitAdmissionError(
                "side must be exactly BACK or LAY"
            )
        _positive_decimal(self.size, "size")
        _positive_decimal(self.price, "price")
        if self.price <= Decimal("1"):
            raise BetfairStandardLimitAdmissionError(
                "price must be greater than 1"
            )
        if self.order_type != "LIMIT":
            raise BetfairStandardLimitAdmissionError(
                "only plain LIMIT belongs to this admission authority"
            )
        if self.bet_target_type is not None:
            raise BetfairStandardLimitAdmissionError(
                "target-size orders are outside standard-size LIMIT admission"
            )


def _build_result_meta():
    sealed: set[type] = set()
    protected = frozenset(
        {
            "state",
            "reason_codes",
            "as_of",
            "currency_code",
            "login_route",
            "submitted_payout",
            "account_identity_id",
            "jurisdiction_authority_id",
            "constraint_resolution_sha256",
            "policy_version",
            "result_sha256",
            "execution_authorized",
            "real_money_execution",
            "_execution_authorized_constant",
            "_real_money_execution_constant",
        }
    )

    class _ResultMeta(type):
        def __setattr__(cls, name: str, value: object) -> None:
            if cls in sealed and name in protected:
                raise TypeError(
                    "Betfair standard-LIMIT result authority surface is sealed: "
                    + name
                )
            super().__setattr__(name, value)

        def __delattr__(cls, name: str) -> None:
            if cls in sealed and name in protected:
                raise TypeError(
                    "Betfair standard-LIMIT result authority surface is sealed: "
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
class BetfairStandardLimitAdmission(metaclass=_ResultMeta):
    """Decision-time provider-minimum result; never provider-write authority."""

    state: BetfairStandardLimitAdmissionState
    reason_codes: tuple[str, ...]
    as_of: datetime
    currency_code: str | None
    login_route: str | None
    submitted_payout: Decimal
    account_identity_id: str | None
    jurisdiction_authority_id: str | None
    constraint_resolution_sha256: str | None
    policy_version: int
    result_sha256: str

    _execution_authorized_constant = False
    _real_money_execution_constant = False

    execution_authorized = property(
        attrgetter("_execution_authorized_constant")
    )
    real_money_execution = property(
        attrgetter("_real_money_execution_constant")
    )

    def __post_init__(self) -> None:
        if type(self.state) is not BetfairStandardLimitAdmissionState:
            raise BetfairStandardLimitAdmissionError(
                "state must be exact BetfairStandardLimitAdmissionState"
            )
        if type(self.reason_codes) is not tuple or any(
            type(reason) is not str or not reason for reason in self.reason_codes
        ):
            raise BetfairStandardLimitAdmissionError(
                "reason_codes must be an exact tuple of non-empty strings"
            )
        _utc(self.as_of, "as_of")
        if self.currency_code is not None:
            _currency(self.currency_code)
        if self.login_route is not None and (
            type(self.login_route) is not str or not self.login_route
        ):
            raise BetfairStandardLimitAdmissionError(
                "login_route must be non-empty text or None"
            )
        _positive_decimal(self.submitted_payout, "submitted_payout")
        for field, value in (
            ("account_identity_id", self.account_identity_id),
            ("jurisdiction_authority_id", self.jurisdiction_authority_id),
            (
                "constraint_resolution_sha256",
                self.constraint_resolution_sha256,
            ),
        ):
            if value is not None:
                _sha256_hex(value, field)
        if type(self.policy_version) is not int or self.policy_version != POLICY_VERSION:
            raise BetfairStandardLimitAdmissionError(
                "policy_version must be the current product-owned version"
            )
        _sha256_hex(self.result_sha256, "result_sha256")
        expected = _result_sha256(
            state=self.state,
            reason_codes=self.reason_codes,
            as_of=self.as_of,
            currency_code=self.currency_code,
            login_route=self.login_route,
            submitted_payout=self.submitted_payout,
            account_identity_id=self.account_identity_id,
            jurisdiction_authority_id=self.jurisdiction_authority_id,
            constraint_resolution_sha256=self.constraint_resolution_sha256,
        )
        if expected != self.result_sha256:
            raise BetfairStandardLimitAdmissionError(
                "result_sha256 does not match admission projection"
            )


_ResultMeta.seal(BetfairStandardLimitAdmission)


@dataclass(frozen=True, slots=True)
class _IssuedRecord:
    value_ref: ReferenceType[BetfairStandardLimitAdmission]
    fingerprint: str
    client: BetfairReadOnlyClient
    identity: BetfairAuthenticatedAccountIdentity
    jurisdiction: BetfairAuthenticatedJurisdiction
    constraints: BetfairProviderConstraintResolution


def _build_admission_authority():
    result_type = BetfairStandardLimitAdmission
    action_type = BetfairStandardLimitAction
    client_type = BetfairReadOnlyClient
    identity_type = BetfairAuthenticatedAccountIdentity
    jurisdiction_type = BetfairAuthenticatedJurisdiction
    constraint_type = BetfairProviderConstraintResolution

    identity_require = require_authoritative_betfair_account_identity
    jurisdiction_require = require_authoritative_betfair_authenticated_jurisdiction
    italy_evaluate = evaluate_italian_limit_batch
    italy_instruction_type = ItalianLimitInstruction
    italy_state_type = ItalianLimitAdmissionState
    spain_assess = assess_betfair_spain_limit_order
    spain_state_type = SpainOrderAdmission
    constraint_state_type = BetfairConstraintResolutionState
    jurisdiction_enum = BetfairLoginJurisdiction
    admission_state_type = BetfairStandardLimitAdmissionState
    error_type = BetfairStandardLimitAdmissionError
    record_type = _IssuedRecord

    snapshot_action_fn = _snapshot_action
    exact_multiply_fn = _exact_multiply
    utc_fn = _utc
    make_result_fn = _make_result
    fingerprint_fn = _fingerprint

    issued: dict[int, _IssuedRecord] = {}
    lock = RLock()

    def remember(
        value: BetfairStandardLimitAdmission,
        *,
        client: BetfairReadOnlyClient,
        identity: BetfairAuthenticatedAccountIdentity,
        jurisdiction: BetfairAuthenticatedJurisdiction,
        constraints: BetfairProviderConstraintResolution,
    ) -> None:
        key = id(value)

        def discard(dead_ref: ReferenceType[BetfairStandardLimitAdmission]) -> None:
            with lock:
                current = issued.get(key)
                if current is not None and current.value_ref is dead_ref:
                    issued.pop(key, None)

        with lock:
            issued[key] = record_type(
                ref(value, discard),
                fingerprint_fn(value),
                client,
                identity,
                jurisdiction,
                constraints,
            )

    def assess_betfair_standard_limit_admission(
        *,
        client: BetfairReadOnlyClient,
        account_identity: BetfairAuthenticatedAccountIdentity | None,
        jurisdiction: BetfairAuthenticatedJurisdiction | None,
        constraints: BetfairProviderConstraintResolution | None,
        action: BetfairStandardLimitAction,
        as_of: datetime,
    ) -> BetfairStandardLimitAdmission:
        if type(client) is not client_type:
            raise error_type(
                "client must be exact BetfairReadOnlyClient"
            )
        if type(action) is not action_type:
            raise error_type(
                "action must be exact BetfairStandardLimitAction"
            )
        current = utc_fn(as_of, "as_of")
        action_snapshot = snapshot_action_fn(action)
        payout = exact_multiply_fn(action_snapshot[2], action_snapshot[3])

        reasons: list[str] = []
        identity_value: BetfairAuthenticatedAccountIdentity | None = None
        jurisdiction_value: BetfairAuthenticatedJurisdiction | None = None
        constraint_value: BetfairProviderConstraintResolution | None = None

        if account_identity is None:
            reasons.append("AUTHENTICATED_ACCOUNT_IDENTITY_MISSING")
        elif type(account_identity) is not identity_type:
            raise error_type(
                "account_identity must be exact BetfairAuthenticatedAccountIdentity or None"
            )
        else:
            try:
                identity_value = identity_require(account_identity, client=client)
            except BetfairAccountIdentityError:
                reasons.append("K07_SESSION_CONTEXT_UNPROVEN")
            else:
                if identity_value.remote_provider_origin_proven is not True:
                    reasons.append("ACCOUNT_REMOTE_PROVIDER_ORIGIN_UNPROVEN")
                if identity_value.provider_account_details_origin_proven is not True:
                    reasons.append(
                        "ACCOUNT_DETAILS_REMOTE_PROVIDER_ORIGIN_UNPROVEN"
                    )

        if jurisdiction is None:
            reasons.append("AUTHENTICATED_JURISDICTION_MISSING")
        elif type(jurisdiction) is not jurisdiction_type:
            raise error_type(
                "jurisdiction must be exact BetfairAuthenticatedJurisdiction or None"
            )
        else:
            try:
                jurisdiction_value = jurisdiction_require(
                    jurisdiction,
                    client=client,
                )
            except BetfairSessionOriginError:
                reasons.append("LOGIN_ROUTE_CONTEXT_UNPROVEN")
            else:
                if jurisdiction_value.remote_provider_origin_proven is not True:
                    reasons.append("JURISDICTION_REMOTE_PROVIDER_ORIGIN_UNPROVEN")
                if (
                    jurisdiction_value.remote_provider_jurisdiction_proven
                    is not True
                ):
                    reasons.append(
                        "REMOTE_PROVIDER_JURISDICTION_CLASS_UNPROVEN"
                    )

        if identity_value is not None and jurisdiction_value is not None:
            if (
                jurisdiction_value.session_context_id
                != identity_value.session_context_id
                or jurisdiction_value.account_identity_id
                != identity_value.identity_id
            ):
                reasons.append("ACCOUNT_JURISDICTION_CONTEXT_MISMATCH")

        if constraints is None:
            reasons.append("CURRENT_CONSTRAINT_EVIDENCE_MISSING")
        elif type(constraints) is not constraint_type:
            raise error_type(
                "constraints must be exact BetfairProviderConstraintResolution or None"
            )
        else:
            constraint_value = constraints
            if constraints.as_of != current:
                reasons.append("CONSTRAINT_DECISION_CUT_MISMATCH")
            if constraints.state is not constraint_state_type.CONSISTENT_UNVERIFIED:
                reasons.append("CONSTRAINT_" + constraints.state.value)
            if constraints.provider_origin_proven is not True:
                reasons.append("CONSTRAINT_PROVIDER_ORIGIN_UNPROVEN")
            if constraints.current_constraint_authority is not True:
                reasons.append("CURRENT_CONSTRAINT_AUTHORITY_UNPROVEN")
            if identity_value is not None and (
                constraints.currency_code != identity_value.currency_code
            ):
                reasons.append("ACCOUNT_CONSTRAINT_CURRENCY_MISMATCH")

        route = (
            None
            if jurisdiction_value is None
            else jurisdiction_value.jurisdiction.value
        )
        if jurisdiction_value is not None:
            if jurisdiction_value.jurisdiction is jurisdiction_enum.GLOBAL_COM:
                reasons.append("GLOBAL_COM_ECONOMIC_PROFILE_UNPROVEN")
            elif jurisdiction_value.jurisdiction in {
                jurisdiction_enum.AUSTRALIA_NEW_ZEALAND,
                jurisdiction_enum.ROMANIA,
            }:
                reasons.append("JURISDICTION_ORDER_SEMANTICS_UNQUALIFIED")

        italy_reasons: tuple[str, ...] = ()
        if (
            jurisdiction_value is not None
            and jurisdiction_value.jurisdiction is jurisdiction_enum.ITALY
        ):
            italy_result = italy_evaluate(
                (
                    italy_instruction_type(
                        action_snapshot[0],
                        action_snapshot[1],
                        action_snapshot[2],
                        action_snapshot[3],
                        None,
                    ),
                )
            )
            if italy_result.state is italy_state_type.REJECTED:
                italy_reasons = tuple(
                    "ITALY_RULE:" + reason for reason in italy_result.reason_codes
                )
                reasons.extend(italy_reasons)
            if (
                constraint_value is not None
                and constraint_value.jurisdiction_scope != "ITALY"
            ):
                reasons.append("CONSTRAINT_JURISDICTION_SCOPE_MISMATCH")

        if (
            jurisdiction_value is not None
            and jurisdiction_value.jurisdiction is jurisdiction_enum.SPAIN
            and constraint_value is not None
            and constraint_value.jurisdiction_scope != "SPAIN"
        ):
            reasons.append("CONSTRAINT_JURISDICTION_SCOPE_MISMATCH")

        deduped = tuple(dict.fromkeys(reasons))
        if deduped:
            result = make_result_fn(
                state=admission_state_type.UNKNOWN_UNPROVEN,
                reason_codes=deduped,
                as_of=current,
                currency_code=(
                    None if identity_value is None else identity_value.currency_code
                ),
                login_route=route,
                submitted_payout=payout,
                account_identity_id=(
                    None if identity_value is None else identity_value.identity_id
                ),
                jurisdiction_authority_id=(
                    None
                    if jurisdiction_value is None
                    else jurisdiction_value.authority_id
                ),
                constraint_resolution_sha256=(
                    None
                    if constraint_value is None
                    else constraint_value.resolution_sha256
                ),
            )
        else:
            assert identity_value is not None
            assert jurisdiction_value is not None
            assert constraint_value is not None
            min_size = constraint_value.min_standard_size
            assert min_size is not None

            if action_snapshot[2] >= min_size:
                state = (
                    admission_state_type.STANDARD_MINIMUM_SATISFIED
                )
                positive_reasons: tuple[str, ...] = ()
            elif not constraint_value.lower_minimum_payout_enabled:
                state = admission_state_type.BELOW_PROVIDER_MINIMUM
                positive_reasons = ("SIZE_BELOW_CURRENT_STANDARD_MINIMUM",)
            elif (
                jurisdiction_value.jurisdiction
                is jurisdiction_enum.SPAIN
            ):
                spain = spain_assess(
                    backer_stake=action_snapshot[2],
                    as_of=current,
                    minimum_stake_evidence=None,
                    uses_bet_target=False,
                    uses_lower_minimum_payout_exception=True,
                )
                if spain.state is not spain_state_type.REJECTED:
                    raise error_type(
                        "Spain lower-minimum delegate failed closed contract"
                    )
                state = admission_state_type.BELOW_PROVIDER_MINIMUM
                positive_reasons = (
                    "SPAIN_LOWER_MINIMUM_PAYOUT_EXCEPTION_DISABLED",
                )
            elif (
                jurisdiction_value.jurisdiction
                is jurisdiction_enum.ITALY
            ):
                state = admission_state_type.BELOW_PROVIDER_MINIMUM
                positive_reasons = (
                    "ITALY_LOWER_MINIMUM_PAYOUT_EXCEPTION_DISABLED",
                )
            else:
                min_payout = constraint_value.min_payout
                if min_payout is None:
                    state = admission_state_type.UNKNOWN_UNPROVEN
                    positive_reasons = ("CURRENT_MIN_PAYOUT_UNPROVEN",)
                elif payout >= min_payout:
                    state = (
                        admission_state_type.LOWER_MINIMUM_PAYOUT_SATISFIED
                    )
                    positive_reasons = ()
                else:
                    state = admission_state_type.BELOW_PROVIDER_MINIMUM
                    positive_reasons = ("PAYOUT_BELOW_CURRENT_MINIMUM",)

            result = make_result_fn(
                state=state,
                reason_codes=positive_reasons,
                as_of=current,
                currency_code=identity_value.currency_code,
                login_route=route,
                submitted_payout=payout,
                account_identity_id=identity_value.identity_id,
                jurisdiction_authority_id=jurisdiction_value.authority_id,
                constraint_resolution_sha256=constraint_value.resolution_sha256,
            )

        if (
            identity_value is not None
            and jurisdiction_value is not None
            and constraint_value is not None
        ):
            remember(
                result,
                client=client,
                identity=identity_value,
                jurisdiction=jurisdiction_value,
                constraints=constraint_value,
            )
        return result

    def is_authoritative_betfair_standard_limit_admission(
        value: object,
    ) -> bool:
        if type(value) is not result_type:
            return False
        with lock:
            record = issued.get(id(value))
            if record is None or record.value_ref() is not value:
                return False

        def revoke() -> None:
            with lock:
                current = issued.get(id(value))
                if current is record:
                    issued.pop(id(value), None)

        try:
            if fingerprint_fn(value) != record.fingerprint:
                revoke()
                return False
            identity_require(record.identity, client=record.client)
            jurisdiction_require(record.jurisdiction, client=record.client)
            if record.jurisdiction.session_context_id != record.identity.session_context_id:
                revoke()
                return False
            if record.jurisdiction.account_identity_id != record.identity.identity_id:
                revoke()
                return False
            if record.constraints.resolution_sha256 != value.constraint_resolution_sha256:
                revoke()
                return False
        except (BetfairAccountIdentityError, BetfairSessionOriginError, AttributeError):
            revoke()
            return False
        return True

    return (
        assess_betfair_standard_limit_admission,
        is_authoritative_betfair_standard_limit_admission,
    )


def _snapshot_action(
    action: BetfairStandardLimitAction,
) -> tuple[int, str, Decimal, Decimal]:
    selection_id = action.selection_id
    side = action.side
    size = action.size
    price = action.price
    if type(selection_id) is not int or selection_id <= 0:
        raise BetfairStandardLimitAdmissionError("selection_id changed after construction")
    if type(side) is not str or side not in {"BACK", "LAY"}:
        raise BetfairStandardLimitAdmissionError("side changed after construction")
    _positive_decimal(size, "size")
    _positive_decimal(price, "price")
    if price <= Decimal("1"):
        raise BetfairStandardLimitAdmissionError("price must be greater than 1")
    if action.order_type != "LIMIT" or action.bet_target_type is not None:
        raise BetfairStandardLimitAdmissionError(
            "action changed outside plain standard-size LIMIT"
        )
    return selection_id, side, size, price


def _positive_decimal(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise BetfairStandardLimitAdmissionError(
            f"{field} must be exact positive finite Decimal"
        )
    _sign, digits, exponent = value.as_tuple()
    if len(digits) > _MAX_DECIMAL_DIGITS or abs(exponent) > _MAX_ABS_EXPONENT:
        raise BetfairStandardLimitAdmissionError(
            f"{field} exceeds bounded Decimal shape"
        )
    return value


def _exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    left_parts = left.as_tuple()
    right_parts = right.as_tuple()
    left_coefficient = 0
    right_coefficient = 0
    for digit in left_parts.digits:
        left_coefficient = left_coefficient * 10 + digit
    for digit in right_parts.digits:
        right_coefficient = right_coefficient * 10 + digit
    product = left_coefficient * right_coefficient
    digits = tuple(int(character) for character in str(product))
    exponent = left_parts.exponent + right_parts.exponent
    value = Decimal((0, digits, exponent))
    _positive_decimal(value, "submitted_payout")
    return value


def _utc(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise BetfairStandardLimitAdmissionError(
            f"{field} must be timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _currency(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or value != value.upper()
    ):
        raise BetfairStandardLimitAdmissionError(
            "currency_code must be three-letter uppercase ASCII"
        )
    return value


def _sha256_hex(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BetfairStandardLimitAdmissionError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return value


def _decimal_text(value: Decimal) -> str:
    raw = format(value, "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw


def _result_sha256(
    *,
    state: BetfairStandardLimitAdmissionState,
    reason_codes: tuple[str, ...],
    as_of: datetime,
    currency_code: str | None,
    login_route: str | None,
    submitted_payout: Decimal,
    account_identity_id: str | None,
    jurisdiction_authority_id: str | None,
    constraint_resolution_sha256: str | None,
) -> str:
    payload = {
        "schema": "autosport.betfair_standard_limit_admission",
        "policy_version": POLICY_VERSION,
        "state": state.value,
        "reason_codes": reason_codes,
        "as_of": _utc(as_of, "as_of").isoformat(),
        "currency_code": currency_code,
        "login_route": login_route,
        "submitted_payout": _decimal_text(submitted_payout),
        "account_identity_id": account_identity_id,
        "jurisdiction_authority_id": jurisdiction_authority_id,
        "constraint_resolution_sha256": constraint_resolution_sha256,
        "execution_authorized": False,
        "real_money_execution": False,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


def _make_result(
    *,
    state: BetfairStandardLimitAdmissionState,
    reason_codes: tuple[str, ...],
    as_of: datetime,
    currency_code: str | None,
    login_route: str | None,
    submitted_payout: Decimal,
    account_identity_id: str | None,
    jurisdiction_authority_id: str | None,
    constraint_resolution_sha256: str | None,
) -> BetfairStandardLimitAdmission:
    digest = _result_sha256(
        state=state,
        reason_codes=reason_codes,
        as_of=as_of,
        currency_code=currency_code,
        login_route=login_route,
        submitted_payout=submitted_payout,
        account_identity_id=account_identity_id,
        jurisdiction_authority_id=jurisdiction_authority_id,
        constraint_resolution_sha256=constraint_resolution_sha256,
    )
    return BetfairStandardLimitAdmission(
        state=state,
        reason_codes=reason_codes,
        as_of=as_of,
        currency_code=currency_code,
        login_route=login_route,
        submitted_payout=submitted_payout,
        account_identity_id=account_identity_id,
        jurisdiction_authority_id=jurisdiction_authority_id,
        constraint_resolution_sha256=constraint_resolution_sha256,
        policy_version=POLICY_VERSION,
        result_sha256=digest,
    )


def _fingerprint(value: BetfairStandardLimitAdmission) -> str:
    return _result_sha256(
        state=value.state,
        reason_codes=value.reason_codes,
        as_of=value.as_of,
        currency_code=value.currency_code,
        login_route=value.login_route,
        submitted_payout=value.submitted_payout,
        account_identity_id=value.account_identity_id,
        jurisdiction_authority_id=value.jurisdiction_authority_id,
        constraint_resolution_sha256=value.constraint_resolution_sha256,
    )


(
    assess_betfair_standard_limit_admission,
    is_authoritative_betfair_standard_limit_admission,
) = _build_admission_authority()
del _build_admission_authority
