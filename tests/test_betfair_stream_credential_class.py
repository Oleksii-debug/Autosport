from __future__ import annotations

from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport import betfair_stream_transport as stream


class _Secrets:
    def __init__(self, lease: stream.BetfairStreamCredentialLease) -> None:
        self.lease = lease

    def get_session_lease(self) -> stream.BetfairStreamCredentialLease:
        return self.lease


def _identity() -> stream.BetfairStreamSessionIdentity:
    return stream.BetfairStreamSessionIdentity(
        account_id="acct-1",
        app_identity_id="betfair-app-production-1",
        app_key_class="LIVE",
        session_epoch=1,
    )


def test_vendor_web_bearer_shape_cannot_reach_exchange_stream_network_open(monkeypatch):
    """Vendor-Web bearer material is not an Exchange Stream session authority.

    A safe repair may make this lease structurally unconstructible, or it may
    reject the credential class in connect().  Either is acceptable.  What is
    forbidden is dialing the Stream endpoint with a caller-supplied Vendor-Web
    bearer shape as though it were an API-login session token.
    """

    try:
        lease = stream.BetfairStreamCredentialLease(
            account_id="acct-1",
            app_identity_id="betfair-app-production-1",
            app_key_class="LIVE",
            session_epoch=1,
            credentials=BetfairSessionCredentials(
                "TEST_APP_KEY_NOT_A_REAL_CREDENTIAL",
                "Bearer TEST_VENDOR_WEB_ACCESS_TOKEN_NOT_A_REAL_CREDENTIAL",
            ),
        )
    except (TypeError, ValueError):
        return

    network_open_calls = 0

    def forbidden_network_open(timeout_seconds: float):
        nonlocal network_open_calls
        network_open_calls += 1
        raise OSError("network open reached by unsupported credential class")

    monkeypatch.setattr(
        stream,
        "_open_verified_tls_socket",
        forbidden_network_open,
    )

    transport = stream.BetfairStreamTlsTransport(
        identity=_identity(),
        secret_provider=_Secrets(lease),
        timeout_seconds=0.1,
    )

    try:
        transport.connect()
    except Exception:
        pass

    assert network_open_calls == 0, (
        "Vendor-Web bearer-shaped credentials reached TCP/TLS open; Exchange "
        "Stream requires an API-login session authority, not an unclassified "
        "opaque string"
    )
