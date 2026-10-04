import hashlib
import json
import unittest
from decimal import Decimal

from autosport.domain import MarketEvent
from autosport.replay import (
    ReplayEngine,
    ReplayExecutionReceipt,
    market_event_payload_multiset_sha256,
    market_event_payload_sequence_sha256,
    market_event_payload_sha256,
    verify_replay_execution_receipt,
)


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

    def test_replay_binds_exact_constructor_payload_sequence(self):
        later = self._event(
            "later",
            "2026-01-01T00:10:00+00:00",
            2,
            ingest_ts="2026-01-01T00:10:00+00:00",
        )
        earlier = self._event(
            "earlier",
            "2026-01-01T00:00:00+00:00",
            1,
            ingest_ts="2026-01-01T00:00:00+00:00",
        )
        expected_input = market_event_payload_sequence_sha256(
            (
                market_event_payload_sha256(later),
                market_event_payload_sha256(earlier),
            )
        )
        expected_consumed = market_event_payload_sequence_sha256(
            (
                market_event_payload_sha256(earlier),
                market_event_payload_sha256(later),
            )
        )

        run = ReplayEngine([later, earlier]).run(
            lambda _event: None,
            run_id="payload-sequence",
        )

        self.assertEqual(
            run.input_event_payload_sequence_sha256,
            expected_input,
        )
        self.assertEqual(
            run.consumed_event_payload_sequence_sha256,
            expected_consumed,
        )
        self.assertNotEqual(
            run.input_event_payload_sequence_sha256,
            run.consumed_event_payload_sequence_sha256,
        )

    def test_applied_payload_sequence_excludes_suppressed_stale_event(self):
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
        run = ReplayEngine([late_stale, newer]).run(
            lambda _event: None,
            run_id="applied-payload-sequence",
        )

        self.assertEqual(
            run.applied_event_payload_sequence_sha256,
            market_event_payload_sequence_sha256(
                (market_event_payload_sha256(newer),)
            ),
        )
        self.assertNotEqual(
            run.applied_event_payload_sequence_sha256,
            run.consumed_event_payload_sequence_sha256,
        )

    def test_event_payload_digest_ignores_subclass_serializer_override(self):
        base = self._event(
            "base-event",
            "2026-01-01T00:00:00+00:00",
            1,
        )

        class ForgedEvent(MarketEvent):
            def to_dict(self):
                return {"forged": True}

        forged = ForgedEvent(**{
            field: getattr(base, field)
            for field in MarketEvent.__dataclass_fields__
        })

        self.assertEqual(
            market_event_payload_sha256(forged),
            market_event_payload_sha256(base),
        )

    def test_consumed_payload_multiset_preserves_duplicate_multiplicity(self):
        first = self._event(
            "event-a",
            "2026-01-01T00:00:00+00:00",
            1,
        )
        second = self._event(
            "event-b",
            "2026-01-01T00:01:00+00:00",
            2,
        )
        first_sha = market_event_payload_sha256(first)
        second_sha = market_event_payload_sha256(second)

        run = ReplayEngine([second, first, second]).run(
            lambda _event: None,
            run_id="multiset-multiplicity",
        )

        self.assertEqual(
            run.consumed_event_payload_multiset_sha256,
            market_event_payload_multiset_sha256(
                (second_sha, first_sha, second_sha)
            ),
        )
        self.assertNotEqual(
            run.consumed_event_payload_multiset_sha256,
            market_event_payload_multiset_sha256((second_sha, first_sha)),
        )

    def test_event_payload_multiset_is_order_independent(self):
        left = ("a" * 64, "b" * 64, "a" * 64)
        right = ("a" * 64, "a" * 64, "b" * 64)
        self.assertEqual(
            market_event_payload_multiset_sha256(left),
            market_event_payload_multiset_sha256(right),
        )

    def test_replay_execution_receipt_is_product_issued_and_verifiable(self):
        event = self._event(
            "receipt-event",
            "2026-01-01T00:00:00+00:00",
            1,
        )
        run = ReplayEngine([event]).run(
            lambda _event: None,
            run_id="receipt-run",
        )

        receipt = run.execution_receipt
        self.assertIs(type(receipt), ReplayExecutionReceipt)
        self.assertIs(verify_replay_execution_receipt(receipt), receipt)
        self.assertEqual(receipt.run_id, run.run_id)
        self.assertEqual(receipt.dataset_hash, run.dataset_hash)
        self.assertEqual(receipt.event_count, run.event_count)
        self.assertEqual(
            receipt.consumed_event_payload_multiset_sha256,
            run.consumed_event_payload_multiset_sha256,
        )

        with self.assertRaisesRegex(TypeError, "product-issued"):
            ReplayExecutionReceipt(
                run_id=run.run_id,
                dataset_hash=run.dataset_hash,
                event_count=run.event_count,
                input_event_payload_sequence_sha256=(
                    run.input_event_payload_sequence_sha256
                ),
                consumed_event_payload_sequence_sha256=(
                    run.consumed_event_payload_sequence_sha256
                ),
                applied_event_payload_sequence_sha256=(
                    run.applied_event_payload_sequence_sha256
                ),
                consumed_event_payload_multiset_sha256=(
                    run.consumed_event_payload_multiset_sha256
                ),
                receipt_sha256="0" * 64,
            )

    def test_replay_execution_receipt_rejects_field_forgery(self):
        event = self._event(
            "receipt-forgery",
            "2026-01-01T00:00:00+00:00",
            1,
        )
        receipt = ReplayEngine([event]).run(
            lambda _event: None,
            run_id="receipt-forgery-run",
        ).execution_receipt
        self.assertIs(type(receipt), ReplayExecutionReceipt)

        forged = object.__new__(ReplayExecutionReceipt)
        for field_name in ReplayExecutionReceipt.__dataclass_fields__:
            object.__setattr__(forged, field_name, getattr(receipt, field_name))
        object.__setattr__(
            forged,
            "consumed_event_payload_multiset_sha256",
            "0" * 64,
        )

        with self.assertRaisesRegex(ValueError, "authenticator mismatch"):
            verify_replay_execution_receipt(forged)

    def test_replay_execution_receipt_cannot_be_subclassed(self):
        with self.assertRaisesRegex(TypeError, "must not be subclassed"):
            class ForgedReceipt(ReplayExecutionReceipt):
                pass

    def test_event_payload_sequence_rejects_noncanonical_digest(self):
        with self.assertRaisesRegex(
            ValueError,
            "lowercase SHA-256",
        ):
            market_event_payload_sequence_sha256(("A" * 64,))

    def test_replay_rejects_naive_ingest_timestamp_fail_closed(self):
        event = self._event(
            "naive-ingest",
            "2026-01-01T00:00:00+00:00",
            1,
            ingest_ts="2026-01-01T00:01:00+00:00",
        )
        object.__setattr__(event, "ingest_ts", "2026-01-01T00:01:00")
        with self.assertRaisesRegex(ValueError, "replay event must be canonical"):
            ReplayEngine([event])

    def test_replay_rejects_naive_observed_timestamp_fail_closed(self):
        event = self._event("naive", "2026-01-01T00:00:00+00:00", 1)
        object.__setattr__(event, "observed_ts", "2026-01-01T00:00:00")
        with self.assertRaisesRegex(ValueError, "replay event must be canonical"):
            ReplayEngine([event])


if __name__ == "__main__":
    unittest.main()
