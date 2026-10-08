from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from email.utils import format_datetime
from threading import Barrier

from autosport.provider_request_admission import (
    Decision,
    GatePolicy,
    HardBlockReason,
    ProviderAdmissionGate,
)


class ProviderAdmissionTests(unittest.TestCase):
    def gate(self, **policy_kwargs) -> ProviderAdmissionGate:
        return ProviderAdmissionGate.fresh(
            "betfair",
            "node-A",
            GatePolicy(jitter_fraction=0.0, **policy_kwargs),
        )

    def allow(self, g: ProviderAdmissionGate, now: float = 100):
        a = g.admit(now)
        self.assertIn(a.decision, {Decision.ALLOW, Decision.ALLOW_PROBE})
        self.assertIsNotNone(a.request_token)
        return a

    def result(self, g, admission, now, status, headers=None):
        g.record_http_result(
            now_epoch=now,
            status_code=status,
            headers=headers,
            request_token=admission.request_token,
        )

    def test_fresh_gate_allows_with_generation_token(self):
        g = self.gate()
        a = self.allow(g)
        self.assertTrue(a.request_token.startswith("1:"))

    def test_429_delta_seconds_sets_server_cooldown(self):
        g = self.gate()
        a = self.allow(g)
        self.result(g, a, 100, 429, {"Retry-After": "30"})
        b = g.admit(110)
        self.assertEqual(b.decision, Decision.BLOCK_COOLDOWN)
        self.assertEqual(b.retry_at_epoch, 130)

    def test_429_malformed_retry_after_falls_back_to_backoff(self):
        g = self.gate(base_backoff_seconds=2)
        a = self.allow(g)
        self.result(g, a, 100, 429, {"Retry-After": "nonsense"})
        self.assertEqual(g.state.blocked_until_epoch, 102)

    def test_retry_after_http_date(self):
        g = self.gate()
        a = self.allow(g)
        dt = datetime.fromtimestamp(145, tz=timezone.utc)
        self.result(
            g,
            a,
            100,
            503,
            {"Retry-After": format_datetime(dt, usegmt=True)},
        )
        self.assertEqual(g.state.blocked_until_epoch, 145)

    def test_retry_after_past_date_never_creates_negative_delay(self):
        g = self.gate(base_backoff_seconds=4)
        a = self.allow(g)
        dt = datetime.fromtimestamp(50, tz=timezone.utc)
        self.result(
            g,
            a,
            100,
            503,
            {"Retry-After": format_datetime(dt, usegmt=True)},
        )
        self.assertEqual(g.state.blocked_until_epoch, 104)

    def test_retry_after_is_capped(self):
        g = self.gate(max_server_delay_seconds=60)
        a = self.allow(g)
        self.result(g, a, 100, 429, {"Retry-After": "999999999"})
        self.assertEqual(g.state.blocked_until_epoch, 160)

    def test_retry_after_absurd_integer_is_capped_without_overflow(self):
        g = self.gate(max_server_delay_seconds=60)
        a = self.allow(g)
        self.result(g, a, 100, 429, {"Retry-After": "9" * 10000})
        self.assertEqual(g.state.blocked_until_epoch, 160)

    def test_backoff_huge_failure_count_caps_without_float_overflow(self):
        g = self.gate(
            base_backoff_seconds=1,
            max_backoff_seconds=300,
        )
        g.state.consecutive_transient_failures = 10**6
        a = self.allow(g)
        g.record_transport_failure(
            now_epoch=100,
            request_token=a.request_token,
        )
        self.assertEqual(g.state.blocked_until_epoch, 400)

    def test_policy_rejects_nonfinite_values(self):
        for kwargs in (
            {"base_backoff_seconds": float("inf")},
            {"max_backoff_seconds": float("inf")},
            {"max_server_delay_seconds": float("nan")},
            {"jitter_fraction": float("nan")},
        ):
            with self.assertRaises(ValueError):
                GatePolicy(**kwargs)

    def test_403_explicit_rate_limit_is_transient(self):
        g = self.gate()
        a = self.allow(g)
        self.result(
            g,
            a,
            100,
            403,
            {
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "180",
            },
        )
        self.assertIsNone(g.state.hard_block_reason)
        self.assertEqual(g.state.blocked_until_epoch, 180)

    def test_403_retry_after_is_transient_without_x_rate_headers(self):
        g = self.gate()
        a = self.allow(g)
        self.result(
            g,
            a,
            100,
            403,
            {"Retry-After": "20"},
        )
        self.assertIsNone(g.state.hard_block_reason)
        self.assertEqual(g.state.blocked_until_epoch, 120)

    def test_plain_403_is_hard_block(self):
        g = self.gate()
        a = self.allow(g)
        self.result(g, a, 100, 403)
        b = g.admit(1000)
        self.assertEqual(b.decision, Decision.BLOCK_HARD)
        self.assertEqual(
            b.reason,
            HardBlockReason.FORBIDDEN.value,
        )

    def test_401_is_hard_block(self):
        g = self.gate()
        a = self.allow(g)
        self.result(g, a, 100, 401)
        self.assertEqual(
            g.state.hard_block_reason,
            HardBlockReason.AUTH_REQUIRED.value,
        )

    def test_hard_block_requires_explicit_clear(self):
        g = self.gate()
        a = self.allow(g)
        self.result(g, a, 100, 401)
        g.clear_hard_block()
        self.assertEqual(
            g.admit(101).decision,
            Decision.ALLOW,
        )

    def test_503_exponential_backoff(self):
        g = self.gate(
            base_backoff_seconds=2,
            max_backoff_seconds=10,
        )
        a = self.allow(g)
        self.result(g, a, 100, 503)
        self.assertEqual(g.state.blocked_until_epoch, 102)
        a = self.allow(g, 102)
        self.result(g, a, 102, 503)
        self.assertEqual(g.state.blocked_until_epoch, 106)
        a = self.allow(g, 106)
        self.result(g, a, 106, 503)
        self.assertEqual(g.state.blocked_until_epoch, 114)
        a = self.allow(g, 114)
        self.result(g, a, 114, 503)
        self.assertEqual(g.state.blocked_until_epoch, 124)

    def test_transport_failure_uses_same_backoff(self):
        g = self.gate(base_backoff_seconds=3)
        a = self.allow(g)
        g.record_transport_failure(
            now_epoch=100,
            request_token=a.request_token,
        )
        self.assertEqual(g.state.blocked_until_epoch, 103)

    def test_cooldown_never_shortened_by_later_transient_result(self):
        g = self.gate()
        a1 = self.allow(g)
        a2 = self.allow(g)
        self.result(
            g,
            a1,
            100,
            429,
            {"Retry-After": "100"},
        )
        self.result(g, a2, 110, 503)
        self.assertEqual(g.state.blocked_until_epoch, 200)

    def test_single_half_open_probe_prevents_thundering_herd(self):
        g = self.gate()
        a = self.allow(g)
        self.result(g, a, 100, 503)
        first = g.admit(g.state.blocked_until_epoch)
        second = g.admit(g.state.blocked_until_epoch)
        self.assertEqual(
            first.decision,
            Decision.ALLOW_PROBE,
        )
        self.assertEqual(
            second.decision,
            Decision.BLOCK_PROBE_IN_FLIGHT,
        )

    def test_probe_success_resets_failure_history(self):
        g = self.gate()
        a = self.allow(g)
        self.result(g, a, 100, 503)
        probe = self.allow(
            g,
            g.state.blocked_until_epoch,
        )
        self.result(g, probe, 101, 204)
        self.assertEqual(
            g.state.consecutive_transient_failures,
            0,
        )
        self.assertEqual(
            g.admit(101).decision,
            Decision.ALLOW,
        )

    def test_wrong_request_token_rejected(self):
        g = self.gate()
        a = self.allow(g)
        with self.assertRaises(ValueError):
            g.record_http_result(
                now_epoch=100,
                status_code=200,
                request_token=a.request_token + "x",
            )

    def test_unissued_request_token_rejected(self):
        g = self.gate()
        self.allow(g)
        with self.assertRaises(ValueError):
            g.record_http_result(
                now_epoch=100,
                status_code=200,
                request_token="99:deadbeef",
            )

    def test_client_4xx_does_not_poison_provider(self):
        for status in (400, 404, 409, 422):
            g = self.gate()
            a = self.allow(g)
            self.result(g, a, 100, status)
            self.assertEqual(
                g.admit(100).decision,
                Decision.ALLOW,
            )

    def test_restart_preserves_cooldown_and_failure_count(self):
        g = self.gate()
        a = self.allow(g)
        self.result(
            g,
            a,
            100,
            429,
            {"Retry-After": "50"},
        )
        restored = ProviderAdmissionGate.restore_json(
            g.snapshot_json(),
            g.policy,
        )
        self.assertEqual(
            restored.state.blocked_until_epoch,
            150,
        )
        self.assertEqual(
            restored.state.consecutive_transient_failures,
            1,
        )
        self.assertEqual(
            restored.admit(120).decision,
            Decision.BLOCK_COOLDOWN,
        )

    def test_restart_clears_ephemeral_probe_lease_but_not_failure_history(self):
        g = self.gate()
        a = self.allow(g)
        self.result(g, a, 100, 503)
        first = self.allow(
            g,
            g.state.blocked_until_epoch,
        )
        restored = ProviderAdmissionGate.restore_json(
            g.snapshot_json(),
            g.policy,
        )
        second = self.allow(
            restored,
            restored.state.blocked_until_epoch,
        )
        self.assertNotEqual(
            first.request_token,
            second.request_token,
        )

    def test_snapshot_canonical_and_unicode_safe(self):
        g = ProviderAdmissionGate.fresh(
            "провайдер",
            "вузол-1",
            GatePolicy(jitter_fraction=0),
        )
        s1 = g.snapshot_json()
        s2 = g.snapshot_json()
        self.assertEqual(s1, s2)
        self.assertIn("провайдер", s1)
        self.assertEqual(
            json.loads(s1)["provider"],
            "провайдер",
        )

    def test_snapshot_rejects_unknown_fields(self):
        g = self.gate()
        raw = json.loads(g.snapshot_json())
        raw["unexpected"] = True
        with self.assertRaises(ValueError):
            ProviderAdmissionGate.restore_json(
                json.dumps(raw),
                g.policy,
            )

    def test_snapshot_rejects_truncated_json(self):
        with self.assertRaises(ValueError):
            ProviderAdmissionGate.restore_json(
                '{"schema_version":2'
            )

    def test_snapshot_rejects_invalid_enum(self):
        g = self.gate()
        raw = json.loads(g.snapshot_json())
        raw["hard_block_reason"] = "MAYBE"
        with self.assertRaises(ValueError):
            ProviderAdmissionGate.restore_json(
                json.dumps(raw),
                g.policy,
            )

    def test_snapshot_rejects_generation_invariant_violation(self):
        g = self.gate()
        raw = json.loads(g.snapshot_json())
        raw["throttle_fence_generation"] = 9
        with self.assertRaises(ValueError):
            ProviderAdmissionGate.restore_json(
                json.dumps(raw),
                g.policy,
            )

    def test_deterministic_jitter_stable_across_restart(self):
        p = GatePolicy(
            base_backoff_seconds=10,
            max_backoff_seconds=100,
            jitter_fraction=0.2,
        )
        a = ProviderAdmissionGate.fresh(
            "betfair",
            "node-A",
            p,
        )
        b = ProviderAdmissionGate.restore_json(
            a.snapshot_json(),
            p,
        )
        aa = self.allow(a)
        bb = self.allow(b)
        a.record_transport_failure(
            now_epoch=100,
            request_token=aa.request_token,
        )
        b.record_transport_failure(
            now_epoch=100,
            request_token=bb.request_token,
        )
        self.assertEqual(
            a.state.blocked_until_epoch,
            b.state.blocked_until_epoch,
        )

    def test_different_jitter_seed_desynchronizes_nodes(self):
        p = GatePolicy(
            base_backoff_seconds=10,
            max_backoff_seconds=100,
            jitter_fraction=0.2,
        )
        a = ProviderAdmissionGate.fresh(
            "betfair",
            "node-A",
            p,
        )
        b = ProviderAdmissionGate.fresh(
            "betfair",
            "node-B",
            p,
        )
        aa = self.allow(a)
        bb = self.allow(b)
        a.record_transport_failure(
            now_epoch=100,
            request_token=aa.request_token,
        )
        b.record_transport_failure(
            now_epoch=100,
            request_token=bb.request_token,
        )
        self.assertNotEqual(
            a.state.blocked_until_epoch,
            b.state.blocked_until_epoch,
        )

    def test_probe_client_error_closes_transport_circuit(self):
        g = self.gate()
        a = self.allow(g)
        self.result(g, a, 100, 503)
        probe = self.allow(
            g,
            g.state.blocked_until_epoch,
        )
        self.result(g, probe, 101, 400)
        self.assertEqual(
            g.state.consecutive_transient_failures,
            0,
        )
        self.assertEqual(
            g.admit(101).decision,
            Decision.ALLOW,
        )

    def test_429_server_delay_dominates_shorter_backoff(self):
        g = self.gate(base_backoff_seconds=30)
        a = self.allow(g)
        self.result(
            g,
            a,
            100,
            429,
            {"Retry-After": "120"},
        )
        self.assertEqual(
            g.state.blocked_until_epoch,
            220,
        )

    def test_longer_backoff_dominates_short_retry_after(self):
        g = self.gate(base_backoff_seconds=30)
        a = self.allow(g)
        self.result(
            g,
            a,
            100,
            429,
            {"Retry-After": "1"},
        )
        self.assertEqual(
            g.state.blocked_until_epoch,
            130,
        )

    def test_reset_header_before_now_still_uses_backoff(self):
        g = self.gate(base_backoff_seconds=5)
        a = self.allow(g)
        self.result(
            g,
            a,
            100,
            403,
            {
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "50",
            },
        )
        self.assertEqual(
            g.state.blocked_until_epoch,
            105,
        )

    def test_403_invalid_rate_limit_headers_fail_closed_as_forbidden(self):
        g = self.gate()
        a = self.allow(g)
        self.result(
            g,
            a,
            100,
            403,
            {
                "X-RateLimit-Remaining": "wat",
                "X-RateLimit-Reset": "180",
            },
        )
        self.assertEqual(
            g.state.hard_block_reason,
            HardBlockReason.FORBIDDEN.value,
        )

    def test_403_nonzero_remaining_is_not_assumed_rate_limited(self):
        g = self.gate()
        a = self.allow(g)
        self.result(
            g,
            a,
            100,
            403,
            {
                "X-RateLimit-Remaining": "1",
                "X-RateLimit-Reset": "180",
            },
        )
        self.assertEqual(
            g.state.hard_block_reason,
            HardBlockReason.FORBIDDEN.value,
        )

    def test_invalid_now_rejected(self):
        g = self.gate()
        for bad in (
            -1,
            float("inf"),
            float("nan"),
        ):
            with self.assertRaises(ValueError):
                g.admit(bad)

    def test_invalid_http_status_rejected(self):
        g = self.gate()
        a = self.allow(g)
        with self.assertRaises(ValueError):
            g.record_http_result(
                now_epoch=100,
                status_code=999,
                request_token=a.request_token,
            )

    def test_policy_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            GatePolicy(base_backoff_seconds=0)
        with self.assertRaises(ValueError):
            GatePolicy(
                base_backoff_seconds=2,
                max_backoff_seconds=1,
            )
        with self.assertRaises(ValueError):
            GatePolicy(jitter_fraction=0.9)

    def test_425_and_408_are_transient(self):
        for status in (408, 425):
            g = self.gate()
            a = self.allow(g)
            self.result(g, a, 100, status)
            self.assertGreater(
                g.state.blocked_until_epoch,
                100,
            )
            self.assertIsNone(
                g.state.hard_block_reason
            )

    def test_5xx_family_is_transient(self):
        for status in (
            500,
            501,
            502,
            503,
            504,
            599,
        ):
            g = self.gate()
            a = self.allow(g)
            self.result(g, a, 100, status)
            self.assertGreater(
                g.state.blocked_until_epoch,
                100,
            )

    def test_older_concurrent_success_cannot_clear_newer_429(self):
        g = self.gate()
        old = self.allow(g)
        newer = self.allow(g)
        self.result(
            g,
            newer,
            100,
            429,
            {"Retry-After": "30"},
        )
        self.result(g, old, 101, 200)
        self.assertEqual(
            g.admit(101).decision,
            Decision.BLOCK_COOLDOWN,
        )
        self.assertEqual(
            g.state.blocked_until_epoch,
            130,
        )

    def test_any_pre_throttle_inflight_success_cannot_clear_cooldown(self):
        g = self.gate(base_backoff_seconds=1)
        older = self.allow(g)
        newer = self.allow(g)
        self.result(g, older, 100, 503)
        # The throttle observation fences every request that had already been issued,
        # not merely requests with a lower generation than the failing request.
        self.result(g, newer, 100.5, 200)
        self.assertEqual(
            g.admit(100.5).decision,
            Decision.BLOCK_COOLDOWN,
        )
        probe = self.allow(g, 101)
        self.result(g, probe, 101, 200)
        self.assertEqual(
            g.admit(101).decision,
            Decision.ALLOW,
        )

    def test_outstanding_success_cannot_clear_hard_401(self):
        g = self.gate()
        old = self.allow(g)
        newer = self.allow(g)
        self.result(g, newer, 100, 401)
        self.result(g, old, 101, 200)
        self.assertEqual(
            g.admit(200).decision,
            Decision.BLOCK_HARD,
        )
        self.assertEqual(
            g.state.hard_block_reason,
            HardBlockReason.AUTH_REQUIRED.value,
        )

    def test_outstanding_success_cannot_clear_plain_403(self):
        g = self.gate()
        old = self.allow(g)
        newer = self.allow(g)
        self.result(g, newer, 100, 403)
        self.result(g, old, 101, 200)
        self.assertEqual(
            g.admit(200).decision,
            Decision.BLOCK_HARD,
        )

    def test_explicit_clear_fences_pre_clear_outstanding_success(self):
        g = self.gate()
        old = self.allow(g)
        newer = self.allow(g)
        self.result(g, newer, 100, 401)
        g.clear_hard_block()
        post = self.allow(g, 101)
        # Stale completion remains harmless after explicit recovery.
        self.result(g, old, 102, 200)
        self.assertEqual(
            g.state.throttle_fence_generation,
            2,
        )
        self.result(g, post, 103, 200)
        self.assertEqual(
            g.admit(103).decision,
            Decision.ALLOW,
        )

    def test_request_token_generation_increments_monotonically(self):
        g = self.gate()
        a = self.allow(g)
        b = self.allow(g)
        c = self.allow(g)
        self.assertEqual(
            [
                int(x.request_token.split(":", 1)[0])
                for x in (a, b, c)
            ],
            [1, 2, 3],
        )

    def test_restart_preserves_generation_fence(self):
        g = self.gate()
        old = self.allow(g)
        newer = self.allow(g)
        self.result(g, newer, 100, 503)
        restored = ProviderAdmissionGate.restore_json(
            g.snapshot_json(),
            g.policy,
        )
        restored.record_http_result(
            now_epoch=100.5,
            status_code=200,
            request_token=old.request_token,
        )
        self.assertEqual(
            restored.state.blocked_until_epoch,
            101,
        )
        self.assertEqual(
            restored.state.consecutive_transient_failures,
            1,
        )
        self.assertEqual(
            restored.admit(100.5).decision,
            Decision.BLOCK_COOLDOWN,
        )


    def test_policy_rejects_non_numeric_scalar_types(self):
        for kwargs in (
            {"base_backoff_seconds": True},
            {"max_backoff_seconds": "300"},
            {"max_server_delay_seconds": False},
            {"jitter_fraction": "0.1"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    GatePolicy(**kwargs)

    def test_snapshot_rejects_wrong_scalar_types_fail_closed(self):
        baseline = json.loads(self.gate().snapshot_json())
        cases = {
            "schema_version": True,
            "provider": 7,
            "jitter_seed": [],
            "blocked_until_epoch": True,
            "consecutive_transient_failures": False,
            "issued_generation": 1.0,
            "throttle_fence_generation": "0",
            "probe_in_flight": 0,
            "active_probe_generation": None,
            "hard_block_reason": 7,
        }
        for field, bad in cases.items():
            with self.subTest(field=field, bad=bad):
                raw = dict(baseline)
                raw[field] = bad
                with self.assertRaises(ValueError):
                    ProviderAdmissionGate.restore_json(
                        json.dumps(raw),
                        self.gate().policy,
                    )

    def test_http_status_requires_exact_integer(self):
        for bad in (200.0, True, "200"):
            with self.subTest(bad=bad):
                g = self.gate()
                a = self.allow(g)
                with self.assertRaises(ValueError):
                    g.record_http_result(
                        now_epoch=100,
                        status_code=bad,
                        request_token=a.request_token,
                    )

    def test_now_requires_exact_numeric_scalar(self):
        g = self.gate()
        for bad in (True, "100", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    g.admit(bad)

    def test_concurrent_half_open_admission_issues_exactly_one_probe(self):
        g = self.gate(base_backoff_seconds=1)
        a = self.allow(g)
        self.result(g, a, 100, 503)
        barrier = Barrier(32)

        def attempt():
            barrier.wait()
            return g.admit(101).decision

        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(lambda _index: attempt(), range(32)))

        self.assertEqual(
            results.count(Decision.ALLOW_PROBE),
            1,
        )
        self.assertEqual(
            results.count(Decision.BLOCK_PROBE_IN_FLIGHT),
            31,
        )

    def test_jitter_never_exceeds_configured_max_backoff(self):
        policy = GatePolicy(
            base_backoff_seconds=300,
            max_backoff_seconds=300,
            jitter_fraction=0.5,
        )
        for index in range(64):
            with self.subTest(index=index):
                g = ProviderAdmissionGate.fresh(
                    "betfair",
                    f"node-{index}",
                    policy,
                )
                a = self.allow(g)
                g.record_transport_failure(
                    now_epoch=100,
                    request_token=a.request_token,
                )
                self.assertLessEqual(
                    g.state.blocked_until_epoch,
                    400,
                )

    def test_http_date_timestamp_platform_overflow_falls_back_to_backoff(self):
        class OutOfRangeDate:
            tzinfo = timezone.utc

            def astimezone(self, _tz):
                return self

            def timestamp(self):
                raise OSError("platform timestamp range")

        g = self.gate(base_backoff_seconds=3)
        a = self.allow(g)
        with patch(
            "autosport.provider_request_admission.parsedate_to_datetime",
            return_value=OutOfRangeDate(),
        ):
            self.result(
                g,
                a,
                100,
                503,
                {"Retry-After": "Thu, 01 Jan 9999 00:00:00 GMT"},
            )
        self.assertEqual(
            g.state.blocked_until_epoch,
            103,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
