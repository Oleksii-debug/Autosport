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


def _epoch_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def _market_definition(*, event_id: str, status: str) -> dict:
    runner_status = "WINNER" if status == "CLOSED" else "ACTIVE"
    return {
        "eventId": event_id,
        "eventTypeId": "2593174",
        "marketType": "MATCH_ODDS",
        "status": status,
        "eventName": f"{event_id} table tennis match",
        "runners": [
            {"id": 101, "name": "Player A", "status": runner_status},
        ],
    }


def _stream(*, market_id: str, event_id: str, open_ts: str = "2026-02-10T12:00:00Z") -> list[dict]:
    return [
        {
            "op": "mcm",
            "pt": _epoch_ms(open_ts),
            "mc": [
                {
                    "id": market_id,
                    "marketDefinition": _market_definition(event_id=event_id, status="OPEN"),
                    "rc": [{"id": 101, "ltp": 1.8}],
                }
            ],
        },
        {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T13:00:00Z"),
            "mc": [
                {
                    "id": market_id,
                    "marketDefinition": _market_definition(event_id=event_id, status="CLOSED"),
                }
            ],
        },
    ]


def _write_bz2(path: Path, lines: list[dict]) -> None:
    with bz2.open(path, "wt", encoding="utf-8") as handle:
        for item in lines:
            handle.write(json.dumps(item, sort_keys=True) + "\n")


class BetfairHistoricalTruthTests(unittest.TestCase):
    def test_ltp_semantics_survive_canonical_load_and_identity_bound_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "market.bz2"
            output = root / "dataset"
            _write_bz2(source, _stream(market_id="1.100", event_id="event-1"))

            report = import_betfair_historical(
                [source],
                output,
                acquired_at="2026-02-10T13:30:00Z",
                imported_at="2026-02-10T14:00:00Z",
                terms_reference="test-rights",
                retention_basis="test-retention",
            )

            dataset = load_dataset(output)
            event = dataset.load_market_events()[0]
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(dataset.import_identity, report.import_identity)
        self.assertEqual(event.metadata["price_semantics"], "betfair_last_traded_price")
        self.assertEqual(event.metadata["provider_price_field"], "rc[].ltp")
        self.assertIs(event.metadata["execution_quote_verified"], False)
        self.assertEqual(
            manifest["governance"]["price_semantics"],
            {
                "decimal_odds": "betfair_last_traded_price",
                "provider_field": "rc[].ltp",
                "execution_quote_verified": False,
            },
        )
        self.assertEqual(
            manifest["governance"]["availability_semantics"],
            {
                "strategy_visible_market_status": "OPEN_QUOTES_PLUS_EXPLICIT_SOURCE_STATE_TRANSITIONS",
                "definition_state_transitions_preserved": True,
                "suspended_or_non_open_intervals_preserved": False,
                "complete_availability_history_verified": False,
            },
        )

    def test_non_executable_ltp_cannot_open_paper_value_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "market.bz2"
            output = root / "dataset"
            _write_bz2(source, _stream(market_id="1.100", event_id="event-1"))
            import_betfair_historical(
                [source],
                output,
                acquired_at="2026-02-10T13:30:00Z",
                imported_at="2026-02-10T14:00:00Z",
                terms_reference="test-rights",
                retention_basis="test-retention",
            )
            event = load_dataset(output).load_market_events()[0]

        context = AgentContext(paper_book=PaperBook("1000"))
        agent = PaperValueAgent(
            {
                event.quote_key: Forecast(
                    quote_key=event.quote_key,
                    probability=Decimal("0.90"),
                    model_id="truth-boundary-test",
                    as_of_ts=event.observed_ts,
                )
            },
            stake="50",
            minimum_expected_profit_per_unit="0",
        )
        agent.on_market_event(event, context)

        self.assertEqual(context.paper_book.balance, Decimal("1000"))
        self.assertEqual(context.paper_book.tickets, {})

    def test_equal_publish_time_across_different_source_files_fails_closed_in_either_input_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.bz2"
            second = root / "second.bz2"
            _write_bz2(first, _stream(market_id="1.100", event_id="event-1"))
            _write_bz2(second, _stream(market_id="1.200", event_id="event-2"))

            for index, inputs in enumerate(((first, second), (second, first)), start=1):
                with self.subTest(input_order=index):
                    output = root / f"dataset-{index}"
                    with self.assertRaisesRegex(
                        ValueError,
                        "replay-visible events share publish time across input files.*cross-file source order is ambiguous",
                    ):
                        import_betfair_historical(
                            inputs,
                            output,
                            acquired_at="2026-02-10T13:30:00Z",
                            imported_at="2026-02-10T14:00:00Z",
                            terms_reference="test-rights",
                            retention_basis="test-retention",
                        )
                    self.assertFalse(output.exists())

    def test_distinct_publish_times_across_source_files_remain_importable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.bz2"
            second = root / "second.bz2"
            output = root / "dataset"
            _write_bz2(first, _stream(market_id="1.100", event_id="event-1"))
            _write_bz2(
                second,
                _stream(
                    market_id="1.200",
                    event_id="event-2",
                    open_ts="2026-02-10T12:01:00Z",
                ),
            )

            import_betfair_historical(
                [first, second],
                output,
                acquired_at="2026-02-10T13:30:00Z",
                imported_at="2026-02-10T14:00:00Z",
                terms_reference="test-rights",
                retention_basis="test-retention",
            )

            events = load_dataset(output).load_market_events()

        self.assertEqual(len(events), 2)
        self.assertEqual([event.sequence for event in events], [1, 2])


if __name__ == "__main__":
    unittest.main()
