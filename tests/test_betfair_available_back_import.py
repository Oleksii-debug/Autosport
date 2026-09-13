from __future__ import annotations

import bz2
import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext
from autosport.betfair_historical_import import import_betfair_historical
from autosport.dataset import load_dataset
from autosport.paper import PaperBook
from autosport.paper_strategy import Forecast, PaperValueAgent
from autosport.price_truth import paper_quote_rejection_reason


def _epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _definition(*, status: str, bet_delay: int = 0) -> dict:
    runners = [{"id": 101, "name": "Player A", "status": "ACTIVE"}]
    if status == "CLOSED":
        runners = [{"id": 101, "name": "Player A", "status": "WINNER"}]
    return {
        "eventId": "event-available-back",
        "eventTypeId": "2593174",
        "marketType": "MATCH_ODDS",
        "status": status,
        "eventName": "Player A v Player B",
        "inPlay": bet_delay > 0,
        "betDelay": bet_delay,
        "runners": runners,
    }


def _write(path: Path, lines: list[dict]) -> None:
    with bz2.open(path, "wt", encoding="utf-8") as handle:
        for item in lines:
            handle.write(json.dumps(item, sort_keys=True) + "\n")


def _pro_stream(*, bet_delay: int = 0, image: bool = True) -> list[dict]:
    return [
        {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T12:00:00Z"),
            "mc": [
                {
                    "id": "1.advanced",
                    "img": image,
                    "marketDefinition": _definition(status="OPEN", bet_delay=bet_delay),
                    "rc": [{"id": 101, "ltp": 1.84, "atb": [[1.8, 100.0], [1.9, 40.0]]}],
                }
            ],
        },
        {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T12:05:00Z"),
            "mc": [
                {
                    "id": "1.advanced",
                    "rc": [{"id": 101, "atb": [[1.9, 0], [1.85, 60.0]]}],
                }
            ],
        },
        {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T13:00:00Z"),
            "mc": [
                {
                    "id": "1.advanced",
                    "marketDefinition": _definition(status="CLOSED", bet_delay=bet_delay),
                }
            ],
        },
    ]


class BetfairAvailableBackImportTests(unittest.TestCase):
    def _import(self, root: Path, lines: list[dict]):
        source = root / "market.bz2"
        output = root / "dataset"
        _write(source, lines)
        import_betfair_historical(
            [source],
            output,
            acquired_at="2026-02-10T13:30:00Z",
            imported_at="2026-02-10T14:00:00Z",
            terms_reference="test-rights",
            retention_basis="test-retention",
        )
        return output, load_dataset(output).load_market_events()

    def test_pro_atb_image_and_deltas_become_capacity_bound_paper_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, events = self._import(Path(tmp), _pro_stream())
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual([event.decimal_odds for event in events], [Decimal("1.9"), Decimal("1.85")])
        self.assertEqual(events[0].metadata["price_semantics"], "betfair_available_to_back")
        self.assertEqual(events[0].metadata["provider_price_field"], "rc[].atb")
        self.assertIs(events[0].metadata["execution_quote_verified"], True)
        self.assertIs(events[0].metadata["actual_fill_verified"], False)
        self.assertIs(events[0].metadata["paper_fill_eligible"], True)
        self.assertEqual(events[0].metadata["paper_fill_available_size"], "40.0")
        self.assertEqual(events[1].metadata["paper_fill_available_size"], "60.0")
        self.assertIs(manifest["governance"]["price_semantics"]["actual_fill_verified"], False)
        self.assertIs(manifest["governance"]["price_semantics"]["paper_fill_capacity_enforced"], True)

        # Stake 50 cannot use the first 40-unit quote, but can use the later 60-unit quote.
        # Keep the bankroll large enough that the canonical 2% per-ticket risk cap is not
        # the reason for rejection; this test isolates observed quote-capacity semantics.
        book = PaperBook("10000")
        agent = PaperValueAgent(
            {
                events[0].quote_key: Forecast(
                    quote_key=events[0].quote_key,
                    probability=Decimal("0.90"),
                    model_id="capacity-test",
                    as_of_ts=events[0].observed_ts,
                )
            },
            stake="50",
            minimum_expected_profit_per_unit="0",
        )
        context = AgentContext(paper_book=book)
        agent.on_market_event(events[0], context)
        self.assertEqual(book.tickets, {})
        agent.on_market_event(events[1], context)
        self.assertEqual(len(book.tickets), 1)
        ticket = next(iter(book.tickets.values()))
        self.assertEqual(ticket.legs[0].locked_odds, Decimal("1.85"))

    def test_positive_bet_delay_preserves_quote_but_blocks_paper_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _output, events = self._import(Path(tmp), _pro_stream(bet_delay=2))

        event = events[0]
        self.assertIs(event.metadata["execution_quote_verified"], True)
        self.assertIs(event.metadata["paper_fill_eligible"], False)
        self.assertEqual(event.metadata["betfair_bet_delay_seconds"], 2)
        self.assertIn("positive Betfair betDelay", paper_quote_rejection_reason(event, "10"))

    def test_delta_without_provider_image_is_observed_but_not_verified_for_paper_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _output, events = self._import(Path(tmp), _pro_stream(image=False))

        self.assertFalse(events[0].metadata["execution_quote_verified"])
        self.assertFalse(events[0].metadata["paper_fill_capacity_verified"])
        self.assertFalse(events[0].metadata["paper_fill_eligible"])
        self.assertIn("not a verified executable quote", paper_quote_rejection_reason(events[0], "1"))

    def test_advanced_batb_level_zero_is_imported_as_best_available_back(self) -> None:
        lines = _pro_stream()
        first = lines[0]["mc"][0]["rc"][0]
        first.pop("atb")
        first["batb"] = [[0, 2.1, 25.0], [1, 2.08, 50.0], [2, 2.06, 100.0]]
        second = lines[1]["mc"][0]["rc"][0]
        second.pop("atb")
        second["batb"] = [[0, 2.04, 30.0]]

        with tempfile.TemporaryDirectory() as tmp:
            _output, events = self._import(Path(tmp), lines)

        self.assertEqual([event.decimal_odds for event in events], [Decimal("2.1"), Decimal("2.04")])
        self.assertEqual(events[0].metadata["provider_price_field"], "rc[].batb")
        self.assertEqual(events[0].metadata["betfair_ladder_kind"], "best_three_level_ladder")


if __name__ == "__main__":
    unittest.main()
