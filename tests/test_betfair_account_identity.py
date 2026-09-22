from __future__ import annotations

import copy
from dataclasses import replace
import json
import os
import pickle
from pathlib import Path
import subprocess
import sys
import urllib.request as _urllib_request
from concurrent.futures import ThreadPoolExecutor

import pytest

from autosport import betfair_account_identity as _identity
from autosport import betfair_account_readonly as _readonly
from autosport.betfair_account_identity import (
    IDENTITY_SCOPE,
    BetfairAccountIdentityError,
    BetfairAccountIdentityMode,
    BetfairAuthenticatedAccountIdentity,
    build_betfair_authenticated_client,
    is_authoritative_betfair_account_identity,
    require_authoritative_betfair_account_identity,
    resolve_betfair_authenticated_account_identity,
)
from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
    UrllibBetfairHttpTransport,
)


def _details_result(*, currency_code: str = "EUR") -> dict[str, object]:
    return {
        "currencyCode": currency_code,
        "localeCode": "en",
        "region": "GBR",
        "timezone": "Europe/London",
    }


def _install_details_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, object] | None = None,
    error_message: str | None = None,
) -> None:
    response_result = result or _details_result()

    class Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit: int) -> bytes:
            assert limit >= len(self._payload)
            return self._payload

    def fake_open(request, timeout: float):
        assert request.full_url == ACCOUNT_JSON_RPC_ENDPOINT
        assert timeout > 0
        assert request.data is not None
        decoded_request = json.loads(request.data.decode("utf-8"))
        assert decoded_request["method"] == "AccountAPING/v1.0/getAccountDetails"
        assert decoded_request["params"] == {}
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["x-application"]
        assert headers["x-authentication"]
        if error_message is not None:
            payload = {
                "jsonrpc": "2.0",
                "id": decoded_request["id"],
                "error": {"code": -32099, "message": error_message},
            }
        else:
            payload = {
                "jsonrpc": "2.0",
                "id": decoded_request["id"],
                "result": response_result,
            }
        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return Response(raw)

    class Opener:
        def open(self, request, data=None, timeout: float = 0):
            assert data is None
            return fake_open(request, timeout)

    # Preserve the exact autosport.betfair_account_readonly.urlopen function that
    # K07 treats as part of the canonical provider-origin dependency. Replace only
    # stdlib's process opener below that function for deterministic unit I/O.
    monkeypatch.setattr(_urllib_request, "_opener", Opener())


def _client(
    *,
    application_key: str = "app-key-a",
    session_token: str = "session-token-a",
    account_label: str = "caller-label-a",
) -> BetfairReadOnlyClient:
    return build_betfair_authenticated_client(
        BetfairSessionCredentials(application_key, session_token),
        account_label=account_label,
    )


