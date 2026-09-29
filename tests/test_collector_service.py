import json
import signal
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    CursorRegressionError,
    GapState,
    SyncState,
    canonical_event_digest,
    digest_source_payload,
)
from autosport.collector_service import (
    CollectorServiceConfig,
    CollectorServiceError,
    CollectorServiceStoppedError,
    CollectorStorageLimitError,
    CollectorRetentionRequiredError,
    HeadlessCollectorService,
    ReadOnlyCollectorDeltaFeed,
    _SignalStopRequest,
)
from autosport.domain import MarketEvent
from autosport.event_lifecycle import CatalogEvent, CatalogPage, EventPhase
from autosport.providers import ProviderUnavailableError


class FakeCollectorSource:
    source_id = "source-x"
    stream_epoch = "epoch-1"

    def __init__(self, pages, batches):
        self.pages = list(pages)
        self.batches = list(batches)
        self.catalog_calls = 0
        self.delta_calls = 0

    def fetch_catalog_page(self, checkpoint):
        index = min(self.catalog_calls, len(self.pages) - 1)
        self.catalog_calls += 1
        return self.pages[index]

    def fetch_deltas(self, checkpoint, records, max_items):
        index = min(self.delta_calls, len(self.batches) - 1)
        self.delta_calls += 1
        return self.batches[index]


class UnavailableCatalogSource(FakeCollectorSource):
    def __init__(self, page, failures):
        super().__init__([page], [()])
        self.failures = failures

    def fetch_catalog_page(self, checkpoint):
        if self.failures:
            self.failures -= 1
            raise ProviderUnavailableError("secret-token-must-not-be-persisted")
        return super().fetch_catalog_page(checkpoint)


def market_payload(event_id="source-x:event-1", *, odds="1.80", sequence=1):
    return {
        "event_id": event_id,
        "market_id": "source-x:winner",
        "selection_id": "source-x:player-a",
        "decimal_odds": odds,
        "observed_ts": "2026-01-01T00:00:01+00:00",
        "source_id": "source-x",
        "sequence": sequence,
        "market_type": "winner",
        "status": "open",
        "source_ts": "2026-01-01T00:00:00+00:00",
        "ingest_ts": "2026-01-01T00:00:01+00:00",
        "metadata": {},
        "score_state": None,
        "sport": "table_tennis",
    }


def make_delta(
    *,
    delta_id="d1",
    position=1,
    event_id="source-x:event-1",
    revision_of=None,
    revision_number=0,
    gap_state=GapState.NONE,
    sync_state=None,
):
    payload = market_payload(event_id, sequence=max(position, 1))
    event = MarketEvent.from_dict(payload)
    if sync_state is None:
        sync_state = {
            GapState.NONE: SyncState.READY,
            GapState.DETECTED: SyncState.GAP_DETECTED,
            GapState.RECOVERED: SyncState.RECOVERED,
            GapState.CURSOR_RESET: SyncState.CURSOR_RESET,
        }[gap_state]
    raw_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="source-x",
        lawful_terms_ref="terms:source-x:v1",
        retention_ref="retention:source-x:v1",
        stream_epoch="epoch-1",
        source_cursor=str(position),
        cursor_position=position,
        event_dedupe_key=event.dedupe_key,
        event_id=event.event_id,
        source_payload_digest=digest_source_payload(raw_payload),
        canonical_event_digest=canonical_event_digest(event),
        source_observed_at="2026-01-01T00:00:01+00:00",
        collector_received_at="2026-01-01T00:00:02+00:00",
        collector_committed_at="2026-01-01T00:00:03+00:00",
        desktop_available_at="2026-01-01T00:00:04+00:00",
        revision_of=revision_of,
        revision_number=revision_number,
        gap_state=gap_state,
        sync_state=sync_state,
        gap_from_cursor=(
            "1" if gap_state in {GapState.DETECTED, GapState.RECOVERED} else None
        ),
        gap_to_cursor=(
            "2" if gap_state in {GapState.DETECTED, GapState.RECOVERED} else None
        ),
    )


