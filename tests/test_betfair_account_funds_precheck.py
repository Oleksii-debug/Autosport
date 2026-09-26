from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pickle

import pytest

from autosport import betfair_account_funds_precheck as subject
from autosport.betfair_account_readonly import (
    BetfairAccountDetailsObservation,
    BetfairAccountFundsObservation,
    BetfairEvidence,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
DETAILS_SHA = "1" * 64
FUNDS_SHA = "2" * 64



class _AdversarialDecimal(Decimal):
    def __new__(cls, value: str) -> "_AdversarialDecimal":
        return super().__new__(cls, value)

    def is_finite(self) -> bool:
        return True

    def __lt__(self, other: object) -> bool:
        return False

    def __le__(self, other: object) -> bool:
        return True

    def __ge__(self, other: object) -> bool:
        return True


def evidence(at: datetime, digest: str) -> BetfairEvidence:
    return BetfairEvidence(at.isoformat().replace("+00:00", "Z"), digest)


def details(at: datetime = NOW) -> BetfairAccountDetailsObservation:
    return BetfairAccountDetailsObservation(
        "EUR", "en", "SK", "Europe/Bratislava", evidence(at, DETAILS_SHA)
    )


def funds(balance: Decimal, at: datetime = NOW) -> BetfairAccountFundsObservation:
    return BetfairAccountFundsObservation(
        balance, Decimal("0"), Decimal("0"), Decimal("1000"), evidence(at, FUNDS_SHA)
    )


def install_client(monkeypatch, *, balance=Decimal("100"), details_at=NOW, funds_at=NOW):
    class FakeClient:
        def __init__(self, credentials, **kwargs):
            assert type(credentials) is BetfairSessionCredentials
            assert kwargs["venue_id"] == "betfair"
            assert kwargs["account_id"] == "authenticated-account"

        def read_account_details(self):
            return details(details_at)

        def read_account_funds(self):
            return funds(balance, funds_at)

    monkeypatch.setattr(subject, "BetfairReadOnlyClient", FakeClient)
    monkeypatch.setattr(subject, "_utc_now", lambda: NOW)


def test_happy_path_issues_process_local_authority(monkeypatch):
    install_client(monkeypatch, balance=Decimal("100"))
    result = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("25"),
        required_currency_code="EUR",
    )
    assert result.passed is True
    assert result.execution_authorized is False
    assert result.account_id == f"betfair-account-evidence:{DETAILS_SHA}"
    assert result.currency_code == "EUR"
    assert result.account_funds_sha256 == FUNDS_SHA
    assert subject.is_authoritative_funds_precheck(result) is True
    assert subject.require_authoritative_funds_precheck(result) is result


def test_equal_balance_passes_and_one_cent_over_fails(monkeypatch):
    install_client(monkeypatch, balance=Decimal("25.00"))
    equal = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("25.00"),
        required_currency_code="EUR",
    )
    assert equal.passed is True
    over = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("25.01"),
        required_currency_code="EUR",
    )
    assert over.passed is False
    assert subject.is_authoritative_funds_precheck(over) is True


@pytest.mark.parametrize("bad", [Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_required_liability_fails_before_provider_read(monkeypatch, bad):
    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("provider must not be read")

    monkeypatch.setattr(subject, "BetfairReadOnlyClient", ForbiddenClient)
    with pytest.raises(subject.BetfairAccountFundsPrecheckError):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"), bad,
            required_currency_code="EUR",
        )


def test_negative_provider_available_balance_fails_closed(monkeypatch):
    install_client(monkeypatch, balance=Decimal("-0.01"))
    with pytest.raises(subject.BetfairAccountFundsPrecheckError):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"), Decimal("0"),
            required_currency_code="EUR",
        )


def test_stale_and_future_funds_evidence_fail_closed(monkeypatch):
    install_client(
        monkeypatch,
        funds_at=NOW - subject.MAX_FUNDS_EVIDENCE_AGE - timedelta(microseconds=1),
    )
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="stale"):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"), Decimal("1"),
            required_currency_code="EUR",
        )
    install_client(monkeypatch, funds_at=NOW + timedelta(seconds=2))
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="future-dated"):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"), Decimal("1"),
            required_currency_code="EUR",
        )


def test_provider_failure_maps_to_precheck_error(monkeypatch):
    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        def read_account_details(self):
            raise BetfairReadOnlyError("network")

    monkeypatch.setattr(subject, "BetfairReadOnlyClient", FailingClient)
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="acquisition failed"):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"), Decimal("1"),
            required_currency_code="EUR",
        )


