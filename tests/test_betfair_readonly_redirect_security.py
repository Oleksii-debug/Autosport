from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyError,
    UrllibBetfairHttpTransport,
)


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
