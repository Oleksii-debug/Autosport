import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    GapState,
    SyncState,
    canonical_event_digest,
    digest_source_payload,
)
from autosport.collector_service import CollectorServiceConfig, HeadlessCollectorService
from autosport.domain import MarketEvent
from autosport.event_lifecycle import CatalogEvent, CatalogPage, ContinuousEventLifecycle, EventPhase
from autosport.providers import ProviderUnavailableError
from autosport.source_universe_commitment import (
    SourceUniverseCommitment,
    SourceUniverseCommitmentError,
    build_source_universe_commitment,
    verify_source_universe_commitment,
)


class _SequenceSource:
    source_id = "source-x"
    stream_epoch = "epoch-1"

    def __init__(self, catalog_actions, delta_actions):
        self._catalog_actions = list(catalog_actions)
        self._delta_actions = list(delta_actions)

    @staticmethod
    def _next(actions):
        action = actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action

    def fetch_catalog_page(self, checkpoint):
        return self._next(self._catalog_actions)

    def fetch_deltas(self, checkpoint, records, max_items):
        return self._next(self._delta_actions)


def _catalog_page(position=1, *event_ids):
    return CatalogPage(
        source_id="source-x",
        stream_epoch="catalog-epoch-1",
        cursor=f"catalog-{position}",
        position=position,
        events=tuple(
            CatalogEvent(
                source_id="source-x",
                sport="table_tennis",
                event_id=event_id,
                phase=EventPhase.PRE_MATCH,
                available_at="2026-01-01T00:00:01+00:00",
            )
            for event_id in event_ids
        ),
    )


def _delta(delta_id="d1"):
    payload = {
        "event_id": "source-x:event-1",
        "market_id": "source-x:winner",
        "selection_id": "source-x:player-a",
        "decimal_odds": "1.80",
        "observed_ts": "2026-01-01T00:00:01+00:00",
        "source_id": "source-x",
        "sequence": 1,
        "market_type": "winner",
        "status": "open",
        "source_ts": "2026-01-01T00:00:00+00:00",
        "ingest_ts": "2026-01-01T00:00:01+00:00",
        "metadata": {},
        "score_state": None,
        "sport": "table_tennis",
    }
    event = MarketEvent.from_dict(payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="source-x",
        lawful_terms_ref="terms:source-x:v1",
        retention_ref="retention:source-x:v1",
        stream_epoch="epoch-1",
        source_cursor="1",
        cursor_position=1,
        event_dedupe_key=event.dedupe_key,
        event_id=event.event_id,
        source_payload_digest=digest_source_payload(raw),
        canonical_event_digest=canonical_event_digest(event),
        source_observed_at="2026-01-01T00:00:01+00:00",
        collector_received_at="2026-01-01T00:00:02+00:00",
        collector_committed_at="2026-01-01T00:00:03+00:00",
        desktop_available_at="2026-01-01T00:00:04+00:00",
        gap_state=GapState.NONE,
        sync_state=SyncState.READY,
    )


def _finish_zero_result_cycle(store, *, sequence, run_id="run-1"):
    attempted_second = 4 + sequence * 2
    completed_second = attempted_second + 1
    cycle_seq = store._begin_collector_cycle(
        source_id="source-x",
        run_id=run_id,
        stream_epoch="epoch-1",
        attempted_at=f"2026-01-01T00:00:{attempted_second:02d}+00:00",
    )
    store._finish_collector_cycle(
        source_id="source-x",
        cycle_seq=cycle_seq,
        status="SUCCESS",
        completed_at=f"2026-01-01T00:00:{completed_second:02d}+00:00",
        catalog_changes=(),
        observed_delta_ids=(),
        committed_delta_ids=(),
        duplicate_delta_ids=(),
    )
    return cycle_seq


