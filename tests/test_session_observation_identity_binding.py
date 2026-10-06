from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal

from autosport.providers import ProviderBatch, ProviderQuote
from autosport.session import AutosportSession


class _SingleReadSourceIdProvider:
    """Provider whose validated source identity may not be re-read after acquisition."""

    def __init__(self) -> None:
        self.source_id_reads = 0

    @property
    def source_id(self) -> str:
        self.source_id_reads += 1
        if self.source_id_reads == 1:
            return "fixture:committed-source"
        raise RuntimeError("source_id was re-read after ingestion commit")

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        return ProviderBatch(
            "fixture:committed-source",
            (
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="player-a",
                    decimal_odds=Decimal("1.80"),
                    observed_ts="2026-09-14T10:00:00+00:00",
                    sequence=1,
                ),
            ),
            cursor="1",
        )


class SessionObservationIdentityBindingTests(unittest.TestCase):
    def test_post_commit_readback_uses_ingestion_stats_source_identity(self) -> None:
        provider = _SingleReadSourceIdProvider()

        with tempfile.TemporaryDirectory() as tmp:
            session = AutosportSession(tmp, "10000")
            try:
                result = session.observe_provider_once(provider, max_items=10)

                self.assertEqual(provider.source_id_reads, 1)
                self.assertEqual(result.stats.source_id, "fixture:committed-source")
                self.assertEqual(result.health.source_id, "fixture:committed-source")
                self.assertEqual(result.health.total_accepted, 1)
                self.assertEqual(len(result.current_quotes), 1)
                self.assertEqual(result.current_quotes[0].source_id, "fixture:committed-source")
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
