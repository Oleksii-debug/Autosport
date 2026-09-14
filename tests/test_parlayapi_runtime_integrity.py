import math
import unittest

from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderTransportError,
    _parse_retry_after,
)


class ParlayApiRuntimeIntegrityTests(unittest.TestCase):
    def test_constructor_rejects_nonfinite_or_ambiguous_runtime_limits(self):
        invalid_timeouts = (True, False, 0, -1, float("nan"), float("inf"), float("-inf"), "1")
        for value in invalid_timeouts:
            with self.subTest(field="timeout_seconds", value=value):
                with self.assertRaises(ValueError):
                    ParlayApiTableTennisProvider(public_preview=True, timeout_seconds=value)  # type: ignore[arg-type]

        invalid_attempts = (True, False, 0, -1, 6, 1.5, "2")
        for value in invalid_attempts:
            with self.subTest(field="max_attempts", value=value):
                with self.assertRaises(ValueError):
                    ParlayApiTableTennisProvider(public_preview=True, max_attempts=value)  # type: ignore[arg-type]

        invalid_backoffs = (True, False, -1, float("nan"), float("inf"), float("-inf"), "1")
        for value in invalid_backoffs:
            with self.subTest(field="max_backoff_seconds", value=value):
                with self.assertRaises(ValueError):
                    ParlayApiTableTennisProvider(public_preview=True, max_backoff_seconds=value)  # type: ignore[arg-type]

        provider = ParlayApiTableTennisProvider(
            public_preview=True,
            timeout_seconds=3,
            max_attempts=2,
            max_backoff_seconds=0,
        )
        self.assertEqual(provider.timeout_seconds, 3.0)
        self.assertEqual(provider.max_attempts, 2)
        self.assertEqual(provider.max_backoff_seconds, 0.0)

    def test_read_batch_rejects_invalid_limit_before_clock_or_network(self):
        clock_calls = 0
        transport_calls = 0

        def clock() -> str:
            nonlocal clock_calls
            clock_calls += 1
            return "2026-09-13T20:00:00+00:00"

        def transport(url, headers, timeout):
            nonlocal transport_calls
            transport_calls += 1
            return HttpJsonResponse([], 200, {})

        provider = ParlayApiTableTennisProvider(
            public_preview=True,
            clock=clock,
            transport=transport,
        )
        for value in (True, False, 0, -1, 1.5, "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    provider.read_batch(value)  # type: ignore[arg-type]

        self.assertEqual(clock_calls, 0)
        self.assertEqual(transport_calls, 0)

        batch = provider.read_batch(1)
        self.assertEqual(batch.quotes, ())
        self.assertEqual(clock_calls, 1)
        self.assertEqual(transport_calls, 1)

    def test_retry_delay_falls_back_when_transport_supplies_nonfinite_retry_after(self):
        for retry_after in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(retry_after=retry_after):
                calls = 0
                sleeps: list[float] = []

                def transport(url, headers, timeout):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise ProviderTransportError("retry", status_code=429, retry_after=retry_after)
                    return HttpJsonResponse([], 200, {})

                provider = ParlayApiTableTennisProvider(
                    public_preview=True,
                    max_attempts=2,
                    max_backoff_seconds=2,
                    transport=transport,
                    sleeper=sleeps.append,
                    clock=lambda: "2026-09-13T20:00:00+00:00",
                )
                result = provider.read_batch(1)
                self.assertEqual(result.quotes, ())
                self.assertEqual(calls, 2)
                self.assertEqual(sleeps, [0.25])
                self.assertTrue(math.isfinite(sleeps[0]))

    def test_retry_after_parser_rejects_nonfinite_values(self):
        for value in ("NaN", "nan", "Inf", "inf", "-Infinity"):
            with self.subTest(value=value):
                self.assertIsNone(_parse_retry_after(value))
        self.assertEqual(_parse_retry_after("-2"), 0.0)
        self.assertEqual(_parse_retry_after("1.5"), 1.5)
        self.assertIsNone(_parse_retry_after("not-a-number"))


if __name__ == "__main__":
    unittest.main()
