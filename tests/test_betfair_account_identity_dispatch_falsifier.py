from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport import betfair_account_identity as _identity
from autosport import betfair_account_readonly as _readonly
from autosport.betfair_account_identity import (
    BetfairAccountIdentityError,
    build_betfair_authenticated_client,
    resolve_betfair_authenticated_account_identity,
)
from autosport.betfair_account_readonly import (
    BetfairAccountDetailsObservation,
    BetfairEvidence,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)


def _client() -> BetfairReadOnlyClient:
    return build_betfair_authenticated_client(
        BetfairSessionCredentials("test-app-key", "test-session-token")
    )


def _forged_details(currency_code: str = "GBP") -> BetfairAccountDetailsObservation:
    return BetfairAccountDetailsObservation(
        currency_code=currency_code,
        locale_code="en",
        region="GBR",
        timezone_name="Europe/London",
        evidence=BetfairEvidence(
            observed_at=datetime(2026, 9, 22, tzinfo=timezone.utc).isoformat(),
            source_payload_sha256="1" * 64,
        ),
    )


def test_instance_read_account_details_rebinding_cannot_mint_k07_authority() -> None:
    client = _client()
    client.read_account_details = lambda: _forged_details("GBP")

    with pytest.raises(BetfairAccountIdentityError):
        resolve_betfair_authenticated_account_identity(client)


def test_instance_rpc_rebinding_cannot_mint_k07_authority() -> None:
    client = _client()

    def forged_rpc(method: str, params: object):
        assert method == "AccountAPING/v1.0/getAccountDetails"
        return _readonly._RpcResult(
            result={
                "currencyCode": "GBP",
                "localeCode": "en",
                "region": "GBR",
                "timezone": "Europe/London",
            },
            evidence=BetfairEvidence(
                observed_at=datetime(2026, 9, 22, tzinfo=timezone.utc).isoformat(),
                source_payload_sha256="2" * 64,
            ),
        )

    client._rpc = forged_rpc

    with pytest.raises(BetfairAccountIdentityError):
        resolve_betfair_authenticated_account_identity(client)


def test_class_read_account_details_rebinding_cannot_mint_k07_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    monkeypatch.setattr(
        BetfairReadOnlyClient,
        "read_account_details",
        lambda self: _forged_details("GBP"),
    )

    with pytest.raises(BetfairAccountIdentityError):
        resolve_betfair_authenticated_account_identity(client)


def test_class_rpc_rebinding_cannot_mint_k07_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()

    def forged_rpc(self, method: str, params: object):
        assert method == "AccountAPING/v1.0/getAccountDetails"
        return _readonly._RpcResult(
            result={
                "currencyCode": "GBP",
                "localeCode": "en",
                "region": "GBR",
                "timezone": "Europe/London",
            },
            evidence=BetfairEvidence(
                observed_at=datetime(2026, 9, 22, tzinfo=timezone.utc).isoformat(),
                source_payload_sha256="3" * 64,
            ),
        )

    monkeypatch.setattr(BetfairReadOnlyClient, "_rpc", forged_rpc)

    with pytest.raises(BetfairAccountIdentityError):
        resolve_betfair_authenticated_account_identity(client)


def test_instance_support_dispatch_rebinding_cannot_mint_k07_authority() -> None:
    for attribute, replacement in (
        ("_next_request_id", lambda: 1),
        ("_observed_at", lambda: datetime(2026, 9, 22, tzinfo=timezone.utc).isoformat()),
    ):
        client = _client()
        setattr(client, attribute, replacement)
        with pytest.raises(BetfairAccountIdentityError):
            resolve_betfair_authenticated_account_identity(client)


def test_readonly_build_opener_rebinding_blocks_k07_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _readonly,
        "build_opener",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("forged opener factory must never be accepted")
        ),
    )

    with pytest.raises(BetfairAccountIdentityError):
        _client()


def test_private_opener_replacement_revokes_k07_origin() -> None:
    client = _client()
    client._transport._opener = object()

    with pytest.raises(BetfairAccountIdentityError):
        resolve_betfair_authenticated_account_identity(client)


def test_private_opener_handler_mutation_revokes_k07_origin() -> None:
    client = _client()
    client._transport._opener.handlers.append(object())

    with pytest.raises(BetfairAccountIdentityError):
        resolve_betfair_authenticated_account_identity(client)


def test_opener_open_rebinding_revokes_k07_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    opener_type = type(client._transport._opener)
    monkeypatch.setattr(
        opener_type,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("forged opener dispatch must never be called")
        ),
    )

    with pytest.raises(BetfairAccountIdentityError):
        resolve_betfair_authenticated_account_identity(client)


def test_readonly_parser_global_rebinding_revokes_k07_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    monkeypatch.setattr(
        _readonly,
        "_decode_json",
        lambda payload: {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"currencyCode": "GBP"},
        },
    )

    with pytest.raises(BetfairAccountIdentityError):
        resolve_betfair_authenticated_account_identity(client)


def test_factory_origin_registry_and_binding_secret_are_not_module_writable() -> None:
    # K07's issuance registry and credential-binding key live only inside the
    # shared closure returned at module initialization. A caller cannot register
    # an ordinary direct client by writing a module-level WeakKeyDictionary.
    assert not hasattr(_identity, "_CANONICAL_CLIENT_ORIGINS")
    assert not hasattr(_identity, "_PROCESS_HMAC_KEY")
    assert not hasattr(_identity, "_credential_binding")

    direct = BetfairReadOnlyClient(
        BetfairSessionCredentials("test-app-key", "test-session-token")
    )
    with pytest.raises(BetfairAccountIdentityError):
        resolve_betfair_authenticated_account_identity(direct)
