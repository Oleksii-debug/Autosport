from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.betfair_spain_guard import (
    ACCOUNTS_JSON_RPC_ENDPOINT,
    BETTING_JSON_RPC_ENDPOINT,
    SPANISH_INTERACTIVE_API_LOGIN_ENDPOINT,
    SPANISH_KEEPALIVE_ENDPOINT,
    SPANISH_NON_INTERACTIVE_LOGIN_ENDPOINT,
    BetfairSpainEndpointConfig,
    BetfairSpainGuardError,
    DynamicMinimumStakeEvidence,
    MinimumStakeSourceKind,
    SpainLoginMode,
    SpainOrderAdmission,
    assess_betfair_spain_limit_order,
    assert_betfair_spain_endpoint_config,
    betfair_spain_session_is_current,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def evidence(
    minimum: str = "1",
    *,
    observed_at: datetime = T0,
    valid_until: datetime = T0 + timedelta(minutes=5),
) -> DynamicMinimumStakeEvidence:
    return DynamicMinimumStakeEvidence(
        jurisdiction="ES",
        currency="EUR",
        minimum_backer_stake=Decimal(minimum),
        observed_at=observed_at,
        valid_until=valid_until,
        source_kind=MinimumStakeSourceKind.LIVE_PROVIDER_SURFACE,
        source_ref="betfair-es://exchange/minimum-stake",
        source_sha256="a" * 64,
    )


@pytest.mark.parametrize(
    ("mode", "login"),
    [
        (SpainLoginMode.NON_INTERACTIVE, SPANISH_NON_INTERACTIVE_LOGIN_ENDPOINT),
        (SpainLoginMode.INTERACTIVE_API, SPANISH_INTERACTIVE_API_LOGIN_ENDPOINT),
    ],
)
def test_exact_spanish_endpoint_contract(mode, login):
    config = BetfairSpainEndpointConfig(
        login_mode=mode,
        login_endpoint=login,
        keepalive_endpoint=SPANISH_KEEPALIVE_ENDPOINT,
        betting_endpoint=BETTING_JSON_RPC_ENDPOINT,
        accounts_endpoint=ACCOUNTS_JSON_RPC_ENDPOINT,
    )
    assert_betfair_spain_endpoint_config(config)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("login_endpoint", "https://identitysso.betfair.com/api/login"),
        ("keepalive_endpoint", "https://identitysso.betfair.com/api/keepAlive"),
        ("betting_endpoint", "https://api.betfair.es/exchange/betting/json-rpc/v1"),
        ("accounts_endpoint", "https://api.betfair.es/exchange/account/json-rpc/v1"),
    ],
)
def test_endpoint_mismatch_fails_closed(field, bad):
    values = dict(
        login_mode=SpainLoginMode.INTERACTIVE_API,
        login_endpoint=SPANISH_INTERACTIVE_API_LOGIN_ENDPOINT,
        keepalive_endpoint=SPANISH_KEEPALIVE_ENDPOINT,
        betting_endpoint=BETTING_JSON_RPC_ENDPOINT,
        accounts_endpoint=ACCOUNTS_JSON_RPC_ENDPOINT,
    )
    values[field] = bad
    with pytest.raises(BetfairSpainGuardError, match=field):
        assert_betfair_spain_endpoint_config(BetfairSpainEndpointConfig(**values))


def test_noninteractive_login_cannot_use_interactive_endpoint():
    config = BetfairSpainEndpointConfig(
        login_mode=SpainLoginMode.NON_INTERACTIVE,
        login_endpoint=SPANISH_INTERACTIVE_API_LOGIN_ENDPOINT,
        keepalive_endpoint=SPANISH_KEEPALIVE_ENDPOINT,
        betting_endpoint=BETTING_JSON_RPC_ENDPOINT,
        accounts_endpoint=ACCOUNTS_JSON_RPC_ENDPOINT,
    )
    with pytest.raises(BetfairSpainGuardError, match="login_endpoint"):
        assert_betfair_spain_endpoint_config(config)


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(seconds=0), True),
        (timedelta(minutes=19, seconds=59, microseconds=999999), True),
        (timedelta(minutes=20), False),
        (timedelta(minutes=20, microseconds=1), False),
    ],
)
def test_spanish_session_twenty_minute_boundary(offset, expected):
    assert (
        betfair_spain_session_is_current(issued_at=T0, as_of=T0 + offset)
        is expected
    )


def test_session_rejects_time_travel():
    with pytest.raises(BetfairSpainGuardError, match="precede"):
        betfair_spain_session_is_current(
            issued_at=T0,
            as_of=T0 - timedelta(microseconds=1),
        )


@pytest.mark.parametrize("naive_name", ["issued", "as_of"])
def test_session_requires_aware_times(naive_name):
    kwargs = dict(issued_at=T0, as_of=T0)
    kwargs["issued_at" if naive_name == "issued" else "as_of"] = datetime(2026, 9, 21, 12, 0)
    with pytest.raises(BetfairSpainGuardError, match="timezone-aware"):
        betfair_spain_session_is_current(**kwargs)


