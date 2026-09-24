from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import http.client as _http_client
import json
import pickle

import pytest

from autosport import betfair_account_funds_precheck as subject
from autosport import betfair_account_readonly as _readonly
from autosport.betfair_account_identity import build_betfair_authenticated_client
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)


class _Response:
    status = 200
    code = 200
    reason = "OK"
    msg = "OK"

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def info(self):
        return {}

    def read(self, limit: int | None = None) -> bytes:
        if limit is None:
            return self._payload
        assert limit >= len(self._payload)
        return self._payload

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


def _install_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    currency_code: str = "EUR",
    balance: object = 100,
    account_error: str | None = None,
    funds_error: str | None = None,
) -> list[str]:
    """Stub provider I/O below the product-owned private opener boundary."""
    methods: list[str] = []

    def fake_request(
        connection,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        *,
        encode_chunked: bool = False,
    ) -> None:
        assert method == "POST"
        assert url.endswith("/json-rpc/v1")
        assert isinstance(body, bytes)
        assert headers is not None
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        assert normalized_headers["x-application"]
        assert normalized_headers["x-authentication"]
        rpc = json.loads(body.decode("utf-8"))
        methods.append(rpc["method"])
        connection._autosport_funds_request = rpc
        connection._autosport_funds_encode_chunked = encode_chunked

    def fake_getresponse(connection):
        rpc = connection._autosport_funds_request
        method = rpc["method"]
        if method.endswith("getAccountDetails"):
            if account_error is not None:
                payload = {
                    "jsonrpc": "2.0",
                    "id": rpc["id"],
                    "error": {"code": -32099, "message": account_error},
                }
            else:
                payload = {
                    "jsonrpc": "2.0",
                    "id": rpc["id"],
                    "result": {
                        "currencyCode": currency_code,
                        "localeCode": "en",
                        "region": "SVK",
                        "timezone": "Europe/Bratislava",
                    },
                }
        elif method.endswith("getAccountFunds"):
            if funds_error is not None:
                payload = {
                    "jsonrpc": "2.0",
                    "id": rpc["id"],
                    "error": {"code": -32098, "message": funds_error},
                }
            else:
                payload = {
                    "jsonrpc": "2.0",
                    "id": rpc["id"],
                    "result": {
                        "availableToBetBalance": balance,
                        "exposure": 0,
                        "retainedCommission": 0,
                        "exposureLimit": -1000,
                    },
                }
        else:  # pragma: no cover - this authority uses only these two reads.
            raise AssertionError(method)
        return _Response(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode(
                "utf-8"
            )
        )

    monkeypatch.setattr(_http_client.HTTPSConnection, "request", fake_request)
    monkeypatch.setattr(_http_client.HTTPSConnection, "getresponse", fake_getresponse)
    return methods


def _client(
    *,
    application_key: str = "app-key",
    session_token: str = "session-token",
) -> BetfairReadOnlyClient:
    return build_betfair_authenticated_client(
        BetfairSessionCredentials(application_key, session_token),
        account_label="funds-precheck-test",
    )


def _evaluate(
    *,
    client: BetfairReadOnlyClient | None = None,
    required: Decimal = Decimal("25"),
    currency: str = "EUR",
):
    return subject.evaluate_betfair_account_funds(
        client or _client(),
        required,
        required_currency_code=currency,
    )


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


def test_happy_path_is_bound_to_exact_product_issued_k07_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    methods = _install_provider(monkeypatch, balance=100)
    client = _client()

    result = _evaluate(client=client)

    assert result.passed is True
    assert result.execution_authorized is False
    assert result.account_context_id.startswith("betfair-session-context:")
    assert len(result.account_identity_id) == 64
    assert len(result.account_details_sha256) == 64
    assert len(result.account_funds_sha256) == 64
    assert result.currency_code == "EUR"
    assert not hasattr(result, "account_id")
    assert subject._ISSUED[id(result)].client is client
    assert subject.is_authoritative_funds_precheck(result)
    assert subject.require_authoritative_funds_precheck(result) is result
    assert methods == [
        "AccountAPING/v1.0/getAccountDetails",
        "AccountAPING/v1.0/getAccountFunds",
    ]


