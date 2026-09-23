from datetime import datetime, timezone
from pathlib import Path

import pytest

import autosport.betfair_session_origin as subject
from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.betfair_session_origin import (
    BetfairAuthenticatedJurisdiction,
    BetfairLoginJurisdiction,
    BetfairNonInteractiveLoginSecrets,
    BetfairSessionOrigin,
    BetfairSessionOriginError,
    cert_login_endpoint,
    is_authoritative_betfair_authenticated_jurisdiction,
    is_authoritative_betfair_session_origin,
    login_betfair_noninteractive,
    parse_noninteractive_login_response,
)

UTC = timezone.utc


def test_current_documented_cert_login_endpoints_are_exact():
    assert cert_login_endpoint(BetfairLoginJurisdiction.GLOBAL_COM) == "https://identitysso-cert.betfair.com/api/certlogin"
    assert cert_login_endpoint(BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND) == "https://identitysso-cert.betfair.com.au/api/certlogin"
    assert cert_login_endpoint(BetfairLoginJurisdiction.ITALY) == "https://identitysso-cert.betfair.it/api/certlogin"
    assert cert_login_endpoint(BetfairLoginJurisdiction.SPAIN) == "https://identitysso-cert.betfair.es/api/certlogin"
    assert cert_login_endpoint(BetfairLoginJurisdiction.ROMANIA) == "https://identitysso-cert.betfair.ro/api/certlogin"


def test_denmark_and_sweden_are_not_silently_inferred_from_currency_or_global_endpoint():
    assert {item.value for item in BetfairLoginJurisdiction} == {
        "GLOBAL_COM", "AUSTRALIA_NEW_ZEALAND", "ITALY", "SPAIN", "ROMANIA"
    }


def test_login_secrets_repr_never_discloses_values():
    secrets = BetfairNonInteractiveLoginSecrets(
        application_key="APP-SECRET",
        username="person@example.invalid",
        password="PASSWORD-SECRET",
        certificate_path=Path("/private/client.crt"),
        private_key_path=Path("/private/client.key"),
    )
    rendered = repr(secrets)
    for secret in ("APP-SECRET", "person@example.invalid", "PASSWORD-SECRET", "/private/client.crt", "/private/client.key"):
        assert secret not in rendered
    assert rendered.count("<redacted>") == 5


def test_success_response_is_parsed_without_persisting_raw_payload():
    payload = b'{"sessionToken":"token-value","loginStatus":"SUCCESS"}'
    response = parse_noninteractive_login_response(payload)
    assert response.login_status == "SUCCESS"
    assert response.session_token == "token-value"
    assert len(response.response_sha256) == 64
    assert "token-value" not in repr(response)
    assert "<redacted>" in repr(response)


def test_failed_response_cannot_smuggle_session_token():
    with pytest.raises(BetfairSessionOriginError, match="unexpectedly carried"):
        parse_noninteractive_login_response(
            b'{"loginStatus":"INVALID_USERNAME_OR_PASSWORD","sessionToken":"do-not-trust"}'
        )


def test_duplicate_json_keys_are_rejected():
    with pytest.raises(BetfairSessionOriginError, match="duplicate"):
        parse_noninteractive_login_response(
            b'{"loginStatus":"SUCCESS","loginStatus":"FAIL","sessionToken":"x"}'
        )


def test_caller_constructed_origin_is_never_authoritative():
    origin = BetfairSessionOrigin(
        venue_id="betfair",
        login_method="NON_INTERACTIVE_CERT",
        jurisdiction=BetfairLoginJurisdiction.GLOBAL_COM,
        login_endpoint=cert_login_endpoint(BetfairLoginJurisdiction.GLOBAL_COM),
        issued_at=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
        response_sha256="a" * 64,
    )
    credentials = BetfairSessionCredentials("app", "token")
    assert is_authoritative_betfair_session_origin(origin) is False
    assert is_authoritative_betfair_session_origin(origin, credentials=credentials) is False
    assert origin.execution_authorized is False


def test_caller_constructed_bound_jurisdiction_is_never_authoritative():
    value = BetfairAuthenticatedJurisdiction(
        venue_id="betfair",
        jurisdiction=BetfairLoginJurisdiction.SPAIN,
        session_context_id="betfair-session-context:" + "a" * 64,
        account_identity_id="b" * 64,
        session_origin_id="c" * 64,
    )
    assert is_authoritative_betfair_authenticated_jurisdiction(value) is False
    assert value.execution_authorized is False