def test_bet_target_is_rejected_without_consulting_minimum():
    result = assess_betfair_spain_limit_order(
        backer_stake=Decimal("100"),
        as_of=T0,
        minimum_stake_evidence=None,
        uses_bet_target=True,
    )
    assert result.state is SpainOrderAdmission.REJECTED
    assert result.reason == "bet_target_not_supported_for_es"
    assert result.execution_authority is False


def test_lower_minimum_payout_exception_is_rejected():
    result = assess_betfair_spain_limit_order(
        backer_stake=Decimal("100"),
        as_of=T0,
        minimum_stake_evidence=evidence(),
        uses_lower_minimum_payout_exception=True,
    )
    assert result.state is SpainOrderAdmission.REJECTED
    assert result.reason == "lower_minimum_payout_exception_not_supported_for_es"


def test_missing_dynamic_minimum_is_unknown_not_admissible():
    result = assess_betfair_spain_limit_order(
        backer_stake=Decimal("2"),
        as_of=T0,
        minimum_stake_evidence=None,
    )
    assert result.state is SpainOrderAdmission.UNKNOWN
    assert result.minimum_backer_stake is None


def test_future_minimum_evidence_is_unknown():
    ev = evidence(
        observed_at=T0 + timedelta(seconds=1),
        valid_until=T0 + timedelta(minutes=2),
    )
    result = assess_betfair_spain_limit_order(
        backer_stake=Decimal("2"),
        as_of=T0,
        minimum_stake_evidence=ev,
    )
    assert result.state is SpainOrderAdmission.UNKNOWN
    assert result.reason == "minimum_stake_evidence_is_future"


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(microseconds=1)])
def test_expired_minimum_evidence_is_unknown(delta):
    ev = evidence(
        observed_at=T0 - timedelta(minutes=1),
        valid_until=T0,
    )
    result = assess_betfair_spain_limit_order(
        backer_stake=Decimal("2"),
        as_of=T0 + delta,
        minimum_stake_evidence=ev,
    )
    assert result.state is SpainOrderAdmission.UNKNOWN
    assert result.reason == "minimum_stake_evidence_is_expired"


def test_stake_below_dynamic_minimum_is_rejected():
    result = assess_betfair_spain_limit_order(
        backer_stake=Decimal("0.99"),
        as_of=T0,
        minimum_stake_evidence=evidence("1"),
    )
    assert result.state is SpainOrderAdmission.REJECTED
    assert result.minimum_backer_stake == Decimal("1")


@pytest.mark.parametrize("stake", ["1", "2", "123.45"])
def test_stake_at_or_above_dynamic_minimum_passes_narrow_precheck(stake):
    result = assess_betfair_spain_limit_order(
        backer_stake=Decimal(stake),
        as_of=T0,
        minimum_stake_evidence=evidence("1"),
    )
    assert result.state is SpainOrderAdmission.ADMISSIBLE
    assert result.execution_authority is False


def test_dynamic_minimum_can_change_without_code_change():
    at_one = assess_betfair_spain_limit_order(
        backer_stake=Decimal("1.50"),
        as_of=T0,
        minimum_stake_evidence=evidence("1"),
    )
    at_two = assess_betfair_spain_limit_order(
        backer_stake=Decimal("1.50"),
        as_of=T0,
        minimum_stake_evidence=evidence("2"),
    )
    assert at_one.state is SpainOrderAdmission.ADMISSIBLE
    assert at_two.state is SpainOrderAdmission.REJECTED


@pytest.mark.parametrize(
    "bad",
    [1, 1.0, "1", True, Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")],
)
def test_backer_stake_requires_exact_positive_decimal(bad):
    with pytest.raises(BetfairSpainGuardError, match="backer_stake"):
        assess_betfair_spain_limit_order(
            backer_stake=bad,
            as_of=T0,
            minimum_stake_evidence=evidence(),
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("jurisdiction", "IT", "jurisdiction ES"),
        ("currency", "GBP", "currency EUR"),
        ("source_sha256", "A" * 64, "lowercase SHA-256"),
    ],
)
def test_minimum_evidence_identity_fails_closed(field, value, match):
    kwargs = dict(
        jurisdiction="ES",
        currency="EUR",
        minimum_backer_stake=Decimal("1"),
        observed_at=T0,
        valid_until=T0 + timedelta(minutes=5),
        source_kind=MinimumStakeSourceKind.LIVE_PROVIDER_SURFACE,
        source_ref="betfair-es://exchange/minimum-stake",
        source_sha256="a" * 64,
    )
    kwargs[field] = value
    with pytest.raises(BetfairSpainGuardError, match=match):
        DynamicMinimumStakeEvidence(**kwargs)


def test_minimum_evidence_cannot_grant_execution_authority():
    with pytest.raises(BetfairSpainGuardError, match="never grants"):
        DynamicMinimumStakeEvidence(
            jurisdiction="ES",
            currency="EUR",
            minimum_backer_stake=Decimal("1"),
            observed_at=T0,
            valid_until=T0 + timedelta(minutes=5),
            source_kind=MinimumStakeSourceKind.LIVE_PROVIDER_SURFACE,
            source_ref="betfair-es://exchange/minimum-stake",
            source_sha256="a" * 64,
            execution_authority=True,
        )


def test_minimum_evidence_validity_window_must_advance():
    with pytest.raises(BetfairSpainGuardError, match="after observed"):
        evidence(observed_at=T0, valid_until=T0)
