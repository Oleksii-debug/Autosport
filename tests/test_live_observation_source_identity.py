import tempfile
import unittest
from decimal import Decimal

from autosport.live_observation import observe_workspace_once
from autosport.providers import ProviderBatch, ProviderQuote


_RECEIVE_TIME = "2026-09-14T12:00:02+00:00"
_SOURCE_ID = "source-once"


class _SingleReadSourceProvider:
    """Provider whose underlying source identity may only be captured once."""

    def __init__(self) -> None:
        self.source_id_reads = 0

    @property
    def source_id(self) -> str:
        self.source_id_reads += 1
        if self.source_id_reads > 1:
            raise AssertionError("underlying provider source_id was reread after capture")
        return _SOURCE_ID

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        return ProviderBatch(
            source_id=_SOURCE_ID,
            quotes=(
                ProviderQuote(
                    provider_event_id="event-1",
                    provider_market_id="winner",
                    provider_selection_id="selection-a",
                    decimal_odds=Decimal("1.80"),
                    observed_ts="2026-09-14T12:00:00+00:00",
                    sequence=1,
                    source_ts="2026-09-14T11:59:59+00:00",
                ),
            ),
            cursor="snapshot-complete",
        )


class LiveObservationSourceIdentityTests(unittest.TestCase):
    def test_post_commit_readback_uses_committed_stats_source_identity(self):
        provider = _SingleReadSourceProvider()

        with tempfile.TemporaryDirectory() as workspace:
            result = observe_workspace_once(
                workspace,
                provider,
                max_items=10,
                clock=lambda: _RECEIVE_TIME,
            )

        self.assertEqual(provider.source_id_reads, 1)
        self.assertEqual(result.stats.source_id, _SOURCE_ID)
        self.assertEqual(result.stats.accepted, 1)
        self.assertEqual(result.health.status, "healthy")
        self.assertEqual(len(result.current_quotes), 1)
        self.assertEqual(result.current_quotes[0].source_id, _SOURCE_ID)


if __name__ == "__main__":
    unittest.main()