def test_identical_public_payloads_in_distinct_authenticated_contexts_do_not_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_provider(monkeypatch, balance=100)
    first_client = _client(application_key="app-a", session_token="session-a")
    second_client = _client(application_key="app-b", session_token="session-b")

    first = _evaluate(client=first_client)
    second = _evaluate(client=second_client)

    assert first.currency_code == second.currency_code == "EUR"
    assert first.account_context_id != second.account_context_id
    assert first.account_identity_id != second.account_identity_id
    assert first.precheck_id != second.precheck_id
    assert subject.is_authoritative_funds_precheck(first)
    assert subject.is_authoritative_funds_precheck(second)


def test_same_credentials_in_distinct_k07_clients_do_not_claim_cross_context_equivalence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_provider(monkeypatch, balance=100)
    credentials = BetfairSessionCredentials("same-app", "same-session")
    first_client = build_betfair_authenticated_client(
        credentials,
        account_label="first",
    )
    second_client = build_betfair_authenticated_client(
        credentials,
        account_label="second",
    )

    first = _evaluate(client=first_client)
    second = _evaluate(client=second_client)

    assert first.account_context_id != second.account_context_id
    assert first.account_identity_id != second.account_identity_id


def test_direct_unissued_readonly_client_cannot_mint_funds_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    methods = _install_provider(monkeypatch)
    direct = BetfairReadOnlyClient(
        BetfairSessionCredentials("app", "session"),
        venue_id="betfair",
        account_id="caller-label",
    )

    with pytest.raises(
        subject.BetfairAccountFundsPrecheckError,
        match="account-funds acquisition failed",
    ):
        _evaluate(client=direct)

    assert methods == []


def test_k07_session_rotation_revokes_existing_positive_funds_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_provider(monkeypatch, balance=100)
    client = _client()
    result = _evaluate(client=client)
    assert subject.is_authoritative_funds_precheck(result)

    client._credentials = BetfairSessionCredentials(
        "rotated-app-key",
        "rotated-session-token",
    )

    assert result.passed is True  # immutable audit fact remains inspectable
    assert not subject.is_authoritative_funds_precheck(result)
    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="authority"):
        subject.require_authoritative_funds_precheck(result)


def test_k07_transport_origin_rotation_revokes_existing_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_provider(monkeypatch, balance=100)
    client = _client()
    result = _evaluate(client=client)
    assert subject.is_authoritative_funds_precheck(result)

    client._transport = _readonly.UrllibBetfairHttpTransport()

    assert not subject.is_authoritative_funds_precheck(result)


def test_equal_balance_passes_and_one_cent_over_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_provider(monkeypatch, balance=25)

    equal = _evaluate(required=Decimal("25"))
    over = _evaluate(required=Decimal("25.01"))

    assert equal.passed is True
    assert over.passed is False
    assert subject.is_authoritative_funds_precheck(equal)
    assert subject.is_authoritative_funds_precheck(over)


def test_currency_mismatch_fails_closed_without_issuing_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_provider(monkeypatch, currency_code="EUR", balance=100)

    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="currency"):
        _evaluate(currency="USD")


@pytest.mark.parametrize("bad_currency", ["", "eur", "EURO", "€UR", " EU"])
def test_invalid_required_currency_fails_before_provider_read(
    monkeypatch: pytest.MonkeyPatch,
    bad_currency: str,
) -> None:
    methods = _install_provider(monkeypatch)
    client = _client()

    with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="currency"):
        _evaluate(client=client, currency=bad_currency)

    assert methods == []


