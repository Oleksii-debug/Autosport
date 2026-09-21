from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import inspect

import pytest

from autosport import betfair_account_funds_precheck as subject
from autosport.betfair_account_readonly import (
    BetfairAccountDetailsObservation,
    BetfairAccountFundsObservation,
    BetfairEvidence,
    BetfairSessionCredentials,
)


NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
DETAILS_SHA = "1" * 64
FUNDS_SHA = "2" * 64


def _evidence(digest: str) -> BetfairEvidence:
    return BetfairEvidence(NOW.isoformat().replace("+00:00", "Z"), digest)


def _install_eur_account(monkeypatch) -> None:
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
                _evidence(DETAILS_SHA),
            )

        def read_account_funds(self):
            return BetfairAccountFundsObservation(
                Decimal("100"),
                Decimal("0"),
                Decimal("0"),
                Decimal("1000"),
                _evidence(FUNDS_SHA),
            )

    monkeypatch.setattr(subject, "BetfairReadOnlyClient", FakeClient)
    monkeypatch.setattr(subject, "_utc_now", lambda: NOW)


def _liability_denomination_parameter() -> str:
    parameters = inspect.signature(subject.evaluate_betfair_account_funds).parameters
    candidates = [
        name
        for name in parameters
        if name not in {"credentials", "required_liability", "timeout_seconds"}
        and ("currency" in name.lower() or "denomination" in name.lower())
    ]
    assert len(candidates) == 1, (
        "positive funds sufficiency must consume one explicit liability "
        "currency/denomination instead of comparing bare Decimal magnitudes"
    )
    return candidates[0]


def test_mismatched_liability_denomination_cannot_issue_positive_precheck(monkeypatch):
    _install_eur_account(monkeypatch)
    denomination_parameter = _liability_denomination_parameter()

    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="currency|denomination"):
        subject.evaluate_betfair_account_funds(
            BetfairSessionCredentials("app", "token"),
            Decimal("90"),
            **{denomination_parameter: "USD"},
        )


def test_positive_precheck_binds_same_provider_currency(monkeypatch):
    _install_eur_account(monkeypatch)
    denomination_parameter = _liability_denomination_parameter()

    result = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"),
        Decimal("90"),
        **{denomination_parameter: "EUR"},
    )

    assert result.passed is True
    assert getattr(result, "currency_code", None) == "EUR"
    assert result.execution_authorized is False
    assert subject.is_authoritative_funds_precheck(result) is True
