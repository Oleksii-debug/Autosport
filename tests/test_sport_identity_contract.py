from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.dataset import ReplayDataset
from autosport.domain import MarketEvent, TicketLeg
from autosport.paper import PaperBook
from autosport.parlayapi_provider import ParlayApiTableTennisProvider
from autosport.session import AutosportSession
from autosport.storage import SQLiteMarketStore


class SportIdentityContractTests(unittest.TestCase):
    @staticmethod
    def _event(
        *,
        sport: str | None,
        event_id: str = "event-1",
        market_id: str = "market-1",
        selection_id: str = "selection-1",
        source_id: str = "provider-a",
        sequence: int = 1,
    ) -> MarketEvent:
        return MarketEvent(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=Decimal("2.10"),
            observed_ts="2026-09-18T12:00:00+00:00",
            source_id=source_id,
            sequence=sequence,
            ingest_ts="2026-09-18T12:00:01+00:00",
            sport=sport,
        )

    @staticmethod
    def _dataset(root: Path, *, sport: str, events: list[MarketEvent], schema_version: int = 3) -> ReplayDataset:
        market_path = root / "market.jsonl"
        market_payload = "".join(
            json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in events
        ).encode("utf-8")
        market_path.write_bytes(market_payload)

        results_path = root / "results.json"
        results_payload = json.dumps(
            {"schema_version": 1, "quote_outcomes": {}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        results_path.write_bytes(results_payload)

        return ReplayDataset(
            root=root,
            name="sport-contract",
            sport=sport,
            market_path=market_path,
            results_path=results_path,
            market_sha256=hashlib.sha256(market_payload).hexdigest(),
            results_sha256=hashlib.sha256(results_payload).hexdigest(),
            schema_version=schema_version,
        )

    def test_same_provider_local_ids_do_not_alias_across_sports(self) -> None:
        table_tennis = self._event(sport="table_tennis")
        soccer = self._event(sport="soccer")

        self.assertNotEqual(table_tennis.quote_key, soccer.quote_key)
        self.assertNotEqual(table_tennis.dedupe_key, soccer.dedupe_key)
        self.assertTrue(table_tennis.quote_key.startswith("sport-v1|table_tennis|"))
        self.assertTrue(soccer.quote_key.startswith("sport-v1|soccer|"))

        tt_leg = TicketLeg("event-1", "market-1", "selection-1", Decimal("2"), sport="table_tennis")
        soccer_leg = TicketLeg("event-1", "market-1", "selection-1", Decimal("2"), sport="soccer")
        self.assertNotEqual(tt_leg.quote_key, soccer_leg.quote_key)

    def test_legacy_identity_is_preserved_without_inventing_sport(self) -> None:
        event = self._event(sport=None)
        self.assertEqual(event.quote_key, "event-1|market-1|selection-1")
        self.assertNotIn("sport", event.to_dict())

        restored = MarketEvent.from_dict(event.to_dict())
        self.assertIsNone(restored.sport)
        self.assertEqual(restored.quote_key, event.quote_key)

    def test_serialized_sport_must_be_canonical(self) -> None:
        raw = self._event(sport=None).to_dict()
        raw["sport"] = "Table_Tennis"
        with self.assertRaisesRegex(ValueError, "lowercase canonical sport identity"):
            MarketEvent.from_dict(raw)

    def test_direct_event_and_ticket_sport_must_be_canonical(self) -> None:
        with self.assertRaises(ValueError):
            self._event(sport="Table_Tennis")
        with self.assertRaises(ValueError):
            TicketLeg(
                "event-1",
                "market-1",
                "selection-1",
                Decimal("2"),
                sport="table|tennis",
            )

    def test_schema_v3_single_and_mixed_sport_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            single = self._dataset(
                root,
                sport="table_tennis",
                events=[self._event(sport="table_tennis")],
            )
            events = single.load_market_events()
            self.assertEqual(single._assert_sport_scope(events), ("table_tennis",))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mixed = self._dataset(
                root,
                sport="mixed",
                events=[
                    self._event(sport="table_tennis", sequence=1),
                    self._event(sport="soccer", sequence=2),
                ],
            )
            events = mixed.load_market_events()
            self.assertEqual(mixed._assert_sport_scope(events), ("soccer", "table_tennis"))

    def test_schema_v3_mismatch_fails_before_session_economic_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as data_tmp, tempfile.TemporaryDirectory() as work_tmp:
            dataset = self._dataset(
                Path(data_tmp),
                sport="table_tennis",
                events=[self._event(sport="soccer")],
            )
            session = AutosportSession(work_tmp, "1000", strategy_id="observe-only-v1")
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "market event sport scope contradicts dataset manifest.sport",
                ):
                    session.run_dataset(dataset)
                self.assertEqual(session.registry.strategy_ids(), ())
                self.assertFalse((Path(work_tmp) / "paper_book.json").exists())
                self.assertEqual(tuple(Path(work_tmp).glob("run-*.json")), ())
            finally:
                session.close()

    def test_legacy_dataset_rejects_event_level_sport_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = self._dataset(
                Path(tmp),
                sport="table_tennis",
                events=[self._event(sport="table_tennis")],
                schema_version=2,
            )
            with self.assertRaisesRegex(
                ValueError,
                "legacy dataset schema cannot make event-level sport claims",
            ):
                dataset.load_market_events()

    def test_paperbook_schema_v6_round_trip_and_v5_backward_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper_book.json"
            book = PaperBook("100")
            leg = TicketLeg(
                "event-1",
                "market-1",
                "selection-1",
                Decimal("2.00"),
                sport="table_tennis",
            )
            ticket = book.open_ticket([leg], "10")
            book.save(path)

            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema_version"], 6)
            self.assertEqual(raw["tickets"][0]["legs"][0]["sport"], "table_tennis")

            restored = PaperBook.load(path)
            self.assertEqual(restored.tickets[ticket.ticket_id].legs[0].sport, "table_tennis")

            raw["schema_version"] = 5
            for item in raw["tickets"]:
                for item_leg in item["legs"]:
                    item_leg.pop("sport", None)
            path.write_text(json.dumps(raw), encoding="utf-8")
            legacy = PaperBook.load(path)
            self.assertIsNone(legacy.tickets[ticket.ticket_id].legs[0].sport)

    def test_sqlite_restart_preserves_cross_sport_identity_without_aliasing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.db"
            table_tennis = self._event(sport="table_tennis", sequence=1)
            soccer = self._event(sport="soccer", sequence=1)
            store = SQLiteMarketStore(path)
            try:
                self.assertTrue(store.append(table_tennis))
                self.assertTrue(store.append(soccer))
                current = store.current_by_source()
                self.assertEqual(len(current), 2)
                self.assertEqual(
                    {event.sport for event in current.values()},
                    {"soccer", "table_tennis"},
                )
            finally:
                store.close()

            reopened = SQLiteMarketStore(path)
            try:
                restored = reopened.events()
                self.assertEqual(len(restored), 2)
                self.assertEqual(
                    {event.quote_key for event in restored},
                    {table_tennis.quote_key, soccer.quote_key},
                )
                self.assertEqual(
                    {event.sport for event in restored},
                    {"soccer", "table_tennis"},
                )
            finally:
                reopened.close()

    def test_sport_scope_and_identity_are_bounded_at_20k_events(self) -> None:
        dataset = ReplayDataset(
            root=Path("."),
            name="bounded-20k",
            sport="table_tennis",
            market_path=Path("unused-market"),
            results_path=Path("unused-results"),
            market_sha256="0" * 64,
            results_sha256="0" * 64,
            schema_version=3,
        )
        events = [
            self._event(
                sport="table_tennis",
                event_id=f"event-{index}",
                sequence=index + 1,
            )
            for index in range(20_000)
        ]

        self.assertEqual(dataset._assert_sport_scope(events), ("table_tennis",))
        self.assertEqual(len({event.quote_key for event in events}), 20_000)
        self.assertEqual(len({event.dedupe_key for event in events}), 20_000)

    def test_parlayapi_adapter_declares_table_tennis_sport(self) -> None:
        self.assertEqual(ParlayApiTableTennisProvider.sport_key, "table_tennis")


if __name__ == "__main__":
    unittest.main()
