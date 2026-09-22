from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from autosport.betfair_account_readonly import (
    BETTING_JSON_RPC_ENDPOINT,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
    UrllibBetfairHttpTransport,
)
from autosport.betfair_supervised_execution import (
    BetfairPlaceOrdersAmbiguous,
    BetfairSupervisedPlaceOrdersClient,
)
from autosport.real_execution_ledger import ExecutionAction


def _serve(handler_type):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_authenticated_transport_never_forwards_secrets_to_redirect_target() -> None:
    sink_hits: list[tuple[str, str | None, str | None]] = []
    source_headers: list[tuple[str | None, str | None]] = []
    redirect_code = {"value": 301}
    sink_url = {"value": ""}

    class SinkHandler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            sink_hits.append(
                (
                    self.command,
                    self.headers.get("X-Application"),
                    self.headers.get("X-Authentication"),
                )
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"unexpected")

        do_GET = _record
        do_POST = _record

        def log_message(self, format, *args) -> None:
            return

    class SourceHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            source_headers.append(
                (
                    self.headers.get("X-Application"),
                    self.headers.get("X-Authentication"),
                )
            )
            code = redirect_code["value"]
            if code == 200:
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(code)
            self.send_header("Location", sink_url["value"])
            self.end_headers()

        def log_message(self, format, *args) -> None:
            return

    sink_server, sink_thread = _serve(SinkHandler)
    source_server, source_thread = _serve(SourceHandler)
    sink_url["value"] = (
        f"http://127.0.0.1:{sink_server.server_address[1]}/credential-sink"
    )
    source_url = f"http://127.0.0.1:{source_server.server_address[1]}/rpc"
    headers = {
        "Content-Type": "application/json",
        "X-Application": "dummy-app-key",
        "X-Authentication": "dummy-session-token",
    }
    transport = UrllibBetfairHttpTransport()

    try:
        for code in (301, 302, 303, 307, 308):
            redirect_code["value"] = code
            with pytest.raises(
                BetfairReadOnlyError,
                match=rf"status {code}",
            ):
                transport.post(
                    source_url,
                    headers=headers,
                    body=b"{}",
                    timeout_seconds=2.0,
                )
            assert sink_hits == []

        redirect_code["value"] = 200
        assert (
            transport.post(
                source_url,
                headers=headers,
                body=b"{}",
                timeout_seconds=2.0,
            )
            == b'{"ok":true}'
        )
        assert sink_hits == []
        assert source_headers
        assert all(
            values == ("dummy-app-key", "dummy-session-token")
            for values in source_headers
        )
    finally:
        source_server.shutdown()
        sink_server.shutdown()
        source_server.server_close()
        sink_server.server_close()
        source_thread.join(timeout=2.0)
        sink_thread.join(timeout=2.0)


class _AllowWriteGate:
    enabled = True

    def require(self, **_kwargs) -> None:
        return None


class _LoopbackBetfairTransport:
    def __init__(self, source_url: str) -> None:
        self.source_url = source_url
        self.inner = UrllibBetfairHttpTransport()

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        assert url == BETTING_JSON_RPC_ENDPOINT
        return self.inner.post(
            self.source_url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )


def test_placeorders_redirect_is_ambiguous_and_never_reaches_location_target() -> None:
    sink_hits: list[tuple[str, str | None, str | None]] = []
    source_headers: list[tuple[str | None, str | None]] = []
    redirect_code = {"value": 301}
    sink_url = {"value": ""}

    class SinkHandler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            sink_hits.append(
                (
                    self.command,
                    self.headers.get("X-Application"),
                    self.headers.get("X-Authentication"),
                )
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"unexpected")

        do_GET = _record
        do_POST = _record

        def log_message(self, format, *args) -> None:
            return

    class SourceHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            source_headers.append(
                (
                    self.headers.get("X-Application"),
                    self.headers.get("X-Authentication"),
                )
            )
            self.send_response(redirect_code["value"])
            self.send_header("Location", sink_url["value"])
            self.end_headers()

        def log_message(self, format, *args) -> None:
            return

    sink_server, sink_thread = _serve(SinkHandler)
    source_server, source_thread = _serve(SourceHandler)
    sink_url["value"] = (
        f"http://127.0.0.1:{sink_server.server_address[1]}/credential-sink"
    )
    source_url = f"http://127.0.0.1:{source_server.server_address[1]}/rpc"

    client = BetfairSupervisedPlaceOrdersClient(
        BetfairSessionCredentials("dummy-app-key", "dummy-session-token"),
        gate=_AllowWriteGate(),
        transport=_LoopbackBetfairTransport(source_url),
        timeout_seconds=2.0,
        clock=lambda: "2026-09-22T10:00:00+00:00",
    )
    action = ExecutionAction(
        action_id="redirect-action",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.23456789",
        selection_id="42",
        side="BACK",
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id="quote-1",
        quote_observed_at="2026-09-22T09:59:59+00:00",
        expires_at="2026-09-22T10:01:00+00:00",
    )

    try:
        for code in (301, 302, 303, 307, 308):
            redirect_code["value"] = code
            with pytest.raises(
                BetfairPlaceOrdersAmbiguous,
                match="authoritative readback required",
            ):
                client.place_action(
                    action,
                    profile=object(),
                    bound=object(),
                    provider_order_ref="a" * 32,
                    execution_workspace=Path("."),
                )
            assert sink_hits == []

        assert source_headers
        assert all(
            values == ("dummy-app-key", "dummy-session-token")
            for values in source_headers
        )
    finally:
        source_server.shutdown()
        sink_server.shutdown()
        source_server.server_close()
        sink_server.server_close()
        source_thread.join(timeout=2.0)
        sink_thread.join(timeout=2.0)
