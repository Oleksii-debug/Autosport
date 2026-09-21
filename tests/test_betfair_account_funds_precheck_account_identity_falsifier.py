from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from autosport import betfair_account_funds_precheck as subject
from autosport.betfair_account_readonly import (
    BetfairAccountDetailsObservation,
    BetfairAccountFundsObservation,
    BetfairEvidence,
    BetfairSessionCredentials,
)


NOW = datetime(2026, 9, 21, 15, 47, tzinfo=timezone.utc)
DETAILS_SHA = "1" * 64
FUNDS_SHA_A = "2" * 64
FUNDS_SHA_B = "3" * 64


def _evidence(digest: str) -> BetfairEvidence:
    return BetfairEvidence(
        NOW.isoformat().replace("+00:00", "Z"),
        digest,
    )


def test_distinct_authenticated_contexts_cannot_alias_via_identical_account_details_digest(
    monkeypatch,
):
    """A getAccountDetails payload hash is not provider account identity.

    Two separately authenticated contexts can legitimately expose byte-identical
    non-PII account attributes. Without a canonical provider-issued account
    identity, a funds precheck must not collapse those contexts onto one
    account_id merely because the account-details response bytes match.
    """

    class FakeClient:
        def __init__(self, credentials, **kwargs):
            assert type(credentials) is BetfairSessionCredentials
            assert kwargs["venue_id"] == "betfair"
            assert kwargs["account_id"] == "authenticated-account"
            self._credentials = credentials

        def read_account_details(self):
            return BetfairAccountDetailsObservation(
                "EUR",
                "en",
                "SK",
                "Europe/Bratislava",
                _evidence(DETAILS_SHA),
            )

        def read_account_funds(self):
            if self._credentials.session_token == "session-a":
                balance = Decimal("100")
                digest = FUNDS_SHA_A
            else:
                balance = Decimal("200")
                digest = FUNDS_SHA_B
            return BetfairAccountFundsObservation(
                balance,
                Decimal("0"),
                Decimal("0"),
                Decimal("1000"),
                _evidence(digest),
            )

    monkeypatch.setattr(subject, "BetfairReadOnlyClient", FakeClient)
    monkeypatch.setattr(subject, "_utc_now", lambda: NOW)

    context_a = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("application-a", "session-a"),
        Decimal("10"),
    )
    context_b = subject.evaluate_betfair_account_funds(
        BetfairSessionCredentials("application-b", "session-b"),
        Decimal("10"),
    )

    assert subject.is_authoritative_funds_precheck(context_a) is True
    assert subject.is_authoritative_funds_precheck(context_b) is True

    # The provider account-details bytes are intentionally identical while the
    # authenticated contexts and current funds observations are distinct.
    assert context_a.account_details_sha256 == context_b.account_details_sha256
    assert context_a.account_funds_sha256 != context_b.account_funds_sha256
    assert context_a.available_to_bet_balance != context_b.available_to_bet_balance

    # Expected RED on #949@859fe71d: current code derives account_id only from
    # account_details_sha256, so both contexts incorrectly collapse to one ID.
    assert context_a.account_id != context_b.account_id
