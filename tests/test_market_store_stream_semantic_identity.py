from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent, MarketType
from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
from autosport.storage import SQLiteMarketStore


class MarketStoreStreamSemanticIdentityTests(unittest.TestCase):
    @staticmethod
    def _event(
        *,
        sport: str = "soccer",
        sequence: int,
        odds: str = "2.40",
        market_type: MarketType = MarketType.WINNER,
        market_semantics_id: str = "match_odds",
    ) -> MarketEvent:
        second = sequence % 60
        timestamp = f"2026-10-03T12:00:{second:02d}Z"
        return MarketEvent(
            event_id="same-event",
            market_id="same-market",
            selection_id="same-selection",
            decimal_odds=Decimal(odds),
            observed_ts=timestamp,
            source_id="test-source",
            sequence=sequence,
            market_type=market_type,
            source_ts=timestamp,
            ingest_ts=timestamp,
            sport=sport,
            market_semantics_id=market_semantics_id,
            provider_source_class="test_only_engineering_conformance",
        )

    def test_higher_sequence_cannot_rebind_market_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                winner = self._event(sequence=10)
                rebound = self._event(
                    sequence=11,
                    odds="2.50",
                    market_type=MarketType.TOTAL,
                    market_semantics_id="total_points",
                )
                self.assertTrue(store.append(winner))
                with self.assertRaisesRegex(
                    ValueError,
                    "market quote stream semantic identity changed",
                ):
                    store.append(rebound)

                self.assertEqual([event.dedupe_key for event in store.events()], [winner.dedupe_key])
                self.assertEqual(
                    store.current_by_source()[("test-source", winner.quote_key)].dedupe_key,
                    winner.dedupe_key,
                )
            finally:
                store.close()

    def test_higher_sequence_cannot_rebind_semantics_id_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                first = self._event(sequence=20)
                rebound = self._event(
                    sequence=21,
                    odds="2.55",
                    market_semantics_id="match_winner_alternate",
                )
                self.assertTrue(store.append(first))
                with self.assertRaisesRegex(
                    ValueError,
                    "market quote stream semantic identity changed",
                ):
                    store.append(rebound)
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()

    def test_missing_and_explicit_semantics_cannot_replace_each_other(self) -> None:
        transitions = (
            (None, "match_odds"),
            ("match_odds", None),
        )
        for first_semantics, rebound_semantics in transitions:
            with self.subTest(
                first_semantics=first_semantics,
                rebound_semantics=rebound_semantics,
            ):
                with tempfile.TemporaryDirectory() as temp_dir:
                    store = SQLiteMarketStore(Path(temp_dir) / "market.db")
                    try:
                        first = self._event(
                            sequence=22,
                            market_semantics_id=first_semantics,
                        )
                        rebound = self._event(
                            sequence=23,
                            odds="2.55",
                            market_semantics_id=rebound_semantics,
                        )
                        self.assertTrue(store.append(first))
                        with self.assertRaisesRegex(
                            ValueError,
                            "market quote stream semantic identity changed",
                        ):
                            store.append(rebound)
                        self.assertEqual(
                            [event.dedupe_key for event in store.events()],
                            [first.dedupe_key],
                        )
                    finally:
                        store.close()

    def test_stale_sequence_cannot_pollute_history_with_different_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                current = self._event(sequence=29)
                stale_conflict = self._event(
                    sequence=28,
                    odds="2.20",
                    market_type=MarketType.TOTAL,
                    market_semantics_id="total_points",
                )
                self.assertTrue(store.append(current))
                with self.assertRaisesRegex(
                    ValueError,
                    "market quote stream semantic identity changed",
                ):
                    store.append(stale_conflict)
                self.assertEqual(
                    [event.dedupe_key for event in store.events()],
                    [current.dedupe_key],
                )
            finally:
                store.close()

    def test_batch_semantic_rebind_rolls_back_earlier_batch_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                baseline = self._event(sequence=30)
                accepted_then_rolled_back = self._event(sequence=31, odds="2.45")
                conflicting = self._event(
                    sequence=32,
                    odds="2.60",
                    market_type=MarketType.HANDICAP,
                    market_semantics_id="asian_handicap",
                )
                self.assertTrue(store.append(baseline))
                with self.assertRaisesRegex(
                    ValueError,
                    "market quote stream semantic identity changed",
                ):
                    store.append_batch_accepted(
                        [accepted_then_rolled_back, conflicting]
                    )

                self.assertEqual([event.dedupe_key for event in store.events()], [baseline.dedupe_key])
                self.assertEqual(
                    store.current_by_source()[("test-source", baseline.quote_key)].dedupe_key,
                    baseline.dedupe_key,
                )
            finally:
                store.close()

    def test_missing_current_projection_cannot_erase_history_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                baseline = self._event(sequence=33)
                conflicting = self._event(
                    sequence=34,
                    odds="2.65",
                    market_type=MarketType.TOTAL,
                    market_semantics_id="total_points",
                )
                self.assertTrue(store.append(baseline))
                store.connection.execute(
                    "DELETE FROM current_quotes WHERE source_id=? AND quote_key=?",
                    (baseline.source_id, baseline.quote_key),
                )
                store.connection.commit()
                # Trusted reads reject missing derived rows rather than returning
                # an incomplete projection that could conceal historical semantics.
                with self.assertRaisesRegex(
                    ValueError,
                    "current quote projection diverges from canonical market history",
                ):
                    store.current_by_source()

                with self.assertRaisesRegex(
                    ValueError,
                    "market quote stream semantic identity changed",
                ):
                    store.append(conflicting)

                self.assertEqual(store.events(), [baseline])
            finally:
                store.close()

    def test_missing_current_projection_allows_same_semantic_history_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                baseline = self._event(sequence=35)
                continuation = self._event(sequence=36, odds="2.55")
                self.assertTrue(store.append(baseline))
                store.connection.execute(
                    "DELETE FROM current_quotes WHERE source_id=? AND quote_key=?",
                    (baseline.source_id, baseline.quote_key),
                )
                store.connection.commit()

                self.assertTrue(store.append(continuation))

                self.assertEqual(store.events(), [baseline, continuation])
                self.assertEqual(
                    store.current_by_source()[
                        (continuation.source_id, continuation.quote_key)
                    ].dedupe_key,
                    continuation.dedupe_key,
                )
            finally:
                store.close()

    def test_missing_current_projection_does_not_promote_stale_history_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                first = self._event(sequence=40, odds="2.40")
                latest = self._event(sequence=42, odds="2.80")
                stale = self._event(sequence=41, odds="2.60")
                self.assertTrue(store.append(first))
                self.assertTrue(store.append(latest))
                store.connection.execute(
                    "DELETE FROM current_quotes WHERE source_id=? AND quote_key=?",
                    (latest.source_id, latest.quote_key),
                )
                store.connection.commit()

                self.assertTrue(store.append(stale))

                self.assertEqual(
                    [event.sequence for event in store.events()],
                    [40, 41, 42],
                )
                restored = store.current_by_source()[
                    (latest.source_id, latest.quote_key)
                ]
                self.assertEqual(restored.sequence, 42)
                self.assertEqual(restored.dedupe_key, latest.dedupe_key)
            finally:
                store.close()

    def test_tampered_current_projection_cannot_redefine_history_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                baseline = self._event(sequence=37)
                forged_projection = self._event(
                    sequence=37,
                    market_type=MarketType.TOTAL,
                    market_semantics_id="total_points",
                )
                continuation = self._event(
                    sequence=38,
                    odds="2.70",
                    market_type=MarketType.TOTAL,
                    market_semantics_id="total_points",
                )
                self.assertTrue(store.append(baseline))
                payload = json.dumps(
                    forged_projection.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                store.connection.execute(
                    """UPDATE current_quotes SET payload_json=?
                       WHERE source_id=? AND quote_key=?""",
                    (payload, baseline.source_id, baseline.quote_key),
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    ValueError,
                    "market quote stream semantic identity changed",
                ):
                    store.append(continuation)

                self.assertEqual(store.events(), [baseline])
            finally:
                store.close()

    def test_reopen_rejects_preexisting_semantic_split_in_authoritative_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "market.db"
            first = self._event(sequence=40)
            second = self._event(sequence=41, odds="2.45")
            store = SQLiteMarketStore(db_path)
            self.assertTrue(store.append(first))
            self.assertTrue(store.append(second))
            store.close()

            conflicting_second = self._event(
                sequence=41,
                odds="2.45",
                market_type=MarketType.TOTAL,
                market_semantics_id="total_points",
            )
            payload = json.dumps(
                conflicting_second.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    "UPDATE market_events SET payload_json=? WHERE dedupe_key=?",
                    (payload, second.dedupe_key),
                )
                connection.commit()
            finally:
                connection.close()

            # The signed append-chain proof fails before per-stream validation.
            # Neither boundary may re-accept the rewritten durable history.
            with self.assertRaisesRegex(
                MonotonicAuthorityRollbackError,
                "positive market append authority semantic binding is invalid",
            ):
                SQLiteMarketStore(db_path)

    def test_same_provider_coordinates_in_different_sports_remain_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                soccer = self._event(
                    sport="soccer",
                    sequence=50,
                    market_type=MarketType.TOTAL,
                    market_semantics_id="total_points",
                )
                table_tennis = self._event(
                    sport="table_tennis",
                    sequence=51,
                    market_type=MarketType.WINNER,
                    market_semantics_id="match_odds",
                )
                self.assertNotEqual(soccer.quote_key, table_tennis.quote_key)
                self.assertTrue(store.append(soccer))
                self.assertTrue(store.append(table_tennis))
                self.assertEqual(
                    {event.sport for event in store.current_by_source().values()},
                    {"soccer", "table_tennis"},
                )
            finally:
                store.close()


    def test_durable_ingress_rejects_market_event_subclass_before_dispatch(self) -> None:
        armed = False

        class HostileMarketEvent(MarketEvent):
            def __getattribute__(self, name: str):
                if armed and name in {"sequence", "to_dict", "dedupe_key"}:
                    raise AssertionError("MarketEvent subclass dispatch must not execute")
                return super().__getattribute__(name)

        canonical = self._event(sequence=52)
        hostile_payload = canonical.to_dict()
        hostile_payload["market_type"] = canonical.market_type
        hostile_payload["decimal_odds"] = canonical.decimal_odds
        hostile = HostileMarketEvent(**hostile_payload)
        armed = True

        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                with self.assertRaisesRegex(TypeError, "exact MarketEvent"):
                    store.append(hostile)
                self.assertEqual(store.events(), [])
            finally:
                store.close()



    def test_duplicate_batch_amortizes_projection_proof_without_skipping_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                first = self._event(sequence=53)
                second = self._event(sequence=54, odds="2.55")
                self.assertEqual(
                    store.append_batch_accepted((first, second)),
                    [first, second],
                )
                with patch.object(
                    store,
                    "_repair_current_projection_for_key",
                    wraps=store._repair_current_projection_for_key,
                ) as repair:
                    self.assertEqual(
                        store.append_batch_accepted((first, second, first, second)),
                        [],
                    )
                    self.assertEqual(repair.call_count, 1)
                self.assertEqual(len(store.events()), 2)
                self.assertEqual(
                    store.current_by_source()[(first.source_id, first.quote_key)].dedupe_key,
                    second.dedupe_key,
                )
            finally:
                store.close()

    def test_new_append_invalidates_duplicate_projection_batch_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                first = self._event(sequence=55)
                next_event = self._event(sequence=56, odds="2.60")
                self.assertTrue(store.append(first))
                with patch.object(
                    store,
                    "_repair_current_projection_for_key",
                    wraps=store._repair_current_projection_for_key,
                ) as repair:
                    self.assertEqual(
                        store.append_batch_accepted((first, next_event, first)),
                        [next_event],
                    )
                    self.assertEqual(repair.call_count, 3)
                store.close()
                store = SQLiteMarketStore(Path(temp_dir) / "market.db")
                self.assertEqual(len(store.events()), 2)
                self.assertEqual(
                    store.current_by_source()[(first.source_id, first.quote_key)].dedupe_key,
                    next_event.dedupe_key,
                )
            finally:
                store.close()


    def test_reuses_only_same_connection_unchanged_positive_append_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                first = self._event(sequence=71)
                second = self._event(sequence=72, odds="2.71")
                self.assertTrue(store.append(first))
                with patch.object(
                    store,
                    "_validated_positive_append_entries",
                    wraps=store._validated_positive_append_entries,
                ) as full_validation:
                    self.assertEqual(store.append_batch_accepted((first,)), [])
                    self.assertEqual(store.append_batch_accepted((second,)), [second])
                    self.assertEqual(store.append_batch_accepted((second,)), [])
                    self.assertEqual(full_validation.call_count, 0)
                self.assertEqual(len(store.events()), 2)
            finally:
                store.close()

    def test_external_sql_tamper_invalidates_cached_append_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "market.db"
            store = SQLiteMarketStore(db_path)
            try:
                first = self._event(sequence=73)
                self.assertTrue(store.append(first))
                with sqlite3.connect(db_path) as other:
                    original = other.execute(
                        "SELECT decimal_odds FROM market_events WHERE dedupe_key=?",
                        (first.dedupe_key,),
                    ).fetchone()[0]
                    other.execute(
                        "UPDATE market_events SET decimal_odds=? WHERE dedupe_key=?",
                        ("999.1", first.dedupe_key),
                    )
                with self.assertRaisesRegex(
                    ValueError, "market event history row identity mismatch"
                ):
                    store.append_batch_accepted((first,))
                with sqlite3.connect(db_path) as other:
                    other.execute(
                        "UPDATE market_events SET decimal_odds=? WHERE dedupe_key=?",
                        (original, first.dedupe_key),
                    )
                self.assertEqual(store.append_batch_accepted((first,)), [])
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()

    def test_same_connection_sql_tamper_invalidates_cached_append_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "market.db")
            try:
                first = self._event(sequence=74)
                self.assertTrue(store.append(first))
                store.connection.execute(
                    "UPDATE market_events SET decimal_odds=? WHERE dedupe_key=?",
                    ("777.1", first.dedupe_key),
                )
                store.connection.commit()
                with self.assertRaisesRegex(
                    ValueError, "market event history row identity mismatch"
                ):
                    store.append_batch_accepted((first,))
            finally:
                store.close()

if __name__ == "__main__":
    unittest.main()
