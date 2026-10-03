from __future__ import annotations

import copy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
import pickle
import urllib.request as _urllib_request

import pytest

from autosport import betfair_account_funds_precheck as subject
from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)


class _AdversarialDecimal(Decimal):
    def __new__(cls, value: str) -> "_AdversarialDecimal":
        return super().__new__(cls, value)

    def is_finite(self) -> bool:
        return True

    def __lt__(self, other: object) -> bool:
        return False


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        assert limit >= len(self._payload)
        return self._payload


def install_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    currency: str = "EUR",
    balance: str = "100",
    calls: list[str] | None = None,
) -> list[str]:
    seen = [] if calls is None else calls

    def fake_open(request, timeout: float):
        assert request.full_url == ACCOUNT_JSON_RPC_ENDPOINT
        assert timeout > 0
        decoded = json.loads(request.data.decode("utf-8"))
        method = decoded["method"]
        seen.append(method)
        if method == "AccountAPING/v1.0/getAccountDetails":
            result = {
                "currencyCode": currency,
                "localeCode": "en",
                "region": "GBR",
                "timezone": "Europe/London",
            }
        elif method == "AccountAPING/v1.0/getAccountFunds":
            result = {
                "availableToBetBalance": float(balance),
                "exposure": 0,
                "retainedCommission": 0,
                "exposureLimit": 1000,
            }
        else:
            raise AssertionError(f"unexpected provider method {method}")
        raw = json.dumps(
            {"jsonrpc": "2.0", "id": decoded["id"], "result": result},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return _Response(raw)

    class Opener:
        def open(self, request, data=None, timeout: float = 0):
            assert data is None
            return fake_open(request, timeout)

    # K07 pins autosport.betfair_account_readonly.urlopen itself. Replacing only
    # stdlib's opener preserves that canonical function while making IO deterministic.
    monkeypatch.setattr(_urllib_request, "_opener", Opener())
    return seen


def creds(token: str = "session-a") -> BetfairSessionCredentials:
    return BetfairSessionCredentials("app-key", token)


def test_same_k07_context_binds_details_and_funds_but_stays_fail_closed(monkeypatch):
    calls = install_provider(monkeypatch, balance="100")
    result = subject.evaluate_betfair_account_funds(
        creds(), Decimal("25"), required_currency_code="EUR"
    )

    assert calls == [
        "AccountAPING/v1.0/getAccountDetails",
        "AccountAPING/v1.0/getAccountFunds",
    ]
    assert result.account_id.startswith("betfair-session-context:")
    assert result.stable_account_identity_proven is False
    assert result.numeric_sufficient is True
    assert result.liability_unit_proven is False
    assert result.passed is False
    assert result.execution_authorized is False
    assert subject.is_authoritative_funds_precheck(result) is True
    assert subject.require_authoritative_funds_precheck(result) is result


def test_numeric_boundary_is_exact_but_never_promoted_to_positive_authority(monkeypatch):
    install_provider(monkeypatch, balance="25")
    equal = subject.evaluate_betfair_account_funds(
        creds("equal"), Decimal("25"), required_currency_code="EUR"
    )
    over = subject.evaluate_betfair_account_funds(
        creds("over"), Decimal("25.01"), required_currency_code="EUR"
    )
    assert equal.numeric_sufficient is True
    assert over.numeric_sufficient is False
    assert equal.passed is False
    assert over.passed is False


def test_identical_provider_payloads_from_distinct_sessions_do_not_alias(monkeypatch):
    install_provider(monkeypatch)
    left = subject.evaluate_betfair_account_funds(
        creds("session-left"), Decimal("1"), required_currency_code="EUR"
    )
    right = subject.evaluate_betfair_account_funds(
        creds("session-right"), Decimal("1"), required_currency_code="EUR"
    )
    assert left.account_details_sha256 == right.account_details_sha256
    assert left.account_funds_sha256 == right.account_funds_sha256
    assert left.account_id != right.account_id
    assert left.precheck_id != right.precheck_id


def test_currency_mismatch_fails_before_funds_read(monkeypatch):
    calls = install_provider(monkeypatch, currency="EUR")
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="currency"):
        subject.evaluate_betfair_account_funds(
            creds(), Decimal("1"), required_currency_code="GBP"
        )
    assert calls == ["AccountAPING/v1.0/getAccountDetails"]


