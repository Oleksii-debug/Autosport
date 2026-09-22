from __future__ import annotations

import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone

from autosport.provider_time_freshness import (
    ProviderTimeEvidence,
    ProviderTimeStatus,
    assess_provider_time_freshness,
)


class ProviderTimeFreshnessAuthorityFalsifiers(unittest.TestCase):
    @staticmethod
    def _base_kwargs() -> dict[str, object]:
        return {
            "source_updated_at": "2026-09-22T12:00:00+00:00",
            "received_wall_at": "2026-09-22T12:00:00.100000+00:00",
            "acquisition_started_monotonic_ns": 1_000_000_000,
            "received_monotonic_ns": 1_100_000_000,
        }

    def test_generic_sequence_identity_is_not_signed_integer_only(self) -> None:
        """Opaque provider clocks must not require a fabricated numeric surrogate.

        This is intentionally repair-shape tolerant. Removing sequence identity from the
        generic timing primitive, or adding a separate tagged/opaque sequence field, both
        satisfy this falsifier. If the generic primitive keeps only ``sequence_id``, it
        must be able to preserve an opaque provider token exactly.
        """

        sequence_fields = [
            field.name
            for field in fields(ProviderTimeEvidence)
            if "sequence" in field.name
        ]
        if sequence_fields != ["sequence_id"]:
            return

        kwargs = self._base_kwargs()
        kwargs["sequence_id"] = "opaque-clk-token"
        try:
            evidence = ProviderTimeEvidence(**kwargs)
        except (TypeError, ValueError) as exc:
            self.fail(
                "generic provider timing evidence is integer-sequence-only; "
                "opaque provider clocks cannot be preserved losslessly: "
                f"{exc}"
            )
        self.assertEqual(evidence.sequence_id, "opaque-clk-token")

    def test_fresh_timing_alone_cannot_mint_live_market_eligibility(self) -> None:
        """Timing evidence has no market-data/feed/continuity authority.

        The parent object deliberately has no observation-kind, feed-mode, heartbeat,
        conflation, delayed-feed, or continuity proof. A recent timestamp can therefore
        prove at most timing freshness. It must not mint a generic positive ``eligible``
        bit that a downstream live decision could mistake for actionable market truth.
        Removing that positive surface is an acceptable repair.
        """

        evidence = ProviderTimeEvidence(
            **self._base_kwargs(),
            sequence_id=17,
        )
        result = assess_provider_time_freshness(
            evidence,
            decision_at=datetime(2026, 9, 22, 12, 0, 1, tzinfo=timezone.utc),
            max_quote_age=timedelta(seconds=2),
            max_source_clock_skew=timedelta(milliseconds=100),
        )

        self.assertEqual(result.status, ProviderTimeStatus.FRESH)
        self.assertFalse(
            getattr(result, "eligible", False),
            "timing-only evidence must not issue generic live/market eligibility",
        )


if __name__ == "__main__":
    unittest.main()
