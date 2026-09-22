from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyError,
    UrllibBetfairHttpTransport,
)


_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "X-Application": "app-secret",
    "X-Authentication": "session-secret",
}
_BODY = b'{"jsonrpc":"2.0","id":1}'


def _start_server(handler_type: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _stop_server(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def test_betfair_transport_direct_post_keeps_credentials_and_body() -> None:
    observed: list[tuple[dict[str, str], bytes]] = []

    class DirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            observed.append((dict(self.headers.items()), self.rfile.read(size)))
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = _start_server(DirectHandler)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/json-rpc"
        payload = UrllibBetfairHttpTransport().post(
            url,
            headers=_HEADERS,
            body=_BODY,
            timeout_seconds=2.0,
        )
    finally:
        _stop_server(server)

    assert payload == b'{"ok":true}'
    assert len(observed) == 1
    headers, body = observed[0]
    assert headers["X-Application"] == "app-secret"
    assert headers["X-Authentication"] == "session-secret"
    assert headers["Content-Type"] == "application/json"
    assert body == _BODY


@pytest.mark.parametrize("status_code", (301, 302, 303, 307, 308))
def test_betfair_transport_blocks_redirect_before_secret_followup(
    status_code: int,
) -> None:
    sink_requests: list[tuple[str, dict[str, str]]] = []

    class SinkHandler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            if size:
                self.rfile.read(size)
            sink_requests.append((self.command, dict(self.headers.items())))
            self.send_response(200)
            self.end_headers()

        do_GET = _record
        do_POST = _record

        def log_message(self, format: str, *args: object) -> None:
            return

    sink = _start_server(SinkHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            if size:
                self.rfile.read(size)
            self.send_response(status_code)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{sink.server_address[1]}/credential-sink",
            )
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    redirect = _start_server(RedirectHandler)
    try:
        url = f"http://127.0.0.1:{redirect.server_address[1]}/json-rpc"
        with pytest.raises(
            BetfairReadOnlyError,
            match=rf"Betfair HTTP request failed with status {status_code}",
        ) as exc_info:
            UrllibBetfairHttpTransport().post(
                url,
                headers=_HEADERS,
                body=_BODY,
                timeout_seconds=2.0,
            )
    finally:
        _stop_server(redirect)
        _stop_server(sink)

    assert sink_requests == []
    error_text = str(exc_info.value)
    assert "app-secret" not in error_text
    assert "session-secret" not in error_text
    assert "credential-sink" not in error_text
