from datetime import datetime, timedelta, timezone
from decimal import Decimal
import threading
import unittest

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror, MirrorUpdate


class MarketMirrorConcurrencyTests(unittest.TestCase):
    @staticmethod
    def event(
        *,
        source: str = "provider-a",
        event_id: str = "event-1",
        market_id: str = "market-1",
        selection_id: str = "selection-1",
        sequence: int = 1,
        odds: str = "2.00",
        status: str = "open",
        observed_ts: str = "2026-09-16T20:00:00+00:00",
        source_ts: str | None = "2026-09-16T19:59:59+00:00",
    ) -> MarketEvent:
        return MarketEvent(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            ingest_ts=observed_ts,
            source_id=source,
            sequence=sequence,
            status=status,
            source_ts=source_ts,
            metadata={"seed": "canonical"},
        )

    @staticmethod
    def decision_time() -> datetime:
        return datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc)

    def test_revision_advances_only_for_material_applied_updates(self) -> None:
        mirror = MarketMirror()
        first = self.event(sequence=1)

        self.assertEqual(mirror.view().revision, 0)
        self.assertEqual(mirror.apply(first).status, MirrorUpdate.APPLIED)
        self.assertEqual(mirror.view().revision, 1)

        self.assertEqual(mirror.apply(first).status, MirrorUpdate.DUPLICATE)
        self.assertEqual(mirror.view().revision, 1)

        self.assertEqual(
            mirror.apply(self.event(sequence=2, odds="2.10")).status,
            MirrorUpdate.APPLIED,
        )
        self.assertEqual(mirror.view().revision, 2)

        self.assertEqual(
            mirror.apply(self.event(sequence=1, odds="1.50")).status,
            MirrorUpdate.STALE,
        )
        self.assertEqual(mirror.view().revision, 2)

        with self.assertRaises(ValueError):
            mirror.apply(self.event(sequence=2, odds="2.11"))
        self.assertEqual(mirror.view().revision, 2)

    def test_focused_view_filters_one_revision_without_creating_state_authority(self) -> None:
        mirror = MarketMirror()
        mirror.apply(
            self.event(
                source="provider-a",
                event_id="event-1",
                selection_id="selection-a",
            )
        )
        mirror.apply(
            self.event(
                source="provider-a",
                event_id="event-2",
                selection_id="selection-b",
            )
        )
        mirror.apply(
            self.event(
                source="provider-b",
                event_id="event-1",
                selection_id="selection-a",
            )
        )

        whole = mirror.view()
        focused = mirror.view(source_ids={"provider-a"}, event_ids={"event-1"})

        self.assertEqual(whole.revision, 3)
        self.assertEqual(focused.revision, whole.revision)
        self.assertEqual(len(whole.events), 3)
        self.assertEqual(len(focused.events), 1)
        self.assertEqual(focused.events[0].source_id, "provider-a")
        self.assertEqual(focused.events[0].event_id, "event-1")

        focused.events[0].metadata["consumer-local"] = True
        canonical = mirror.view(source_ids="provider-a", event_ids="event-1")
        self.assertNotIn("consumer-local", canonical.events[0].metadata)
        self.assertEqual(canonical.revision, whole.revision)

    def test_focused_active_view_applies_identity_and_freshness_in_one_revision(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event(selection_id="fresh"))
        mirror.apply(self.event(selection_id="suspended", status="suspended"))
        mirror.apply(
            self.event(
                selection_id="stale",
                source_ts="2026-09-16T19:40:00+00:00",
            )
        )
        mirror.apply(
            self.event(
                selection_id="future",
                source_ts="2026-09-16T20:00:01+00:00",
            )
        )
        mirror.apply(
            self.event(
                source="provider-b",
                selection_id="other-provider",
            )
        )
        bad_time = self.event(selection_id="bad-time", source_ts=None)
        bad_time = MarketEvent.from_dict(
            {
                **bad_time.to_dict(),
                "observed_ts": "not-a-timestamp",
            }
        )
        mirror.apply(bad_time)

        audit = mirror.view(source_ids="provider-a")
        decision = mirror.active_view(
            as_of=self.decision_time(),
            max_age=timedelta(minutes=5),
            source_ids="provider-a",
        )

        self.assertEqual(decision.revision, audit.revision)
        self.assertEqual(
            tuple(event.selection_id for event in decision.events),
            ("fresh",),
        )
        self.assertEqual(len(audit.events), 5)

    def test_active_view_validates_freshness_boundary_before_capture(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event())

        with self.assertRaises(ValueError):
            mirror.active_view(
                as_of=datetime(2026, 9, 16, 20, 0),
                max_age=timedelta(minutes=5),
                source_ids="provider-a",
            )
        with self.assertRaises(ValueError):
            mirror.active_view(
                as_of=self.decision_time(),
                max_age=timedelta(seconds=-1),
                source_ids="provider-a",
            )

    def test_concurrent_updaters_and_focused_decision_readers_observe_coherent_views(self) -> None:
        mirror = MarketMirror()
        writer_count = 4
        updates_per_writer = 40
        reader_count = 2
        reads_per_reader = 160
        start = threading.Barrier(writer_count + reader_count)
        errors: list[BaseException] = []

        def writer(index: int) -> None:
            try:
                start.wait()
                for sequence in range(1, updates_per_writer + 1):
                    result = mirror.apply(
                        self.event(
                            source=f"provider-{index}",
                            selection_id=f"selection-{index}",
                            sequence=sequence,
                        )
                    )
                    self.assertEqual(result.status, MirrorUpdate.APPLIED)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        def reader() -> None:
            try:
                start.wait()
                previous_revision = 0
                for _ in range(reads_per_reader):
                    view = mirror.active_view(
                        as_of=self.decision_time(),
                        max_age=timedelta(minutes=5),
                        source_ids={"provider-0", "provider-1"},
                    )
                    self.assertGreaterEqual(view.revision, previous_revision)
                    keys = tuple(
                        (event.source_id, event.quote_key) for event in view.events
                    )
                    self.assertEqual(len(keys), len(set(keys)))
                    self.assertTrue(
                        all(
                            event.source_id in {"provider-0", "provider-1"}
                            for event in view.events
                        )
                    )
                    previous_revision = view.revision
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(index,))
            for index in range(writer_count)
        ] + [threading.Thread(target=reader) for _ in range(reader_count)]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        final = mirror.active_view(
            as_of=self.decision_time(),
            max_age=timedelta(minutes=5),
        )
        self.assertEqual(final.revision, writer_count * updates_per_writer)
        self.assertEqual(len(final.events), writer_count)
        self.assertEqual(
            {event.sequence for event in final.events},
            {updates_per_writer},
        )


if __name__ == "__main__":
    unittest.main()