def test_k07_import_order_does_not_patch_client_constructor() -> None:
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env = os.environ.copy()
    env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
    code = """
from autosport.betfair_account_readonly import BetfairReadOnlyClient, BetfairSessionCredentials
original_init = BetfairReadOnlyClient.__init__
client = BetfairReadOnlyClient(BetfairSessionCredentials("app", "token"))
from autosport import betfair_account_identity as identity
assert BetfairReadOnlyClient.__init__ is original_init
try:
    identity.resolve_betfair_authenticated_account_identity(client)
except identity.BetfairAccountIdentityError as exc:
    assert "product-owned" in str(exc)
else:
    raise AssertionError("direct pre-import client unexpectedly gained K07 authority")
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_direct_default_client_never_gains_k07_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-a", "session-a"),
        venue_id="betfair",
        account_id="caller-label",
    )

    with pytest.raises(BetfairAccountIdentityError, match="product-owned"):
        resolve_betfair_authenticated_account_identity(client)


def test_distinct_authenticated_contexts_do_not_alias_identical_account_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    first = _client(application_key="app-a", session_token="session-a")
    second = _client(application_key="app-b", session_token="session-b")

    left = resolve_betfair_authenticated_account_identity(first)
    right = resolve_betfair_authenticated_account_identity(second)

    # Both clients make request id=1 and receive byte-identical provider payloads.
    assert left.account_details_sha256 == right.account_details_sha256
    assert left.session_context_id != right.session_context_id
    assert left.identity_id != right.identity_id
    assert is_authoritative_betfair_account_identity(left, client=first)
    assert is_authoritative_betfair_account_identity(right, client=second)


def test_same_exact_client_reuses_session_context_but_not_response_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    client = _client()

    first = resolve_betfair_authenticated_account_identity(client)
    second = resolve_betfair_authenticated_account_identity(client)

    assert first.session_context_id == second.session_context_id
    # JSON-RPC request ids differ, so response evidence identity differs.
    assert first.account_details_sha256 != second.account_details_sha256
    assert first.identity_id != second.identity_id
    assert is_authoritative_betfair_account_identity(first, client=client)
    assert is_authoritative_betfair_account_identity(second, client=client)


def test_configured_account_label_cannot_mint_or_alias_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    left = _client(account_label="same-caller-label")
    right = _client(
        application_key="app-b",
        session_token="session-b",
        account_label="same-caller-label",
    )
    a = resolve_betfair_authenticated_account_identity(left)
    b = resolve_betfair_authenticated_account_identity(right)

    assert a.session_context_id != b.session_context_id
    assert "same-caller-label" not in repr(a)
    assert "same-caller-label" not in a.identity_id


def test_module_helper_rebinding_cannot_weaken_k07_identity_integrity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(
        monkeypatch,
        result=_details_result(currency_code="EUR"),
    )
    client = _client()

    # These names remain public implementation helpers for the DTO, but K07's
    # authority closure must have pinned its own validation primitives already.
    monkeypatch.setattr(_identity, "_currency_code", lambda _value: "GBP")
    monkeypatch.setattr(
        _identity,
        "_sha256_hex",
        lambda _value, _field: "0" * 64,
    )
    monkeypatch.setattr(
        _identity,
        "_canonical_timestamp",
        lambda _value: "1900-01-01T00:00:00+00:00",
    )

    value = resolve_betfair_authenticated_account_identity(client)

    assert value.currency_code == "EUR"
    assert value.account_details_sha256 != "0" * 64
    assert value.observed_at != "1900-01-01T00:00:00+00:00"
    assert is_authoritative_betfair_account_identity(value, client=client)

    # Authority integrity must not depend on the public identity_id property's
    # module-global canonical-json helper after issuance.
    monkeypatch.setattr(_identity, "_canonical_json", lambda _value: b"forged")
    object.__setattr__(value, "currency_code", "GBP")

    assert not is_authoritative_betfair_account_identity(value, client=client)


def test_identity_class_post_init_rebinding_cannot_mint_altered_k07_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(
        monkeypatch,
        result=_details_result(currency_code="EUR"),
    )
    client = _client()

    def forged_post_init(value) -> None:
        object.__setattr__(value, "currency_code", "GBP")

    monkeypatch.setattr(
        _identity.BetfairAuthenticatedAccountIdentity,
        "__post_init__",
        forged_post_init,
    )

    with pytest.raises(
        BetfairAccountIdentityError,
        match="identity implementation changed|identity construction was altered",
    ):
        resolve_betfair_authenticated_account_identity(client)


def test_personal_developer_identity_never_claims_cross_session_stability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    value = resolve_betfair_authenticated_account_identity(_client())

    assert value.mode is BetfairAccountIdentityMode.PERSONAL_DEVELOPER
    assert value.identity_scope == IDENTITY_SCOPE
    assert value.stable_account_identity_proven is False
    assert value.stable_account_id is None
    assert value.cross_session_equivalence_proven is False


@pytest.mark.parametrize("copy_kind", ["copy", "replace", "pickle"])
def test_copy_reconstruction_or_pickle_does_not_retain_source_authority(
    monkeypatch: pytest.MonkeyPatch,
    copy_kind: str,
) -> None:
    _install_details_transport(monkeypatch)
    client = _client()
    value = resolve_betfair_authenticated_account_identity(client)

    if copy_kind == "copy":
        candidate = copy.copy(value)
    elif copy_kind == "replace":
        candidate = replace(value)
    else:
        candidate = pickle.loads(pickle.dumps(value))

    assert candidate == value
    assert candidate is not value
    assert not is_authoritative_betfair_account_identity(candidate, client=client)
    with pytest.raises(BetfairAccountIdentityError):
        require_authoritative_betfair_account_identity(candidate, client=client)


def test_caller_constructed_matching_dto_cannot_mint_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    client = _client()
    issued = resolve_betfair_authenticated_account_identity(client)
    forged = BetfairAuthenticatedAccountIdentity(
        venue_id=issued.venue_id,
        mode=issued.mode,
        identity_scope=issued.identity_scope,
        session_context_id=issued.session_context_id,
        currency_code=issued.currency_code,
        account_details_sha256=issued.account_details_sha256,
        observed_at=issued.observed_at,
    )

    assert forged.identity_id == issued.identity_id
    assert not is_authoritative_betfair_account_identity(forged, client=client)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("currency_code", "GBP"),
        ("account_details_sha256", "0" * 64),
        ("observed_at", "2026-09-21T00:00:00+00:00"),
        ("session_context_id", "betfair-session-context:" + "1" * 64),
    ],
)
def test_same_object_authority_bearing_mutation_revokes_identity(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    _install_details_transport(monkeypatch)
    client = _client()
    value = resolve_betfair_authenticated_account_identity(client)
    assert is_authoritative_betfair_account_identity(value, client=client)

    object.__setattr__(value, field, replacement)

    assert not is_authoritative_betfair_account_identity(value, client=client)


def test_credential_object_replacement_revokes_context_and_future_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    client = _client()
    value = resolve_betfair_authenticated_account_identity(client)

    client._credentials = BetfairSessionCredentials("app-new", "session-new")

    assert not is_authoritative_betfair_account_identity(value, client=client)
    with pytest.raises(BetfairAccountIdentityError, match="rotated|mutated"):
        resolve_betfair_authenticated_account_identity(client)


def test_in_place_frozen_credential_mutation_revokes_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    client = _client()
    value = resolve_betfair_authenticated_account_identity(client)

    object.__setattr__(client._credentials, "session_token", "session-rotated")

    assert not is_authoritative_betfair_account_identity(value, client=client)


def test_transport_replacement_revokes_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    client = _client()
    value = resolve_betfair_authenticated_account_identity(client)

    client._transport = UrllibBetfairHttpTransport()

    assert not is_authoritative_betfair_account_identity(value, client=client)


def test_explicitly_injected_transport_is_not_product_owned_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-a", "session-a"),
        transport=UrllibBetfairHttpTransport(),
        venue_id="betfair",
        account_id="ignored-label",
    )

    with pytest.raises(BetfairAccountIdentityError, match="product-owned"):
        resolve_betfair_authenticated_account_identity(client)


def test_explicitly_injected_clock_is_not_product_owned_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    from datetime import datetime, timezone

    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-a", "session-a"),
        clock=lambda: datetime.now(timezone.utc),
        venue_id="betfair",
        account_id="ignored-label",
    )

    with pytest.raises(BetfairAccountIdentityError, match="product-owned"):
        resolve_betfair_authenticated_account_identity(client)


def test_subclass_client_cannot_mint_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_details_transport(monkeypatch)

    class SubClient(BetfairReadOnlyClient):
        pass

    client = SubClient(BetfairSessionCredentials("app-a", "session-a"))

    with pytest.raises(BetfairAccountIdentityError, match="exact canonical"):
        resolve_betfair_authenticated_account_identity(client)


def test_licensed_vendor_mode_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    client = _client()

    with pytest.raises(BetfairAccountIdentityError, match="LICENSED_VENDOR"):
        resolve_betfair_authenticated_account_identity(
            client, mode=BetfairAccountIdentityMode.LICENSED_VENDOR
        )


def test_provider_error_is_generic_and_does_not_echo_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application_key = "do-not-leak-application-key"
    session_token = "do-not-leak-session-token"
    _install_details_transport(
        monkeypatch,
        error_message=f"provider leaked {application_key} {session_token}",
    )
    client = _client(
        application_key=application_key,
        session_token=session_token,
    )

    with pytest.raises(BetfairAccountIdentityError) as caught:
        resolve_betfair_authenticated_account_identity(client)

    message = str(caught.value)
    assert application_key not in message
    assert session_token not in message


def test_identity_repr_and_digest_do_not_contain_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application_key = "sensitive-application-key"
    session_token = "sensitive-session-token"
    _install_details_transport(monkeypatch)
    client = _client(
        application_key=application_key,
        session_token=session_token,
    )
    value = resolve_betfair_authenticated_account_identity(client)

    rendered = repr(value)
    assert application_key not in rendered
    assert session_token not in rendered
    assert application_key not in value.identity_id
    assert session_token not in value.identity_id


def test_identity_is_bound_to_exact_issuing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    first = _client()
    second = _client(application_key="app-b", session_token="session-b")
    value = resolve_betfair_authenticated_account_identity(first)

    assert is_authoritative_betfair_account_identity(value, client=first)
    assert not is_authoritative_betfair_account_identity(value, client=second)
    with pytest.raises(BetfairAccountIdentityError):
        require_authoritative_betfair_account_identity(value, client=second)


def test_concurrent_resolution_reuses_one_exact_context_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    client = _client()

    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(
            pool.map(
                lambda _: resolve_betfair_authenticated_account_identity(client),
                range(32),
            )
        )

    assert len({value.session_context_id for value in values}) == 1
    assert all(
        is_authoritative_betfair_account_identity(value, client=client)
        for value in values
    )


def test_factory_rejects_class_level_transport_method_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_post = UrllibBetfairHttpTransport.post

    def replacement(self, url, *, headers, body, timeout_seconds):
        return original_post(
            self,
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(UrllibBetfairHttpTransport, "post", replacement)

    with pytest.raises(BetfairAccountIdentityError, match="invalid origin"):
        _client()


def test_class_level_transport_method_replacement_revokes_issued_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    client = _client()
    value = resolve_betfair_authenticated_account_identity(client)
    assert is_authoritative_betfair_account_identity(value, client=client)

    original_post = UrllibBetfairHttpTransport.post

    def replacement(self, url, *, headers, body, timeout_seconds):
        return original_post(
            self,
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(UrllibBetfairHttpTransport, "post", replacement)

    assert not is_authoritative_betfair_account_identity(value, client=client)


def test_instance_level_transport_method_replacement_revokes_issued_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_details_transport(monkeypatch)
    client = _client()
    value = resolve_betfair_authenticated_account_identity(client)
    assert is_authoritative_betfair_account_identity(value, client=client)

    client._transport.post = lambda *args, **kwargs: b"{}"

    assert not is_authoritative_betfair_account_identity(value, client=client)
