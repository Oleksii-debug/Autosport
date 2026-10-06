from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

import autosport.storage as storage_module
from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
from autosport.storage import SQLiteMarketStore


class ReplayCutoffTimeMonotonicityTests(unittest.TestCase):
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
            source_ts=observed_ts,
            ingest_ts=ingest_ts or observed_ts,
        )

    @classmethod
    def replay(cls, store: SQLiteMarketStore, *, as_of: datetime):
        return MarketMirror.replay_view_from_store(
            store,
            as_of=as_of,
            max_age=timedelta(minutes=5),
        )

    @staticmethod
    def forged_cutoff(
        store: SQLiteMarketStore,
        *,
        as_of: datetime,
        max_generation: int,
        prior_rows: tuple[tuple[str, str, int], ...],
        tx_suffix: str,
        commit_machine: bool,
    ) -> tuple[str, object]:
        canonical_as_of = storage_module._canonical_replay_cutoff(as_of.isoformat())
        cutoff_id = storage_module._replay_cutoff_id(canonical_as_of)
        corpus_sha256 = store._frozen_replay_corpus_sha256(max_generation)
        binding_sha256 = storage_module._replay_cutoff_binding_sha256(
            cutoff_id=cutoff_id,
            canonical_as_of=canonical_as_of,
            max_append_generation=max_generation,
            corpus_sha256=corpus_sha256,
        )
        intended_rows = tuple(
            sorted(
                (*prior_rows, (cutoff_id, canonical_as_of, max_generation)),
                key=lambda row: row[0],
            )
        )
        prior_state = store._replay_cutoff_authority_state_sha256(prior_rows)
        intended_state = storage_module._replay_cutoff_state_sha256(
            intended_rows,
            sealed_corpus_sha256=corpus_sha256,
        )
        assert intended_state is not None
        authority = store._replay_cutoff_authority()
        tx_id = f"{cutoff_id[:32]}-{tx_suffix * 32}"
        authority.prepare(
            tx_id=tx_id,
            observed_state_sha256=prior_state,
            intended_state_sha256=intended_state,
            semantic_binding_sha256=binding_sha256,
        )
        store.connection.execute(
            """INSERT INTO market_replay_cutoffs
               (cutoff_id, as_of, max_append_generation)
               VALUES (?, ?, ?)""",
            (cutoff_id, canonical_as_of, max_generation),
        )
        store.connection.commit()
        if commit_machine:
            authority.recover(
                observed_state_sha256=intended_state,
                tx_id=tx_id,
                semantic_binding_sha256=binding_sha256,
            )
        return tx_id, authority

    def test_cutoff_as_of_rejects_str_subclass_before_replace_hook(self) -> None:
        class HostileString(str):
            replace_called = False

            def replace(self, old, new, count=-1):
                type(self).replace_called = True
                raise AssertionError("hostile str.replace executed")

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                with self.assertRaisesRegex(ValueError, "as_of must be an exact string"):
                    store.replay_events_at_frozen_cutoff(
                        as_of=HostileString(self.CUTOFF.isoformat())
                    )
                self.assertFalse(HostileString.replace_called)
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM market_replay_cutoffs"
                    ).fetchone(),
                    (0,),
                )
            finally:
                store.close()

    def test_older_cutoff_cannot_retroactively_advance_append_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
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
                later = self.CUTOFF + timedelta(seconds=2)
                self.assertEqual(len(self.replay(store, as_of=later).events), 1)
                rows_before = store._validated_replay_cutoff_rows()
                history_before = store._replay_cutoff_authority().read_history()

                self.assertTrue(
                    store.append(
                        self.event(
                            sequence=2,
                            odds="2.10",
                            observed_ts="2026-09-16T18:59:59+00:00",
                            ingest_ts="2026-09-16T18:59:59+00:00",
                        )
                    )
                )

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "issuance retroactively advances append generation",
                ):
                    self.replay(
                        store,
                        as_of=self.CUTOFF - timedelta(seconds=1),
                    )

                self.assertEqual(store._validated_replay_cutoff_rows(), rows_before)
                self.assertEqual(
                    store._replay_cutoff_authority().read_history(),
                    history_before,
                )
            finally:
                store.close()

    def test_committed_cutoff_chain_rejects_retroactive_generation_advance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
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
                later = self.CUTOFF + timedelta(seconds=2)
                self.replay(store, as_of=later)
                prior_rows = store._validated_replay_cutoff_rows()

                self.assertTrue(
                    store.append(
                        self.event(
                            sequence=2,
                            odds="2.10",
                            observed_ts="2026-09-16T19:00:02+00:00",
                        )
                    )
                )
                self.forged_cutoff(
                    store,
                    as_of=self.CUTOFF,
                    max_generation=2,
                    prior_rows=prior_rows,
                    tx_suffix="5",
                    commit_machine=True,
                )

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "commit retroactively advances append generation",
                ):
                    self.replay(store, as_of=later)
            finally:
                store.close()

    def test_pending_retrograde_cutoff_is_not_machine_committed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
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
                later = self.CUTOFF + timedelta(seconds=2)
                self.replay(store, as_of=later)
                prior_rows = store._validated_replay_cutoff_rows()

                self.assertTrue(
                    store.append(
                        self.event(
                            sequence=2,
                            odds="2.10",
                            observed_ts="2026-09-16T19:00:02+00:00",
                        )
                    )
                )
                tx_id, authority = self.forged_cutoff(
                    store,
                    as_of=self.CUTOFF,
                    max_generation=2,
                    prior_rows=prior_rows,
                    tx_suffix="6",
                    commit_machine=False,
                )

                with self.assertRaisesRegex(
                    MonotonicAuthorityRollbackError,
                    "PREPARE retroactively advances append generation",
                ):
                    self.replay(store, as_of=later)

                history = authority.read_history()
                self.assertEqual(history[-1].phase.value, "PREPARE")
                self.assertEqual(history[-1].tx_id, tx_id)
            finally:
                store.close()


    def test_unseen_earlier_cutoff_can_reuse_same_append_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertTrue(store.append(first))

                far_future = self.CUTOFF + timedelta(days=365)
                future_snapshot = self.replay(store, as_of=far_future)
                self.assertEqual(
                    tuple(event.to_dict() for event in future_snapshot.events),
                    (first.to_dict(),),
                )

                earlier_snapshot = self.replay(store, as_of=self.CUTOFF)
                self.assertEqual(
                    tuple(event.to_dict() for event in earlier_snapshot.events),
                    (first.to_dict(),),
                )
                rows = store._validated_replay_cutoff_rows()
                self.assertEqual(len(rows), 2)
                self.assertEqual({row[2] for row in rows}, {1})
            finally:
                store.close()

    def test_previously_issued_earlier_cutoff_remains_readable_after_frontier_advances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                self.assertTrue(store.append(first))
                earlier = self.replay(store, as_of=self.CUTOFF)
                self.assertEqual(
                    tuple(event.to_dict() for event in earlier.events),
                    (first.to_dict(),),
                )

                second = self.event(
                    sequence=2,
                    odds="2.10",
                    observed_ts="2026-09-16T19:00:02+00:00",
                )
                self.assertTrue(store.append(second))
                later_cutoff = self.CUTOFF + timedelta(seconds=2)
                later = self.replay(store, as_of=later_cutoff)
                self.assertEqual(
                    tuple(event.sequence for event in later.events),
                    (2,),
                )
                rows_after_later = store._validated_replay_cutoff_rows()
                history_after_later = store._replay_cutoff_authority().read_history()

                earlier_again = self.replay(store, as_of=self.CUTOFF)
                self.assertEqual(
                    tuple(event.to_dict() for event in earlier_again.events),
                    (first.to_dict(),),
                )
                self.assertEqual(
                    store._validated_replay_cutoff_rows(),
                    rows_after_later,
                )
                self.assertEqual(
                    store._replay_cutoff_authority().read_history(),
                    history_after_later,
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
