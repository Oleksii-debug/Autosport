"""Fail-closed Betfair Spain jurisdiction boundary.

This module owns only stable .es endpoint/session rules and a narrow order-admission
precheck. It does not own provider transport, login credentials, placeOrders,
execution/reconciliation, real-money enablement, or the acquisition of dynamic
minimum-stake evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum


SPANISH_NON_INTERACTIVE_LOGIN_ENDPOINT = (
    "https://identitysso-cert.betfair.es/api/certlogin"
)
SPANISH_INTERACTIVE_API_LOGIN_ENDPOINT = (
    "https://identitysso.betfair.es/api/login"
)
SPANISH_KEEPALIVE_ENDPOINT = "https://identitysso.betfair.es/api/keepAlive"
BETTING_JSON_RPC_ENDPOINT = "https://api.betfair.com/exchange/betting/json-rpc/v1"
ACCOUNTS_JSON_RPC_ENDPOINT = "https://api.betfair.com/exchange/account/json-rpc/v1"

SPANISH_SESSION_LIFETIME = timedelta(minutes=20)


class BetfairSpainGuardError(ValueError):
    """Malformed Spain-jurisdiction evidence or configuration."""


class SpainLoginMode(str, Enum):
    NON_INTERACTIVE = "NON_INTERACTIVE"
    INTERACTIVE_API = "INTERACTIVE_API"


class SpainOrderAdmission(str, Enum):
    ADMISSIBLE = "ADMISSIBLE"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class MinimumStakeSourceKind(str, Enum):
    LIVE_PROVIDER_SURFACE = "LIVE_PROVIDER_SURFACE"
    PROVIDER_ACCEPTANCE_PROBE = "PROVIDER_ACCEPTANCE_PROBE"


@dataclass(frozen=True, slots=True)
class BetfairSpainEndpointConfig:
    login_mode: SpainLoginMode
    login_endpoint: str
    keepalive_endpoint: str
    betting_endpoint: str
    accounts_endpoint: str

    def __post_init__(self) -> None:
        if type(self.login_mode) is not SpainLoginMode:
            raise BetfairSpainGuardError("login_mode must be exact SpainLoginMode")
        for name in (
            "login_endpoint",
            "keepalive_endpoint",
            "betting_endpoint",
            "accounts_endpoint",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise BetfairSpainGuardError(f"{name} must be canonical non-empty text")


@dataclass(frozen=True, slots=True)
class DynamicMinimumStakeEvidence:
    """Time-bounded external evidence, never execution authority by itself."""

    jurisdiction: str
    currency: str
    minimum_backer_stake: Decimal
    observed_at: datetime
    valid_until: datetime
    source_kind: MinimumStakeSourceKind
    source_ref: str
    source_sha256: str
    execution_authority: bool = False

    def __post_init__(self) -> None:
        if self.jurisdiction != "ES":
            raise BetfairSpainGuardError("minimum-stake evidence must bind jurisdiction ES")
        if self.currency != "EUR":
            raise BetfairSpainGuardError("minimum-stake evidence must bind currency EUR")
        _positive_decimal(self.minimum_backer_stake, "minimum_backer_stake")
        observed = _utc(self.observed_at, "observed_at")
        valid_until = _utc(self.valid_until, "valid_until")
        if valid_until <= observed:
            raise BetfairSpainGuardError("valid_until must be after observed_at")
        if type(self.source_kind) is not MinimumStakeSourceKind:
            raise BetfairSpainGuardError(
                "source_kind must be exact MinimumStakeSourceKind"
            )
        _text(self.source_ref, "source_ref")
        _sha256(self.source_sha256, "source_sha256")
        if self.execution_authority is not False:
            raise BetfairSpainGuardError(
                "minimum-stake evidence never grants execution authority"
            )


@dataclass(frozen=True, slots=True)
class BetfairSpainAdmissionResult:
    state: SpainOrderAdmission
    reason: str
    minimum_backer_stake: Decimal | None
    minimum_evidence_sha256: str | None
    execution_authority: bool = False

    def __post_init__(self) -> None:
        if type(self.state) is not SpainOrderAdmission:
            raise BetfairSpainGuardError("state must be exact SpainOrderAdmission")
        _text(self.reason, "reason")
        if self.minimum_backer_stake is not None:
            _positive_decimal(self.minimum_backer_stake, "minimum_backer_stake")
        if self.minimum_evidence_sha256 is not None:
            _sha256(self.minimum_evidence_sha256, "minimum_evidence_sha256")
        if self.execution_authority is not False:
            raise BetfairSpainGuardError(
                "Spain guard result never grants execution authority"
            )


def assert_betfair_spain_endpoint_config(
    config: BetfairSpainEndpointConfig,
) -> None:
    """Require jurisdiction-local login/keepAlive and global Exchange API endpoints."""

    if type(config) is not BetfairSpainEndpointConfig:
        raise BetfairSpainGuardError(
            "config must be exact BetfairSpainEndpointConfig"
        )
    expected_login = (
        SPANISH_NON_INTERACTIVE_LOGIN_ENDPOINT
        if config.login_mode is SpainLoginMode.NON_INTERACTIVE
        else SPANISH_INTERACTIVE_API_LOGIN_ENDPOINT
    )
    expected = {
        "login_endpoint": expected_login,
        "keepalive_endpoint": SPANISH_KEEPALIVE_ENDPOINT,
        "betting_endpoint": BETTING_JSON_RPC_ENDPOINT,
        "accounts_endpoint": ACCOUNTS_JSON_RPC_ENDPOINT,
    }
    for name, value in expected.items():
        if getattr(config, name) != value:
            raise BetfairSpainGuardError(
                f"{name} does not match the Betfair Spain jurisdiction contract"
            )


def betfair_spain_session_is_current(
    *,
    issued_at: datetime,
    as_of: datetime,
) -> bool:
    """Return true only strictly before the documented 20-minute Spain expiry."""

    issued = _utc(issued_at, "issued_at")
    current = _utc(as_of, "as_of")
    if current < issued:
        raise BetfairSpainGuardError("as_of cannot precede issued_at")
    return current - issued < SPANISH_SESSION_LIFETIME


def assess_betfair_spain_limit_order(
    *,
    backer_stake: Decimal,
    as_of: datetime,
    minimum_stake_evidence: DynamicMinimumStakeEvidence | None,
    uses_bet_target: bool = False,
    uses_lower_minimum_payout_exception: bool = False,
) -> BetfairSpainAdmissionResult:
    """Apply only stable Spain-specific pre-admission rules.

    BetTarget sizing and the lower-minimum-at-large-price exception are explicitly
    unavailable to .es customers. The current ordinary LIMIT minimum is deliberately
    not hard-coded because live Betfair surfaces have exposed conflicting EUR values.
    A caller must supply time-bounded provider-derived minimum-stake evidence.

    ADMISSIBLE means only that this narrow jurisdiction precheck did not find a
    blocker. It is never authorization to place an order.
    """

    stake = _positive_decimal(backer_stake, "backer_stake")
    current = _utc(as_of, "as_of")
    if type(uses_bet_target) is not bool:
        raise BetfairSpainGuardError("uses_bet_target must be bool")
    if type(uses_lower_minimum_payout_exception) is not bool:
        raise BetfairSpainGuardError(
            "uses_lower_minimum_payout_exception must be bool"
        )

    if uses_bet_target:
        return BetfairSpainAdmissionResult(
            state=SpainOrderAdmission.REJECTED,
            reason="bet_target_not_supported_for_es",
            minimum_backer_stake=None,
            minimum_evidence_sha256=None,
        )
    if uses_lower_minimum_payout_exception:
        return BetfairSpainAdmissionResult(
            state=SpainOrderAdmission.REJECTED,
            reason="lower_minimum_payout_exception_not_supported_for_es",
            minimum_backer_stake=None,
            minimum_evidence_sha256=None,
        )

    if minimum_stake_evidence is None:
        return BetfairSpainAdmissionResult(
            state=SpainOrderAdmission.UNKNOWN,
            reason="current_dynamic_minimum_stake_evidence_required",
            minimum_backer_stake=None,
            minimum_evidence_sha256=None,
        )
    if type(minimum_stake_evidence) is not DynamicMinimumStakeEvidence:
        raise BetfairSpainGuardError(
            "minimum_stake_evidence must be exact DynamicMinimumStakeEvidence"
        )

    observed = _utc(minimum_stake_evidence.observed_at, "observed_at")
    valid_until = _utc(minimum_stake_evidence.valid_until, "valid_until")
    if observed > current:
        return BetfairSpainAdmissionResult(
            state=SpainOrderAdmission.UNKNOWN,
            reason="minimum_stake_evidence_is_future",
            minimum_backer_stake=None,
            minimum_evidence_sha256=minimum_stake_evidence.source_sha256,
        )
    if current >= valid_until:
        return BetfairSpainAdmissionResult(
            state=SpainOrderAdmission.UNKNOWN,
            reason="minimum_stake_evidence_is_expired",
            minimum_backer_stake=None,
            minimum_evidence_sha256=minimum_stake_evidence.source_sha256,
        )

    minimum = minimum_stake_evidence.minimum_backer_stake
    if stake < minimum:
        return BetfairSpainAdmissionResult(
            state=SpainOrderAdmission.REJECTED,
            reason="backer_stake_below_current_dynamic_minimum",
            minimum_backer_stake=minimum,
            minimum_evidence_sha256=minimum_stake_evidence.source_sha256,
        )
    return BetfairSpainAdmissionResult(
        state=SpainOrderAdmission.ADMISSIBLE,
        reason="narrow_es_jurisdiction_precheck_passed",
        minimum_backer_stake=minimum,
        minimum_evidence_sha256=minimum_stake_evidence.source_sha256,
    )


def _positive_decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise BetfairSpainGuardError(
            f"{name} must be exact finite positive Decimal"
        )
    return value


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise BetfairSpainGuardError(f"{name} must be timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetfairSpainGuardError(f"{name} must be canonical non-empty text")
    return value


def _sha256(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise BetfairSpainGuardError(f"{name} must be lowercase SHA-256 hex")
    return raw
