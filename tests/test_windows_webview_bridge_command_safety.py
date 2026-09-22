from __future__ import annotations

import threading
import unittest

from autosport.windows_webview_shell import AutosportWebController


class WindowsWebViewBridgeCommandSafetyTests(unittest.TestCase):
    @staticmethod
    def _bare_controller() -> AutosportWebController:
        controller = AutosportWebController.__new__(AutosportWebController)
        controller._lock = threading.RLock()
        controller._closing = False
        controller.log = []
        controller.last_error = ""
        controller.status = "ready"
        return controller

    def test_duplicate_request_id_cannot_repeat_backend_side_effect(self) -> None:
        controller = self._bare_controller()
        calls: list[dict[str, object]] = []

        def live_refresh(payload):
            calls.append(dict(payload))
            return {"status": "completed", "message": "ok"}

        controller._action_live_refresh = live_refresh
        command = {
            "request_id": "req-duplicate-001",
            "action_id": "live.refresh",
            "payload": {"mode": "public_preview"},
        }

        first = controller.dispatch(command)
        second = controller.dispatch(command)

        self.assertEqual(first["request_id"], command["request_id"])
        self.assertEqual(second["request_id"], command["request_id"])
        self.assertEqual(
            len(calls),
            1,
            "re-delivery of one product request_id must not execute the backend action twice",
        )

    def test_backend_exception_secret_does_not_cross_bridge_response_or_state(self) -> None:
        controller = self._bare_controller()
        secret = "WEBVIEW-BRIDGE-SECRET-SENTINEL-47f2"

        def live_refresh(_payload):
            raise RuntimeError(
                f"provider transport failed Authorization=Bearer {secret}"
            )

        controller._action_live_refresh = live_refresh
        response = controller.dispatch(
            {
                "request_id": "req-secret-001",
                "action_id": "live.refresh",
                "payload": {},
            }
        )

        self.assertEqual(response["status"], "rejected")
        published = "\n".join(
            [
                str(response.get("message", "")),
                controller.last_error,
                controller.status,
                *controller.log,
            ]
        )
        self.assertNotIn(
            secret,
            published,
            "exception-controlled credential text must be redacted before WebView/ARIA publication",
        )


if __name__ == "__main__":
    unittest.main()
