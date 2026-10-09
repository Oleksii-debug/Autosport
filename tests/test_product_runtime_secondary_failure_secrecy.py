from __future__ import annotations

import unittest
from threading import RLock
from types import SimpleNamespace

from autosport.product_runtime import AutonomousProductRuntime


class ProductRuntimeSecondaryFailureSecrecyTests(unittest.TestCase):
    def test_failed_start_cleanup_note_does_not_expose_secret_message_or_type(self) -> None:
        primary = RuntimeError("primary START failure")
        secret_type = type("secret_access_token_in_exception_class", (RuntimeError,), {})
        secondary = secret_type("Authorization: Bearer private-provider-token")

        AutonomousProductRuntime._note_secondary_failure(
            primary,
            action="collector STOP compensation",
            secondary_error=secondary,
        )

        self.assertEqual(
            primary.__notes__,
            ["collector STOP compensation also failed: RuntimeError"],
        )
        self.assertNotIn("private-provider-token", repr(primary.__notes__))
        self.assertNotIn("secret_access_token", repr(primary.__notes__))

    def test_cleanup_does_not_evaluate_hostile_secondary_exception_str(self) -> None:
        class HostileError(RuntimeError):
            def __str__(self) -> str:
                raise AssertionError("exception string must not be evaluated")

        primary = RuntimeError("primary")
        AutonomousProductRuntime._note_secondary_failure(
            primary, action="session STOP compensation", secondary_error=HostileError()
        )
        self.assertEqual(
            primary.__notes__,
            ["session STOP compensation also failed: RuntimeError"],
        )

    def test_close_preserves_primary_error_and_redacts_lease_failure(self) -> None:
        calls: list[str] = []
        primary = RuntimeError("storage close failed")
        secret_type = type("credential_session_secret", (RuntimeError,), {})
        secondary = secret_type("Cookie: private-session-token")

        def close_storage() -> None:
            calls.append("storage")
            raise primary

        def release_lease() -> None:
            calls.append("release")
            raise secondary

        runtime = object.__new__(AutonomousProductRuntime)
        runtime._operation_fence = RLock()
        runtime.market_store = SimpleNamespace(close=close_storage)
        runtime._runtime_lease = SimpleNamespace(release=release_lease)

        with self.assertRaises(RuntimeError) as raised:
            runtime.close()

        self.assertIs(raised.exception, primary)
        self.assertEqual(calls, ["storage", "release"])
        self.assertTrue(runtime._closed)
        self.assertEqual(
            primary.__notes__,
            [
                "product runtime lease release also failed while closing "
                "market storage: RuntimeError"
            ],
        )
        self.assertNotIn("private-session-token", repr(primary.__notes__))
        self.assertNotIn("credential_session_secret", repr(primary.__notes__))


if __name__ == "__main__":
    unittest.main()
