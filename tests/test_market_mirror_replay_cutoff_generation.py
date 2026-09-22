from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import autosport.storage as storage_module
from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.monotonic_workspace_authority import (
    MonotonicAuthorityRollbackError,
    MonotonicWorkspaceAuthority,
)
from autosport.storage import SQLiteMarketStore


class MarketMirrorReplayCutoffGenerationTests(unittest.TestCase):
    CUTOFF = datetime(2026, 9, 16, 19, 0, 1, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self._authority_directory = tempfile.TemporaryDirectory()
        self._authority_env = patch.dict(
            os.environ,
            {
                "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT": str(
                    Path(self._authority_directory.name) / "machine-authority"
                )
            },
        )
        self._authority_env.start()

    def tearDown(self) -> None:
        self._authority_env.stop()
        self._authority_directory.cleanup()

    @staticmethod
    def event(
        *,
        sequence: int,
        odds: str,
        observed_ts: str,
        ingest_ts: str | None = None,
        source_ts: str | None = None,
    ) -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            source_id="provider-a",
            sequence=sequence,
            status="open",
            source_ts=source_ts or observed_ts,
            ingest_ts=ingest_ts or observed_ts,
        )

    @classmethod
    def replay(
        cls,
        store: SQLiteMarketStore,
        *,
        as_of: datetime | None = None,
    ):
        return MarketMirror.replay_view_from_store(
            store,
            as_of=as_of or cls.CUTOFF,
            max_age=timedelta(minutes=2),
        )

    @staticmethod
    def semantic_events(snapshot) -> tuple[dict[str, object], ...]:
        return tuple(event.to_dict() for event in snapshot.events)

    def test_late_backdated_append_cannot_rewrite_frozen_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                frozen = self.replay(store)
                expected = self.semantic_events(frozen)

                store.append(
                    self.event(
                        sequence=2,
                        odds="9.99",
                        observed_ts="2026-09-16T18:59:59+00:00",
                        ingest_ts="2026-09-16T18:59:59+00:00",
                    )
                )

                repeated = self.replay(store)
                self.assertEqual(self.semantic_events(repeated), expected)
                self.assertEqual(len(store.events()), 2)
            finally:
                store.close()

    def test_restart_re_resolves_original_frozen_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                expected = self.semantic_events(self.replay(store))
                store.append(
                    self.event(
                        sequence=2,
                        odds="7.77",
                        observed_ts="2026-09-16T18:59:58+00:00",
                        ingest_ts="2026-09-16T18:59:58+00:00",
                    )
                )
            finally:
                store.close()

            reopened = SQLiteMarketStore(path)
            try:
                self.assertEqual(
                    self.semantic_events(self.replay(reopened)),
                    expected,
                )
                self.assertEqual(len(reopened.events()), 2)
            finally:
                reopened.close()

    def test_equivalent_timezone_instant_reuses_same_frozen_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                expected = self.semantic_events(self.replay(store))
                store.append(
                    self.event(
                        sequence=2,
                        odds="8.88",
                        observed_ts="2026-09-16T18:59:57+00:00",
                        ingest_ts="2026-09-16T18:59:57+00:00",
                    )
                )

                equivalent = self.CUTOFF.astimezone(
                    timezone(timedelta(hours=2))
                )
                self.assertEqual(
                    self.semantic_events(self.replay(store, as_of=equivalent)),
                    expected,
                )
            finally:
                store.close()

    def test_new_later_cutoff_can_observe_later_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                first = self.replay(store)
                self.assertEqual(first.events[0].sequence, 1)

                store.append(
                    self.event(
                        sequence=2,
                        odds="3.00",
                        observed_ts="2026-09-16T18:59:59+00:00",
                        ingest_ts="2026-09-16T18:59:59+00:00",
                    )
                )
                self.assertEqual(self.replay(store).events[0].sequence, 1)

                later = self.replay(
                    store,
                    as_of=self.CUTOFF + timedelta(microseconds=1),
                )
                self.assertEqual(len(later.events), 1)
                self.assertEqual(later.events[0].sequence, 2)
                self.assertEqual(later.events[0].decimal_odds, Decimal("3.00"))
            finally:
                store.close()

    def test_replay_decode_does_not_hold_sqlite_writer_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            first = SQLiteMarketStore(path)
            second = SQLiteMarketStore(path)
            replay_errors: list[BaseException] = []
            writer_errors: list[BaseException] = []
            decode_entered = threading.Event()
            release_decode = threading.Event()
            writer_done = threading.Event()
            try:
                first.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                # Issue the durable cutoff before the concurrency check. The
                # second resolution should need no writer transaction at all.
                self.replay(first)
                original_decoder = storage_module._event_from_history_row

                def blocking_decoder(row):
                    decode_entered.set()
                    if not release_decode.wait(timeout=10):
                        raise RuntimeError("test decoder release timeout")
                    return original_decoder(row)

                def resolve_existing_cutoff() -> None:
                    try:
                        self.replay(first)
                    except BaseException as exc:  # pragma: no cover - thread handoff
                        replay_errors.append(exc)

                def append_from_second_connection() -> None:
                    try:
                        second.append(
                            self.event(
                                sequence=2,
                                odds="2.10",
                                observed_ts="2026-09-16T19:00:02+00:00",
                            )
                        )
                    except BaseException as exc:  # pragma: no cover - thread handoff
                        writer_errors.append(exc)
                    finally:
                        writer_done.set()

                with patch.object(
                    storage_module,
                    "_event_from_history_row",
                    side_effect=blocking_decoder,
                ):
                    replay_thread = threading.Thread(target=resolve_existing_cutoff)
                    replay_thread.start()
                    self.assertTrue(
                        decode_entered.wait(timeout=10),
                        "replay never reached history decoding",
                    )

                    writer_thread = threading.Thread(
                        target=append_from_second_connection
                    )
                    writer_thread.start()
                    try:
                        self.assertTrue(
                            writer_done.wait(timeout=3),
                            "replay decoding held a SQLite writer transaction",
                        )
                    finally:
                        release_decode.set()
                    writer_thread.join(timeout=10)
                    replay_thread.join(timeout=10)

                self.assertFalse(writer_thread.is_alive())
                self.assertFalse(replay_thread.is_alive())
                self.assertEqual(writer_errors, [])
                self.assertEqual(replay_errors, [])
                self.assertEqual(len(first.events()), 2)
            finally:
                release_decode.set()
                second.close()
                first.close()

    def test_duplicate_provider_sequence_preserves_first_append_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                source_time = "2026-09-16T18:59:55+00:00"
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                    ingest_ts="2026-09-16T19:00:00+00:00",
                    source_ts=source_time,
                )
                retry = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:05+00:00",
                    ingest_ts="2026-09-16T19:00:06+00:00",
                    source_ts=source_time,
                )

                self.assertTrue(store.append(first))
                self.assertFalse(store.append(retry))
                rows = store.connection.execute(
                    "SELECT dedupe_key, append_generation "
                    "FROM market_event_commit_order"
                ).fetchall()
                self.assertEqual(rows, [(first.dedupe_key, 1)])
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()

    def test_second_store_append_cannot_rewrite_first_store_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            first = SQLiteMarketStore(path)
            second = SQLiteMarketStore(path)
            try:
                first.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                expected = self.semantic_events(self.replay(first))

                second.append(
                    self.event(
                        sequence=2,
                        odds="6.66",
                        observed_ts="2026-09-16T18:59:56+00:00",
                        ingest_ts="2026-09-16T18:59:56+00:00",
                    )
                )

                self.assertEqual(self.semantic_events(self.replay(first)), expected)
                self.assertEqual(len(first.events()), 2)
                generations = first.connection.execute(
                    "SELECT append_generation FROM market_event_commit_order "
                    "ORDER BY append_generation"
                ).fetchall()
                self.assertEqual(generations, [(1,), (2,)])
            finally:
                second.close()
                first.close()

    def test_pre_v1_history_migrates_to_generation_zero_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            original = SQLiteMarketStore(path)
            try:
                original.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
            finally:
                original.close()

            # Simulate the exact pre-v1 durable shape by removing only the new
            # causal companion objects while retaining canonical history/current.
            raw = sqlite3.connect(path)
            try:
                raw.execute("DROP TABLE market_replay_cutoffs")
                raw.execute("DROP TABLE market_event_commit_order")
                raw.commit()
            finally:
                raw.close()

            migrated = SQLiteMarketStore(path)
            try:
                legacy_rows = migrated.connection.execute(
                    "SELECT append_generation FROM market_event_commit_order"
                ).fetchall()
                self.assertEqual(legacy_rows, [(0,)])

                migrated.append(
                    self.event(
                        sequence=2,
                        odds="2.20",
                        observed_ts="2026-09-16T19:00:02+00:00",
                    )
                )
                generations = migrated.connection.execute(
                    "SELECT append_generation FROM market_event_commit_order "
                    "ORDER BY append_generation, dedupe_key"
                ).fetchall()
                self.assertEqual(generations, [(0,), (1,)])

                index_terms = migrated.connection.execute(
                    'PRAGMA index_xinfo("idx_market_event_commit_generation")'
                ).fetchall()
                key_terms = tuple(
                    row[2] for row in index_terms if len(row) >= 6 and row[5] == 1
                )
                self.assertEqual(key_terms, ("append_generation",))
            finally:
                migrated.close()

    def test_partial_causal_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            store.close()

            raw = sqlite3.connect(path)
            try:
                raw.execute("DROP TABLE market_replay_cutoffs")
                raw.commit()
            finally:
                raw.close()

            with self.assertRaises(ValueError):
                SQLiteMarketStore(path)

    def test_commit_generation_delete_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                event = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                store.append(event)

                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "market event append-generation rows are immutable",
                ):
                    store.connection.execute(
                        "DELETE FROM market_event_commit_order WHERE dedupe_key=?",
                        (event.dedupe_key,),
                    )
                store.connection.rollback()

                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_event_commit_order"
                    ).fetchone(),
                    (1,),
                )
            finally:
                store.close()

    def test_append_generation_swap_is_rejected_and_frozen_replay_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                second = self.event(
                    sequence=2,
                    odds="9.99",
                    observed_ts="2026-09-16T18:59:59+00:00",
                    ingest_ts="2026-09-16T18:59:59+00:00",
                )
                self.assertTrue(store.append(first))
                expected = self.semantic_events(self.replay(store))
                self.assertTrue(store.append(second))

                self.assertEqual(
                    store.connection.execute(
                        "SELECT append_generation FROM market_event_commit_order "
                        "ORDER BY append_generation"
                    ).fetchall(),
                    [(1,), (2,)],
                )

                # Without append-generation immutability this single statement leaves
                # the table unique and contiguous while swapping which event belongs
                # to the already-frozen generation-1 decision corpus.
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "market event append-generation rows are immutable",
                ):
                    store.connection.execute(
                        """UPDATE market_event_commit_order
                           SET append_generation = CASE append_generation
                               WHEN 1 THEN 2
                               WHEN 2 THEN 1
                               ELSE append_generation
                           END"""
                    )
                store.connection.rollback()

                self.assertEqual(self.semantic_events(self.replay(store)), expected)
            finally:
                store.close()

            reopened = SQLiteMarketStore(path)
            try:
                self.assertEqual(
                    self.semantic_events(self.replay(reopened)),
                    expected,
                )
            finally:
                reopened.close()

    def test_valid_range_cutoff_rollback_is_rejected_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                store.append(
                    self.event(
                        sequence=2,
                        odds="2.10",
                        observed_ts="2026-09-16T19:00:00.500000+00:00",
                    )
                )
                expected = self.semantic_events(self.replay(store))
                self.assertEqual(
                    store.connection.execute(
                        "SELECT max_append_generation FROM market_replay_cutoffs"
                    ).fetchone(),
                    (2,),
                )

                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "market replay cutoff rows are immutable",
                ):
                    store.connection.execute(
                        "UPDATE market_replay_cutoffs "
                        "SET max_append_generation=1"
                    )
                store.connection.rollback()

                self.assertEqual(
                    store.connection.execute(
                        "SELECT max_append_generation FROM market_replay_cutoffs"
                    ).fetchone(),
                    (2,),
                )
            finally:
                store.close()

            reopened = SQLiteMarketStore(path)
            try:
                self.assertEqual(
                    self.semantic_events(self.replay(reopened)),
                    expected,
                )
            finally:
                reopened.close()

    def test_issued_cutoff_delete_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                expected = self.semantic_events(self.replay(store))

                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "market replay cutoff rows are immutable",
                ):
                    store.connection.execute("DELETE FROM market_replay_cutoffs")
                store.connection.rollback()

                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (1,),
                )
                self.assertEqual(
                    self.semantic_events(self.replay(store)),
                    expected,
                )
            finally:
                store.close()

    def test_missing_commit_order_immutability_trigger_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                self.replay(store)
                store.connection.execute(
                    "DROP TRIGGER market_event_commit_order_no_update"
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    ValueError,
                    "immutable append-generation triggers mismatch",
                ):
                    self.replay(store)
            finally:
                store.close()

            with self.assertRaisesRegex(
                ValueError,
                "immutable append-generation triggers mismatch",
            ):
                SQLiteMarketStore(path)

    def test_missing_cutoff_immutability_trigger_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                self.replay(store)
                store.connection.execute(
                    "DROP TRIGGER market_replay_cutoffs_no_update"
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    ValueError,
                    "immutable cutoff triggers mismatch",
                ):
                    self.replay(store)
            finally:
                store.close()

            with self.assertRaisesRegex(
                ValueError,
                "immutable cutoff triggers mismatch",
            ):
                SQLiteMarketStore(path)


    def test_preinserted_cutoff_without_independent_issuance_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                store.append(
                    self.event(
                        sequence=2,
                        odds="2.10",
                        observed_ts="2026-09-16T19:00:00.500000+00:00",
                    )
                )
                canonical = storage_module._canonical_replay_cutoff(
                    self.CUTOFF.isoformat()
                )
                cutoff_id = storage_module._replay_cutoff_id(canonical)
                store.connection.execute(
                    """INSERT INTO market_replay_cutoffs
                       (cutoff_id, as_of, max_append_generation)
                       VALUES (?, ?, ?)""",
                    (cutoff_id, canonical, 1),
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "workspace has state but independent authority history is missing",
                ):
                    self.replay(store)
            finally:
                store.close()

            reopened = SQLiteMarketStore(path)
            try:
                with self.assertRaises(MonotonicAuthorityRollbackError):
                    self.replay(reopened)
            finally:
                reopened.close()

    def test_coherent_generation_swap_after_cutoff_is_rejected_by_corpus_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                second = self.event(
                    sequence=2,
                    odds="9.99",
                    observed_ts="2026-09-16T18:59:59+00:00",
                    ingest_ts="2026-09-16T18:59:59+00:00",
                )
                store.append(first)
                expected = self.semantic_events(self.replay(store))
                store.append(second)

                # Simulate a coherent same-DB DDL-capable rewrite: remove the guards,
                # change which event belongs to frozen generation 1, then restore the
                # exact canonical trigger SQL before replay validation runs.
                store.connection.execute(
                    "DROP TRIGGER market_event_commit_order_no_delete"
                )
                store.connection.execute(
                    "DROP TRIGGER market_event_commit_order_no_update"
                )
                store.connection.execute(
                    """UPDATE market_event_commit_order
                       SET append_generation = CASE append_generation
                           WHEN 1 THEN 2
                           WHEN 2 THEN 1
                           ELSE append_generation
                       END"""
                )
                for trigger_sql in (
                    storage_module._COMMIT_ORDER_IMMUTABILITY_TRIGGERS.values()
                ):
                    store.connection.execute(trigger_sql)
                store.connection.commit()

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "lacks unique independent product issuance authority",
                ):
                    self.replay(store)
                self.assertNotEqual(
                    store.connection.execute(
                        """SELECT dedupe_key
                           FROM market_event_commit_order
                           WHERE append_generation=1"""
                    ).fetchone(),
                    (first.dedupe_key,),
                )
                self.assertEqual(len(expected), 1)
            finally:
                store.close()

    def test_cutoff_issuance_recovers_after_sqlite_commit_before_authority_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                original_commit = MonotonicWorkspaceAuthority.commit
                calls = 0

                def fail_first_commit(authority, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise RuntimeError("simulated post-SQLite authority commit crash")
                    return original_commit(authority, **kwargs)

                with patch.object(
                    MonotonicWorkspaceAuthority,
                    "commit",
                    new=fail_first_commit,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "simulated post-SQLite authority commit crash",
                    ):
                        self.replay(store)

                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (1,),
                )
                recovered = self.replay(store)
                self.assertEqual(len(recovered.events), 1)
                self.assertEqual(recovered.events[0].sequence, 1)
            finally:
                store.close()

    def test_direct_cutoff_insert_after_authority_history_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                self.replay(store)

                forged_as_of = (
                    self.CUTOFF + timedelta(microseconds=1)
                ).astimezone(timezone.utc).isoformat()
                forged_id = storage_module._replay_cutoff_id(forged_as_of)
                store.connection.execute(
                    """INSERT INTO market_replay_cutoffs
                       (cutoff_id, as_of, max_append_generation)
                       VALUES (?, ?, ?)""",
                    (forged_id, forged_as_of, 1),
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "workspace state is missing, rolled back, or unproven",
                ):
                    self.replay(store)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