@pytest.mark.parametrize("bad", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_liability_fails_before_provider_io(monkeypatch, bad):
    calls = install_provider(monkeypatch)
    with pytest.raises(subject.BetfairAccountFundsPrecheckError):
        subject.evaluate_betfair_account_funds(
            creds(), bad, required_currency_code="EUR"
        )
    assert calls == []


def test_decimal_subclass_cannot_override_economic_comparison(monkeypatch):
    calls = install_provider(monkeypatch)
    with pytest.raises(subject.BetfairAccountFundsPrecheckError):
        subject.evaluate_betfair_account_funds(
            creds(), _AdversarialDecimal("0"), required_currency_code="EUR"
        )
    assert calls == []


@pytest.mark.parametrize("bad_currency", ["", "eur", "EURO", "€UR", " EUR"])
def test_invalid_currency_fails_before_provider_io(monkeypatch, bad_currency):
    calls = install_provider(monkeypatch)
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="currency"):
        subject.evaluate_betfair_account_funds(
            creds(), Decimal("1"), required_currency_code=bad_currency
        )
    assert calls == []


def test_copy_reconstruction_and_pickle_cannot_mint_authority(monkeypatch):
    install_provider(monkeypatch)
    issued = subject.evaluate_betfair_account_funds(
        creds(), Decimal("1"), required_currency_code="EUR"
    )
    candidates = (replace(issued), copy.copy(issued), pickle.loads(pickle.dumps(issued)))
    for candidate in candidates:
        assert candidate == issued
        assert subject.is_authoritative_funds_precheck(candidate) is False
        with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="authority"):
            subject.require_authoritative_funds_precheck(candidate)


def test_same_object_mutation_revokes_authority(monkeypatch):
    install_provider(monkeypatch)
    issued = subject.evaluate_betfair_account_funds(
        creds(), Decimal("125"), required_currency_code="EUR"
    )
    assert subject.is_authoritative_funds_precheck(issued) is True
    object.__setattr__(issued, "required_liability", Decimal("0"))
    assert issued.numeric_sufficient is True
    assert subject.is_authoritative_funds_precheck(issued) is False


def test_stale_funds_mutation_revokes_authority(monkeypatch):
    install_provider(monkeypatch)
    issued = subject.evaluate_betfair_account_funds(
        creds(), Decimal("1"), required_currency_code="EUR"
    )
    object.__setattr__(
        issued,
        "funds_observed_at",
        issued.funds_observed_at - subject.MAX_EVIDENCE_AGE - timedelta(seconds=1),
    )
    assert subject.is_authoritative_funds_precheck(issued) is False


def test_import_surface_has_no_writable_issuance_registry_or_mint(monkeypatch):
    install_provider(monkeypatch)
    assert not hasattr(subject, "_ISSUED")
    assert not hasattr(subject, "_remember_issued")
    assert not hasattr(subject, "_make_authority")


def test_rebound_funds_method_fails_before_fake_funds_can_mint_authority(monkeypatch):
    install_provider(monkeypatch)

    def fake_read_funds(self):
        raise AssertionError("rebound funds method must never run")

    monkeypatch.setattr(BetfairReadOnlyClient, "read_account_funds", fake_read_funds)
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="rebound|authority"):
        subject.evaluate_betfair_account_funds(
            creds(), Decimal("1"), required_currency_code="EUR"
        )


def test_precheck_identity_binds_context_and_economic_fields(monkeypatch):
    install_provider(monkeypatch)
    first = subject.evaluate_betfair_account_funds(
        creds("a"), Decimal("1"), required_currency_code="EUR"
    )
    second = subject.evaluate_betfair_account_funds(
        creds("b"), Decimal("2"), required_currency_code="EUR"
    )
    assert first.precheck_id != second.precheck_id
