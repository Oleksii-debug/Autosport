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
        self._clock_lock = threading.Lock()
        self._product_now = self.CUTOFF - timedelta(seconds=1)
        self._product_clock = patch.object(
            storage_module,
            "_market_product_utc_now",
            side_effect=self._read_product_clock,
        )
        self._product_clock.start()

    def _read_product_clock(self) -> str:
        with self._clock_lock:
            return self._product_now.isoformat()

    def tearDown(self) -> None:
        self._product_clock.stop()
        self._authority_env.stop()
        self._authority_directory.cleanup()

    def test_fresh_store_accepts_canonical_integer_rowid_primary_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.sqlite")
            try:
                for table_name in (
                    "market_append_commit_times",
                    "market_append_availability",
                ):
                    index_rows = store.connection.execute(
                        f"PRAGMA index_list({table_name})"
                    ).fetchall()
                    self.assertEqual(
                        [row for row in index_rows if len(row) >= 5 and row[3] == "pk"],
                        [],
                    )
                    storage_module._validate_canonical_table(
                        store.connection,
                        table_name,
                    )
            finally:
                store.close()

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

    def replay(
        self,
        store: SQLiteMarketStore,
        *,
        as_of: datetime | None = None,
    ):
        resolved_as_of = as_of or self.CUTOFF
        with self._clock_lock:
            if self._product_now < resolved_as_of:
                self._product_now = resolved_as_of
        try:
            return MarketMirror.replay_view_from_store(
                store,
                as_of=resolved_as_of,
                max_age=timedelta(minutes=2),
            )
        finally:
            # Make an append after a replay strictly later than that decision cut.
            with self._clock_lock:
                if self._product_now <= resolved_as_of:
                    self._product_now = resolved_as_of + timedelta(
                        microseconds=1
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

    @staticmethod
    def direct_issue_first_cutoff_authority(
        store: SQLiteMarketStore,
        *,
        as_of: datetime,
        max_generation: int,
        tx_id: str | None = None,
    ) -> MonotonicWorkspaceAuthority:
        canonical_as_of = storage_module._canonical_replay_cutoff(as_of.isoformat())
        cutoff_id = storage_module._replay_cutoff_id(canonical_as_of)
        issued_at = storage_module._canonical_product_time(
            canonical_as_of
        )
        availability_rows = store._validated_append_availability_rows(
            store._market_append_authority()
        )
        matching_availability = next(
            (
                row[3]
                for row in availability_rows
                if row[0] == max_generation
            ),
            availability_rows[0][3],
        )
        corpus_sha256 = store._frozen_replay_corpus_sha256(max_generation)
        binding_sha256 = storage_module._replay_cutoff_binding_sha256(
            cutoff_id=cutoff_id,
            canonical_as_of=canonical_as_of,
            max_append_generation=max_generation,
            append_available_at=matching_availability,
            issued_at=issued_at,
            corpus_sha256=corpus_sha256,
        )
        rows = ((cutoff_id, canonical_as_of, max_generation, issued_at),)
        intended_state_sha256 = storage_module._replay_cutoff_state_sha256(
            rows,
            sealed_corpus_sha256=corpus_sha256,
        )
        assert intended_state_sha256 is not None
        authority = store._replay_cutoff_authority()
        self_rows = store._validated_replay_cutoff_rows()
        if self_rows:
            raise AssertionError("direct first-cutoff helper requires an empty cutoff table")
        resolved_tx_id = tx_id or f"{cutoff_id[:32]}-{'0' * 32}"
        authority.prepare(
            tx_id=resolved_tx_id,
            observed_state_sha256=None,
            intended_state_sha256=intended_state_sha256,
            semantic_binding_sha256=binding_sha256,
        )
        store.connection.execute(
            """INSERT INTO market_replay_cutoffs
               (cutoff_id, as_of, max_append_generation, issued_at)
               VALUES (?, ?, ?, ?)""",
            (cutoff_id, canonical_as_of, max_generation, issued_at),
        )
        store.connection.commit()
        authority.recover(
            observed_state_sha256=intended_state_sha256,
            tx_id=resolved_tx_id,
            semantic_binding_sha256=binding_sha256,
        )
        return authority

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
                expected_path = (Path(opened_directory) / "market.db").resolve(strict=False)
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

    def test_database_file_symlink_reuses_canonical_authority_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary_path = Path(directory) / "market.db"
            first = SQLiteMarketStore(primary_path)
            try:
                self.assertTrue(
                    first.append(
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T19:00:00+00:00",
                        )
                    )
                )
                expected = self.semantic_events(self.replay(first))
                append_journal = first._market_append_authority().journal_dir
                cutoff_journal = first._replay_cutoff_authority().journal_dir
            finally:
                first.close()

            alias_path = Path(directory) / "market-alias.db"
            try:
                alias_path.symlink_to(primary_path.name)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"file symlinks unavailable on this platform: {exc}")

            reopened = SQLiteMarketStore(alias_path)
            try:
                self.assertEqual(reopened.path, primary_path.resolve(strict=False))
                self.assertEqual(
                    reopened._market_append_authority().journal_dir,
                    append_journal,
                )
                self.assertEqual(
                    reopened._replay_cutoff_authority().journal_dir,
                    cutoff_journal,
                )
                self.assertEqual(
                    self.semantic_events(self.replay(reopened)),
                    expected,
                )
            finally:
                reopened.close()

    def test_database_authority_key_folds_win32_case_and_trailing_aliases(self) -> None:
        mixed_case = Path("Odds.DB")
        trailing_alias = Path("ODDS.DB. ")
        with patch.object(storage_module.os, "name", "nt"):
            self.assertEqual(
                storage_module._database_authority_key(mixed_case),
                "odds.db",
            )
            self.assertEqual(
                storage_module._database_authority_key(trailing_alias),
                "odds.db",
            )

    def test_database_authority_key_preserves_posix_case_distinction(self) -> None:
        mixed_case = Path("Odds.DB")
        lower_case = Path("odds.db")
        with patch.object(storage_module.os, "name", "posix"):
            self.assertEqual(
                storage_module._database_authority_key(mixed_case),
                "Odds.DB",
            )
            self.assertEqual(
                storage_module._database_authority_key(lower_case),
                "odds.db",
            )

    @unittest.skipUnless(os.name == "nt", "Win32 case alias semantics required")
    def test_windows_case_alias_reuses_database_machine_authorities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary_path = Path(directory) / "Market.DB"
            first = SQLiteMarketStore(primary_path)
            try:
                append_journal = first._market_append_authority().journal_dir
                cutoff_journal = first._replay_cutoff_authority().journal_dir
            finally:
                first.close()

            reopened = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                self.assertEqual(
                    reopened._market_append_authority().journal_dir,
                    append_journal,
                )
                self.assertEqual(
                    reopened._replay_cutoff_authority().journal_dir,
                    cutoff_journal,
                )
            finally:
                reopened.close()

    def test_append_uses_same_global_lock_order_as_trusted_readers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            entered: list[str] = []

            class RecordingLock:
                def __init__(self, name: str) -> None:
                    self.name = name

                def __enter__(self):
                    entered.append(self.name)
                    return self

                def __exit__(self, exc_type, exc_value, traceback) -> None:
                    return None

            store._connection_lock = RecordingLock("connection")
            try:
                with patch.object(
                    SQLiteMarketStore,
                    "_market_append_issuance_lock",
                    return_value=RecordingLock("append-authority"),
                ):
                    self.assertTrue(
                        store.append(
                            self.event(
                                sequence=1,
                                odds="2.00",
                                observed_ts="2026-09-16T19:00:00+00:00",
                            )
                        )
                    )
                self.assertEqual(
                    entered[:2],
                    ["append-authority", "connection"],
                )
            finally:
                store.close()

    def test_restart_reproves_history_inside_projection_rebuild_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            original = self.event(
                sequence=1,
                odds="2.00",
                observed_ts="2026-09-16T19:00:00+00:00",
            )
            store = SQLiteMarketStore(path)
            try:
                self.assertTrue(store.append(original))
            finally:
                store.close()

            forged = self.event(
                sequence=1,
                odds="9.99",
                observed_ts="2026-09-16T19:00:00+00:00",
            )
            original_rebuild = SQLiteMarketStore._rebuild_current_quotes
            tampered = False

            def tamper_before_rebuild(instance, *, append_authority):
                nonlocal tampered
                if not tampered:
                    tampered = True
                    external = sqlite3.connect(path)
                    try:
                        external.execute(
                            """UPDATE market_events
                               SET decimal_odds=?, payload_json=?
                               WHERE dedupe_key=?""",
                            (
                                str(forged.decimal_odds),
                                storage_module._canonical_payload(forged),
                                original.dedupe_key,
                            ),
                        )
                        external.commit()
                    finally:
                        external.close()
                return original_rebuild(
                    instance,
                    append_authority=append_authority,
                )

            with patch.object(
                SQLiteMarketStore,
                "_rebuild_current_quotes",
                new=tamper_before_rebuild,
            ):
                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "positive market append authority semantic binding is invalid",
                ):
                    SQLiteMarketStore(path)
            self.assertTrue(tampered)

    def test_restart_rejects_projection_trigger_before_rebuild_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            event = self.event(
                sequence=1,
                odds="2.00",
                observed_ts="2026-09-16T19:00:00+00:00",
            )
            store = SQLiteMarketStore(path)
            try:
                self.assertTrue(store.append(event))
            finally:
                store.close()

            original_rebuild = SQLiteMarketStore._rebuild_current_quotes
            injected = False

            def inject_trigger_before_rebuild(instance, *, append_authority):
                nonlocal injected
                if not injected:
                    injected = True
                    external = sqlite3.connect(path)
                    try:
                        external.execute(
                            """CREATE TRIGGER startup_projection_side_effect
                               AFTER DELETE ON current_quotes
                               BEGIN
                                   DELETE FROM market_events;
                               END"""
                        )
                        external.commit()
                    finally:
                        external.close()
                return original_rebuild(
                    instance,
                    append_authority=append_authority,
                )

            with patch.object(
                SQLiteMarketStore,
                "_rebuild_current_quotes",
                new=inject_trigger_before_rebuild,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "current_quotes schema is not canonical: triggers are not allowed",
                ):
                    SQLiteMarketStore(path)

            self.assertTrue(injected)
            external = sqlite3.connect(path)
            try:
                self.assertEqual(
                    external.execute("SELECT COUNT(*) FROM market_events").fetchone(),
                    (1,),
                )
            finally:
                external.close()

    def test_restart_repairs_projection_row_missing_from_proven_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            event = self.event(
                sequence=1,
                odds="2.00",
                observed_ts="2026-09-16T19:00:00+00:00",
            )
            store = SQLiteMarketStore(path)
            try:
                self.assertTrue(store.append(event))
            finally:
                store.close()

            forged_raw = event.to_dict()
            forged_raw["selection_id"] = "selection-forged"
            forged_raw["sequence"] = 999
            forged = MarketEvent.from_dict(forged_raw)
            external = sqlite3.connect(path)
            try:
                external.execute(
                    """INSERT INTO current_quotes
                       (source_id, quote_key, observed_ts, sequence, payload_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        forged.source_id,
                        forged.quote_key,
                        forged.observed_ts,
                        forged.sequence,
                        storage_module._canonical_payload(forged),
                    ),
                )
                external.commit()
            finally:
                external.close()

            reopened = SQLiteMarketStore(path)
            try:
                current = reopened.current_by_source()
                self.assertEqual(set(current), {(event.source_id, event.quote_key)})
                self.assertEqual(current[(event.source_id, event.quote_key)], event)
                self.assertEqual(
                    reopened.connection.execute(
                        "SELECT COUNT(*) FROM current_quotes"
                    ).fetchone(),
                    (1,),
                )
            finally:
                reopened.close()

    def test_restart_does_not_parse_untrusted_old_projection_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            event = self.event(
                sequence=1,
                odds="2.00",
                observed_ts="2026-09-16T19:00:00+00:00",
            )
            store = SQLiteMarketStore(path)
            try:
                self.assertTrue(store.append(event))
            finally:
                store.close()

            pathological_json = "[" * 2000 + "0" + "]" * 2000
            external = sqlite3.connect(path)
            try:
                external.execute(
                    """UPDATE current_quotes
                       SET payload_json=?
                       WHERE source_id=? AND quote_key=?""",
                    (
                        pathological_json,
                        event.source_id,
                        event.quote_key,
                    ),
                )
                external.commit()
            finally:
                external.close()

            reopened = SQLiteMarketStore(path)
            try:
                current = reopened.current_by_source()
                self.assertEqual(
                    current[(event.source_id, event.quote_key)],
                    event,
                )
            finally:
                reopened.close()

    def test_append_rejects_temp_table_shadow_before_history_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:59+00:00",
                )
                self.assertTrue(store.append(first))
                before = store._market_append_authority().read_history()

                store.connection.execute(
                    """CREATE TEMP TABLE current_quotes (
                        source_id TEXT NOT NULL,
                        quote_key TEXT NOT NULL,
                        observed_ts TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (source_id, quote_key)
                    )"""
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "current_quotes schema is not canonical: temporary schema objects are not allowed",
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
                        "SELECT COUNT(*) FROM main.market_events"
                    ).fetchone(),
                    (1,),
                )
            finally:
                store.close()

    def test_append_rejects_temp_history_trigger_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:59+00:00",
                )
                self.assertTrue(store.append(first))
                before = store._market_append_authority().read_history()

                store.connection.execute(
                    """CREATE TEMP TRIGGER temp_history_side_effect
                       AFTER INSERT ON main.market_events
                       BEGIN
                           DELETE FROM current_quotes;
                       END"""
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "market_events schema is not canonical: temporary schema objects are not allowed",
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
                        "SELECT COUNT(*) FROM main.market_events"
                    ).fetchone(),
                    (1,),
                )
            finally:
                store.close()

    def test_replay_rejects_temp_history_shadow_even_when_bytes_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                event = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertTrue(store.append(event))
                expected = self.semantic_events(self.replay(store))

                store.connection.execute(
                    """CREATE TEMP TABLE market_events (
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
                store.connection.execute(
                    """INSERT INTO temp.market_events
                       SELECT * FROM main.market_events"""
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "market_events schema is not canonical: temporary schema objects are not allowed",
                ):
                    self.replay(store)

                self.assertEqual(
                    tuple(
                        row[0]
                        for row in store.connection.execute(
                            "SELECT payload_json FROM main.market_events"
                        ).fetchall()
                    ),
                    tuple(
                        storage_module._canonical_payload(item)
                        for item in (event,)
                    ),
                )
                self.assertEqual(len(expected), 1)
            finally:
                store.close()

    def test_hardlink_database_alias_is_rejected_before_authority_remint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary_path = Path(directory) / "market.db"
            store = SQLiteMarketStore(primary_path)
            store.close()

            alias_path = Path(directory) / "market-hardlink.db"
            try:
                os.link(primary_path, alias_path)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"hard links unavailable on this platform: {exc}")

            with self.assertRaisesRegex(
                ValueError,
                "market database pathname must be a single-link regular file",
            ):
                SQLiteMarketStore(alias_path)

    def test_open_store_rejects_database_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            moved_path = Path(directory) / "market-moved.db"
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
                try:
                    os.replace(path, moved_path)
                    path.touch()
                except OSError as exc:
                    self.skipTest(
                        f"open SQLite pathname replacement unavailable on this platform: {exc}"
                    )

                with self.assertRaisesRegex(
                    ValueError,
                    "market database pathname no longer identifies the opened database",
                ):
                    store.events()
            finally:
                store.close()

    def test_path_replacement_after_append_prepare_cannot_commit_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            moved_path = Path(directory) / "market-moved.db"
            store = SQLiteMarketStore(path)
            original_require = store._require_database_path_identity
            calls = 0

            def replace_before_commit():
                nonlocal calls
                calls += 1
                if calls == 2:
                    try:
                        os.replace(path, moved_path)
                        path.touch()
                    except OSError as exc:
                        self.skipTest(
                            f"open SQLite pathname replacement unavailable on this platform: {exc}"
                        )
                return original_require()

            try:
                authority = store._market_append_authority()
                before = authority.read_history()
                calls = 0
                with patch.object(
                    store,
                    "_require_database_path_identity",
                    new=replace_before_commit,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "market database pathname no longer identifies the opened database",
                    ):
                        store.append(
                            self.event(
                                sequence=1,
                                odds="2.00",
                                observed_ts="2026-09-16T19:00:00+00:00",
                            )
                        )

                after = authority.read_history()
                before_commits = [
                    record for record in before if record.phase.value == "COMMIT"
                ]
                after_commits = [
                    record for record in after if record.phase.value == "COMMIT"
                ]
                self.assertEqual(after_commits, before_commits)
            finally:
                store.close()

    def test_path_replacement_after_cutoff_prepare_cannot_commit_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            moved_path = Path(directory) / "market-moved.db"
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
                authority = store._replay_cutoff_authority()
                before = authority.read_history()
                original_commit = store._commit_stable_database_path
                replaced = False

                def replace_before_cutoff_commit():
                    nonlocal replaced
                    if not replaced:
                        replaced = True
                        try:
                            os.replace(path, moved_path)
                            path.touch()
                        except OSError as exc:
                            self.skipTest(
                                f"open SQLite pathname replacement unavailable on this platform: {exc}"
                            )
                    return original_commit()

                with patch.object(
                    store,
                    "_commit_stable_database_path",
                    new=replace_before_cutoff_commit,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "market database pathname no longer identifies the opened database",
                    ):
                        self.replay(store)

                self.assertTrue(replaced)
                after = authority.read_history()
                before_commits = [
                    record for record in before if record.phase.value == "COMMIT"
                ]
                after_commits = [
                    record for record in after if record.phase.value == "COMMIT"
                ]
                self.assertEqual(after_commits, before_commits)
            finally:
                store.close()

    def test_post_commit_path_failure_keeps_cutoff_prepare_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            moved_path = Path(directory) / "market-moved.db"
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
                authority = store._replay_cutoff_authority()
                original_require = store._require_database_path_identity
                replaced = False

                def commit_then_fail_postcheck():
                    nonlocal replaced
                    original_require()
                    store.connection.commit()
                    if not replaced:
                        replaced = True
                        try:
                            os.replace(path, moved_path)
                            path.touch()
                        except OSError as exc:
                            self.skipTest(
                                f"open SQLite pathname replacement unavailable on this platform: {exc}"
                            )
                    original_require()

                with patch.object(
                    store,
                    "_commit_stable_database_path",
                    new=commit_then_fail_postcheck,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "market database pathname no longer identifies the opened database",
                    ):
                        self.replay(store)

                self.assertTrue(replaced)
                self.assertEqual(authority.read_history()[-1].phase.value, "PREPARE")

                try:
                    path.unlink()
                    os.replace(moved_path, path)
                except OSError as exc:
                    self.skipTest(
                        f"database pathname restoration unavailable on this platform: {exc}"
                    )

                recovered = self.replay(store)
                self.assertEqual(len(recovered.events), 1)
                self.assertEqual(recovered.events[0].sequence, 1)
                self.assertEqual(authority.read_history()[-1].phase.value, "COMMIT")
            finally:
                store.close()

    def test_persist_and_apply_duplicate_uses_persisted_local_clocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                original = MarketEvent(
                    event_id="event-1",
                    market_id="market-1",
                    selection_id="selection-1",
                    decimal_odds=Decimal("2.00"),
                    observed_ts="2026-09-16T18:59:59+00:00",
                    ingest_ts="2026-09-16T18:59:59+00:00",
                    source_id="provider-a",
                    sequence=1,
                    status="open",
                    source_ts=None,
                )
                retry = MarketEvent(
                    event_id="event-1",
                    market_id="market-1",
                    selection_id="selection-1",
                    decimal_odds=Decimal("2.00"),
                    observed_ts="2026-09-16T19:00:02+00:00",
                    ingest_ts="2026-09-16T19:00:02+00:00",
                    source_id="provider-a",
                    sequence=1,
                    status="open",
                    source_ts=None,
                )
                self.assertEqual(original.dedupe_key, retry.dedupe_key)
                self.assertTrue(store.append(original))

                mirror = MarketMirror()
                result = mirror.persist_and_apply(store, retry)
                self.assertEqual(result.status.value, "applied")

                persisted = mirror.event_for_quote_key(
                    original.source_id,
                    original.quote_key,
                )
                self.assertIsNotNone(persisted)
                assert persisted is not None
                self.assertEqual(persisted.to_dict(), original.to_dict())
                self.assertNotEqual(persisted.observed_ts, retry.observed_ts)
                self.assertEqual(
                    len(
                        mirror.active_view(
                            as_of=self.CUTOFF,
                            max_age=timedelta(minutes=2),
                        ).events
                    ),
                    1,
                )
            finally:
                store.close()

    def test_persist_and_apply_hot_duplicate_does_not_rescan_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                source_ts = "2026-09-16T18:59:59+00:00"
                original = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:59+00:00",
                    ingest_ts="2026-09-16T18:59:59+00:00",
                    source_ts=source_ts,
                )
                retry = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:02+00:00",
                    ingest_ts="2026-09-16T19:00:02+00:00",
                    source_ts=source_ts,
                )
                mirror = MarketMirror()
                first = mirror.persist_and_apply(store, original)
                self.assertEqual(first.status.value, "applied")

                with patch.object(
                    store,
                    "events",
                    side_effect=AssertionError(
                        "hot duplicate must not rescan canonical history"
                    ),
                ):
                    duplicate = mirror.persist_and_apply(store, retry)

                self.assertEqual(duplicate.status.value, "duplicate")
                persisted = mirror.event_for_quote_key(
                    original.source_id,
                    original.quote_key,
                )
                self.assertIsNotNone(persisted)
                assert persisted is not None
                self.assertEqual(persisted.to_dict(), original.to_dict())
            finally:
                store.close()

    def test_path_replacement_after_sqlite_append_commit_blocks_machine_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            moved_path = Path(directory) / "market-moved.db"
            store = SQLiteMarketStore(path)
            try:
                authority = store._market_append_authority()
                before = authority.read_history()
                original_commit = store._commit_stable_database_path
                replaced = False

                def commit_then_replace():
                    nonlocal replaced
                    result = original_commit()
                    if not replaced:
                        replaced = True
                        try:
                            os.replace(path, moved_path)
                            path.touch()
                        except OSError as exc:
                            self.skipTest(
                                f"open SQLite pathname replacement unavailable on this platform: {exc}"
                            )
                    return result

                with patch.object(
                    store,
                    "_commit_stable_database_path",
                    new=commit_then_replace,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "market database pathname no longer identifies the opened database",
                    ):
                        store.append(
                            self.event(
                                sequence=1,
                                odds="2.00",
                                observed_ts="2026-09-16T19:00:00+00:00",
                            )
                        )

                self.assertTrue(replaced)
                after = authority.read_history()
                before_commits = [
                    record for record in before if record.phase.value == "COMMIT"
                ]
                after_commits = [
                    record for record in after if record.phase.value == "COMMIT"
                ]
                self.assertEqual(after_commits, before_commits)
            finally:
                store.close()

    def test_post_commit_path_failure_keeps_append_prepare_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            moved_path = Path(directory) / "market-moved.db"
            store = SQLiteMarketStore(path)
            authority = store._market_append_authority()
            original_require = store._require_database_path_identity
            replaced = False

            def commit_then_fail_postcheck():
                nonlocal replaced
                original_require()
                store.connection.commit()
                if not replaced:
                    replaced = True
                    try:
                        os.replace(path, moved_path)
                        path.touch()
                    except OSError as exc:
                        self.skipTest(
                            f"open SQLite pathname replacement unavailable on this platform: {exc}"
                        )
                original_require()

            try:
                with patch.object(
                    store,
                    "_commit_stable_database_path",
                    new=commit_then_fail_postcheck,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "market database pathname no longer identifies the opened database",
                    ):
                        store.append(
                            self.event(
                                sequence=1,
                                odds="2.00",
                                observed_ts="2026-09-16T19:00:00+00:00",
                            )
                        )

                self.assertTrue(replaced)
                self.assertEqual(authority.read_history()[-1].phase.value, "PREPARE")

                try:
                    path.unlink()
                    os.replace(moved_path, path)
                except OSError as exc:
                    self.skipTest(
                        f"database pathname restoration unavailable on this platform: {exc}"
                    )

                events = store.events()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].sequence, 1)
                self.assertEqual(authority.read_history()[-1].phase.value, "COMMIT")
            finally:
                store.close()

    def test_first_historical_cutoff_excludes_late_backdated_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertTrue(store.append(first))

                # The second event is appended after decision cutoff D but lies about
                # caller-carried observed/ingest/source clocks by backdating them.
                # Product availability, not those fields, decides historical membership.
                with self._clock_lock:
                    self._product_now = self.CUTOFF + timedelta(
                        microseconds=1
                    )
                late_backdated = self.event(
                    sequence=2,
                    odds="9.99",
                    observed_ts="2026-09-16T18:59:59+00:00",
                    ingest_ts="2026-09-16T18:59:59+00:00",
                    source_ts="2026-09-16T18:59:59+00:00",
                )
                self.assertTrue(store.append(late_backdated))

                first_resolution = self.replay(store, as_of=self.CUTOFF)
                self.assertEqual(
                    self.semantic_events(first_resolution),
                    (first.to_dict(),),
                )
                cutoff = store.connection.execute(
                    """SELECT max_append_generation
                       FROM market_replay_cutoffs"""
                ).fetchone()
                self.assertEqual(cutoff, (1,))
                availability = store.connection.execute(
                    """SELECT max_append_generation, available_at
                       FROM market_append_availability
                       ORDER BY max_append_generation"""
                ).fetchall()
                self.assertEqual([row[0] for row in availability], [0, 1, 2])
                self.assertLessEqual(
                    storage_module._timezone_aware_instant(
                        availability[1][1], "generation-1 available_at"
                    ),
                    self.CUTOFF,
                )
                self.assertGreater(
                    storage_module._timezone_aware_instant(
                        availability[2][1], "generation-2 available_at"
                    ),
                    self.CUTOFF,
                )
            finally:
                store.close()

    def test_first_future_cutoff_fails_before_authority_publication(self) -> None:
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
                future_cutoff = self.CUTOFF + timedelta(seconds=5)
                with patch.object(
                    storage_module,
                    "_market_product_utc_now",
                    return_value=self.CUTOFF.isoformat(),
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "future replay cutoff cannot be issued before its decision time",
                    ):
                        MarketMirror.replay_view_from_store(
                            store,
                            as_of=future_cutoff,
                            max_age=timedelta(minutes=2),
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

    def test_cutoff_issued_at_is_durably_bound_to_decision_cutoff(self) -> None:
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
                row = store.connection.execute(
                    """SELECT as_of, issued_at
                       FROM market_replay_cutoffs"""
                ).fetchone()
                self.assertIsNotNone(row)
                assert row is not None
                self.assertGreaterEqual(
                    storage_module._timezone_aware_instant(
                        row[1], "issued_at"
                    ),
                    storage_module._timezone_aware_instant(
                        row[0], "as_of"
                    ),
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
                    # Patch targets the replay worker only. The independent
                    # writer can also decode history while proving append
                    # authority; blocking it would make this test mistake its
                    # own global monkeypatch for an SQLite writer transaction.
                    if threading.current_thread() is replay_thread:
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
                       (cutoff_id, as_of, max_append_generation, issued_at)
                       VALUES (?, ?, ?, ?)""",
                    (cutoff_id, canonical, 1, storage_module._canonical_product_time(canonical)),
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
                    cutoff_id: str,
                    expected_binding_sha256: str,
                ) -> None:
                    nonlocal tampered_once
                    original_require(
                        authority,
                        cutoff_id=cutoff_id,
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
                    "market append authority baseline semantic binding is invalid",
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
                    "positive market append authority does not cover durable entries",
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
                "positive market append authority does not cover durable entries",
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
                    "positive market append authority does not cover durable entries",
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
                    "positive market append authority semantic binding is invalid",
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
                commit_times = store.connection.execute(
                    """SELECT start_append_generation,
                              end_append_generation,
                              append_tx_id,
                              committed_at
                       FROM market_append_commit_times"""
                ).fetchall()
                self.assertEqual(len(commit_times), 1)
                self.assertEqual(commit_times[0][:3], (1, 2, commits[0].tx_id))
                self.assertEqual(
                    commit_times[0][3],
                    storage_module._canonical_product_time(
                        self._product_now.isoformat()
                    ),
                )
                self.assertEqual(len(self.replay(store).events), 1)
                self.assertEqual(self.replay(store).events[0].sequence, 2)
            finally:
                store.close()

    def test_append_chronology_keeps_clock_classes_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                source_time = self.CUTOFF - timedelta(seconds=4)
                observed_time = self.CUTOFF - timedelta(seconds=3)
                ingest_time = self.CUTOFF - timedelta(seconds=2)
                commit_time = self.CUTOFF - timedelta(seconds=1)
                availability_time = self.CUTOFF
                event = self.event(
                    sequence=1,
                    odds="2.00",
                    source_ts=source_time.isoformat(),
                    observed_ts=observed_time.isoformat(),
                    ingest_ts=ingest_time.isoformat(),
                )

                with patch.object(
                    storage_module,
                    "_market_product_utc_now",
                    side_effect=(
                        commit_time.isoformat(),
                        availability_time.isoformat(),
                    ),
                ):
                    self.assertTrue(store.append(event))

                [stored] = store.events()
                committed_at = store.connection.execute(
                    """SELECT committed_at
                       FROM market_append_commit_times
                       WHERE end_append_generation=1"""
                ).fetchone()[0]
                available_at = store.connection.execute(
                    """SELECT available_at
                       FROM market_append_availability
                       WHERE max_append_generation=1"""
                ).fetchone()[0]

                self.assertEqual(stored.source_ts, source_time.isoformat())
                self.assertEqual(stored.observed_ts, observed_time.isoformat())
                self.assertEqual(stored.ingest_ts, ingest_time.isoformat())
                self.assertEqual(
                    committed_at,
                    storage_module._canonical_product_time(
                        commit_time.isoformat()
                    ),
                )
                self.assertEqual(
                    available_at,
                    storage_module._canonical_product_time(
                        availability_time.isoformat()
                    ),
                )
                chronology = (
                    storage_module._timezone_aware_instant(
                        stored.source_ts, "source_ts"
                    ),
                    storage_module._timezone_aware_instant(
                        stored.observed_ts, "observed_ts"
                    ),
                    storage_module._timezone_aware_instant(
                        stored.ingest_ts, "ingest_ts"
                    ),
                    storage_module._timezone_aware_instant(
                        committed_at, "append committed_at"
                    ),
                    storage_module._timezone_aware_instant(
                        available_at, "append available_at"
                    ),
                )
                self.assertEqual(tuple(sorted(chronology)), chronology)
                self.assertEqual(len(set(chronology)), len(chronology))
            finally:
                store.close()

    def test_append_rejects_product_commit_clock_rollback_without_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:00+00:00",
                )
                second = self.event(
                    sequence=2,
                    odds="2.10",
                    observed_ts="2026-09-16T18:59:01+00:00",
                )
                self.assertTrue(store.append(first))
                with self._clock_lock:
                    self._product_now -= timedelta(seconds=1)

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "market append commit-time product clock moved backwards",
                ):
                    store.append(second)

                self.assertFalse(store.connection.in_transaction)
                self.assertEqual(store.events(), [first])
                self.assertEqual(
                    store.connection.execute(
                        """SELECT start_append_generation,
                                  end_append_generation
                           FROM market_append_commit_times"""
                    ).fetchall(),
                    [(1, 1)],
                )
            finally:
                store.close()

    def test_append_commit_time_cannot_precede_prior_product_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:00+00:00",
                )
                second = self.event(
                    sequence=2,
                    odds="2.10",
                    observed_ts="2026-09-16T18:59:01+00:00",
                )
                commit_one = self.CUTOFF - timedelta(seconds=1)
                availability_one = self.CUTOFF + timedelta(seconds=1)
                commit_two = self.CUTOFF
                with patch.object(
                    storage_module,
                    "_market_product_utc_now",
                    side_effect=(
                        commit_one.isoformat(),
                        availability_one.isoformat(),
                    ),
                ):
                    self.assertTrue(store.append(first))

                with patch.object(
                    storage_module,
                    "_market_product_utc_now",
                    return_value=commit_two.isoformat(),
                ):
                    with self.assertRaisesRegex(
                        MonotonicAuthorityRollbackError,
                        "market append commit time precedes prior product availability",
                    ):
                        store.append(second)

                self.assertFalse(store.connection.in_transaction)
                self.assertEqual(store.events(), [first])
                self.assertEqual(
                    store.connection.execute(
                        """SELECT start_append_generation,
                                  end_append_generation
                           FROM market_append_commit_times"""
                    ).fetchall(),
                    [(1, 1)],
                )
            finally:
                store.close()

    def test_append_interrupt_rolls_back_sqlite_and_recovers_prepare_as_abort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            authority = store._market_append_authority()
            try:
                with patch.object(
                    store,
                    "_commit_stable_database_path",
                    side_effect=KeyboardInterrupt("simulated append interrupt"),
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        store.append(
                            self.event(
                                sequence=1,
                                odds="2.00",
                                observed_ts="2026-09-16T19:00:00+00:00",
                            )
                        )

                self.assertFalse(store.connection.in_transaction)
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_events"
                    ).fetchone(),
                    (0,),
                )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_append_commit_times"
                    ).fetchone(),
                    (0,),
                )
                self.assertEqual(authority.read_history()[-1].phase.value, "PREPARE")

                self.assertEqual(store.events(), [])
                self.assertEqual(authority.read_history()[-1].phase.value, "ABORT")
            finally:
                store.close()

    def test_events_interrupt_releases_sqlite_read_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                with patch.object(
                    store,
                    "_require_product_issued_positive_history",
                    side_effect=KeyboardInterrupt("simulated trusted-read interrupt"),
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        store.events()
                self.assertFalse(store.connection.in_transaction)
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

                commit_row = store.connection.execute(
                    """SELECT start_append_generation,
                              end_append_generation,
                              append_tx_id,
                              committed_at
                       FROM market_append_commit_times"""
                ).fetchone()
                self.assertIsNotNone(commit_row)
                self.assertEqual(commit_row[:2], (1, 1))
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()

            reopened = SQLiteMarketStore(path)
            try:
                snapshot = self.replay(reopened)
                self.assertEqual(len(snapshot.events), 1)
                history = reopened._market_append_authority().read_history()
                self.assertEqual(history[-1].phase.value, "COMMIT")
                self.assertEqual(
                    reopened.connection.execute(
                        """SELECT start_append_generation,
                                  end_append_generation,
                                  append_tx_id,
                                  committed_at
                           FROM market_append_commit_times"""
                    ).fetchone(),
                    commit_row,
                )
            finally:
                reopened.close()

    def test_append_commit_time_tamper_breaks_existing_append_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                self.assertTrue(
                    store.append(
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T18:59:00+00:00",
                        )
                    )
                )
                forged = storage_module._canonical_product_time(
                    (self.CUTOFF - timedelta(seconds=2)).isoformat()
                )
                store.connection.execute(
                    "DROP TRIGGER market_append_commit_times_no_update"
                )
                store.connection.execute(
                    """UPDATE market_append_commit_times
                       SET committed_at=?""",
                    (forged,),
                )
                store.connection.execute(
                    storage_module._APPEND_COMMIT_TIME_IMMUTABILITY_TRIGGERS[
                        "market_append_commit_times_no_update"
                    ]
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "positive market append authority semantic binding is invalid",
                ):
                    store.events()
            finally:
                store.close()

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

    def test_cutoff_resolver_recovers_abandoned_append_prepare_after_lock_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            first = SQLiteMarketStore(path)
            second = SQLiteMarketStore(path)
            original_recover = MonotonicWorkspaceAuthority.recover
            failed_append_commit = False

            def fail_append_machine_commit_once(authority, **kwargs):
                nonlocal failed_append_commit
                tx_id = kwargs.get("tx_id")
                if (
                    not failed_append_commit
                    and isinstance(tx_id, str)
                    and tx_id.startswith("append-")
                ):
                    failed_append_commit = True
                    raise RuntimeError("simulated abandoned append PREPARE")
                return original_recover(authority, **kwargs)

            try:
                with patch.object(
                    MonotonicWorkspaceAuthority,
                    "recover",
                    new=fail_append_machine_commit_once,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "simulated abandoned append PREPARE",
                    ):
                        first.append(
                            self.event(
                                sequence=1,
                                odds="2.00",
                                observed_ts="2026-09-16T19:00:00+00:00",
                            )
                        )

                # SQLite is committed and the live writer has released the append
                # sibling lock. A different store may now prove/recover the abandoned
                # PREPARE before issuing its first replay cutoff.
                history_before = second._market_append_authority().read_history()
                self.assertEqual(history_before[-1].phase.value, "PREPARE")

                snapshot = self.replay(second)
                self.assertEqual(len(snapshot.events), 1)

                history_after = second._market_append_authority().read_history()
                self.assertEqual(history_after[-1].phase.value, "COMMIT")
                self.assertEqual(
                    second.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (1,),
                )
            finally:
                second.close()
                first.close()

    def test_cutoff_resolver_does_not_recover_live_append_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            first = SQLiteMarketStore(path)
            second = SQLiteMarketStore(path)
            writer_at_machine_commit = threading.Event()
            release_writer = threading.Event()
            foreign_recovery = threading.Event()
            writer_errors: list[BaseException] = []
            original_recover = MonotonicWorkspaceAuthority.recover
            cutoff_authority = second._replay_cutoff_authority()
            cutoff_history_before = cutoff_authority.read_history()

            def guarded_recover(authority, **kwargs):
                tx_id = kwargs.get("tx_id")
                if isinstance(tx_id, str) and tx_id.startswith("append-"):
                    if threading.current_thread().name == "append-writer":
                        writer_at_machine_commit.set()
                        if not release_writer.wait(timeout=5):
                            raise TimeoutError("append writer was not released")
                    else:
                        foreign_recovery.set()
                return original_recover(authority, **kwargs)

            def append_from_first() -> None:
                try:
                    first.append(
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T19:00:00+00:00",
                        )
                    )
                except BaseException as exc:  # pragma: no cover - thread handoff
                    writer_errors.append(exc)

            try:
                with patch.object(
                    MonotonicWorkspaceAuthority,
                    "recover",
                    new=guarded_recover,
                ):
                    writer = threading.Thread(
                        target=append_from_first,
                        name="append-writer",
                    )
                    writer.start()
                    self.assertTrue(writer_at_machine_commit.wait(timeout=5))

                    # The first store has committed SQLite but still owns the live
                    # append PREPARE. A second-store cutoff resolver must fail closed
                    # on the sibling append lock rather than recover that live writer.
                    with self.assertRaises(WorkspaceEconomicLockBusyError):
                        self.replay(second)
                    self.assertFalse(foreign_recovery.is_set())
                    self.assertEqual(
                        second.connection.execute(
                            "SELECT COUNT(*) FROM market_replay_cutoffs"
                        ).fetchone(),
                        (0,),
                    )
                    self.assertEqual(
                        cutoff_authority.read_history(),
                        cutoff_history_before,
                    )

                    release_writer.set()
                    writer.join(timeout=5)
                    self.assertFalse(writer.is_alive())

                self.assertEqual(writer_errors, [])
                snapshot = self.replay(second)
                self.assertEqual(len(snapshot.events), 1)
                self.assertFalse(foreign_recovery.is_set())
            finally:
                release_writer.set()
                second.close()
                first.close()

    def test_existing_cutoff_stays_readable_during_live_append_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            first = SQLiteMarketStore(path)
            second = SQLiteMarketStore(path)
            writer_at_machine_commit = threading.Event()
            release_writer = threading.Event()
            foreign_recovery = threading.Event()
            writer_errors: list[BaseException] = []
            original_recover = MonotonicWorkspaceAuthority.recover

            # Seal generation zero before the writer starts. This cutoff is already
            # independently authoritative and must not depend on a later append's
            # machine-COMMIT progress. Equivalent timezone representations must reuse
            # the same canonical cutoff identity rather than being treated as new.
            self.assertEqual(len(self.replay(second).events), 0)
            equivalent_cutoff = self.CUTOFF.astimezone(
                timezone(timedelta(hours=2))
            )

            def guarded_recover(authority, **kwargs):
                tx_id = kwargs.get("tx_id")
                if isinstance(tx_id, str) and tx_id.startswith("append-"):
                    if threading.current_thread().name == "append-writer":
                        writer_at_machine_commit.set()
                        if not release_writer.wait(timeout=5):
                            raise TimeoutError("append writer was not released")
                    else:
                        foreign_recovery.set()
                return original_recover(authority, **kwargs)

            def append_from_first() -> None:
                try:
                    first.append(
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T19:00:00+00:00",
                        )
                    )
                except BaseException as exc:  # pragma: no cover - thread handoff
                    writer_errors.append(exc)

            try:
                with patch.object(
                    MonotonicWorkspaceAuthority,
                    "recover",
                    new=guarded_recover,
                ):
                    writer = threading.Thread(
                        target=append_from_first,
                        name="append-writer",
                    )
                    writer.start()
                    self.assertTrue(writer_at_machine_commit.wait(timeout=5))

                    snapshot = self.replay(second, as_of=equivalent_cutoff)
                    self.assertEqual(len(snapshot.events), 0)
                    self.assertFalse(foreign_recovery.is_set())

                    release_writer.set()
                    writer.join(timeout=5)
                    self.assertFalse(writer.is_alive())

                self.assertEqual(writer_errors, [])
                self.assertEqual(len(self.replay(second).events), 0)
            finally:
                release_writer.set()
                second.close()
                first.close()

    def test_positive_committed_cutoff_stays_readable_during_new_append_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            first = SQLiteMarketStore(path)
            second = SQLiteMarketStore(path)
            writer_at_machine_commit = threading.Event()
            release_writer = threading.Event()
            foreign_recovery = threading.Event()
            writer_errors: list[BaseException] = []
            original_recover = MonotonicWorkspaceAuthority.recover

            first_event = self.event(
                sequence=1,
                odds="2.00",
                observed_ts="2026-09-16T19:00:00+00:00",
            )
            self.assertTrue(first.append(first_event))
            sealed = self.replay(second)
            self.assertEqual(self.semantic_events(sealed), (first_event.to_dict(),))

            def guarded_recover(authority, **kwargs):
                tx_id = kwargs.get("tx_id")
                if isinstance(tx_id, str) and tx_id.startswith("append-2-2-"):
                    if threading.current_thread().name == "append-writer":
                        writer_at_machine_commit.set()
                        if not release_writer.wait(timeout=5):
                            raise TimeoutError("append writer was not released")
                    else:
                        foreign_recovery.set()
                return original_recover(authority, **kwargs)

            def append_from_first() -> None:
                try:
                    first.append(
                        self.event(
                            sequence=2,
                            odds="2.10",
                            observed_ts="2026-09-16T19:00:02+00:00",
                        )
                    )
                except BaseException as exc:  # pragma: no cover - thread handoff
                    writer_errors.append(exc)

            try:
                with patch.object(
                    MonotonicWorkspaceAuthority,
                    "recover",
                    new=guarded_recover,
                ):
                    writer = threading.Thread(
                        target=append_from_first,
                        name="append-writer",
                    )
                    writer.start()
                    self.assertTrue(writer_at_machine_commit.wait(timeout=5))

                    repeated = self.replay(second)
                    self.assertEqual(self.semantic_events(repeated), (first_event.to_dict(),))
                    self.assertFalse(foreign_recovery.is_set())

                    release_writer.set()
                    writer.join(timeout=5)
                    self.assertFalse(writer.is_alive())

                self.assertEqual(writer_errors, [])
                self.assertEqual(
                    self.semantic_events(self.replay(second)),
                    (first_event.to_dict(),),
                )
            finally:
                release_writer.set()
                second.close()
                first.close()

    def test_distinct_new_cutoff_still_requires_append_lock_when_old_cutoff_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            first = SQLiteMarketStore(path)
            second = SQLiteMarketStore(path)
            later_cutoff = self.CUTOFF + timedelta(seconds=1)
            writer_at_machine_commit = threading.Event()
            release_writer = threading.Event()
            foreign_recovery = threading.Event()
            writer_errors: list[BaseException] = []
            original_recover = MonotonicWorkspaceAuthority.recover

            # An older exact cutoff is already independently sealed. Its existence
            # must not let a distinct later cutoff bypass live append issuance.
            self.assertEqual(len(self.replay(second).events), 0)
            cutoff_authority = second._replay_cutoff_authority()
            cutoff_history_before = cutoff_authority.read_history()

            def guarded_recover(authority, **kwargs):
                tx_id = kwargs.get("tx_id")
                if isinstance(tx_id, str) and tx_id.startswith("append-"):
                    if threading.current_thread().name == "append-writer":
                        writer_at_machine_commit.set()
                        if not release_writer.wait(timeout=5):
                            raise TimeoutError("append writer was not released")
                    else:
                        foreign_recovery.set()
                return original_recover(authority, **kwargs)

            def append_from_first() -> None:
                try:
                    first.append(
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T19:00:00+00:00",
                        )
                    )
                except BaseException as exc:  # pragma: no cover - thread handoff
                    writer_errors.append(exc)

            try:
                with patch.object(
                    MonotonicWorkspaceAuthority,
                    "recover",
                    new=guarded_recover,
                ):
                    writer = threading.Thread(
                        target=append_from_first,
                        name="append-writer",
                    )
                    writer.start()
                    self.assertTrue(writer_at_machine_commit.wait(timeout=5))

                    # The already sealed exact cutoff remains readable.
                    self.assertEqual(len(self.replay(second).events), 0)

                    # A different cutoff identity is new authority and must not
                    # recover the live append PREPARE merely because an older row
                    # already exists in market_replay_cutoffs.
                    with self.assertRaises(WorkspaceEconomicLockBusyError):
                        self.replay(second, as_of=later_cutoff)
                    self.assertFalse(foreign_recovery.is_set())
                    self.assertEqual(
                        second.connection.execute(
                            "SELECT COUNT(*) FROM market_replay_cutoffs"
                        ).fetchone(),
                        (1,),
                    )
                    self.assertEqual(
                        cutoff_authority.read_history(),
                        cutoff_history_before,
                    )

                    release_writer.set()
                    writer.join(timeout=5)
                    self.assertFalse(writer.is_alive())

                self.assertEqual(writer_errors, [])
                self.assertEqual(
                    len(self.replay(second, as_of=later_cutoff).events),
                    1,
                )
                self.assertFalse(foreign_recovery.is_set())
            finally:
                release_writer.set()
                second.close()
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

    def test_events_authority_proof_and_rows_share_one_sqlite_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            external = sqlite3.connect(path)
            original_require = SQLiteMarketStore._require_product_issued_positive_history
            try:
                event = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertTrue(store.append(event))
                forged = self.event(
                    sequence=1,
                    odds="9.99",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                tampered = False

                def prove_then_tamper(instance, authority):
                    nonlocal tampered
                    result = original_require(instance, authority)
                    if instance is store and not tampered:
                        tampered = True
                        external.execute(
                            """UPDATE market_events
                               SET decimal_odds=?, payload_json=?
                               WHERE dedupe_key=?""",
                            (
                                str(forged.decimal_odds),
                                storage_module._canonical_payload(forged),
                                event.dedupe_key,
                            ),
                        )
                        external.commit()
                    return result

                with patch.object(
                    SQLiteMarketStore,
                    "_require_product_issued_positive_history",
                    new=prove_then_tamper,
                ):
                    snapshot = store.events()

                self.assertTrue(tampered)
                self.assertEqual(len(snapshot), 1)
                self.assertEqual(snapshot[0].decimal_odds, Decimal("2.00"))

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "positive market append authority semantic binding is invalid",
                ):
                    store.events()
            finally:
                external.close()
                store.close()

    def test_current_read_rejects_deleted_projection_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                event = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertTrue(store.append(event))
                store.connection.execute(
                    """DELETE FROM current_quotes
                       WHERE source_id=? AND quote_key=?""",
                    (event.source_id, event.quote_key),
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    ValueError,
                    "current quote projection diverges from canonical market history",
                ):
                    store.current_by_source()
            finally:
                store.close()

    def test_current_read_rejects_coherent_forged_projection_row(self) -> None:
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

                with self.assertRaisesRegex(
                    ValueError,
                    "current quote projection diverges from canonical market history",
                ):
                    store.current_by_source()
                with self.assertRaisesRegex(
                    ValueError,
                    "current quote projection diverges from canonical market history",
                ):
                    store.current()
            finally:
                store.close()

    def test_events_rejects_coherent_positive_history_rewrite(self) -> None:
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

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "positive market append authority semantic binding is invalid",
                ):
                    store.events()
            finally:
                store.close()

    def test_mirror_reapply_rejects_coherent_positive_history_rewrite(self) -> None:
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

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "positive market append authority semantic binding is invalid",
                ):
                    MarketMirror.from_store(store)
            finally:
                store.close()

    def test_events_rejects_generation_zero_membership_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            raw = sqlite3.connect(path)
            legacy = self.event(
                sequence=1,
                odds="2.00",
                observed_ts="2026-09-16T19:00:00+00:00",
            )
            payload = storage_module._canonical_payload(legacy)
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
                """INSERT INTO market_events
                   (dedupe_key,quote_key,event_id,market_id,selection_id,decimal_odds,
                    observed_ts,source_id,sequence,payload_json)
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
            raw.commit()
            raw.close()

            store = SQLiteMarketStore(path)
            try:
                injected = self.event(
                    sequence=2,
                    odds="2.10",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                store.connection.execute(
                    """INSERT INTO market_events
                       (dedupe_key,quote_key,event_id,market_id,selection_id,decimal_odds,
                        observed_ts,source_id,sequence,payload_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        injected.dedupe_key,
                        injected.quote_key,
                        injected.event_id,
                        injected.market_id,
                        injected.selection_id,
                        str(injected.decimal_odds),
                        injected.observed_ts,
                        injected.source_id,
                        injected.sequence,
                        storage_module._canonical_payload(injected),
                    ),
                )
                store.connection.execute(
                    """INSERT INTO market_event_commit_order
                       (dedupe_key, append_generation)
                       VALUES (?, 0)""",
                    (injected.dedupe_key,),
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "market append authority baseline semantic binding is invalid",
                ):
                    store.events()
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

    def test_projection_repair_decodes_only_authoritative_tip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                events = tuple(
                    self.event(
                        sequence=sequence,
                        odds=f"2.{sequence:02d}",
                        observed_ts=(
                            "2026-09-16T18:59:"
                            f"{min(sequence, 59):02d}+00:00"
                        ),
                    )
                    for sequence in range(1, 17)
                )
                for event in events:
                    self.assertTrue(store.append(event))

                original_decoder = storage_module._event_from_history_row
                decoded_rows = 0

                def counted_decoder(row):
                    nonlocal decoded_rows
                    decoded_rows += 1
                    return original_decoder(row)

                with patch.object(
                    storage_module,
                    "_event_from_history_row",
                    side_effect=counted_decoder,
                ):
                    repaired = store._repair_current_projection_for_key(
                        source_id=events[-1].source_id,
                        quote_key=events[-1].quote_key,
                    )

                self.assertEqual(repaired.dedupe_key, events[-1].dedupe_key)
                self.assertEqual(decoded_rows, 1)
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
                    "positive market append authority semantic binding is invalid",
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
                    "positive market append authority semantic binding is invalid",
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

    def test_cutoff_interrupt_rolls_back_sqlite_and_recovers_prepare_as_abort(self) -> None:
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
                authority = store._replay_cutoff_authority()
                with patch.object(
                    store,
                    "_commit_stable_database_path",
                    side_effect=KeyboardInterrupt("simulated cutoff interrupt"),
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        self.replay(store)

                self.assertFalse(store.connection.in_transaction)
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (0,),
                )
                self.assertEqual(authority.read_history()[-1].phase.value, "PREPARE")

                recovered = self.replay(store)
                self.assertEqual(len(recovered.events), 1)
                self.assertEqual(authority.read_history()[-1].phase.value, "COMMIT")
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
                       (cutoff_id, as_of, max_append_generation, issued_at)
                       VALUES (?, ?, ?, ?)""",
                    (forged_id, forged_as_of, 1, storage_module._canonical_product_time(forged_as_of)),
                )
                store.connection.commit()

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "workspace state is missing, rolled back, or unproven",
                ):
                    self.replay(store)
            finally:
                store.close()

    def test_frozen_cutoff_survives_later_unissued_tail_but_new_cutoff_rejects_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertTrue(store.append(first))
                frozen = self.replay(store)
                self.assertEqual(self.semantic_events(frozen), (first.to_dict(),))

                forged_tail = self.event(
                    sequence=2,
                    odds="9.99",
                    observed_ts="2026-09-16T19:00:02+00:00",
                )
                self.direct_insert_positive_generation(
                    store,
                    forged_tail,
                    generation=2,
                )

                self.assertEqual(
                    self.semantic_events(self.replay(store)),
                    (first.to_dict(),),
                )

                later_cutoff = self.CUTOFF + timedelta(seconds=2)
                with self.assertRaises(MonotonicAuthorityRollbackError):
                    self.replay(store, as_of=later_cutoff)
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (1,),
                )
            finally:
                store.close()

    def test_cutoff_with_unissued_positive_generation_is_rejected_even_if_cutoff_authority_committed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                event = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.direct_insert_positive_generation(
                    store,
                    event,
                    generation=1,
                )

                canonical_as_of = storage_module._canonical_replay_cutoff(
                    self.CUTOFF.isoformat()
                )
                cutoff_id = storage_module._replay_cutoff_id(canonical_as_of)
                corpus_sha256 = store._frozen_replay_corpus_sha256(1)
                append_available_at = store.connection.execute(
                    """SELECT available_at
                       FROM market_append_availability
                       WHERE max_append_generation=0"""
                ).fetchone()[0]
                binding_sha256 = storage_module._replay_cutoff_binding_sha256(
                    cutoff_id=cutoff_id,
                    canonical_as_of=canonical_as_of,
                    max_append_generation=1,
                    append_available_at=append_available_at,
                    issued_at=storage_module._canonical_product_time(
                        canonical_as_of
                    ),
                    corpus_sha256=corpus_sha256,
                )
                rows = ((
                    cutoff_id,
                    canonical_as_of,
                    1,
                    storage_module._canonical_product_time(canonical_as_of),
                ),)
                intended_state_sha256 = storage_module._replay_cutoff_state_sha256(
                    rows,
                    sealed_corpus_sha256=corpus_sha256,
                )
                assert intended_state_sha256 is not None
                authority = store._replay_cutoff_authority()
                tx_id = f"{cutoff_id[:32]}-{'0' * 32}"
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=intended_state_sha256,
                    semantic_binding_sha256=binding_sha256,
                )
                store.connection.execute(
                    """INSERT INTO market_replay_cutoffs
                       (cutoff_id, as_of, max_append_generation, issued_at)
                       VALUES (?, ?, ?, ?)""",
                    (cutoff_id, canonical_as_of, 1, storage_module._canonical_product_time(canonical_as_of)),
                )
                store.connection.commit()
                authority.recover(
                    observed_state_sha256=intended_state_sha256,
                    tx_id=tx_id,
                    semantic_binding_sha256=binding_sha256,
                )

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "cutoff exceeds independently committed append authority",
                ):
                    self.replay(store)
            finally:
                store.close()

    def test_committed_cutoff_with_noncanonical_transaction_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                self.direct_issue_first_cutoff_authority(
                    store,
                    as_of=self.CUTOFF,
                    max_generation=0,
                    tx_id="legacy-manual-cutoff",
                )

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "committed transaction identity is invalid",
                ):
                    self.replay(store)
            finally:
                store.close()

    def test_cutoff_inside_atomic_append_batch_is_rejected(self) -> None:
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
                    odds="2.10",
                    observed_ts="2026-09-16T19:00:00.500000+00:00",
                )
                self.assertEqual(
                    store.append_batch_accepted((first, second)),
                    [first, second],
                )

                append_history = store._market_append_authority().read_history()
                positive_commits = [
                    record
                    for record in append_history
                    if record.phase.value == "COMMIT"
                    and record.tx_id.startswith("append-")
                ]
                self.assertEqual(len(positive_commits), 1)
                self.assertRegex(
                    positive_commits[0].tx_id,
                    r"^append-1-2-[0-9a-f]{32}$",
                )

                self.direct_issue_first_cutoff_authority(
                    store,
                    as_of=self.CUTOFF,
                    max_generation=1,
                )

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "not an exact committed append transition boundary",
                ):
                    self.replay(store)
            finally:
                store.close()

    def test_replay_future_source_sequence_cannot_shadow_visible_prior_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                visible = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:58+00:00",
                    ingest_ts="2026-09-16T18:59:58+00:00",
                    source_ts="2026-09-16T18:59:58+00:00",
                )
                future_source = self.event(
                    sequence=2,
                    odds="9.99",
                    observed_ts="2026-09-16T18:59:59+00:00",
                    ingest_ts="2026-09-16T18:59:59+00:00",
                    source_ts="2026-09-16T19:00:01+00:00",
                )
                self.assertTrue(store.append(visible))
                self.assertTrue(store.append(future_source))

                snapshot = self.replay(store)

                self.assertEqual(len(snapshot.events), 1)
                self.assertEqual(snapshot.events[0].sequence, 1)
                self.assertEqual(snapshot.events[0].decimal_odds, Decimal("2.00"))
            finally:
                store.close()

    def test_replay_inverted_local_clock_cannot_shadow_visible_prior_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                visible = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T18:59:57+00:00",
                    ingest_ts="2026-09-16T18:59:57+00:00",
                    source_ts="2026-09-16T18:59:57+00:00",
                )
                impossible = self.event(
                    sequence=2,
                    odds="9.99",
                    observed_ts="2026-09-16T18:59:59+00:00",
                    ingest_ts="2026-09-16T18:59:58+00:00",
                    source_ts="2026-09-16T18:59:56+00:00",
                )
                self.assertTrue(store.append(visible))
                self.assertTrue(store.append(impossible))

                snapshot = self.replay(store)

                self.assertEqual(len(snapshot.events), 1)
                self.assertEqual(snapshot.events[0].sequence, 1)
                self.assertEqual(snapshot.events[0].decimal_odds, Decimal("2.00"))
            finally:
                store.close()

    def test_live_active_view_excludes_late_local_availability(self) -> None:
        mirror = MarketMirror()
        late_ingest = self.event(
            sequence=1,
            odds="2.00",
            observed_ts="2026-09-16T18:59:58+00:00",
            ingest_ts="2026-09-16T19:00:02+00:00",
            source_ts="2026-09-16T18:59:57+00:00",
        )
        future_observation = self.event(
            sequence=2,
            odds="2.10",
            observed_ts="2026-09-16T19:00:02+00:00",
            ingest_ts="2026-09-16T19:00:03+00:00",
            source_ts="2026-09-16T18:59:59+00:00",
        )
        mirror.apply(late_ingest)
        mirror.apply(future_observation)

        active = mirror.active_view(
            as_of=self.CUTOFF,
            max_age=timedelta(minutes=2),
        )

        self.assertEqual(active.events, ())
        self.assertEqual(len(mirror.snapshot()), 1)

    def test_focused_active_view_excludes_late_ingest_even_with_fresh_source_time(self) -> None:
        mirror = MarketMirror()
        event = self.event(
            sequence=1,
            odds="2.00",
            observed_ts="2026-09-16T18:59:59+00:00",
            ingest_ts="2026-09-16T19:00:01.000001+00:00",
            source_ts="2026-09-16T19:00:00+00:00",
        )
        mirror.apply(event)

        active = mirror.active_view_for_keys(
            ((event.source_id, event.quote_key),),
            as_of=self.CUTOFF,
            max_age=timedelta(minutes=2),
        )

        self.assertEqual(active.events, ())
        self.assertEqual(active.revision, 1)

    def test_live_active_view_accepts_local_availability_exactly_at_boundary(self) -> None:
        mirror = MarketMirror()
        event = self.event(
            sequence=1,
            odds="2.00",
            observed_ts="2026-09-16T19:00:00+00:00",
            ingest_ts=self.CUTOFF.isoformat(),
            source_ts="2026-09-16T18:59:59+00:00",
        )
        mirror.apply(event)

        active = mirror.active_view(
            as_of=self.CUTOFF,
            max_age=timedelta(minutes=2),
        )

        self.assertEqual(active.events, (event,))

    def test_live_active_view_rejects_submicrosecond_future_source_time(self) -> None:
        mirror = MarketMirror()
        # Invalid precision must be rejected at the domain boundary, before
        # the event can enter either the live view or the audit snapshot.
        with self.assertRaisesRegex(
            ValueError,
            "source_ts precision finer than microseconds is unsupported",
        ):
            self.event(
                sequence=1,
                odds="2.00",
                observed_ts="2026-09-16T19:00:01+00:00",
                ingest_ts="2026-09-16T19:00:01+00:00",
                source_ts="2026-09-16T19:00:01.0000001+00:00",
            )
        self.assertEqual(mirror.snapshot(), ())
        self.assertEqual(mirror.active_view(
            as_of=self.CUTOFF,
            max_age=timedelta(minutes=2),
        ).events, ())

    def test_live_active_view_rejects_submicrosecond_future_local_time(self) -> None:
        mirror = MarketMirror()
        # Observed time with submicrosecond precision is noncanonical; it
        # cannot be accepted and silently rounded into a causal cutoff.
        with self.assertRaisesRegex(
            ValueError,
            "observed_ts precision finer than microseconds is unsupported",
        ):
            self.event(
                sequence=1,
                odds="2.00",
                observed_ts="2026-09-16T19:00:01.0000001+00:00",
                ingest_ts="2026-09-16T19:00:01+00:00",
                source_ts="2026-09-16T19:00:00+00:00",
            )
        self.assertEqual(mirror.snapshot(), ())
        self.assertEqual(mirror.active_view_for_keys(
            (("provider-a", "unused-quote-key"),),
            as_of=self.CUTOFF,
            max_age=timedelta(minutes=2),
        ).events, ())

    def test_generation_zero_baseline_is_sealed_inside_append_issuance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = {"held": False, "baseline": False, "rebuild": False}

            class TracingLock:
                def __enter__(self):
                    self.assert_not_held = not state["held"]
                    state["held"] = True
                    return self

                def __exit__(self, exc_type, exc, tb):
                    state["held"] = False
                    return False

            def traced_lock(_authority):
                return TracingLock()

            def require_locked_baseline(_store, authority):
                self.assertTrue(state["held"])
                self.assertIsInstance(authority, MonotonicWorkspaceAuthority)
                state["baseline"] = True

            def require_locked_rebuild(_store, *, append_authority):
                self.assertTrue(state["held"])
                self.assertIsInstance(append_authority, MonotonicWorkspaceAuthority)
                state["rebuild"] = True

            with patch.object(
                SQLiteMarketStore,
                "_market_append_issuance_lock",
                new=staticmethod(traced_lock),
            ), patch.object(
                SQLiteMarketStore,
                "_ensure_market_append_baseline_authority",
                new=require_locked_baseline,
            ), patch.object(
                SQLiteMarketStore,
                "_rebuild_current_quotes",
                new=require_locked_rebuild,
            ):
                store = SQLiteMarketStore(Path(directory) / "market.db")
                store.close()

            self.assertTrue(state["baseline"])
            self.assertTrue(state["rebuild"])
            self.assertFalse(state["held"])

    def test_direct_positive_generation_with_noncanonical_machine_binding_is_rejected(self) -> None:
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

                authority = store._market_append_authority()
                baseline_state = store._generation_zero_baseline_state_sha256()
                entries = store._validated_positive_append_entries()
                intended_state = store._append_state_from_entries(
                    entries,
                    baseline_state_sha256=baseline_state,
                )
                tx_id = "append-1-1-" + ("0" * 32)
                forged_binding = "0" * 64
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=baseline_state,
                    intended_state_sha256=intended_state,
                    semantic_binding_sha256=forged_binding,
                )
                authority.recover(
                    observed_state_sha256=intended_state,
                    tx_id=tx_id,
                    semantic_binding_sha256=forged_binding,
                )

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "semantic binding is invalid",
                ):
                    store.events()
            finally:
                store.close()

            with self.assertRaisesRegex(
                MonotonicAuthorityRollbackError,
                "semantic binding is invalid",
            ):
                SQLiteMarketStore(path)

    def test_noncanonical_pending_positive_append_is_not_machine_committed(self) -> None:
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

                authority = store._market_append_authority()
                baseline_state = store._generation_zero_baseline_state_sha256()
                entries = store._validated_positive_append_entries()
                intended_state = store._append_state_from_entries(
                    entries,
                    baseline_state_sha256=baseline_state,
                )
                tx_id = "append-1-1-" + ("1" * 32)
                forged_binding = "1" * 64
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=baseline_state,
                    intended_state_sha256=intended_state,
                    semantic_binding_sha256=forged_binding,
                )

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "PREPARE semantic binding is invalid",
                ):
                    store.events()

                history = authority.read_history()
                self.assertEqual(history[-1].phase.value, "PREPARE")
                self.assertEqual(history[-1].tx_id, tx_id)
            finally:
                store.close()

    def test_append_batch_materializes_generator_before_issuance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            state = {"consumed": False, "lock_entered": False}

            def generated_events():
                yield self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                state["consumed"] = True

            class GuardLock:
                def __enter__(self):
                    self.assert_consumed = state["consumed"]
                    if not self.assert_consumed:
                        raise AssertionError("append iterable executed under issuance lock")
                    state["lock_entered"] = True
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            try:
                with patch.object(
                    SQLiteMarketStore,
                    "_market_append_issuance_lock",
                    new=staticmethod(lambda _authority: GuardLock()),
                ):
                    accepted = store.append_batch_accepted(generated_events())

                self.assertEqual(len(accepted), 1)
                self.assertTrue(state["consumed"])
                self.assertTrue(state["lock_entered"])
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()

    def test_append_batch_generator_failure_occurs_before_durable_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            authority = store._market_append_authority()
            before = authority.read_history()

            def broken_events():
                yield self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                raise RuntimeError("generator failed before batch admission")

            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "generator failed before batch admission",
                ):
                    store.append_batch_accepted(broken_events())

                self.assertEqual(authority.read_history(), before)
                self.assertEqual(store.events(), [])
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_event_commit_order"
                    ).fetchone(),
                    (0,),
                )
            finally:
                store.close()

    def test_append_batch_freezes_each_event_before_generator_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            original = MarketEvent.from_dict(
                {
                    **self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    ).to_dict(),
                    "metadata": {"witness": {"value": 1}},
                }
            )

            def generated_events():
                yield original
                original.metadata["witness"]["value"] = 2

            try:
                accepted = store.append_batch_accepted(generated_events())
                self.assertEqual(accepted[0].metadata["witness"]["value"], 1)
                self.assertEqual(store.events()[0].metadata["witness"]["value"], 1)
                self.assertEqual(original.metadata["witness"]["value"], 2)
            finally:
                store.close()

    def test_append_batch_rejects_non_event_before_issuance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            entered = False

            class ForbiddenLock:
                def __enter__(self):
                    nonlocal entered
                    entered = True
                    raise AssertionError("issuance lock must not be entered")

                def __exit__(self, exc_type, exc, tb):
                    return False

            try:
                with patch.object(
                    SQLiteMarketStore,
                    "_market_append_issuance_lock",
                    new=staticmethod(lambda _authority: ForbiddenLock()),
                ):
                    with self.assertRaisesRegex(
                        TypeError,
                        "only MarketEvent",
                    ):
                        store.append_batch_accepted((object(),))
                self.assertFalse(entered)
            finally:
                store.close()

    def test_constructor_rejects_path_replacement_during_sqlite_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            moved_path = Path(directory) / "market-opened.db"
            original_connect = storage_module.sqlite3.connect
            replaced = False

            def connect_then_replace(database, *args, **kwargs):
                nonlocal replaced
                connection = original_connect(database, *args, **kwargs)
                try:
                    os.replace(path, moved_path)
                    path.touch()
                    replaced = True
                except OSError as exc:
                    connection.close()
                    self.skipTest(
                        f"SQLite open-time pathname replacement unavailable: {exc}"
                    )
                return connection

            with patch.object(
                storage_module.sqlite3,
                "connect",
                new=connect_then_replace,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "pathname changed while opening database",
                ):
                    SQLiteMarketStore(path)

            self.assertTrue(replaced)
            self.assertTrue(moved_path.exists())

    def test_noncanonical_pending_cutoff_is_not_machine_committed(self) -> None:
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
                canonical_as_of = storage_module._canonical_replay_cutoff(
                    self.CUTOFF.isoformat()
                )
                cutoff_id = storage_module._replay_cutoff_id(canonical_as_of)
                store.connection.execute(
                    """INSERT INTO market_replay_cutoffs
                       (cutoff_id, as_of, max_append_generation, issued_at)
                       VALUES (?, ?, ?, ?)""",
                    (cutoff_id, canonical_as_of, 1, storage_module._canonical_product_time(canonical_as_of)),
                )
                store.connection.commit()

                cutoff_rows = store._validated_replay_cutoff_rows()
                intended_state = store._replay_cutoff_authority_state_sha256(
                    cutoff_rows
                )
                self.assertIsNotNone(intended_state)
                authority = store._replay_cutoff_authority()
                tx_id = f"{cutoff_id[:32]}-" + ("2" * 32)
                forged_binding = "2" * 64
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=intended_state,
                    semantic_binding_sha256=forged_binding,
                )

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "cutoff PREPARE semantic binding is invalid",
                ):
                    self.replay(store)

                history = authority.read_history()
                self.assertEqual(history[-1].phase.value, "PREPARE")
                self.assertEqual(history[-1].tx_id, tx_id)
            finally:
                store.close()

    def test_persist_and_apply_uses_exact_storage_admission_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            mirror = MarketMirror()
            original = MarketEvent.from_dict(
                {
                    **self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    ).to_dict(),
                    "metadata": {"witness": {"value": 1}},
                }
            )
            original_commit = store._commit_stable_database_path
            mutated = False

            def commit_then_mutate_caller():
                nonlocal mutated
                result = original_commit()
                if not mutated:
                    original.metadata["witness"]["value"] = 2
                    mutated = True
                return result

            try:
                with patch.object(
                    store,
                    "_commit_stable_database_path",
                    new=commit_then_mutate_caller,
                ):
                    result = mirror.persist_and_apply(store, original)

                self.assertEqual(result.status.value, "applied")
                self.assertTrue(mutated)
                live = mirror.event_for_quote_key(original.source_id, original.quote_key)
                self.assertIsNotNone(live)
                assert live is not None
                self.assertEqual(live.metadata["witness"]["value"], 1)
                self.assertEqual(store.events()[0].metadata["witness"]["value"], 1)
                self.assertEqual(original.metadata["witness"]["value"], 2)
            finally:
                store.close()


    def test_replay_rejects_datetime_subclass_before_causal_dispatch(self) -> None:
        class HostileDateTime(datetime):
            def utcoffset(self):
                raise AssertionError("datetime subclass dispatch must not execute")

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                hostile = HostileDateTime(
                    2026,
                    9,
                    16,
                    19,
                    0,
                    1,
                    tzinfo=timezone.utc,
                )
                with self.assertRaisesRegex(TypeError, "exact datetime"):
                    MarketMirror.replay_view_from_store(
                        store,
                        as_of=hostile,
                        max_age=timedelta(minutes=2),
                    )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (0,),
                )
            finally:
                store.close()

    def test_replay_rejects_timedelta_subclass_before_causal_dispatch(self) -> None:
        class HostileTimedelta(timedelta):
            def __lt__(self, other):
                raise AssertionError("timedelta subclass dispatch must not execute")

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                with self.assertRaisesRegex(TypeError, "exact timedelta"):
                    MarketMirror.replay_view_from_store(
                        store,
                        as_of=self.CUTOFF,
                        max_age=HostileTimedelta(minutes=2),
                    )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (0,),
                )
            finally:
                store.close()

    def test_replay_rejects_store_subclass_before_authority_dispatch(self) -> None:
        class HostileStore(SQLiteMarketStore):
            def replay_events_at_frozen_cutoff(self, *, as_of: str):
                raise AssertionError("store subclass authority must not execute")

        with tempfile.TemporaryDirectory() as directory:
            store = HostileStore(Path(directory) / "market.db")
            try:
                with self.assertRaisesRegex(TypeError, "exact SQLiteMarketStore"):
                    MarketMirror.replay_view_from_store(
                        store,
                        as_of=self.CUTOFF,
                        max_age=timedelta(minutes=2),
                    )
            finally:
                store.close()

    def test_store_cutoff_rejects_str_subclass_before_replace_dispatch(self) -> None:
        class HostileStr(str):
            def replace(self, *args, **kwargs):
                raise AssertionError("str subclass replace must not execute")

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                with self.assertRaisesRegex(ValueError, "exact ISO-8601 string"):
                    store.replay_events_at_frozen_cutoff(
                        as_of=HostileStr(self.CUTOFF.isoformat())
                    )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (0,),
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