def catalog_page(position, *event_ids):
    events = tuple(
        CatalogEvent(
            source_id="source-x",
            sport="table_tennis",
            event_id=event_id,
            phase=EventPhase.PRE_MATCH,
            available_at=f"2026-01-01T00:00:0{position}+00:00",
        )
        for event_id in event_ids
    )
    return CatalogPage(
        source_id="source-x",
        stream_epoch="catalog-epoch-1",
        cursor=f"catalog-{position}",
        position=position,
        events=events,
    )


class HeadlessCollectorServiceTests(unittest.TestCase):
    def make_service(
        self,
        root,
        source,
        *,
        run_id="run-1",
        clock=None,
        sleep=None,
        random_value=None,
        config=None,
        stop_requested=None,
        stop_reason=None,
    ):
        from autosport.event_lifecycle import ContinuousEventLifecycle

        return HeadlessCollectorService(
            delta_store=CollectorDeltaStore(Path(root) / "collector.json"),
            lifecycle=ContinuousEventLifecycle(Path(root) / "catalog.json"),
            source=source,
            state_path=Path(root) / "service.json",
            run_id=run_id,
            config=config
            or CollectorServiceConfig(
                poll_interval_seconds=1,
                initial_backoff_seconds=1,
                max_backoff_seconds=4,
                jitter_fraction=0,
            ),
            clock=clock or (lambda: "2026-01-01T00:00:10+00:00"),
            sleep=sleep or (lambda _: None),
            random_value=random_value or (lambda: 0),
            stop_requested=stop_requested,
            stop_reason=stop_reason,
        )

    def test_runtime_epoch_activation_is_durable_and_revalidated_each_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1")
            epoch_2_delta = replace(
                make_delta(
                    delta_id="e2-d1",
                    position=0,
                    gap_state=GapState.CURSOR_RESET,
                ),
                stream_epoch="epoch-2",
            )
            epoch_1_return = make_delta(
                delta_id="e1-d1",
                position=0,
                gap_state=GapState.CURSOR_RESET,
            )
            source = FakeCollectorSource(
                [page, page],
                [(epoch_2_delta,), (epoch_1_return,)],
            )
            service = self.make_service(tmp, source)
            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 1),
            )

            source.stream_epoch = "epoch-2"
            service.run_cycle()
            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-2", 2),
            )
            reopened = CollectorDeltaStore(Path(tmp) / "collector.json")
            self.assertEqual(
                reopened.runtime_stream_epoch("source-x"),
                ("epoch-2", 2),
            )

            # Reactivating a previously used epoch is a new generation, so a
            # retention plan previewed under generation 2 cannot survive ABA.
            source.stream_epoch = "epoch-1"
            service.run_cycle()
            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 3),
            )

    def test_existing_durable_history_blocks_source_only_activation_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            epoch_2_delta = replace(
                make_delta(
                    delta_id="e2-preexisting",
                    position=0,
                    gap_state=GapState.CURSOR_RESET,
                ),
                stream_epoch="epoch-2",
            )
            self.assertTrue(store.append(epoch_2_delta))
            self.assertIsNone(store.runtime_stream_epoch("source-x"))

            page = catalog_page(1, "event-1")
            source = FakeCollectorSource([page], [()])
            source.stream_epoch = "epoch-1"
            service = self.make_service(tmp, source)

            self.assertIsNone(
                service.delta_store.runtime_stream_epoch("source-x")
            )
            empty = service.run_cycle()
            self.assertEqual(empty.committed_delta_ids, ())
            self.assertIsNone(
                service.delta_store.runtime_stream_epoch("source-x")
            )

    def test_restart_does_not_activate_changed_epoch_without_durable_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1")
            first = self.make_service(
                tmp,
                FakeCollectorSource([page], [()]),
            )
            self.assertEqual(
                first.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 1),
            )

            epoch_2_delta = replace(
                make_delta(
                    delta_id="e2-d1",
                    position=0,
                    gap_state=GapState.CURSOR_RESET,
                ),
                stream_epoch="epoch-2",
            )
            source = FakeCollectorSource(
                [page, page],
                [(), (epoch_2_delta,)],
            )
            source.stream_epoch = "epoch-2"
            reopened = self.make_service(tmp, source)
            self.assertEqual(
                reopened.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 1),
            )

            empty = reopened.run_cycle()
            self.assertEqual(empty.committed_delta_ids, ())
            self.assertEqual(
                reopened.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 1),
            )

            admitted = reopened.run_cycle()
            self.assertEqual(admitted.committed_delta_ids, ("e2-d1",))
            self.assertEqual(
                reopened.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-2", 2),
            )

    def test_delta_and_epoch_activation_roll_back_together_on_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1")
            epoch_2_delta = replace(
                make_delta(
                    delta_id="e2-crash",
                    position=0,
                    gap_state=GapState.CURSOR_RESET,
                ),
                stream_epoch="epoch-2",
            )
            source = FakeCollectorSource([page], [(epoch_2_delta,)])
            service = self.make_service(tmp, source)
            source.stream_epoch = "epoch-2"

            def crash_before_commit(_raw):
                raise RuntimeError("simulated crash before durable commit")

            service.delta_store._write = crash_before_commit
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                service.run_cycle()

            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 1),
            )
            self.assertEqual(
                service.delta_store.deltas_after_commit(source_id="source-x"),
                (),
            )
            reopened = CollectorDeltaStore(Path(tmp) / "collector.json")
            self.assertEqual(
                reopened.runtime_stream_epoch("source-x"),
                ("epoch-1", 1),
            )
            self.assertEqual(
                reopened.deltas_after_commit(source_id="source-x"),
                (),
            )

    def test_restart_heals_legacy_durable_epoch_crash_prefix_before_empty_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1")
            first = self.make_service(
                tmp,
                FakeCollectorSource([page], [()]),
            )
            epoch_2_delta = replace(
                make_delta(
                    delta_id="e2-legacy-crash",
                    position=0,
                    gap_state=GapState.CURSOR_RESET,
                ),
                stream_epoch="epoch-2",
            )

            # Reproduce the predecessor crash prefix: durable E2 evidence exists,
            # but the older service died before publishing the matching activation.
            self.assertTrue(first.delta_store.append(epoch_2_delta))
            self.assertEqual(
                first.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 1),
            )

            source = FakeCollectorSource([page], [()])
            source.stream_epoch = "epoch-2"
            reopened = self.make_service(tmp, source)
            self.assertEqual(
                reopened.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-2", 2),
            )

            empty = reopened.run_cycle()
            self.assertEqual(empty.committed_delta_ids, ())
            self.assertEqual(empty.duplicate_delta_ids, ())
            self.assertEqual(
                reopened.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-2", 2),
            )

    def test_duplicate_replay_can_heal_durable_epoch_without_new_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1")
            epoch_2_delta = replace(
                make_delta(
                    delta_id="e2-duplicate",
                    position=0,
                    gap_state=GapState.CURSOR_RESET,
                ),
                stream_epoch="epoch-2",
            )
            source = FakeCollectorSource([page], [(epoch_2_delta,)])
            service = self.make_service(tmp, source)

            self.assertTrue(service.delta_store.append(epoch_2_delta))
            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 1),
            )

            source.stream_epoch = "epoch-2"
            replay = service.run_cycle()
            self.assertEqual(replay.committed_delta_ids, ())
            self.assertEqual(replay.duplicate_delta_ids, ("e2-duplicate",))
            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-2", 2),
            )

    def test_old_duplicate_cannot_aba_regress_active_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1")
            epoch_1_first = make_delta(delta_id="e1-first", position=1)
            epoch_2 = replace(
                make_delta(
                    delta_id="e2-first",
                    position=0,
                    gap_state=GapState.CURSOR_RESET,
                ),
                stream_epoch="epoch-2",
            )
            epoch_1_return = make_delta(delta_id="e1-return", position=2)
            source = FakeCollectorSource(
                [page, page, page, page],
                [
                    (epoch_1_first,),
                    (epoch_2,),
                    (epoch_1_first,),
                    (epoch_1_return,),
                ],
            )
            service = self.make_service(tmp, source)

            first = service.run_cycle()
            self.assertEqual(first.committed_delta_ids, ("e1-first",))
            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 1),
            )

            source.stream_epoch = "epoch-2"
            second = service.run_cycle()
            self.assertEqual(second.committed_delta_ids, ("e2-first",))
            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-2", 2),
            )

            source.stream_epoch = "epoch-1"
            replay = service.run_cycle()
            self.assertEqual(replay.committed_delta_ids, ())
            self.assertEqual(replay.duplicate_delta_ids, ("e1-first",))
            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-2", 2),
            )

            # A genuinely new durable E1 commit is a new transition witness and
            # may reactivate E1 with a fresh monotonic generation.
            returned = service.run_cycle()
            self.assertEqual(returned.committed_delta_ids, ("e1-return",))
            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 3),
            )

    def test_replaced_source_instance_cannot_mint_epoch_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1")
            source = FakeCollectorSource([page], [()])
            service = self.make_service(tmp, source)
            replacement = FakeCollectorSource([page], [()])
            replacement.stream_epoch = "epoch-2"
            service.source = replacement

            with self.assertRaisesRegex(
                CollectorServiceError,
                "source instance cannot be replaced",
            ):
                service.run_cycle()

            self.assertEqual(replacement.catalog_calls, 0)
            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 1),
            )

    def test_restart_reuses_durable_delta_identity_without_duplicate_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1")
            delta = make_delta()
            first_source = FakeCollectorSource([page], [(delta,)])
            first = self.make_service(tmp, first_source)
            result = first.run_cycle()
            self.assertEqual(result.committed_delta_ids, ("d1",))
            self.assertEqual(result.duplicate_delta_ids, ())

            reopened_source = FakeCollectorSource([page], [(delta,)])
            reopened = self.make_service(tmp, reopened_source)
            second = reopened.run_cycle()
            self.assertEqual(second.committed_delta_ids, ())
            self.assertEqual(second.duplicate_delta_ids, ("d1",))
            self.assertEqual(
                [
                    item.delta_id
                    for item in reopened.delta_store.deltas_after_commit(
                        source_id="source-x"
                    )
                ],
                ["d1"],
            )
            status = reopened.status()
            self.assertEqual(status["cycles_succeeded"], 2)
            self.assertEqual(status["deltas_committed"], 1)
            self.assertEqual(status["duplicate_deltas"], 1)

    def test_persisted_stop_blocks_reopen_until_explicit_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1")
            delta = make_delta()
            first = self.make_service(
                tmp, FakeCollectorSource([page], [(delta,)])
            )
            first.stop("maintenance")
            self.assertEqual(first.status()["stop_reason"], "maintenance")

            reopened_source = FakeCollectorSource([page], [(delta,)])
            reopened = self.make_service(tmp, reopened_source)
            with self.assertRaises(CollectorServiceStoppedError):
                reopened.run(max_cycles=1)
            self.assertEqual(reopened_source.catalog_calls, 0)
            self.assertEqual(reopened_source.delta_calls, 0)
            self.assertEqual(reopened.status()["stop_reason"], "maintenance")

            reopened.resume()
            result = reopened.run_cycle()
            self.assertEqual(result.committed_delta_ids, ("d1",))
            self.assertEqual(reopened_source.catalog_calls, 1)
            self.assertEqual(reopened_source.delta_calls, 1)
            self.assertIsNone(reopened.status()["stopped_at"])
            self.assertIsNone(reopened.status()["stop_reason"])

    def test_sigint_and_sigterm_stop_after_current_provider_cycle(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum), tempfile.TemporaryDirectory() as tmp:
                stop = _SignalStopRequest()
                page = catalog_page(1, "event-1")
                delta = make_delta()

                class SignalDuringDeltaSource(FakeCollectorSource):
                    def fetch_deltas(self, checkpoint, records, max_items):
                        result = super().fetch_deltas(
                            checkpoint, records, max_items
                        )
                        stop.handle(signum, None)
                        return result

                source = SignalDuringDeltaSource([page], [(delta,)])
                service = self.make_service(
                    tmp,
                    source,
                    stop_requested=stop,
                    stop_reason=stop.reason,
                )
                result = service.run(max_cycles=3)
                self.assertEqual(result.cycles_executed, 1)
                self.assertEqual(source.catalog_calls, 1)
                self.assertEqual(source.delta_calls, 1)
                self.assertEqual(
                    service.status()["stop_reason"],
                    f"signal:{signal.Signals(signum).name}",
                )

                reopened_source = FakeCollectorSource([page], [(delta,)])
                reopened = self.make_service(tmp, reopened_source)
                with self.assertRaises(CollectorServiceStoppedError):
                    reopened.run(max_cycles=1)
                self.assertEqual(reopened_source.catalog_calls, 0)
                self.assertEqual(reopened_source.delta_calls, 0)

    def test_post_start_catalog_discovery_is_collected_in_later_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_page = catalog_page(1, "event-1")
            second_page = catalog_page(2, "event-1", "event-2")
            d1 = make_delta(
                delta_id="d1", position=1, event_id="source-x:event-1"
            )
            d2 = make_delta(
                delta_id="d2", position=2, event_id="source-x:event-2"
            )
            source = FakeCollectorSource(
                [first_page, second_page],
                [(d1,), (d2,)],
            )
            service = self.make_service(tmp, source)
            result = service.run(max_cycles=2)
            self.assertEqual(result.cycles_executed, 2)
            self.assertEqual(result.last_cycle.committed_delta_ids, ("d2",))
            self.assertEqual(
                [
                    item.delta_id
                    for item in service.delta_store.deltas_after_commit(
                        source_id="source-x"
                    )
                ],
                ["d1", "d2"],
            )
            records = service.lifecycle.records()
            self.assertEqual(
                [item.event_id for item in records],
                ["event-1", "event-2"],
            )
            self.assertEqual(
                service.status()["stop_reason"], "max_cycles_reached"
            )

    def test_run_keeps_only_last_cycle_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1")
            delta = make_delta()
            source = FakeCollectorSource([page], [(delta,)])
            service = self.make_service(tmp, source)
            result = service.run(max_cycles=3)
            self.assertEqual(result.cycles_executed, 3)
            self.assertEqual(result.last_cycle.duplicate_delta_ids, ("d1",))
            self.assertFalse(hasattr(result, "results"))

    def test_provider_retry_is_bounded_and_redacts_provider_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            sleeps = []
            source = UnavailableCatalogSource(
                catalog_page(1, "event-1"), failures=99
            )
            service = self.make_service(
                tmp,
                source,
                sleep=sleeps.append,
                config=CollectorServiceConfig(
                    poll_interval_seconds=1,
                    retry_attempts=3,
                    initial_backoff_seconds=1,
                    max_backoff_seconds=4,
                    jitter_fraction=0,
                ),
            )
            source.stream_epoch = "epoch-2"
            result = service.run_cycle()
            self.assertTrue(result.provider_unavailable)
            self.assertEqual(sleeps, [1, 2])
            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 1),
            )
            state_text = (Path(tmp) / "service.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("secret-token-must-not-be-persisted", state_text)
            self.assertEqual(
                service.status()["last_error_code"],
                "ProviderUnavailableError",
            )
            self.assertEqual(service.status()["provider_failures"], 1)
            self.assertEqual(service.status()["cycles_succeeded"], 0)

    def test_local_storage_budget_failure_is_not_provider_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = FakeCollectorSource([catalog_page(1, "event-1")], [()])
            service = self.make_service(
                tmp,
                source,
                config=CollectorServiceConfig(
                    poll_interval_seconds=1,
                    retry_attempts=3,
                    initial_backoff_seconds=1,
                    max_backoff_seconds=4,
                    jitter_fraction=0,
                    max_store_bytes=1,
                ),
            )
            source.stream_epoch = "epoch-2"
            with self.assertRaises(CollectorRetentionRequiredError) as caught:
                service.run_cycle()
            self.assertEqual(
                service.delta_store.runtime_stream_epoch("source-x"),
                ("epoch-1", 1),
            )
            self.assertIsInstance(caught.exception, CollectorStorageLimitError)
            self.assertEqual(caught.exception.code, "RETENTION_REQUIRED")
            self.assertEqual(source.catalog_calls, 0)
            self.assertEqual(service.status()["provider_failures"], 0)
            self.assertEqual(
                service.status()["last_error_code"],
                "CollectorRetentionRequiredError",
            )

    def test_wrong_source_delta_fails_closed_without_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1")
            wrong = replace(make_delta(), source_id="other-source")
            source = FakeCollectorSource([page], [(wrong,)])
            service = self.make_service(tmp, source)
            with self.assertRaises(CollectorServiceError):
                service.run_cycle()
            self.assertEqual(service.status()["cycles_succeeded"], 0)
            self.assertIsNone(service.delta_store.get("d1"))

    def test_delta_for_undiscovered_event_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1")
            undiscovered = make_delta(
                delta_id="d2",
                position=2,
                event_id="source-x:event-2",
            )
            source = FakeCollectorSource([page], [(undiscovered,)])
            service = self.make_service(tmp, source)
            with self.assertRaises(CollectorServiceError):
                service.run_cycle()
            self.assertEqual(service.status()["cycles_succeeded"], 0)
            self.assertIsNone(service.delta_store.get("d2"))

    def test_missing_feed_commit_order_keeps_late_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            d1 = make_delta(delta_id="d1", position=1)
            d2 = make_delta(
                delta_id="d2",
                position=2,
                event_id="source-x:event-2",
            )
            d1r = make_delta(
                delta_id="d1r",
                position=1,
                revision_of="d1",
                revision_number=1,
            )
            store.append(d1)
            store.append(d2)
            store.append(d1r)

            feed = ReadOnlyCollectorDeltaFeed(
                store, source_id="source-x"
            )
            first = feed.read_page(max_items=2)
            self.assertEqual(
                [item.delta_id for item in first.deltas], ["d1", "d2"]
            )
            self.assertTrue(first.has_more)
            self.assertEqual(first.next_after_delta_id, "d2")

            second = feed.read_page(
                after_delta_id=first.next_after_delta_id,
                max_items=2,
            )
            self.assertEqual(
                [item.delta_id for item in second.deltas], ["d1r"]
            )
            self.assertFalse(second.has_more)

    def test_missing_feed_rejects_unknown_transport_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            store.append(make_delta())
            feed = ReadOnlyCollectorDeltaFeed(
                store, source_id="source-x"
            )
            with self.assertRaises(CursorRegressionError):
                feed.read_page(after_delta_id="unknown", max_items=10)

    def test_source_batch_bound_fails_before_any_delta_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = catalog_page(1, "event-1", "event-2")
            source = FakeCollectorSource(
                [page],
                [
                    (
                        make_delta(delta_id="d1", position=1),
                        make_delta(
                            delta_id="d2",
                            position=2,
                            event_id="source-x:event-2",
                        ),
                    )
                ],
            )
            service = self.make_service(
                tmp,
                source,
                config=CollectorServiceConfig(
                    max_items=1,
                    poll_interval_seconds=1,
                    initial_backoff_seconds=1,
                    max_backoff_seconds=4,
                    jitter_fraction=0,
                ),
            )
            with self.assertRaises(CollectorServiceError):
                service.run_cycle()
            self.assertEqual(
                service.delta_store.deltas_after_commit(
                    source_id="source-x"
                ),
                (),
            )


class CollectorServiceConfigTests(unittest.TestCase):
    def test_backoff_and_storage_bounds_are_strict(self):
        with self.assertRaises(ValueError):
            CollectorServiceConfig(poll_interval_seconds=0)
        with self.assertRaises(ValueError):
            CollectorServiceConfig(retry_attempts=0)
        with self.assertRaises(ValueError):
            CollectorServiceConfig(
                initial_backoff_seconds=2,
                max_backoff_seconds=1,
            )
        with self.assertRaises(ValueError):
            CollectorServiceConfig(jitter_fraction=1.1)
        with self.assertRaises(ValueError):
            CollectorServiceConfig(max_store_bytes=0)
        with self.assertRaises(ValueError):
            CollectorServiceConfig(max_items=5001)
        with self.assertRaises(ValueError):
            CollectorServiceConfig(retry_attempts=11)

    def test_delta_feed_page_bound_is_hard_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            feed = ReadOnlyCollectorDeltaFeed(store, source_id="source-x")
            with self.assertRaises(ValueError):
                feed.read_page(max_items=5001)


if __name__ == "__main__":
    unittest.main()
