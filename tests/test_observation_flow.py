import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.cli import run_observe_table_tennis
import autosport.storage as storage_module
from autosport.domain import MarketEvent
from autosport.providers import InMemoryProvider, ProviderQuote
from autosport.session import AutosportSession


class ObservationFlowTests(unittest.TestCase):
    @staticmethod
    def _provider(source_id: str = "fixture:table_tennis") -> InMemoryProvider:
        return InMemoryProvider(
            source_id,
            [
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="player-a",
                    decimal_odds=Decimal("1.80"),
                    observed_ts="2026-09-12T20:00:00+00:00",
                    sequence=1,
                ),
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="player-b",
                    decimal_odds=Decimal("2.05"),
                    observed_ts="2026-09-12T20:00:00+00:00",
                    sequence=2,
                ),
            ],
        )

    def test_session_observation_persists_market_and_health_without_paper_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = AutosportSession(tmp, "10000")
            result = session.observe_provider_once(self._provider(), max_items=10)
            self.assertEqual(result.stats.accepted, 2)
            self.assertEqual(result.health.status, "healthy")
            self.assertEqual(len(result.current_quotes), 2)
            self.assertEqual(len(session.book.tickets), 0)
            self.assertEqual(session.book.balance, Decimal("10000"))
            session.close()

            reopened = AutosportSession(tmp, "10000")
            current = [event for event in reopened.store.current().values() if event.source_id == "fixture:table_tennis"]
            self.assertEqual(len(current), 2)
            self.assertEqual(reopened.source_health.get("fixture:table_tennis").total_accepted, 2)
            self.assertEqual(len(reopened.book.tickets), 0)
            reopened.close()

    def test_session_observation_uses_full_canonical_quote_order(self):
        provider = InMemoryProvider(
            "fixture:identity-order",
            [
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="player-a",
                    decimal_odds=Decimal("1.80"),
                    observed_ts="2026-09-12T20:00:00+00:00",
                    sequence=1,
                    sport="tennis",
                    exchange_side="lay",
                ),
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="player-a",
                    decimal_odds=Decimal("1.81"),
                    observed_ts="2026-09-12T20:00:01+00:00",
                    sequence=2,
                    sport="basketball",
                    exchange_side="back",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            session = AutosportSession(tmp, "10000")
            try:
                result = session.observe_provider_once(provider, max_items=10)
                quote_keys = tuple(event.quote_key for event in result.current_quotes)

                self.assertEqual(quote_keys, tuple(sorted(quote_keys)))
                self.assertEqual(len(set(quote_keys)), 2)
            finally:
                session.close()

    def test_session_observation_excludes_generation_zero_migration_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.db"
            legacy = MarketEvent(
                event_id="legacy-event",
                market_id="winner",
                selection_id="legacy-selection",
                decimal_odds=Decimal("2.20"),
                observed_ts="2026-09-12T20:00:00+00:00",
                ingest_ts="2026-09-12T20:00:00+00:00",
                source_id="fixture:table_tennis",
                sequence=3,
                status="open",
                source_ts="2026-09-12T19:59:59+00:00",
            )
            payload = storage_module._canonical_payload(legacy)
            raw = sqlite3.connect(path)
            try:
                raw.execute(
                    """CREATE TABLE market_events (
                        dedupe_key TEXT PRIMARY KEY,
                        quote_key TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        market_id TEXT NOT NULL,
                        selection_id TEXT NOT NULL,
                        decimal_odds TEXT NOT NULL,
                        observed_ts TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        payload_json TEXT NOT NULL
                    )"""
                )
                raw.execute(
                    """CREATE TABLE current_quotes (
                        source_id TEXT NOT NULL,
                        quote_key TEXT NOT NULL,
                        observed_ts TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (source_id, quote_key)
                    )"""
                )
                raw.execute(
                    """INSERT INTO market_events
                       (dedupe_key,quote_key,event_id,market_id,selection_id,
                        decimal_odds,observed_ts,source_id,sequence,payload_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        legacy.dedupe_key,
                        legacy.quote_key,
                        legacy.event_id,
                        legacy.market_id,
                        legacy.selection_id,
                        str(legacy.decimal_odds),
                        legacy.observed_ts,
                        legacy.source_id,
                        legacy.sequence,
                        payload,
                    ),
                )
                raw.execute(
                    """INSERT INTO current_quotes
                       (source_id,quote_key,observed_ts,sequence,payload_json)
                       VALUES (?,?,?,?,?)""",
                    (
                        legacy.source_id,
                        legacy.quote_key,
                        legacy.observed_ts,
                        legacy.sequence,
                        payload,
                    ),
                )
                raw.commit()
            finally:
                raw.close()

            session = AutosportSession(tmp, "10000")
            try:
                result = session.observe_provider_once(self._provider(), max_items=10)

                self.assertEqual(result.stats.accepted, 2)
                self.assertNotIn(
                    legacy.dedupe_key,
                    {event.dedupe_key for event in result.current_quotes},
                )
                self.assertEqual(len(result.current_quotes), 2)
            finally:
                session.close()

    def test_session_observation_rejects_nonfinite_provider_odds_before_persistence(self):
        provider = InMemoryProvider(
            "fixture:finite-boundary",
            [
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="valid-a",
                    decimal_odds=Decimal("1.80"),
                    observed_ts="2026-09-12T20:00:00+00:00",
                    sequence=1,
                ),
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="nan",
                    decimal_odds=Decimal("NaN"),
                    observed_ts="2026-09-12T20:00:01+00:00",
                    sequence=2,
                ),
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="infinity",
                    decimal_odds=Decimal("Infinity"),
                    observed_ts="2026-09-12T20:00:02+00:00",
                    sequence=3,
                ),
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="valid-b",
                    decimal_odds=Decimal("2.05"),
                    observed_ts="2026-09-12T20:00:03+00:00",
                    sequence=4,
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            session = AutosportSession(tmp, "10000")
            result = session.observe_provider_once(provider, max_items=10)

            self.assertEqual(result.stats.received, 4)
            self.assertEqual(result.stats.accepted, 2)
            self.assertEqual(result.stats.rejected, 2)
            persisted = session.store.events()
            self.assertEqual([event.sequence for event in persisted], [1, 4])
            self.assertTrue(all(event.decimal_odds.is_finite() for event in persisted))
            self.assertEqual(
                session.source_health.get("fixture:finite-boundary").total_rejected,
                2,
            )
            session.close()

    def test_cli_requires_environment_key_unless_public_preview_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            output = io.StringIO()
            with redirect_stdout(output):
                code = run_observe_table_tennis(
                    Path(tmp), public_preview=False, max_items=10, show=5
                )
            self.assertEqual(code, 2)
            self.assertIn("AUTOSPORT_PARLAYAPI_KEY", output.getvalue())

    def test_cli_public_preview_path_is_injectable_and_prints_accessible_text(self):
        calls = []

        def factory(api_key, *, public_preview):
            calls.append((api_key, public_preview))
            return self._provider("preview-source")

        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with redirect_stdout(output):
                code = run_observe_table_tennis(
                    Path(tmp),
                    public_preview=True,
                    max_items=10,
                    show=1,
                    provider_factory=factory,
                )
            text = output.getvalue()
            self.assertEqual(code, 0)
            self.assertEqual(calls, [(None, True)])
            self.assertIn("source=preview-source health=healthy", text)
            self.assertIn("current_quotes=2", text)
            self.assertIn("odds=1.80", text)
            self.assertIn("1 more current quotes not printed", text)

    def test_cli_never_passes_environment_key_in_output(self):
        seen = []

        def factory(api_key, *, public_preview):
            seen.append(api_key)
            return self._provider("keyed-source")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"AUTOSPORT_PARLAYAPI_KEY": "super-secret"}, clear=True
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                code = run_observe_table_tennis(
                    Path(tmp),
                    public_preview=False,
                    max_items=10,
                    show=0,
                    provider_factory=factory,
                )
            self.assertEqual(code, 0)
            self.assertEqual(seen, ["super-secret"])
            self.assertNotIn("super-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
