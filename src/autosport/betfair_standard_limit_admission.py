"""Current reviewed Betfair standard-LIMIT size/payout admission authority.

This module composes three product-owned truths for one narrow surface:
1. K07 authenticated session/account currency;
2. the exact product-observed non-interactive certificate-login route bound to
   that same K07 session context; and
3. an explicit, expiring product review of current first-party Betfair
   documentation and its known historical GBP conflict.

The reviewed generation is intentionally narrow: GBP, standard-size LIMIT,
canonical GLOBAL_COM certificate-login sessions. It does not authorize writes,
prove provider acceptance, prove price-ladder validity, reserve liquidity, or
claim a timeless provider rule. The review expires fail-closed and must be
re-issued after fresh source/conflict review.

No real placeOrders probe is used to discover provider limits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from enum import Enum
from hashlib import sha256
import hmac
import json
from threading import RLock
from weakref import ReferenceType, ref

from .betfair_account_identity import (
    BetfairAuthenticatedAccountIdentity,
    require_authoritative_betfair_account_identity,
)
from .betfair_account_readonly import BetfairReadOnlyClient
from .betfair_session_origin import (
    BetfairAuthenticatedJurisdiction,
    BetfairLoginJurisdiction,
    require_authoritative_betfair_authenticated_jurisdiction,
)

REVIEW_GENERATION_ID = "betfair-standard-limit-gbp-review-2026-10-03-v1"
REVIEW_AVAILABLE_FROM_UTC = datetime(
    2026, 10, 3, 20, 30, tzinfo=timezone.utc
)
REVIEW_EXPIRES_AT_UTC = datetime(
    2026, 10, 10, 20, 30, tzinfo=timezone.utc
)

CURRENT_PLACE_ORDERS_SOURCE = (
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/"
    "1smk3cen4v3lu3yomq5qye0ni/pages/2687496/placeOrders"
)
NON_INTERACTIVE_LOGIN_SOURCE = (
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/"
    "1smk3cen4v3lu3yomq5qye0ni/pages/2687915/Non-Interactive+bot+login"
)
SWEDEN_LOGIN_SOURCE = (
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/"
    "1smk3cen4v3lu3yomq5qye0ni/pages/2687739/"
    "New+API+Release+-+1st+January+2019"
)
HISTORICAL_CURRENCY_PARAMETERS_SOURCE = (
    "https://betfair-developer-docs.atlassian.net/wiki/pages/"
    "viewpage.action?pageId=2686993&pageVersion=3"
)
HISTORICAL_2016_RELEASE_SOURCE = (
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/"
    "1smk3cen4v3lu3yomq5qye0ni/pages/2687154/"
    "New+API+Release+-+8th+August+2016"
)

# Product-reviewed normalized semantics. These globals are diagnostic exports;
# the canonical resolver captures immutable copies at import and does not read
# them again after authority construction.
GBP_MIN_BET_SIZE = Decimal("1")
GBP_MIN_BET_PAYOUT = Decimal("10")
PRODUCT_PRECEDENCE_REVIEW_RESOLVED = True
PROVIDER_REVISION_METADATA_PROVEN = False
RAW_PROVIDER_CONTENT_SHA256_PROVEN = False

_MAX_DECIMAL_DIGITS = 64
_MAX_ABS_EXPONENT = 18


class BetfairStandardLimitAdmissionError(ValueError):
    """Raised when a standard-LIMIT admission projection is malformed."""


class BetfairStandardLimitAdmissionState(str, Enum):
    ADMISSION_CANDIDATE = "ADMISSION_CANDIDATE"
    REJECTED_REVIEWED_CONSTRAINT = "REJECTED_REVIEWED_CONSTRAINT"
    UNKNOWN_UNPROVEN = "UNKNOWN_UNPROVEN"


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairStandardLimitAdmission:
    """Secret-free decision record; source authority is closure-issued."""

    state: BetfairStandardLimitAdmissionState
    reason_code: str
    side: str
    currency_code: str
    jurisdiction: str
    size: Decimal
    price: Decimal
    gross_payout: Decimal
    decision_as_of: datetime
    session_context_id: str
    account_identity_id: str
    jurisdiction_authority_id: str
    review_generation_id: str
    review_semantic_sha256: str
    review_available_from: datetime
    review_expires_at: datetime

    def __post_init__(self) -> None:
        if type(self.state) is not BetfairStandardLimitAdmissionState:
            raise BetfairStandardLimitAdmissionError(
                "state must be exact BetfairStandardLimitAdmissionState"
            )
        if type(self.reason_code) is not str or not self.reason_code:
            raise BetfairStandardLimitAdmissionError(
                "reason_code must be non-empty exact text"
            )
        if type(self.side) is not str or self.side not in {"BACK", "LAY"}:
            raise BetfairStandardLimitAdmissionError(
                "side must be exactly BACK or LAY"
            )
        _currency_code(self.currency_code)
        if type(self.jurisdiction) is not str or not self.jurisdiction:
            raise BetfairStandardLimitAdmissionError(
                "jurisdiction must be non-empty exact text"
            )
        _positive_decimal(self.size, "size")
        _positive_decimal(self.price, "price")
        if self.price <= Decimal("1"):
            raise BetfairStandardLimitAdmissionError(
                "price must be greater than 1"
            )
        _positive_decimal(self.gross_payout, "gross_payout", product=True)
        _aware_utc(self.decision_as_of, "decision_as_of")
        _text(self.session_context_id, "session_context_id")
        _sha256_hex(self.account_identity_id, "account_identity_id")
        _sha256_hex(
            self.jurisdiction_authority_id,
            "jurisdiction_authority_id",
        )
        _text(self.review_generation_id, "review_generation_id")
        _sha256_hex(
            self.review_semantic_sha256,
            "review_semantic_sha256",
        )
        available = _aware_utc(
            self.review_available_from,
            "review_available_from",
        )
        expires = _aware_utc(
            self.review_expires_at,
            "review_expires_at",
        )
        if available >= expires:
            raise BetfairStandardLimitAdmissionError(
                "review interval must be non-empty"
            )

    @property
    def admission_candidate(self) -> bool:
        """Structural state only; use the authority verifier for trust."""
        return (
            self.state
            is BetfairStandardLimitAdmissionState.ADMISSION_CANDIDATE
        )

    @property
    def provider_acceptance_proven(self) -> bool:
        return False

    @property
    def market_price_admissibility_proven(self) -> bool:
        return False

    @property
    def liquidity_reserved(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class _IssuedAdmissionRecord:
    value_ref: ReferenceType[BetfairStandardLimitAdmission]
    fingerprint: str
    client_ref: ReferenceType[BetfairReadOnlyClient]
    identity_ref: ReferenceType[BetfairAuthenticatedAccountIdentity]
    jurisdiction_ref: ReferenceType[BetfairAuthenticatedJurisdiction]


def _build_standard_limit_admission_authority():
    """Capture the reviewed generation and upstream trust roots once."""

    lock = RLock()
    issued: dict[int, _IssuedAdmissionRecord] = {}

    result_type = BetfairStandardLimitAdmission
    result_state = BetfairStandardLimitAdmissionState
    client_type = BetfairReadOnlyClient
    identity_type = BetfairAuthenticatedAccountIdentity
    jurisdiction_type = BetfairAuthenticatedJurisdiction
    jurisdiction_enum = BetfairLoginJurisdiction
    identity_require = require_authoritative_betfair_account_identity
    jurisdiction_require = require_authoritative_betfair_authenticated_jurisdiction

    generation_id = REVIEW_GENERATION_ID
    available_from = REVIEW_AVAILABLE_FROM_UTC
    expires_at = REVIEW_EXPIRES_AT_UTC
    min_size = GBP_MIN_BET_SIZE
    min_payout = GBP_MIN_BET_PAYOUT
    current_source = CURRENT_PLACE_ORDERS_SOURCE
    login_source = NON_INTERACTIVE_LOGIN_SOURCE
    sweden_source = SWEDEN_LOGIN_SOURCE
    historical_currency_source = HISTORICAL_CURRENCY_PARAMETERS_SOURCE
    historical_release_source = HISTORICAL_2016_RELEASE_SOURCE

    # This digest identifies the exact *reviewed semantic generation*, not raw
    # provider bytes. Raw provider-content SHA/revision remain explicitly
    # unproven because the current Confluence page does not expose an immutable
    # revision identifier through the reviewed public surface.
    review_material = {
        "generation_id": generation_id,
        "available_from": instant(available_from),
        "expires_at": instant(expires_at),
        "order_type": "LIMIT",
        "size_mode": "STANDARD_SIZE",
        "currency": "GBP",
        "min_bet_size": str(min_size),
        "min_bet_payout": str(min_payout),
        "positive_route_scope": "GLOBAL_COM_NON_INTERACTIVE_CERT",
        "lower_minimum_excluded": ["IT", "ES", "DK", "SE"],
        "current_place_orders_source": current_source,
        "non_interactive_login_source": login_source,
        "sweden_login_source": sweden_source,
        "historical_conflict_sources": [
            historical_currency_source,
            historical_release_source,
        ],
        "precedence_resolution": (
            "product_review_2026-10-03_current_placeOrders_operation_page_"
            "over_historical_GBP_2_sources_for_current_decisions_only"
        ),
        "provider_revision_metadata_proven": False,
        "raw_provider_content_sha256_proven": False,
        "real_money_probe_used": False,
    }
    review_semantic_sha256 = sha256(
        json.dumps(
            review_material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    canonical_result_init = result_type.__init__
    canonical_result_post_init = result_type.__post_init__
    sha256_fn = sha256
    json_dumps = json.dumps
    hmac_compare = hmac.compare_digest
    weakref_fn = ref
    utc = timezone.utc
    instant = _instant
    aware_utc = _aware_utc
    positive_decimal = _positive_decimal
    exact_multiply = _exact_multiply

    def implementation_current() -> bool:
        return bool(
            BetfairStandardLimitAdmission is result_type
            and BetfairStandardLimitAdmissionState is result_state
            and BetfairReadOnlyClient is client_type
            and BetfairAuthenticatedAccountIdentity is identity_type
            and BetfairAuthenticatedJurisdiction is jurisdiction_type
            and BetfairLoginJurisdiction is jurisdiction_enum
            and result_type.__init__ is canonical_result_init
            and result_type.__post_init__ is canonical_result_post_init
        )

    def fingerprint(value: BetfairStandardLimitAdmission) -> str:
        if type(value) is not result_type:
            raise BetfairStandardLimitAdmissionError(
                "admission result type changed"
            )
        payload = {
            "state": value.state.value,
            "reason_code": value.reason_code,
            "side": value.side,
            "currency_code": value.currency_code,
            "jurisdiction": value.jurisdiction,
            "size": str(value.size),
            "price": str(value.price),
            "gross_payout": str(value.gross_payout),
            "decision_as_of": instant(value.decision_as_of),
            "session_context_id": value.session_context_id,
            "account_identity_id": value.account_identity_id,
            "jurisdiction_authority_id": value.jurisdiction_authority_id,
            "review_generation_id": value.review_generation_id,
            "review_semantic_sha256": value.review_semantic_sha256,
            "review_available_from": instant(value.review_available_from),
            "review_expires_at": instant(value.review_expires_at),
            "provider_acceptance_proven": False,
            "execution_authorized": False,
        }
        encoded = json_dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return sha256_fn(encoded).hexdigest()

    def make_result(
        *,
        state: BetfairStandardLimitAdmissionState,
        reason_code: str,
        side: str,
        identity: BetfairAuthenticatedAccountIdentity,
        jurisdiction: BetfairAuthenticatedJurisdiction,
        size: Decimal,
        price: Decimal,
        gross_payout: Decimal,
        as_of: datetime,
    ) -> BetfairStandardLimitAdmission:
        return result_type(
            state=state,
            reason_code=reason_code,
            side=side,
            currency_code=identity.currency_code,
            jurisdiction=jurisdiction.jurisdiction.value,
            size=size,
            price=price,
            gross_payout=gross_payout,
            decision_as_of=as_of,
            session_context_id=identity.session_context_id,
            account_identity_id=identity.identity_id,
            jurisdiction_authority_id=jurisdiction.authority_id,
            review_generation_id=generation_id,
            review_semantic_sha256=review_semantic_sha256,
            review_available_from=available_from,
            review_expires_at=expires_at,
        )

    def remember(
        value: BetfairStandardLimitAdmission,
        *,
        client: BetfairReadOnlyClient,
        identity: BetfairAuthenticatedAccountIdentity,
        jurisdiction: BetfairAuthenticatedJurisdiction,
    ) -> None:
        key = id(value)

        def discard(
            dead_ref: ReferenceType[BetfairStandardLimitAdmission],
        ) -> None:
            with lock:
                record = issued.get(key)
                if record is not None and record.value_ref is dead_ref:
                    issued.pop(key, None)

        with lock:
            issued[key] = _IssuedAdmissionRecord(
                value_ref=weakref_fn(value, discard),
                fingerprint=fingerprint(value),
                client_ref=weakref_fn(client),
                identity_ref=weakref_fn(identity),
                jurisdiction_ref=weakref_fn(jurisdiction),
            )

    def unknown(
        *,
        reason_code: str,
        side: str,
        identity: BetfairAuthenticatedAccountIdentity,
        jurisdiction: BetfairAuthenticatedJurisdiction,
        size: Decimal,
        price: Decimal,
        gross_payout: Decimal,
        as_of: datetime,
    ) -> BetfairStandardLimitAdmission:
        return make_result(
            state=result_state.UNKNOWN_UNPROVEN,
            reason_code=reason_code,
            side=side,
            identity=identity,
            jurisdiction=jurisdiction,
            size=size,
            price=price,
            gross_payout=gross_payout,
            as_of=as_of,
        )

    def assess(
        *,
        client: BetfairReadOnlyClient,
        account_identity: BetfairAuthenticatedAccountIdentity,
        authenticated_jurisdiction: BetfairAuthenticatedJurisdiction,
        side: str,
        size: Decimal,
        price: Decimal,
        as_of: datetime,
        bet_target_type: str | None = None,
    ) -> BetfairStandardLimitAdmission:
        if not implementation_current():
            raise BetfairStandardLimitAdmissionError(
                "canonical admission implementation changed"
            )
        if type(side) is not str or side not in {"BACK", "LAY"}:
            raise BetfairStandardLimitAdmissionError(
                "side must be exactly BACK or LAY"
            )
        value_size = positive_decimal(size, "size")
        value_price = positive_decimal(price, "price")
        if value_price <= Decimal("1"):
            raise BetfairStandardLimitAdmissionError(
                "price must be greater than 1"
            )
        if bet_target_type is not None and (
            type(bet_target_type) is not str
            or bet_target_type not in {"PAYOUT", "BACKERS_PROFIT"}
        ):
            raise BetfairStandardLimitAdmissionError(
                "bet_target_type must be PAYOUT, BACKERS_PROFIT, or None"
            )
        decision_time = aware_utc(as_of, "as_of")
        gross_payout = exact_multiply(value_size, value_price)

        # Exact public types are required even for UNKNOWN construction because
        # the result binds their non-secret identifiers. Source authority is
        # still verified separately below.
        if type(account_identity) is not identity_type:
            raise BetfairStandardLimitAdmissionError(
                "account_identity must be exact BetfairAuthenticatedAccountIdentity"
            )
        if type(authenticated_jurisdiction) is not jurisdiction_type:
            raise BetfairStandardLimitAdmissionError(
                "authenticated_jurisdiction must be exact BetfairAuthenticatedJurisdiction"
            )
        if type(client) is not client_type:
            return unknown(
                reason_code="AUTHENTICATED_CLIENT_UNPROVEN",
                side=side,
                identity=account_identity,
                jurisdiction=authenticated_jurisdiction,
                size=value_size,
                price=value_price,
                gross_payout=gross_payout,
                as_of=decision_time,
            )

        try:
            identity_require(account_identity, client=client)
            jurisdiction_require(authenticated_jurisdiction, client=client)
        except Exception:
            return unknown(
                reason_code="UPSTREAM_SESSION_AUTHORITY_UNPROVEN",
                side=side,
                identity=account_identity,
                jurisdiction=authenticated_jurisdiction,
                size=value_size,
                price=value_price,
                gross_payout=gross_payout,
                as_of=decision_time,
            )

        if (
            authenticated_jurisdiction.session_context_id
            != account_identity.session_context_id
            or authenticated_jurisdiction.account_identity_id
            != account_identity.identity_id
        ):
            return unknown(
                reason_code="SESSION_CONTEXT_BINDING_MISMATCH",
                side=side,
                identity=account_identity,
                jurisdiction=authenticated_jurisdiction,
                size=value_size,
                price=value_price,
                gross_payout=gross_payout,
                as_of=decision_time,
            )

        if decision_time < available_from:
            return unknown(
                reason_code="RULE_REVIEW_NOT_CAUSALLY_AVAILABLE",
                side=side,
                identity=account_identity,
                jurisdiction=authenticated_jurisdiction,
                size=value_size,
                price=value_price,
                gross_payout=gross_payout,
                as_of=decision_time,
            )
        if decision_time >= expires_at:
            return unknown(
                reason_code="RULE_REVIEW_EXPIRED",
                side=side,
                identity=account_identity,
                jurisdiction=authenticated_jurisdiction,
                size=value_size,
                price=value_price,
                gross_payout=gross_payout,
                as_of=decision_time,
            )
        if bet_target_type is not None:
            return unknown(
                reason_code="TARGET_SIZING_OUTSIDE_STANDARD_SIZE_CONTRACT",
                side=side,
                identity=account_identity,
                jurisdiction=authenticated_jurisdiction,
                size=value_size,
                price=value_price,
                gross_payout=gross_payout,
                as_of=decision_time,
            )
        if account_identity.currency_code != "GBP":
            return unknown(
                reason_code="CURRENCY_HAS_NO_CURRENT_REVIEWED_GENERATION",
                side=side,
                identity=account_identity,
                jurisdiction=authenticated_jurisdiction,
                size=value_size,
                price=value_price,
                gross_payout=gross_payout,
                as_of=decision_time,
            )
        if (
            authenticated_jurisdiction.jurisdiction
            is not jurisdiction_enum.GLOBAL_COM
        ):
            return unknown(
                reason_code="JURISDICTION_OUTSIDE_REVIEWED_GLOBAL_CERT_SCOPE",
                side=side,
                identity=account_identity,
                jurisdiction=authenticated_jurisdiction,
                size=value_size,
                price=value_price,
                gross_payout=gross_payout,
                as_of=decision_time,
            )

        if value_size >= min_size:
            reason = "GBP_STANDARD_MINIMUM_SATISFIED"
            state = result_state.ADMISSION_CANDIDATE
        elif gross_payout >= min_payout:
            reason = "GBP_LOWER_MINIMUM_PAYOUT_SATISFIED"
            state = result_state.ADMISSION_CANDIDATE
        else:
            reason = "GBP_SIZE_AND_PAYOUT_BELOW_REVIEWED_MINIMUMS"
            state = result_state.REJECTED_REVIEWED_CONSTRAINT

        value = make_result(
            state=state,
            reason_code=reason,
            side=side,
            identity=account_identity,
            jurisdiction=authenticated_jurisdiction,
            size=value_size,
            price=value_price,
            gross_payout=gross_payout,
            as_of=decision_time,
        )
        if state is result_state.ADMISSION_CANDIDATE:
            remember(
                value,
                client=client,
                identity=account_identity,
                jurisdiction=authenticated_jurisdiction,
            )
        return value

    def is_authoritative(
        value: object,
        *,
        client: BetfairReadOnlyClient | None = None,
    ) -> bool:
        if (
            type(value) is not result_type
            or value.state is not result_state.ADMISSION_CANDIDATE
            or not implementation_current()
        ):
            return False
        with lock:
            record = issued.get(id(value))
            if record is None or record.value_ref() is not value:
                return False
            try:
                if not hmac_compare(
                    record.fingerprint,
                    fingerprint(value),
                ):
                    return False
            except Exception:
                return False
            issued_client = record.client_ref()
            identity = record.identity_ref()
            jurisdiction = record.jurisdiction_ref()
            if (
                issued_client is None
                or identity is None
                or jurisdiction is None
            ):
                return False
            if client is not None and issued_client is not client:
                return False
            try:
                identity_require(identity, client=issued_client)
                jurisdiction_require(jurisdiction, client=issued_client)
            except Exception:
                return False
            return bool(
                value.session_context_id == identity.session_context_id
                and value.account_identity_id == identity.identity_id
                and value.jurisdiction_authority_id
                == jurisdiction.authority_id
                and value.currency_code == "GBP"
                and value.jurisdiction
                == jurisdiction_enum.GLOBAL_COM.value
                and value.review_generation_id == generation_id
                and hmac_compare(
                    value.review_semantic_sha256,
                    review_semantic_sha256,
                )
                and value.review_available_from == available_from
                and value.review_expires_at == expires_at
                and available_from <= value.decision_as_of < expires_at
                and value.gross_payout
                == exact_multiply(value.size, value.price)
                and (
                    value.size >= min_size
                    or value.gross_payout >= min_payout
                )
                and value.provider_acceptance_proven is False
                and value.execution_authorized is False
                and value.real_money_execution is False
            )

    def require_authoritative(
        value: object,
        *,
        client: BetfairReadOnlyClient | None = None,
    ) -> BetfairStandardLimitAdmission:
        if not is_authoritative(value, client=client):
            raise BetfairStandardLimitAdmissionError(
                "standard LIMIT admission lacks current product authority"
            )
        assert type(value) is result_type
        return value

    return assess, is_authoritative, require_authoritative

def _currency_code(value: object) -> str:
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


def _text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise BetfairStandardLimitAdmissionError(
            f"{field} must be canonical non-empty text"
        )
    return value


def _sha256_hex(value: object, field: str) -> str:
    raw = _text(value, field)
    if (
        len(raw) != 64
        or any(ch not in "0123456789abcdef" for ch in raw)
    ):
        raise BetfairStandardLimitAdmissionError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return raw


def _aware_utc(value: object, field: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise BetfairStandardLimitAdmissionError(
            f"{field} must be timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _instant(value: datetime) -> str:
    return _aware_utc(value, "timestamp").isoformat().replace(
        "+00:00", "Z"
    )


def _positive_decimal(
    value: object,
    field: str,
    *,
    product: bool = False,
) -> Decimal:
    if (
        type(value) is not Decimal
        or not value.is_finite()
        or value <= 0
    ):
        raise BetfairStandardLimitAdmissionError(
            f"{field} must be an exact positive finite Decimal"
        )
    digits = value.as_tuple().digits
    exponent = value.as_tuple().exponent
    max_digits = _MAX_DECIMAL_DIGITS * (2 if product else 1)
    max_exponent = _MAX_ABS_EXPONENT * (2 if product else 1)
    if len(digits) > max_digits or abs(exponent) > max_exponent:
        raise BetfairStandardLimitAdmissionError(
            f"{field} exceeds bounded Decimal shape"
        )
    return value


def _exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    left_digits = len(left.as_tuple().digits)
    right_digits = len(right.as_tuple().digits)
    with localcontext() as context:
        context.prec = left_digits + right_digits + 4
        result = left * right
    return _positive_decimal(result, "gross_payout", product=True)


(
    assess_betfair_standard_limit_admission,
    is_authoritative_betfair_standard_limit_admission,
    require_authoritative_betfair_standard_limit_admission,
) = _build_standard_limit_admission_authority()
del _build_standard_limit_admission_authority
