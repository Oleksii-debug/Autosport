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


_T0 = datetime(2026, 9, 21, 15, 0, 0, tzinfo=timezone.utc)
_DETAILS_SHA = "1" * 64
_FUNDS_SHA = "2" * 64


def _evidence(digest: str) -> BetfairEvidence:
    return BetfairEvidence(_T0.isoformat().replace("+00:00", "Z"), digest)


def test_positive_funds_precheck_expires_at_use_time(monkeypatch) -> None:
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
                _evidence(_DETAILS_SHA),
            )

        def read_account_funds(self):
            return BetfairAccountFundsObservation(
                Decimal("100"),
                Decimal("0"),
                Decimal("0"),
                Decimal("1000"),
                _evidence(_FUNDS_SHA),
            )

    now = [_T0]
    monkeypatch.setattr(subject, "BetfairReadOnlyClient", FakeClient)
    monkeypatch.setattr(subject, "_utc_now", lambda: now[0])

    result = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("app", "token"), Decimal("25")
    )
    assert result.passed is True
    assert subject.require_authoritative_funds_precheck(result) is result

    now[0] = _T0 + subject.MAX_FUNDS_EVIDENCE_AGE + timedelta(microseconds=1)

    # Process-local identity is not sufficient current-funds authority after the
    # exact evidence window has expired. The current parent never rechecks time
    # after issuance, so the same once-fresh positive object remains reusable.
    assert subject.is_authoritative_funds_precheck(result) is False
    with pytest.raises(subject.BetfairAccountFundsPrecheckError):
        subject.require_authoritative_funds_precheck(result)
