from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from autosport.parlayapi_provider import ProviderTransportError, _default_transport


_DUMMY_API_KEY = "dummy-parlay-key-for-redirect-test"


def _start_server(handler_class: type[BaseHTTPRequestHandler]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01),
        daemon=True,
    )
    thread.start()
    return server, thread


def _stop_server(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2.0)


def _json_handler(seen: list[dict[str, str]]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append({key: value for key, value in self.headers.items()})
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return Handler


def _redirect_handler(
    status_code: int,
    location: str,
    seen: list[dict[str, str]],
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append({key: value for key, value in self.headers.items()})
            self.send_response(status_code)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return Handler


class ParlayApiRedirectSecurityTests(unittest.TestCase):
    def test_authenticated_transport_rejects_every_standard_redirect_before_follow_up(self) -> None:
        target_seen: list[dict[str, str]] = []
        target, target_thread = _start_server(_json_handler(target_seen))
        target_url = f"http://127.0.0.1:{target.server_port}/capture"
        try:
            for status_code in (301, 302, 303, 307, 308):
                with self.subTest(status_code=status_code):
                    origin_seen: list[dict[str, str]] = []
                    origin, origin_thread = _start_server(
                        _redirect_handler(status_code, target_url, origin_seen)
                    )
                    try:
                        with self.assertRaises(ProviderTransportError) as captured:
                            _default_transport(
                                f"http://127.0.0.1:{origin.server_port}/provider",
                                {"X-API-Key": _DUMMY_API_KEY, "Accept": "application/json"},
                                2.0,
                            )
                    finally:
                        _stop_server(origin, origin_thread)

                    self.assertEqual(captured.exception.status_code, status_code)
                    self.assertEqual(len(origin_seen), 1)
                    self.assertEqual(origin_seen[0].get("X-API-Key"), _DUMMY_API_KEY)
                    self.assertEqual(target_seen, [])
                    self.assertNotIn(_DUMMY_API_KEY, str(captured.exception))
        finally:
            _stop_server(target, target_thread)

    def test_direct_success_preserves_existing_json_transport_behavior(self) -> None:
        seen: list[dict[str, str]] = []
        server, thread = _start_server(_json_handler(seen))
        try:
            response = _default_transport(
                f"http://127.0.0.1:{server.server_port}/provider",
                {"X-API-Key": _DUMMY_API_KEY, "Accept": "application/json"},
                2.0,
            )
        finally:
            _stop_server(server, thread)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.payload, {"ok": True})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].get("X-API-Key"), _DUMMY_API_KEY)


if __name__ == "__main__":
    unittest.main()