@pytest.mark.parametrize(
    "bad",
    [Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")],
)
def test_invalid_required_liability_fails_before_provider_read(
    monkeypatch: pytest.MonkeyPatch,
    bad: Decimal,
) -> None:
    methods = _install_provider(monkeypatch)
    client = _client()

    with pytest.raises(subject.BetfairAccountFundsPrecheckError):
        _evaluate(client=client, required=bad)

    assert methods == []


def test_adversarial_decimal_subclass_is_rejected_before_provider_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    methods = _install_provider(monkeypatch)
    client = _client()
    required = _AdversarialDecimal("1000000")

    with pytest.raises(
        subject.BetfairAccountFundsPrecheckError,
        match="finite non-negative Decimal",
    ):
        _evaluate(client=client, required=required)

    assert methods == []


def test_negative_provider_balance_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_provider(monkeypatch, balance=-1)

    with pytest.raises(
        subject.BetfairAccountFundsPrecheckError,
        match="finite non-negative Decimal",
    ):
        _evaluate(required=Decimal("0"))


def test_provider_identity_or_funds_failure_is_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "DO-NOT-ECHO-SECRET"
    _install_provider(monkeypatch, account_error=secret)

    with pytest.raises(
        subject.BetfairAccountFundsPrecheckError,
        match="account-funds acquisition failed",
    ) as caught:
        _evaluate()

    assert secret not in str(caught.value)

    _install_provider(monkeypatch, funds_error=secret)
    with pytest.raises(
        subject.BetfairAccountFundsPrecheckError,
        match="account-funds acquisition failed",
    ) as caught:
        _evaluate()
    assert secret not in str(caught.value)


def test_caller_copy_replace_pickle_and_reconstruction_lack_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_provider(monkeypatch)
    issued = _evaluate()
    forged = subject.BetfairAccountFundsPrecheck(
        venue_id=issued.venue_id,
        account_context_id=issued.account_context_id,
        account_identity_id=issued.account_identity_id,
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
    candidates = (
        forged,
        replace(issued),
        pickle.loads(pickle.dumps(issued)),
    )
    assert forged == issued
    for candidate in candidates:
        assert not subject.is_authoritative_funds_precheck(candidate)
        with pytest.raises(subject.BetfairAccountFundsPrecheckError, match="lacks"):
            subject.require_authoritative_funds_precheck(candidate)


def test_authority_expires_at_use_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_provider(monkeypatch)
    result = _evaluate()
    assert subject.is_authoritative_funds_precheck(result)

    checked_at = (
        result.funds_observed_at
        + subject.MAX_FUNDS_EVIDENCE_AGE
        + timedelta(microseconds=1)
    )
    monkeypatch.setattr(subject, "_utc_now", lambda: checked_at)

    assert not subject.is_authoritative_funds_precheck(result)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("required_liability", Decimal("0")),
        ("available_to_bet_balance", Decimal("999999")),
        ("account_context_id", "betfair-session-context:" + "0" * 64),
        ("account_identity_id", "1" * 64),
        ("currency_code", "USD"),
        ("account_details_sha256", "2" * 64),
        ("account_funds_sha256", "3" * 64),
    ],
)
def test_same_object_authority_field_mutation_revokes_precheck(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    _install_provider(monkeypatch, balance=100)
    result = _evaluate(required=Decimal("125"))
    assert subject.is_authoritative_funds_precheck(result)

    object.__setattr__(result, field, replacement)

    assert not subject.is_authoritative_funds_precheck(result)


def test_precheck_identity_binds_economic_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_provider(monkeypatch, balance=100)
    client = _client()

    left = _evaluate(client=client, required=Decimal("10"))
    right = _evaluate(client=client, required=Decimal("11"))

    assert left.account_context_id == right.account_context_id
    assert left.precheck_id != right.precheck_id


def test_wrong_client_type_rejected_before_provider_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    methods = _install_provider(monkeypatch)

    with pytest.raises(TypeError, match="exact canonical"):
        subject.evaluate_betfair_account_funds(
            object(),
            Decimal("1"),
            required_currency_code="EUR",
        )

    assert methods == []