class SourceUniverseCommitmentTests(unittest.TestCase):
    def _service(self, root, source):
        return HeadlessCollectorService(
            delta_store=CollectorDeltaStore(Path(root) / "collector.db"),
            lifecycle=ContinuousEventLifecycle(Path(root) / "catalog.json"),
            source=source,
            state_path=Path(root) / "service.json",
            run_id="run-1",
            config=CollectorServiceConfig(
                retry_attempts=1,
                poll_interval_seconds=1,
                initial_backoff_seconds=1,
                max_backoff_seconds=1,
                jitter_fraction=0,
            ),
            clock=lambda: "2026-01-01T00:00:10+00:00",
            sleep=lambda _: None,
            random_value=lambda: 0,
        )

    def test_zero_result_and_provider_failure_remain_in_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = _SequenceSource(
                [
                    _catalog_page(1),
                    ProviderUnavailableError("secret provider diagnostic"),
                ],
                [()],
            )
            service = self._service(tmp, source)

            first = service.run_cycle()
            second = service.run_cycle()
            self.assertEqual(first.committed_delta_ids, ())
            self.assertTrue(second.provider_unavailable)

            commitment = build_source_universe_commitment(
                service.delta_store,
                expected_store_path=service.delta_store.path,
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=2,
            )
            self.assertEqual(commitment.cycle_count, 2)
            self.assertEqual(commitment.success_count, 1)
            self.assertEqual(commitment.zero_result_success_count, 1)
            self.assertEqual(commitment.provider_unavailable_count, 1)
            self.assertEqual(commitment.pending_count, 0)
            self.assertTrue(commitment.observation_ledger_complete)
            self.assertFalse(commitment.provider_observation_complete)
            self.assertFalse(commitment.external_provider_universe_complete)
            self.assertFalse(commitment.promotion_ready)

            evidence = service.delta_store.collector_cycle_evidence(
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=2,
            )
            self.assertEqual(
                tuple(item["terminal"]["status"] for item in evidence),
                ("SUCCESS", "PROVIDER_UNAVAILABLE"),
            )
            self.assertNotIn(
                "secret provider diagnostic",
                json.dumps(evidence, sort_keys=True),
            )

    def test_success_terminal_binds_exact_durable_delta_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            delta = _delta()
            self.assertTrue(store.append(delta))
            seq = store._begin_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                attempted_at="2026-01-01T00:00:05+00:00",
            )
            store._finish_collector_cycle(
                source_id="source-x",
                cycle_seq=seq,
                status="SUCCESS",
                completed_at="2026-01-01T00:00:06+00:00",
                catalog_changes=(),
                observed_delta_ids=("d1",),
                committed_delta_ids=("d1",),
                duplicate_delta_ids=(),
            )

            evidence = store.collector_cycle_evidence(
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
            )
            observed = evidence[0]["terminal"]["observed_deltas"]
            self.assertEqual(observed[0]["delta_id"], "d1")
            self.assertEqual(len(observed[0]["payload_sha256"]), 64)

            commitment = build_source_universe_commitment(
                store,
                expected_store_path=store.path,
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
            )
            self.assertTrue(commitment.provider_observation_complete)
            self.assertEqual(commitment.observed_delta_occurrence_count, 1)
            self.assertEqual(commitment.observed_unique_delta_count, 1)
            self.assertEqual(len(commitment.cycle_evidence_sha256), 64)
            self.assertEqual(len(commitment.commitment_sha256), 64)

    def test_pending_cycle_is_explicit_and_cannot_mint_complete_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            store._begin_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                attempted_at="2026-01-01T00:00:05+00:00",
            )
            commitment = build_source_universe_commitment(
                store,
                expected_store_path=store.path,
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
            )
            self.assertEqual(commitment.pending_count, 1)
            self.assertFalse(commitment.observation_ledger_complete)
            self.assertFalse(commitment.provider_observation_complete)
            self.assertFalse(commitment.promotion_ready)

    def test_terminal_cannot_reference_noncanonical_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            seq = store._begin_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                attempted_at="2026-01-01T00:00:05+00:00",
            )
            with self.assertRaisesRegex(
                ValueError,
                "without canonical source evidence",
            ):
                store._finish_collector_cycle(
                    source_id="source-x",
                    cycle_seq=seq,
                    status="SUCCESS",
                    completed_at="2026-01-01T00:00:06+00:00",
                    catalog_changes=(),
                    observed_delta_ids=("invented",),
                    committed_delta_ids=("invented",),
                    duplicate_delta_ids=(),
                )

    def test_cycle_rows_are_sql_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            seq = store._begin_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                attempted_at="2026-01-01T00:00:05+00:00",
            )
            store._finish_collector_cycle(
                source_id="source-x",
                cycle_seq=seq,
                status="SUCCESS",
                completed_at="2026-01-01T00:00:06+00:00",
                catalog_changes=(),
                observed_delta_ids=(),
                committed_delta_ids=(),
                duplicate_delta_ids=(),
            )

            connection = sqlite3.connect(path)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "UPDATE collector_cycle_starts_v1 "
                        "SET attempted_at='2030-01-01T00:00:00+00:00' "
                        "WHERE source_id='source-x' AND cycle_seq=1"
                    )
                connection.rollback()
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "DELETE FROM collector_cycle_terminals_v1 "
                        "WHERE source_id='source-x' AND cycle_seq=1"
                    )
            finally:
                connection.rollback()
                connection.close()

    def test_explicit_window_must_be_contiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            store._begin_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                attempted_at="2026-01-01T00:00:05+00:00",
            )
            with self.assertRaisesRegex(
                SourceUniverseCommitmentError,
                "incomplete or non-contiguous",
            ):
                build_source_universe_commitment(
                    store,
                    expected_store_path=store.path,
                    source_id="source-x",
                    start_cycle_seq=1,
                    end_cycle_seq=2,
                )

    def test_builder_rejects_store_subclass_before_forged_evidence_dispatch(self):
        class ForgedCollectorDeltaStore(CollectorDeltaStore):
            def collector_cycle_evidence(self, **_kwargs):
                return (
                    {
                        "source_id": "source-x",
                        "cycle_seq": 1,
                        "terminal": {
                            "status": "SUCCESS",
                            "observed_deltas": [],
                        },
                    },
                )

        with tempfile.TemporaryDirectory() as tmp:
            store = ForgedCollectorDeltaStore(Path(tmp) / "collector.db")
            with self.assertRaisesRegex(TypeError, "exact canonical CollectorDeltaStore"):
                build_source_universe_commitment(
                    store,
                    expected_store_path=store.path,
                    source_id="source-x",
                    start_cycle_seq=1,
                    end_cycle_seq=1,
                )

    def test_builder_rejects_instance_rebound_cycle_evidence_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            store._begin_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                attempted_at="2026-01-01T00:00:05+00:00",
            )
            store.collector_cycle_evidence = lambda **_kwargs: (
                {
                    "source_id": "source-x",
                    "cycle_seq": 1,
                    "terminal": {
                        "status": "SUCCESS",
                        "observed_deltas": [],
                    },
                },
            )

            with self.assertRaisesRegex(TypeError, "instance-rebound"):
                build_source_universe_commitment(
                    store,
                    expected_store_path=store.path,
                    source_id="source-x",
                    start_cycle_seq=1,
                    end_cycle_seq=1,
                )

    def test_builder_rejects_rebound_internal_durable_read_seams(self):
        for seam_name in (
            "_connect",
            "_connect_path",
            "_cycle_terminal_payload_sha256",
        ):
            with self.subTest(seam_name=seam_name), tempfile.TemporaryDirectory() as tmp:
                store = CollectorDeltaStore(Path(tmp) / "collector.db")
                store._begin_collector_cycle(
                    source_id="source-x",
                    run_id="run-1",
                    stream_epoch="epoch-1",
                    attempted_at="2026-01-01T00:00:05+00:00",
                )
                setattr(store, seam_name, getattr(store, seam_name))

                with self.assertRaisesRegex(TypeError, "instance-rebound"):
                    build_source_universe_commitment(
                        store,
                        expected_store_path=store.path,
                        source_id="source-x",
                        start_cycle_seq=1,
                        end_cycle_seq=1,
                    )

    def test_product_expected_store_path_rejects_decoy_database_rebind(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical_path = Path(tmp) / "canonical.db"
            decoy_path = Path(tmp) / "decoy.db"
            store = CollectorDeltaStore(canonical_path)
            _finish_zero_result_cycle(store, sequence=1)

            candidate = build_source_universe_commitment(
                store,
                expected_store_path=canonical_path,
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
            )
            decoy = CollectorDeltaStore(decoy_path)
            _finish_zero_result_cycle(decoy, sequence=1)
            store.path = decoy_path

            with self.assertRaisesRegex(
                SourceUniverseCommitmentError,
                "does not match product-expected authority path",
            ):
                build_source_universe_commitment(
                    store,
                    expected_store_path=canonical_path,
                    source_id="source-x",
                    start_cycle_seq=1,
                    end_cycle_seq=1,
                )
            with self.assertRaisesRegex(
                SourceUniverseCommitmentError,
                "does not match product-expected authority path",
            ):
                verify_source_universe_commitment(
                    store,
                    candidate,
                    expected_store_path=canonical_path,
                    expected_source_id="source-x",
                    expected_start_cycle_seq=1,
                    expected_end_cycle_seq=1,
                )


    def test_verifier_accepts_canonical_candidate_and_returns_rebuilt_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            _finish_zero_result_cycle(store, sequence=1)
            candidate = build_source_universe_commitment(
                store,
                expected_store_path=store.path,
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
            )

            verified = verify_source_universe_commitment(
                store,
                candidate,
                expected_store_path=store.path,
                expected_source_id="source-x",
                expected_start_cycle_seq=1,
                expected_end_cycle_seq=1,
            )

            self.assertIsNot(verified, candidate)
            self.assertEqual(verified.to_dict(), candidate.to_dict())

    def test_verifier_rejects_caller_minted_positive_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            store._begin_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                attempted_at="2026-01-01T00:00:05+00:00",
            )
            canonical = build_source_universe_commitment(
                store,
                expected_store_path=store.path,
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
            )
            forged_payload = canonical.to_dict()
            forged_payload["observation_ledger_complete"] = True
            forged_payload["provider_observation_complete"] = True
            forged_payload["commitment_sha256"] = "1" * 64
            forged = SourceUniverseCommitment._issue(forged_payload)

            with self.assertRaisesRegex(
                SourceUniverseCommitmentError,
                "does not match canonical expected scope",
            ):
                verify_source_universe_commitment(
                    store,
                    forged,
                    expected_store_path=store.path,
                    expected_source_id="source-x",
                    expected_start_cycle_seq=1,
                    expected_end_cycle_seq=1,
                )

    def test_verifier_rejects_stale_candidate_after_durable_cycle_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            cycle_seq = store._begin_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                attempted_at="2026-01-01T00:00:05+00:00",
            )
            stale = build_source_universe_commitment(
                store,
                expected_store_path=store.path,
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
            )
            self.assertEqual(stale.pending_count, 1)

            store._finish_collector_cycle(
                source_id="source-x",
                cycle_seq=cycle_seq,
                status="SUCCESS",
                completed_at="2026-01-01T00:00:06+00:00",
                catalog_changes=(),
                observed_delta_ids=(),
                committed_delta_ids=(),
                duplicate_delta_ids=(),
            )

            with self.assertRaisesRegex(
                SourceUniverseCommitmentError,
                "does not match canonical expected scope",
            ):
                verify_source_universe_commitment(
                    store,
                    stale,
                    expected_store_path=store.path,
                    expected_source_id="source-x",
                    expected_start_cycle_seq=1,
                    expected_end_cycle_seq=1,
                )

    def test_verifier_rejects_valid_candidate_for_different_expected_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            _finish_zero_result_cycle(store, sequence=1)
            _finish_zero_result_cycle(store, sequence=2)
            favorable_smaller_window = build_source_universe_commitment(
                store,
                expected_store_path=store.path,
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
            )
            self.assertTrue(favorable_smaller_window.provider_observation_complete)

            with self.assertRaisesRegex(
                SourceUniverseCommitmentError,
                "does not match canonical expected scope",
            ):
                verify_source_universe_commitment(
                    store,
                    favorable_smaller_window,
                    expected_store_path=store.path,
                    expected_source_id="source-x",
                    expected_start_cycle_seq=1,
                    expected_end_cycle_seq=2,
                )

    def test_commitment_truth_flags_are_not_constructor_inputs(self):
        with self.assertRaises(TypeError):
            SourceUniverseCommitment(
                schema_version=1,
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
                cycle_count=1,
                success_count=1,
                zero_result_success_count=0,
                provider_unavailable_count=0,
                local_failure_count=0,
                stop_requested_count=0,
                pending_count=0,
                observed_delta_occurrence_count=0,
                observed_unique_delta_count=0,
                cycle_evidence_sha256="0" * 64,
                commitment_sha256="1" * 64,
                observation_ledger_complete=True,
                provider_observation_complete=True,
                external_provider_universe_complete=True,
                promotion_ready=True,
            )


if __name__ == "__main__":
    unittest.main()
