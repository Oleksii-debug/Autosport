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
        BetfairSessionCredentials("app", "token"), Decimal("25")
    )
    assert result.passed is True
    assert result.execution_authorized is False
    assert result.account_id == f"betfair-account-evidence:{DETAILS_SHA}"
    assert result.account_funds_sha256 == FUNDS_SHA
    assert subject.is_authoritative_funds_precheck(result) is True
    assert subject.require_authoritative_funds_precheck(result) is result


def test_equal_balance_passes_and_one_cent_over_fails(monkeypatch):
    install_client(monkeypatch, balance=Decimal("25.00"))
    equal = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("25.00")
    )
    assert equal.passed is True
    over = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("25.01")
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
            BetfairSessionCredentials("app", "token"), bad
        )


def test_negative_provider_available_balance_fails_closed(monkeypatch):
    install_client(monkeypatch, balance=Decimal("-0.01"))
    with pytest.raises(subject.BetfairAccountFundsPrecheckError):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"), Decimal("0")
        )


def test_stale_and_future_funds_evidence_fail_closed(monkeypatch):
    install_client(
        monkeypatch,
        funds_at=NOW - subject.MAX_FUNDS_EVIDENCE_AGE - timedelta(microseconds=1),
    )
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="stale"):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"), Decimal("1")
        )
    install_client(monkeypatch, funds_at=NOW + timedelta(seconds=2))
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="future-dated"):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"), Decimal("1")
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
            BetfairSessionCredentials("app", "token"), Decimal("1")
        )


def test_caller_constructed_replace_and_pickle_objects_lack_authority(monkeypatch):
    install_client(monkeypatch)
    issued = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("25")
    )
    forged = subject.BetfairAccountFundsPrecheck(
        venue_id=issued.venue_id,
        account_id=issued.account_id,
        adapter_id=issued.adapter_id,
        adapter_version=issued.adapter_version,
        required_liability=issued.required_liability,
        available_to_bet_balance=issued.available_to_bet_balance,
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
        BetfairSessionCredentials("app", "token"), Decimal("10")
    )
    b = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("11")
    )
    assert a.precheck_id != b.precheck_id


def test_wrong_credentials_type_rejected_without_network(monkeypatch):
    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network")

    monkeypatch.setattr(subject, "BetfairReadOnlyClient", ForbiddenClient)
    with pytest.raises(TypeError):
        subject.evaluate_betfair_account_funds(object(), Decimal("1"))
