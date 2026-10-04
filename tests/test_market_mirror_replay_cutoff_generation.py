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
from autosport.workspace_lock import WorkspaceEconomicLockBusyError


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

    @staticmethod
    def direct_insert_positive_generation(
        store: SQLiteMarketStore,
        event: MarketEvent,
        *,
        generation: int,
    ) -> None:
        payload = storage_module._canonical_payload(event)
        store.connection.execute(
            """INSERT INTO market_events
               (dedupe_key,quote_key,event_id,market_id,selection_id,decimal_odds,
                observed_ts,source_id,sequence,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                event.dedupe_key,
                event.quote_key,
                event.event_id,
                event.market_id,
                event.selection_id,
                str(event.decimal_odds),
                event.observed_ts,
                event.source_id,
                event.sequence,
                payload,
            ),
        )
        store.connection.execute(
            """INSERT INTO market_event_commit_order
               (dedupe_key, append_generation)
               VALUES (?, ?)""",
            (event.dedupe_key, generation),
        )
        store.connection.commit()

    def test_generation_zero_baseline_recovers_after_prepare_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            original_recover = MonotonicWorkspaceAuthority.recover
            calls = 0

            def fail_first_recover(authority, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("simulated baseline authority crash")
                return original_recover(authority, **kwargs)

            with patch.object(
                MonotonicWorkspaceAuthority,
                "recover",
                new=fail_first_recover,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated baseline authority crash",
                ):
                    SQLiteMarketStore(path)

            recovered = SQLiteMarketStore(path)
            try:
                history = recovered._market_append_authority().read_history()
                self.assertEqual(history[-1].phase.value, "COMMIT")
                self.assertRegex(
                    history[-1].tx_id,
                    r"^baseline-[0-9a-f]{32}$",
                )
                self.assertTrue(
                    recovered.append(
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T19:00:00+00:00",
                        )
                    )
                )
                self.assertEqual(len(self.replay(recovered).events), 1)
            finally:
                recovered.close()

    def test_relative_database_path_keeps_cutoff_authority_bound_to_opened_workspace(self) -> None:
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as opened_directory, tempfile.TemporaryDirectory() as later_directory:
            try:
                os.chdir(opened_directory)
                expected_path = (Path(opened_directory) / "market.db").absolute()
                store = SQLiteMarketStore("market.db")
                try:
                    store.append(
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T19:00:00+00:00",
                        )
                    )
                    os.chdir(later_directory)

                    self.assertEqual(store.path, expected_path)
                    authority = store._replay_cutoff_authority()
                    self.assertEqual(authority.workspace, expected_path.parent)
                    self.assertEqual(len(self.replay(store).events), 1)
                finally:
                    store.close()
            finally:
                os.chdir(original_cwd)

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
            legacy_event = self.event(
                sequence=1,
                odds="2.00",
                observed_ts="2026-09-16T19:00:00+00:00",
            )
            payload = storage_module._canonical_payload(legacy_event)

            # Construct the exact pre-causal-companion durable shape without ever
            # activating the new independent machine authority.
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
                        legacy_event.dedupe_key,
                        legacy_event.quote_key,
                        legacy_event.event_id,
                        legacy_event.market_id,
                        legacy_event.selection_id,
                        str(legacy_event.decimal_odds),
                        legacy_event.observed_ts,
                        legacy_event.source_id,
                        legacy_event.sequence,
                        payload,
                    ),
                )
                raw.execute(
                    """INSERT INTO current_quotes
                       (source_id,quote_key,observed_ts,sequence,payload_json)
                       VALUES (?,?,?,?,?)""",
                    (
                        legacy_event.source_id,
                        legacy_event.quote_key,
                        legacy_event.observed_ts,
                        legacy_event.sequence,
                        payload,
                    ),
                )
                raw.commit()
            finally:
                raw.close()

            migrated = SQLiteMarketStore(path)
            try:
                legacy_rows = migrated.connection.execute(
                    "SELECT append_generation FROM market_event_commit_order"
                ).fetchall()
                self.assertEqual(legacy_rows, [(0,)])

                baseline_history = migrated._market_append_authority().read_history()
                baseline_commits = [
                    record
                    for record in baseline_history
                    if record.phase.value == "COMMIT"
                ]
                self.assertEqual(len(baseline_commits), 1)
                self.assertRegex(
                    baseline_commits[0].tx_id,
                    r"^baseline-[0-9a-f]{32}$",
                )

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
                    "workspace state is missing, rolled back, or unproven",
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

    def test_tampered_frozen_corpus_cannot_mint_a_later_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                second = self.event(
                    sequence=2,
                    odds="8.88",
                    observed_ts="2026-09-16T18:59:59+00:00",
                    ingest_ts="2026-09-16T18:59:59+00:00",
                )
                store.append(first)
                self.replay(store)
                store.append(second)

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
                    "workspace state is missing, rolled back, or unproven",
                ):
                    self.replay(
                        store,
                        as_of=self.CUTOFF + timedelta(microseconds=1),
                    )

                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (1,),
                )
            finally:
                store.close()

    def test_verified_cutoff_rows_cannot_change_between_binding_check_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            external = sqlite3.connect(path)
            external.execute("PRAGMA journal_mode=WAL")
            try:
                original = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                tampered = self.event(
                    sequence=1,
                    odds="9.99",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                store.append(original)
                expected = self.semantic_events(self.replay(store))
                original_require = (
                    SQLiteMarketStore._require_independent_cutoff_issuance
                )
                tampered_once = False

                def mutate_after_binding_check(
                    authority,
                    *,
                    expected_binding_sha256: str,
                ) -> None:
                    nonlocal tampered_once
                    original_require(
                        authority,
                        expected_binding_sha256=expected_binding_sha256,
                    )
                    if tampered_once:
                        return
                    external.execute(
                        """UPDATE market_events
                           SET decimal_odds=?, payload_json=?
                           WHERE dedupe_key=?""",
                        (
                            str(tampered.decimal_odds),
                            storage_module._canonical_payload(tampered),
                            original.dedupe_key,
                        ),
                    )
                    external.commit()
                    tampered_once = True

                with patch.object(
                    SQLiteMarketStore,
                    "_require_independent_cutoff_issuance",
                    new=staticmethod(mutate_after_binding_check),
                ):
                    repeated = self.replay(store)

                self.assertTrue(tampered_once)
                self.assertEqual(self.semantic_events(repeated), expected)
                self.assertEqual(
                    external.execute(
                        "SELECT decimal_odds FROM market_events WHERE dedupe_key=?",
                        (original.dedupe_key,),
                    ).fetchone(),
                    ("9.99",),
                )
                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "workspace state is missing, rolled back, or unproven",
                ):
                    self.replay(store)
            finally:
                external.close()
                store.close()

    def test_submicrosecond_local_timestamp_cannot_round_back_into_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                event = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00.0000004+00:00",
                    ingest_ts="2026-09-16T19:00:00.0000004+00:00",
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "observed_ts precision finer than microseconds is unsupported",
                ):
                    store.append(event)

                self.assertEqual(store.events(), [])
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_event_commit_order"
                    ).fetchone(),
                    (0,),
                )
            finally:
                store.close()

    def test_submicrosecond_source_time_cannot_round_future_evidence_to_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                        ingest_ts="2026-09-16T19:00:00+00:00",
                        source_ts="2026-09-16T19:00:01.0000004+00:00",
                    )
                )

                snapshot = self.replay(store)
                self.assertEqual(snapshot.events, ())
            finally:
                store.close()

    def test_legacy_submicrosecond_local_clock_is_not_blessed_by_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                original = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                tampered = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00.0000004+00:00",
                    ingest_ts="2026-09-16T19:00:00.0000004+00:00",
                )
                store.append(original)
                store.connection.execute(
                    """UPDATE market_events
                       SET observed_ts=?, payload_json=?
                       WHERE dedupe_key=?""",
                    (
                        tampered.observed_ts,
                        storage_module._canonical_payload(tampered),
                        original.dedupe_key,
                    ),
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    ValueError,
                    "observed_ts precision finer than microseconds is unsupported",
                ):
                    self.replay(store)

                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (0,),
                )
                self.assertEqual(
                    store._replay_cutoff_authority().read_history(),
                    (),
                )
            finally:
                store.close()

    def test_zero_only_submicrosecond_tail_is_exact_and_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                self.assertTrue(
                    store.append(
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T19:00:00.123456000+00:00",
                            ingest_ts="2026-09-16T19:00:00.123456000+00:00",
                        )
                    )
                )
                snapshot = self.replay(store)
                self.assertEqual(len(snapshot.events), 1)
                self.assertEqual(snapshot.events[0].sequence, 1)
            finally:
                store.close()

    def test_invalid_selector_does_not_issue_durable_cutoff(self) -> None:
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

                with self.assertRaisesRegex(
                    ValueError,
                    "source_ids entries must be non-empty strings",
                ):
                    MarketMirror.replay_view_from_store(
                        store,
                        as_of=self.CUTOFF,
                        max_age=timedelta(minutes=2),
                        source_ids=("provider-a", ""),
                    )

                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (0,),
                )
                self.assertEqual(
                    store._replay_cutoff_authority().read_history(),
                    (),
                )
            finally:
                store.close()

    def test_one_shot_selector_is_preserved_after_prevalidation(self) -> None:
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
                source_ids = (source_id for source_id in ("provider-a",))

                snapshot = MarketMirror.replay_view_from_store(
                    store,
                    as_of=self.CUTOFF,
                    max_age=timedelta(minutes=2),
                    source_ids=source_ids,
                )

                self.assertEqual(len(snapshot.events), 1)
                self.assertEqual(snapshot.events[0].source_id, "provider-a")
            finally:
                store.close()

    def test_malformed_history_is_not_blessed_by_first_cutoff_issuance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                event = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                store.append(event)
                store.connection.execute(
                    "UPDATE market_events SET payload_json=? WHERE dedupe_key=?",
                    ('{"broken":', event.dedupe_key),
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    ValueError,
                    "stored market event payload must be valid JSON",
                ):
                    self.replay(store)

                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (0,),
                )
                self.assertEqual(
                    store._replay_cutoff_authority().read_history(),
                    (),
                )
            finally:
                store.close()

    def test_direct_generation_zero_insert_after_activation_cannot_be_blessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            forged = self.event(
                sequence=1,
                odds="9.99",
                observed_ts="2026-09-16T19:00:00+00:00",
            )
            try:
                payload = storage_module._canonical_payload(forged)
                store.connection.execute(
                    """INSERT INTO market_events
                       (dedupe_key,quote_key,event_id,market_id,selection_id,
                        decimal_odds,observed_ts,source_id,sequence,payload_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        forged.dedupe_key,
                        forged.quote_key,
                        forged.event_id,
                        forged.market_id,
                        forged.selection_id,
                        str(forged.decimal_odds),
                        forged.observed_ts,
                        forged.source_id,
                        forged.sequence,
                        payload,
                    ),
                )
                store.connection.execute(
                    """INSERT INTO market_event_commit_order
                       (dedupe_key, append_generation)
                       VALUES (?, 0)""",
                    (forged.dedupe_key,),
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "positive market append chronology is missing, forged, or unproven",
                ):
                    self.replay(store)

                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (0,),
                )
            finally:
                store.close()

            with self.assertRaisesRegex(
                MonotonicAuthorityRollbackError,
                "generation-zero market baseline is missing, changed, or unproven",
            ):
                SQLiteMarketStore(path)

    def test_direct_first_positive_generation_cannot_be_blessed_by_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                forged = self.event(
                    sequence=1,
                    odds="9.99",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.direct_insert_positive_generation(
                    store,
                    forged,
                    generation=1,
                )

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "positive market append chronology is missing, forged, or unproven",
                ):
                    self.replay(store)

                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (0,),
                )
                self.assertEqual(
                    store._replay_cutoff_authority().read_history(),
                    (),
                )
            finally:
                store.close()

            with self.assertRaisesRegex(
                MonotonicAuthorityRollbackError,
                "positive market append chronology is missing, forged, or unproven",
            ):
                SQLiteMarketStore(path)

    def test_direct_contiguous_tail_after_product_append_cannot_be_blessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                product_event = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:58+00:00",
                )
                forged = self.event(
                    sequence=2,
                    odds="9.99",
                    observed_ts="2026-09-16T18:59:59+00:00",
                    ingest_ts="2026-09-16T18:59:59+00:00",
                )
                self.assertTrue(store.append(product_event))
                self.direct_insert_positive_generation(
                    store,
                    forged,
                    generation=2,
                )

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "positive market append chronology is missing, forged, or unproven",
                ):
                    self.replay(store)

                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (0,),
                )
            finally:
                store.close()

    def test_same_head_corpus_rewrite_cannot_be_blessed_by_first_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                original = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                tampered = self.event(
                    sequence=1,
                    odds="9.99",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertTrue(store.append(original))
                store.connection.execute(
                    """UPDATE market_events
                       SET decimal_odds=?, payload_json=?
                       WHERE dedupe_key=?""",
                    (
                        str(tampered.decimal_odds),
                        storage_module._canonical_payload(tampered),
                        original.dedupe_key,
                    ),
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "positive market append chronology is missing, forged, or unproven",
                ):
                    self.replay(store)

                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (0,),
                )
            finally:
                store.close()

    def test_product_positive_append_authority_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                self.assertTrue(
                    store.append(
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T19:00:00+00:00",
                        )
                    )
                )
                history = store._market_append_authority().read_history()
                self.assertEqual(
                    tuple(record.phase.value for record in history),
                    ("PREPARE", "COMMIT", "PREPARE", "COMMIT"),
                )
            finally:
                store.close()

            reopened = SQLiteMarketStore(path)
            try:
                snapshot = self.replay(reopened)
                self.assertEqual(len(snapshot.events), 1)
                self.assertEqual(snapshot.events[0].sequence, 1)
            finally:
                reopened.close()

    def test_product_append_batch_issues_one_contiguous_machine_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:58+00:00",
                )
                second = self.event(
                    sequence=2,
                    odds="2.10",
                    observed_ts="2026-09-16T18:59:59+00:00",
                )
                accepted = store.append_batch_accepted((first, second))
                self.assertEqual(accepted, [first, second])
                self.assertEqual(
                    store.connection.execute(
                        """SELECT append_generation
                           FROM market_event_commit_order
                           ORDER BY append_generation"""
                    ).fetchall(),
                    [(1,), (2,)],
                )
                history = store._market_append_authority().read_history()
                commits = [
                    record
                    for record in history
                    if record.phase.value == "COMMIT"
                    and record.tx_id.startswith("append-")
                ]
                self.assertEqual(len(commits), 1)
                self.assertRegex(
                    commits[0].tx_id,
                    r"^append-1-2-[0-9a-f]{32}$",
                )
                self.assertEqual(len(self.replay(store).events), 1)
                self.assertEqual(self.replay(store).events[0].sequence, 2)
            finally:
                store.close()

    def test_append_authority_recovers_after_sqlite_commit_before_machine_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            original_recover = MonotonicWorkspaceAuthority.recover
            calls = 0

            def fail_first_recover(authority, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("simulated append-authority post-SQLite crash")
                return original_recover(authority, **kwargs)

            try:
                with patch.object(
                    MonotonicWorkspaceAuthority,
                    "recover",
                    new=fail_first_recover,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "simulated append-authority post-SQLite crash",
                    ):
                        store.append(
                            self.event(
                                sequence=1,
                                odds="2.00",
                                observed_ts="2026-09-16T19:00:00+00:00",
                            )
                        )

                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()

            reopened = SQLiteMarketStore(path)
            try:
                snapshot = self.replay(reopened)
                self.assertEqual(len(snapshot.events), 1)
                history = reopened._market_append_authority().read_history()
                self.assertEqual(history[-1].phase.value, "COMMIT")
            finally:
                reopened.close()

    def test_restart_during_live_append_issuance_does_not_recover_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            first = SQLiteMarketStore(path)
            authority = first._market_append_authority()
            try:
                before = authority.read_history()
                with first._market_append_issuance_lock(authority):
                    with self.assertRaises(WorkspaceEconomicLockBusyError):
                        SQLiteMarketStore(path)
                after = authority.read_history()
                self.assertEqual(after, before)
                self.assertEqual(first.events(), [])
            finally:
                first.close()

    def test_parallel_store_append_lock_contention_has_no_partial_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            first = SQLiteMarketStore(path)
            second = SQLiteMarketStore(path)
            authority = first._market_append_authority()
            try:
                with first._market_append_issuance_lock(authority):
                    with self.assertRaises(WorkspaceEconomicLockBusyError):
                        second.append(
                            self.event(
                                sequence=1,
                                odds="2.00",
                                observed_ts="2026-09-16T19:00:00+00:00",
                            )
                        )

                self.assertEqual(first.events(), [])
                self.assertEqual(
                    first.connection.execute(
                        "SELECT COUNT(*) FROM market_event_commit_order"
                    ).fetchone(),
                    (0,),
                )
                history = first._market_append_authority().read_history()
                self.assertEqual(
                    tuple(record.tx_id.startswith("baseline-") for record in history),
                    (True, True),
                )
            finally:
                second.close()
                first.close()

    def test_duplicate_retry_does_not_advance_positive_machine_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                event = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertTrue(store.append(event))
                before = store._market_append_authority().read_history()
                self.assertFalse(store.append(event))
                after = store._market_append_authority().read_history()
                self.assertEqual(after, before)
            finally:
                store.close()

    def test_append_rejects_post_startup_current_projection_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:58+00:00",
                )
                self.assertTrue(store.append(first))
                before = store._market_append_authority().read_history()
                store.connection.execute(
                    """CREATE TRIGGER forged_current_projection
                       AFTER UPDATE ON current_quotes
                       BEGIN
                           DELETE FROM current_quotes
                           WHERE source_id = NEW.source_id
                             AND quote_key = NEW.quote_key;
                       END"""
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    ValueError,
                    "current_quotes schema is not canonical: triggers are not allowed",
                ):
                    store.append(
                        self.event(
                            sequence=2,
                            odds="2.10",
                            observed_ts="2026-09-16T19:00:00+00:00",
                        )
                    )

                self.assertEqual(store._market_append_authority().read_history(), before)
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_events"
                    ).fetchone(),
                    (1,),
                )
            finally:
                store.close()

    def test_append_rejects_post_startup_history_trigger_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:58+00:00",
                )
                self.assertTrue(store.append(first))
                before = store._market_append_authority().read_history()
                store.connection.execute(
                    """CREATE TRIGGER forged_market_history
                       AFTER INSERT ON market_events
                       BEGIN
                           INSERT INTO market_events
                               (dedupe_key,quote_key,event_id,market_id,selection_id,
                                decimal_odds,observed_ts,source_id,sequence,payload_json)
                           VALUES
                               (NEW.dedupe_key || '-ghost','ghost|market|selection',
                                NEW.event_id,NEW.market_id,NEW.selection_id,
                                NEW.decimal_odds,NEW.observed_ts,'ghost-provider',
                                NEW.sequence,NEW.payload_json);
                       END"""
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    ValueError,
                    "market_events schema is not canonical: triggers are not allowed",
                ):
                    store.append(
                        self.event(
                            sequence=2,
                            odds="2.10",
                            observed_ts="2026-09-16T19:00:00+00:00",
                        )
                    )

                self.assertEqual(store._market_append_authority().read_history(), before)
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_events"
                    ).fetchone(),
                    (1,),
                )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_event_commit_order"
                    ).fetchone(),
                    (1,),
                )
            finally:
                store.close()

    def test_new_append_repairs_forged_current_projection_from_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:58+00:00",
                )
                self.assertTrue(store.append(first))
                forged = self.event(
                    sequence=999,
                    odds="99.00",
                    observed_ts="2026-09-16T18:59:59+00:00",
                )
                store.connection.execute(
                    """UPDATE current_quotes
                       SET observed_ts=?, sequence=?, payload_json=?
                       WHERE source_id=? AND quote_key=?""",
                    (
                        forged.observed_ts,
                        forged.sequence,
                        storage_module._canonical_payload(forged),
                        first.source_id,
                        first.quote_key,
                    ),
                )
                store.connection.commit()

                second = self.event(
                    sequence=2,
                    odds="2.10",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertTrue(store.append(second))
                current = store.current_by_source()[(second.source_id, second.quote_key)]
                self.assertEqual(current.sequence, 2)
                self.assertEqual(current.decimal_odds, Decimal("2.10"))
                self.assertEqual(current.dedupe_key, second.dedupe_key)
            finally:
                store.close()

    def test_duplicate_retry_repairs_forged_current_projection_from_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                event = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertTrue(store.append(event))
                forged = self.event(
                    sequence=999,
                    odds="99.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                store.connection.execute(
                    """UPDATE current_quotes
                       SET observed_ts=?, sequence=?, payload_json=?
                       WHERE source_id=? AND quote_key=?""",
                    (
                        forged.observed_ts,
                        forged.sequence,
                        storage_module._canonical_payload(forged),
                        event.source_id,
                        event.quote_key,
                    ),
                )
                store.connection.commit()

                before = store._market_append_authority().read_history()
                self.assertFalse(store.append(event))
                after = store._market_append_authority().read_history()
                self.assertEqual(after, before)
                current = store.current_by_source()[(event.source_id, event.quote_key)]
                self.assertEqual(current.sequence, 1)
                self.assertEqual(current.decimal_odds, Decimal("2.00"))
                self.assertEqual(current.dedupe_key, event.dedupe_key)
            finally:
                store.close()

    def test_coherent_positive_history_rewrite_blocks_duplicate_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                original = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertTrue(store.append(original))
                forged = self.event(
                    sequence=1,
                    odds="9.99",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertEqual(forged.dedupe_key, original.dedupe_key)
                store.connection.execute(
                    """UPDATE market_events
                       SET decimal_odds=?, payload_json=?
                       WHERE dedupe_key=?""",
                    (
                        str(forged.decimal_odds),
                        storage_module._canonical_payload(forged),
                        original.dedupe_key,
                    ),
                )
                store.connection.commit()

                before = store._market_append_authority().read_history()
                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "positive market append chronology is missing, forged, or unproven",
                ):
                    store.append(forged)
                after = store._market_append_authority().read_history()
                self.assertEqual(after, before)
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_event_commit_order"
                    ).fetchone(),
                    (1,),
                )
            finally:
                store.close()

    def test_coherent_positive_history_rewrite_blocks_new_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                original = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:59+00:00",
                )
                self.assertTrue(store.append(original))
                forged = self.event(
                    sequence=1,
                    odds="9.99",
                    observed_ts="2026-09-16T18:59:59+00:00",
                )
                store.connection.execute(
                    """UPDATE market_events
                       SET decimal_odds=?, payload_json=?
                       WHERE dedupe_key=?""",
                    (
                        str(forged.decimal_odds),
                        storage_module._canonical_payload(forged),
                        original.dedupe_key,
                    ),
                )
                store.connection.commit()
                before = store._market_append_authority().read_history()

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "positive market append chronology is missing, forged, or unproven",
                ):
                    store.append(
                        self.event(
                            sequence=2,
                            odds="2.10",
                            observed_ts="2026-09-16T19:00:00+00:00",
                        )
                    )

                after = store._market_append_authority().read_history()
                self.assertEqual(after, before)
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_events"
                    ).fetchone(),
                    (1,),
                )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT MAX(append_generation) FROM market_event_commit_order"
                    ).fetchone(),
                    (1,),
                )
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
                original_recover = MonotonicWorkspaceAuthority.recover
                calls = 0

                def fail_after_sqlite_commit(authority, **kwargs):
                    nonlocal calls
                    calls += 1
                    # First recover: outer preflight. Second: under SQLite BEGIN
                    # IMMEDIATE before PREPARE. Third: SQLite row is committed and
                    # the machine authority still has the live PREPARE.
                    if calls == 3:
                        raise RuntimeError("simulated post-SQLite authority commit crash")
                    return original_recover(authority, **kwargs)

                with patch.object(
                    MonotonicWorkspaceAuthority,
                    "recover",
                    new=fail_after_sqlite_commit,
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
