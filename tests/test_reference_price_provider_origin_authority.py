from __future__ import annotations

from decimal import Decimal
import unittest

from autosport.domain import MarketEvent, MarketType
from autosport.reference_price_evidence import (
    ReferencePriceEvidenceError,
    build_reference_price_evidence,
)


class ReferencePriceProviderOriginAuthorityTests(unittest.TestCase):
    """Caller-authored canonical-looking rows must not mint provider consensus."""

    DECISION_TS = "2026-09-21T08:30:10+00:00"

    @staticmethod
    def _caller_minted_event(source_id: str, odds: str) -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal(odds),
            observed_ts="2026-09-21T08:30:00+00:00",
            source_id=source_id,
            sequence=1,
            market_type=MarketType.WINNER,
            status="open",
            source_ts="2026-09-21T08:29:59+00:00",
            ingest_ts="2026-09-21T08:30:01+00:00",
            metadata={
                "price_semantics": "best_available_to_back",
                "execution_quote_verified": False,
            },
            sport="table_tennis",
            market_semantics_id="winner.match.v1",
        )

    def test_caller_minted_distinct_sources_cannot_create_reference_consensus(self) -> None:
        """Canonical shape alone is not product/provider-origin authority."""
        first = self._caller_minted_event("caller-minted-provider-a", "2.00")
        second = self._caller_minted_event("caller-minted-provider-b", "2.10")

        with self.assertRaises(ReferencePriceEvidenceError):
            build_reference_price_evidence(
                (first, second),
                decision_ts=self.DECISION_TS,
                max_age_seconds=30,
                max_skew_seconds=5,
                minimum_sources=2,
            )


if __name__ == "__main__":
    unittest.main()