def test_origin_endpoint_cannot_be_relabelled_to_another_jurisdiction():
    with pytest.raises(BetfairSessionOriginError, match="does not match jurisdiction"):
        BetfairSessionOrigin(
            venue_id="betfair",
            login_method="NON_INTERACTIVE_CERT",
            jurisdiction=BetfairLoginJurisdiction.SPAIN,
            login_endpoint=cert_login_endpoint(BetfairLoginJurisdiction.GLOBAL_COM),
            issued_at=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
            response_sha256="a" * 64,
        )


def test_module_opener_swap_cannot_mint_login_origin(monkeypatch, tmp_path):
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_text("not a real cert", encoding="utf-8")
    key.write_text("not a real key", encoding="utf-8")
    secrets = BetfairNonInteractiveLoginSecrets("app", "user", "password", cert, key)
    monkeypatch.setattr(subject, "build_opener", lambda *args, **kwargs: None)
    with pytest.raises(BetfairSessionOriginError, match="transport origin"):
        login_betfair_noninteractive(
            secrets,
            jurisdiction=BetfairLoginJurisdiction.GLOBAL_COM,
        )


def test_ssl_context_factory_swap_cannot_mint_login_origin(monkeypatch, tmp_path):
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_text("not a real cert", encoding="utf-8")
    key.write_text("not a real key", encoding="utf-8")
    secrets = BetfairNonInteractiveLoginSecrets("app", "user", "password", cert, key)
    monkeypatch.setattr(subject.ssl, "create_default_context", lambda: object())
    with pytest.raises(BetfairSessionOriginError, match="transport origin"):
        login_betfair_noninteractive(
            secrets,
            jurisdiction=BetfairLoginJurisdiction.GLOBAL_COM,
        )


def test_endpoint_registry_rebind_cannot_relabel_login_origin(
    monkeypatch, tmp_path
):
    forged = dict(subject._CERT_LOGIN_ENDPOINTS)
    forged[BetfairLoginJurisdiction.SPAIN] = forged[
        BetfairLoginJurisdiction.GLOBAL_COM
    ]
    monkeypatch.setattr(subject, "_CERT_LOGIN_ENDPOINTS", forged)
    secrets = BetfairNonInteractiveLoginSecrets(
        "app",
        "user",
        "password",
        tmp_path / "client.crt",
        tmp_path / "client.key",
    )

    with pytest.raises(
        BetfairSessionOriginError,
        match="canonical Betfair login authority binding changed",
    ):
        login_betfair_noninteractive(
            secrets,
            jurisdiction=BetfairLoginJurisdiction.SPAIN,
        )


def test_endpoint_registry_contents_are_immutable():
    with pytest.raises(TypeError):
        subject._CERT_LOGIN_ENDPOINTS[  # type: ignore[index]
            BetfairLoginJurisdiction.SPAIN
        ] = subject._CERT_LOGIN_ENDPOINTS[
            BetfairLoginJurisdiction.GLOBAL_COM
        ]


def test_paired_transport_and_public_canonical_reference_rebind_cannot_mint_origin(
    monkeypatch, tmp_path
):
    def forged_post(
        self,
        endpoint,
        *,
        application_key,
        username,
        password,
        certificate_path,
        private_key_path,
        timeout_seconds,
    ):
        return b'{"sessionToken":"forged-token","loginStatus":"SUCCESS"}'

    monkeypatch.setattr(
        subject.UrllibBetfairCertLoginTransport,
        "post_cert_login",
        forged_post,
    )
    monkeypatch.setattr(subject, "_CANONICAL_TRANSPORT_POST", forged_post)
    secrets = BetfairNonInteractiveLoginSecrets(
        "app",
        "user",
        "password",
        tmp_path / "client.crt",
        tmp_path / "client.key",
    )

    with pytest.raises(
        BetfairSessionOriginError,
        match="canonical Betfair login authority binding changed",
    ):
        login_betfair_noninteractive(
            secrets,
            jurisdiction=BetfairLoginJurisdiction.GLOBAL_COM,
        )


def test_json_decoder_rebind_cannot_fabricate_successful_login(
    monkeypatch, tmp_path
):
    class ForgedDecoder:
        def __init__(self, *args, **kwargs):
            pass

        def decode(self, payload):
            return {
                "sessionToken": "forged-token",
                "loginStatus": "SUCCESS",
            }

    monkeypatch.setattr(subject.json, "JSONDecoder", ForgedDecoder)
    secrets = BetfairNonInteractiveLoginSecrets(
        "app",
        "user",
        "password",
        tmp_path / "client.crt",
        tmp_path / "client.key",
    )

    with pytest.raises(
        BetfairSessionOriginError,
        match="canonical Betfair login authority binding changed",
    ):
        login_betfair_noninteractive(
            secrets,
            jurisdiction=BetfairLoginJurisdiction.GLOBAL_COM,
        )