def test_caller_constructed_replace_and_pickle_objects_lack_authority(monkeypatch):
    install_client(monkeypatch)
    issued = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("25"),
        required_currency_code="EUR",
    )
    forged = subject.BetfairAccountFundsPrecheck(
        venue_id=issued.venue_id,
        account_id=issued.account_id,
        adapter_id=issued.adapter_id,
        adapter_version=issued.adapter_version,
        required_liability=issued.required_liability,
        available_to_bet_balance=issued.available_to_bet_balance,
        currency_code=issued.currency_code,
        account_observed_at=issued.account_observed_at,
        funds_observed_at=issued.funds_observed_at,
        evaluated_at=issued.evaluated_at,
        account_details_sha256=issued.account_details_sha256,
        account_funds_sha256=issued.account_funds_sha256,
    )
    assert forged == issued
    assert subject.is_authoritative_funds_precheck(forged) is False
    assert subject.is_authoritative_funds_precheck(replace(issued)) is False
    assert subject.is_authoritative_funds_precheck(pickle.loads(pickle.dumps(issued))) is False
    for candidate in (forged, replace(issued), pickle.loads(pickle.dumps(issued))):
        with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="lacks"):
            subject.require_authoritative_funds_precheck(candidate)


def test_digest_changes_with_economic_inputs(monkeypatch):
    install_client(monkeypatch)
    a = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("10"),
        required_currency_code="EUR",
    )
    b = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("11"),
        required_currency_code="EUR",
    )
    assert a.precheck_id != b.precheck_id


def test_wrong_credentials_type_rejected_without_network(monkeypatch):
    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network")

    monkeypatch.setattr(subject, "BetfairReadOnlyClient", ForbiddenClient)
    with pytest.raises(TypeError):
        subject.evaluate_betfair_account_funds(object(), Decimal("1"))


def test_issued_authority_expires_at_use_time(monkeypatch):
    install_client(monkeypatch, balance=Decimal("100"))
    now = [NOW]
    monkeypatch.setattr(subject, "_utc_now", lambda: now[0])
    result = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("25"),
        required_currency_code="EUR",
    )
    assert result.passed is True
    assert subject.require_authoritative_funds_precheck(result) is result

    now[0] = NOW + subject.MAX_FUNDS_EVIDENCE_AGE + timedelta(microseconds=1)

    assert subject.is_authoritative_funds_precheck(result) is False
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="authority"):
        subject.require_authoritative_funds_precheck(result)


def test_inplace_liability_mutation_cannot_upgrade_authority(monkeypatch):
    install_client(monkeypatch, balance=Decimal("100"))
    result = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("125"),
        required_currency_code="EUR",
    )
    assert result.passed is False
    assert subject.is_authoritative_funds_precheck(result) is True

    object.__setattr__(result, "required_liability", Decimal("0"))

    assert result.passed is True
    assert subject.is_authoritative_funds_precheck(result) is False
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="authority"):
        subject.require_authoritative_funds_precheck(result)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("available_to_bet_balance", Decimal("999999")),
        (
            "funds_observed_at",
            NOW - subject.MAX_FUNDS_EVIDENCE_AGE - timedelta(seconds=1),
        ),
        ("account_funds_sha256", "3" * 64),
        ("account_details_sha256", "4" * 64),
    ],
)
def test_inplace_economic_or_evidence_mutation_revokes_authority(
    monkeypatch, field, replacement
):
    install_client(monkeypatch)
    result = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("25"),
        required_currency_code="EUR",
    )
    assert subject.is_authoritative_funds_precheck(result) is True

    object.__setattr__(result, field, replacement)

    assert subject.is_authoritative_funds_precheck(result) is False
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="authority"):
        subject.require_authoritative_funds_precheck(result)


def test_currency_mismatch_fails_closed_before_issuing_authority(monkeypatch):
    install_client(monkeypatch, balance=Decimal("100"))

    with pytest.raises(
        subject.BetfairAccountFundsPrecheckError,
        match="currency",
    ):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"),
            Decimal("25"),
            required_currency_code="USD",
        )


@pytest.mark.parametrize("bad_currency", ["", "eur", "EURO", "€UR", " EU"])
def test_invalid_required_currency_fails_before_provider_read(monkeypatch, bad_currency):
    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("provider must not be read")

    monkeypatch.setattr(subject, "BetfairReadOnlyClient", ForbiddenClient)
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="currency"):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"),
            Decimal("1"),
            required_currency_code=bad_currency,
        )


def test_currency_is_bound_into_precheck_identity(monkeypatch):
    install_client(monkeypatch, balance=Decimal("100"))
    result = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"),
        Decimal("25"),
        required_currency_code="EUR",
    )
    original_id = result.precheck_id

    object.__setattr__(result, "currency_code", "USD")

    assert result.precheck_id != original_id
    assert subject.is_authoritative_funds_precheck(result) is False

def test_decimal_subclass_required_liability_rejected_before_provider_read(monkeypatch):
    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("provider must not be read")

    monkeypatch.setattr(subject, "BetfairReadOnlyClient", ForbiddenClient)
    required = _AdversarialDecimal("1000000")

    with pytest.raises(
        subject.BetfairAccountFundsPrecheckError,
        match="finite non-negative Decimal",
    ):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"),
            required,
            required_currency_code="EUR",
        )


def test_decimal_subclass_provider_balance_cannot_mint_sufficiency(monkeypatch):
    install_client(monkeypatch, balance=_AdversarialDecimal("0"))

    with pytest.raises(
        subject.BetfairAccountFundsPrecheckError,
        match="finite non-negative Decimal",
    ):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"),
            Decimal("1000000"),
            required_currency_code="EUR",
        )

