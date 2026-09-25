import hashlib
import json
import unittest
from decimal import Decimal

from autosport.domain import MarketEvent
from autosport.replay import ReplayEngine


class ReplayTimestampOrderTests(unittest.TestCase):
    @staticmethod
    def _event(
        event_id: str,
        observed_ts: str,
        sequence: int,
        *,
        ingest_ts: str | None = None,
        decimal_odds: str = "2.0",
    ) -> MarketEvent:
        payload = {
            "event_id": event_id,
            "market_id": "m",
            "selection_id": "a",
            "decimal_odds": decimal_odds,
            "observed_ts": observed_ts,
            "source_id": "source",
            "sequence": sequence,
        }
        if ingest_ts is not None:
            payload["ingest_ts"] = ingest_ts
        return MarketEvent.from_dict(payload)

    def test_replay_orders_mixed_offsets_by_absolute_instant(self):
        later_lexically_first = self._event(
            "later",
            "2026-01-01T00:30:00+00:00",
            1,
        )
        earlier_lexically_last = self._event(
            "earlier",
            "2026-01-01T01:00:00+01:00",
            2,
        )

        seen: list[str] = []
        ReplayEngine([later_lexically_first, earlier_lexically_last]).run(
            lambda event: seen.append(event.event_id),
            run_id="absolute-order",
        )

        self.assertEqual(seen, ["earlier", "later"])

    def test_replay_orders_by_when_event_was_actually_available(self):
        late_old_quote = self._event(
            "late-old",
            "2026-01-01T00:00:00+00:00",
            1,
            ingest_ts="2026-01-01T00:10:00+00:00",
        )
        on_time_newer_quote = self._event(
            "on-time-newer",
            "2026-01-01T00:05:00+00:00",
            2,
            ingest_ts="2026-01-01T00:05:00+00:00",
        )

        seen: list[str] = []
        ReplayEngine([late_old_quote, on_time_newer_quote]).run(
            lambda event: seen.append(event.event_id),
            run_id="causal-availability-order",
        )

        self.assertEqual(seen, ["on-time-newer", "late-old"])

    def test_replay_waits_for_later_observation_or_ingest_clock(self):
        observed_late = self._event(
            "observed-late",
            "2026-01-01T00:10:00+00:00",
            1,
            ingest_ts="2026-01-01T00:00:00+00:00",
        )
        fully_available_earlier = self._event(
            "available-earlier",
            "2026-01-01T00:05:00+00:00",
            2,
            ingest_ts="2026-01-01T00:05:00+00:00",
        )

        seen: list[str] = []
        ReplayEngine([observed_late, fully_available_earlier]).run(
            lambda event: seen.append(event.event_id),
            run_id="both-clocks-causal",
        )

        self.assertEqual(seen, ["available-earlier", "observed-late"])

    @staticmethod
    def _dataset_hash_in_order(events: list[MarketEvent]) -> str:
        digest = hashlib.sha256()
        for event in events:
            canonical = json.dumps(
                event.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest.update(canonical.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def test_replay_suppresses_late_stale_sequence_for_same_quote(self):
        newer = self._event(
            "same-event",
            "2026-01-01T00:05:00+00:00",
            2,
            ingest_ts="2026-01-01T00:05:00+00:00",
        )
        late_stale = self._event(
            "same-event",
            "2026-01-01T00:00:00+00:00",
            1,
            ingest_ts="2026-01-01T00:10:00+00:00",
        )

        engine = ReplayEngine([late_stale, newer])
        seen: list[int] = []
        run = engine.run(lambda event: seen.append(event.sequence), run_id="stale-parity")

        self.assertEqual([event.sequence for event in engine.events], [2, 1])
        self.assertEqual(seen, [2])
        self.assertEqual(run.event_count, 2)

    def test_replay_suppresses_exact_duplicate_sequence_for_same_quote(self):
        first = self._event(
            "same-event",
            "2026-01-01T00:01:00+00:00",
            1,
            ingest_ts="2026-01-01T00:01:00+00:00",
        )
        duplicate = self._event(
            "same-event",
            "2026-01-01T00:00:30+00:00",
            1,
            ingest_ts="2026-01-01T00:02:00+00:00",
        )

        seen: list[int] = []
        run = ReplayEngine([duplicate, first]).run(
            lambda event: seen.append(event.sequence),
            run_id="duplicate-parity",
        )

        self.assertEqual(seen, [1])
        self.assertEqual(run.event_count, 2)

    def test_replay_rejects_conflicting_payload_reusing_sequence(self):
        first = self._event(
            "same-event",
            "2026-01-01T00:01:00+00:00",
            1,
            ingest_ts="2026-01-01T00:01:00+00:00",
            decimal_odds="2.0",
        )
        conflict = self._event(
            "same-event",
            "2026-01-01T00:00:30+00:00",
            1,
            ingest_ts="2026-01-01T00:02:00+00:00",
            decimal_odds="2.1",
        )
        seen: list[Decimal] = []

        with self.assertRaisesRegex(
            ValueError,
            "conflicting MarketEvent payload reused an existing source-local sequence",
        ):
            ReplayEngine([conflict, first]).run(
                lambda event: seen.append(event.decimal_odds),
                run_id="conflict-parity",
            )

        self.assertEqual(seen, [Decimal("2.0")])

    def test_dataset_hash_keeps_historical_observed_time_order(self):
        late_old = self._event(
            "old",
            "2026-01-01T00:00:00+00:00",
            1,
            ingest_ts="2026-01-01T00:10:00+00:00",
        )
        on_time_new = self._event(
            "new",
            "2026-01-01T00:05:00+00:00",
            2,
            ingest_ts="2026-01-01T00:05:00+00:00",
        )
        engine = ReplayEngine([on_time_new, late_old])

        historical_hash = self._dataset_hash_in_order([late_old, on_time_new])
        delivery_order_hash = self._dataset_hash_in_order([on_time_new, late_old])

        self.assertEqual([event.event_id for event in engine.events], ["new", "old"])
        self.assertEqual(engine.dataset_hash, historical_hash)
        self.assertNotEqual(engine.dataset_hash, delivery_order_hash)

    def test_replay_rejects_naive_ingest_timestamp_fail_closed(self):
        event = self._event(
            "naive-ingest",
            "2026-01-01T00:00:00+00:00",
            1,
            ingest_ts="2026-01-01T00:01:00",
        )
        with self.assertRaisesRegex(ValueError, "ingest_ts must include timezone"):
            ReplayEngine([event])

    def test_replay_rejects_naive_observed_timestamp_fail_closed(self):
        event = self._event("naive", "2026-01-01T00:00:00+00:00", 1)
        object.__setattr__(event, "observed_ts", "2026-01-01T00:00:00")
        with self.assertRaisesRegex(ValueError, "must include timezone"):
            ReplayEngine([event])


if __name__ == "__main__":
    unittest.main()
