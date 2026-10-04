from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import ssl
from urllib.request import OpenerDirector

import pytest

import autosport.betfair_account_readonly as readonly
from autosport.betfair_account_identity import (
    build_betfair_authenticated_client,
    resolve_betfair_authenticated_account_identity,
)
from autosport.betfair_account_readonly import ACCOUNT_JSON_RPC_ENDPOINT
from autosport.betfair_session_origin import (
    BetfairLoginJurisdiction,
    BetfairNonInteractiveLoginSecrets,
    bind_betfair_authenticated_jurisdiction,
    is_authoritative_betfair_authenticated_jurisdiction,
    is_authoritative_betfair_session_origin,
    login_betfair_noninteractive,
    require_authoritative_betfair_authenticated_jurisdiction,
)


class _Response:
    def __init__(self, payload: bytes, *, url: str | None = None) -> None:
        self._payload = payload
        self.status = 200
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        assert limit >= len(self._payload)
        return self._payload

    def geturl(self) -> str | None:
        return self._url


def _install_provider_boundary_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_cert_file_io(
        self: ssl.SSLContext,
        certfile: str,
        keyfile: str | None = None,
        password=None,
    ) -> None:
        return None

    monkeypatch.setattr(
        ssl.SSLContext,
        "load_cert_chain",
        no_cert_file_io,
    )

    def login_open(self, request, timeout: float):
        assert timeout > 0
        endpoint = request.full_url
        assert endpoint == (
            "https://identitysso-cert.betfair.com/api/certlogin"
        )
        payload = json.dumps(
            {
                "sessionToken": "provider-session-token",
                "loginStatus": "SUCCESS",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _Response(payload, url=endpoint)

    monkeypatch.setattr(OpenerDirector, "open", login_open)

    def account_urlopen(request, timeout: float):
        assert timeout > 0
        assert request.full_url == ACCOUNT_JSON_RPC_ENDPOINT
        assert request.data is not None
        rpc = json.loads(request.data.decode("utf-8"))
        assert rpc["method"] == "AccountAPING/v1.0/getAccountDetails"
        assert rpc["params"] == {}
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": rpc["id"],
                "result": {
                    "currencyCode": "GBP",
                    "localeCode": "en",
                    "region": "GBR",
                    "timezone": "Europe/London",
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _Response(payload)

    monkeypatch.setattr(readonly, "urlopen", account_urlopen)


def test_successful_login_origin_binds_to_same_current_k07_session_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_provider_boundary_fakes(monkeypatch)

    login = login_betfair_noninteractive(
        BetfairNonInteractiveLoginSecrets(
            application_key="app-key",
            username="account-user",
            password="account-password",
            certificate_path=tmp_path / "client.crt",
            private_key_path=tmp_path / "client.key",
        ),
        jurisdiction=BetfairLoginJurisdiction.GLOBAL_COM,
    )

    assert is_authoritative_betfair_session_origin(
        login.origin,
        credentials=login.credentials,
    )

    client = build_betfair_authenticated_client(login.credentials)
    identity = resolve_betfair_authenticated_account_identity(client)

    bound = bind_betfair_authenticated_jurisdiction(
        login.origin,
        identity,
        client=client,
    )

    assert (
        require_authoritative_betfair_authenticated_jurisdiction(
            bound,
            client=client,
        )
        is bound
    )
    assert bound.jurisdiction is BetfairLoginJurisdiction.GLOBAL_COM
    assert bound.session_context_id == identity.session_context_id
    assert bound.account_identity_id == identity.identity_id
    assert bound.session_origin_id == login.origin.origin_id
    assert bound.execution_authorized is False

    copied = replace(bound)
    assert not is_authoritative_betfair_authenticated_jurisdiction(
        copied,
        client=client,
    )