def test_bound_issuer_has_no_writable_canonical_reference_dependency():
    assert "_CANONICAL_ACCOUNT_IDENTITY_REQUIRE" not in (
        bind_betfair_authenticated_jurisdiction.__code__.co_names
    )
    assert "require_authoritative_betfair_account_identity" not in (
        bind_betfair_authenticated_jurisdiction.__code__.co_names
    )


def test_missing_certificate_fails_without_exposing_secrets(tmp_path):
    secrets = BetfairNonInteractiveLoginSecrets(
        "app-secret",
        "user-secret",
        "password-secret",
        tmp_path / "missing.crt",
        tmp_path / "missing.key",
    )
    with pytest.raises(BetfairSessionOriginError) as caught:
        login_betfair_noninteractive(
            secrets,
            jurisdiction=BetfairLoginJurisdiction.GLOBAL_COM,
        )
    message = str(caught.value)
    assert "app-secret" not in message
    assert "user-secret" not in message
    assert "password-secret" not in message


def test_login_response_rejects_nonstandard_constants_and_non_object():
    with pytest.raises(BetfairSessionOriginError):
        parse_noninteractive_login_response(b'{"loginStatus":NaN}')
    with pytest.raises(BetfairSessionOriginError, match="JSON object"):
        parse_noninteractive_login_response(b'[]')


def test_failed_status_is_bounded_safe_provider_code():
    with pytest.raises(BetfairSessionOriginError, match="bounded uppercase"):
        parse_noninteractive_login_response(b'{"loginStatus":"bad status: token-secret"}')
    with pytest.raises(BetfairSessionOriginError, match="bounded uppercase"):
        parse_noninteractive_login_response(
            (b'{"loginStatus":"' + b'A' * 129 + b'"}')
        )


def test_success_requires_nonempty_session_token():
    with pytest.raises(BetfairSessionOriginError, match="sessionToken"):
        parse_noninteractive_login_response(
            b'{"loginStatus":"SUCCESS","sessionToken":""}'
        )


def test_response_size_is_bounded_before_json_decode():
    payload = b'{' + b' ' * (subject._MAX_RESPONSE_BYTES + 1) + b'}'
    with pytest.raises(BetfairSessionOriginError, match="safe limit"):
        parse_noninteractive_login_response(payload)


def test_non_enum_jurisdiction_cannot_select_login_endpoint():
    with pytest.raises(BetfairSessionOriginError, match="exact BetfairLoginJurisdiction"):
        cert_login_endpoint("SPAIN")  # type: ignore[arg-type]


def test_login_session_repr_redacts_credentials_and_token():
    origin = BetfairSessionOrigin(
        venue_id="betfair",
        login_method="NON_INTERACTIVE_CERT",
        jurisdiction=BetfairLoginJurisdiction.GLOBAL_COM,
        login_endpoint=cert_login_endpoint(BetfairLoginJurisdiction.GLOBAL_COM),
        issued_at=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
        response_sha256="d" * 64,
    )
    session = subject.BetfairLoginSession(
        credentials=BetfairSessionCredentials("app-secret", "session-token-secret"),
        origin=origin,
    )
    rendered = repr(session)
    assert "app-secret" not in rendered
    assert "session-token-secret" not in rendered
    assert "<redacted>" in rendered


def test_login_secrets_accept_concrete_path_subclasses_from_path_factory(tmp_path):
    value = BetfairNonInteractiveLoginSecrets(
        "app", "user", "password", tmp_path / "client.crt", tmp_path / "client.key"
    )
    assert value.certificate_path == tmp_path / "client.crt"


def test_redirect_handler_never_follows_authentication_redirects():
    handler = subject._NoRedirectHandler()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.invalid/") is None


def test_module_exposes_no_registration_or_registry_mint_hook():
    for name in (
        "_remember_origin",
        "_remember_bound",
        "_ISSUED_ORIGINS",
        "_ISSUED_BOUND",
        "_PROCESS_HMAC_KEY",
        "_build_origin_authority_runtime",
        "_build_bound_authority_runtime",
    ):
        assert not hasattr(subject, name), name
