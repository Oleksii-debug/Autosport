from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport import betfair_account_funds_precheck as subject
from autosport.betfair_account_readonly import (
    BetfairAccountDetailsObservation,
    BetfairAccountFundsObservation,
    BetfairEvidence,
    BetfairSessionCredentials,
)


NOW = datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)
DETAILS_SHA = "1" * 64
FUNDS_SHA = "2" * 64


def _evidence(at: datetime, digest: str) -> BetfairEvidence:
    return BetfairEvidence(
        at.isoformat().replace("+00:00", "Z"),
        digest,
    )


def _install_client(monkeypatch, *, balance: Decimal = Decimal("100")) -> None:
    class FakeClient:
        def __init__(self, credentials, **kwargs):
            assert type(credentials) is BetfairSessionCredentials
            assert kwargs["venue_id"] == "betfair"
            assert kwargs["account_id"] == "authenticated-account"

        def read_account_details(self):
            return BetfairAccountDetailsObservation(
                "EUR",
                "en",
                "SK",
                "Europe/Bratislava",
                _evidence(NOW, DETAILS_SHA),
            )

        def read_account_funds(self):
            return BetfairAccountFundsObservation(
                balance,
                Decimal("0"),
                Decimal("0"),
                Decimal("1000"),
                _evidence(NOW, FUNDS_SHA),
            )

    monkeypatch.setattr(subject, "BetfairReadOnlyClient", FakeClient)
    monkeypatch.setattr(subject, "_utc_now", lambda: NOW)


def _issue(monkeypatch, *, required: Decimal):
    _install_client(monkeypatch)
    return subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"),
        required,
    )


def test_inplace_liability_mutation_cannot_upgrade_insufficient_result(monkeypatch):
    result = _issue(monkeypatch, required=Decimal("125"))
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
        ("funds_observed_at", NOW - subject.MAX_FUNDS_EVIDENCE_AGE - timedelta(seconds=1)),
        ("account_funds_sha256", "3" * 64),
        ("account_details_sha256", "4" * 64),
    ],
)
def test_any_inplace_economic_or_evidence_mutation_revokes_authority(
    monkeypatch,
    field,
    replacement,
):
    result = _issue(monkeypatch, required=Decimal("25"))
    assert result.passed is True
    assert subject.is_authoritative_funds_precheck(result) is True

    object.__setattr__(result, field, replacement)

    assert subject.is_authoritative_funds_precheck(result) is False
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="authority"):
        subject.require_authoritative_funds_precheck(result)


def test_unmodified_issued_result_remains_authoritative(monkeypatch):
    result = _issue(monkeypatch, required=Decimal("25"))

    assert subject.is_authoritative_funds_precheck(result) is True
    assert subject.require_authoritative_funds_precheck(result) is result
