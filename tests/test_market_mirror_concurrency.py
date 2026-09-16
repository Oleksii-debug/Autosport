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
    ) -> MarketEvent:
        return MarketEvent(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=Decimal(odds),
            observed_ts="2026-09-16T20:00:00+00:00",
            source_id=source,
            sequence=sequence,
            status="open",
            source_ts="2026-09-16T19:59:59+00:00",
            metadata={"seed": "canonical"},
        )

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

    def test_concurrent_updaters_and_readers_observe_coherent_monotonic_views(self) -> None:
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
                    view = mirror.view()
                    self.assertGreaterEqual(view.revision, previous_revision)
                    keys = tuple(
                        (event.source_id, event.quote_key) for event in view.events
                    )
                    self.assertEqual(len(keys), len(set(keys)))
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
        final = mirror.view()
        self.assertEqual(final.revision, writer_count * updates_per_writer)
        self.assertEqual(len(final.events), writer_count)
        self.assertEqual(
            {event.sequence for event in final.events},
            {updates_per_writer},
        )


if __name__ == "__main__":
    unittest.main()
