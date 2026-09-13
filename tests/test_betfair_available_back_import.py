from __future__ import annotations

import bz2
import json
import tempfile
import unittest
from dataclasses import replace
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
        "priceLadderDefinition": {"type": "CLASSIC"},
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

    def test_pro_atb_preserves_quote_and_size_but_unbound_units_block_paper_economics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, events = self._import(Path(tmp), _pro_stream())
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual([event.decimal_odds for event in events], [Decimal("1.9"), Decimal("1.85")])
        self.assertEqual(events[0].metadata["price_semantics"], "betfair_available_to_back")
        self.assertEqual(events[0].metadata["provider_price_field"], "rc[].atb")
        self.assertEqual(events[0].metadata["betfair_price_ladder_type"], "CLASSIC")
        self.assertIs(events[0].metadata["execution_price_ladder_verified"], True)
        self.assertIs(events[0].metadata["execution_quote_verified"], True)
        self.assertIs(events[0].metadata["actual_fill_verified"], False)
        self.assertIs(events[0].metadata["paper_fill_eligible"], False)
        self.assertIs(events[0].metadata["paper_fill_capacity_verified"], False)
        self.assertIs(events[0].metadata["paper_fill_capacity_unit_bound"], False)
        self.assertEqual(events[0].metadata["paper_fill_available_size"], "40.0")
        self.assertEqual(events[1].metadata["paper_fill_available_size"], "60.0")
        self.assertIn(
            "not canonically bound",
            events[0].metadata["paper_fill_eligibility_reason"],
        )
        price_truth = manifest["governance"]["price_semantics"]
        self.assertIs(price_truth["actual_fill_verified"], False)
        self.assertIs(price_truth["execution_price_ladder_contract_verified"], True)
        self.assertEqual(price_truth["supported_execution_price_ladder_types"], ["CLASSIC"])
        self.assertIs(price_truth["runner_roster_membership_verified"], True)
        self.assertIs(price_truth["paper_fill_capacity_enforced"], False)
        self.assertIs(price_truth["paper_fill_capacity_unit_bound"], False)
        self.assertIs(price_truth["paper_fill_capacity_authorizes_economics"], False)

        book = PaperBook("10000")
        agent = PaperValueAgent(
            {
                events[0].quote_key: Forecast(
                    quote_key=events[0].quote_key,
                    probability=Decimal("0.90"),
                    model_id="unit-boundary-test",
                    as_of_ts=events[0].observed_ts,
                )
            },
            stake="1",
            minimum_expected_profit_per_unit="0",
        )
        context = AgentContext(paper_book=book)
        agent.on_market_event(events[0], context)
        agent.on_market_event(events[1], context)
        self.assertEqual(book.tickets, {})

    def test_explicit_capacity_flag_cannot_bypass_missing_canonical_stake_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _output, events = self._import(Path(tmp), _pro_stream())

        event = events[0]
        for provider_unit in ("betfair_historical_stream_size_unit", "mismatched-provider-unit"):
            with self.subTest(provider_unit=provider_unit):
                metadata = dict(event.metadata)
                metadata.update(
                    {
                        "execution_quote_verified": True,
                        "paper_fill_eligible": True,
                        "paper_fill_capacity_verified": True,
                        "paper_fill_capacity_unit_bound": False,
                        "paper_fill_size_unit": provider_unit,
                    }
                )
                forged = replace(event, metadata=metadata)
                self.assertIn(
                    "not canonically bound",
                    paper_quote_rejection_reason(forged, "1"),
                )

    def test_ltp_only_delta_keeps_persisted_available_back_as_latest_replay_state(self) -> None:
        lines = _pro_stream()
        lines.insert(
            2,
            {
                "op": "mcm",
                "pt": _epoch_ms("2026-02-10T12:10:00Z"),
                "mc": [
                    {
                        "id": "1.advanced",
                        "rc": [{"id": 101, "ltp": 1.83}],
                    }
                ],
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            _output, events = self._import(Path(tmp), lines)

        self.assertEqual(len(events), 3)
        latest = events[-1]
        self.assertEqual(latest.observed_ts, "2026-02-10T12:10:00Z")
        self.assertEqual(latest.decimal_odds, Decimal("1.85"))
        self.assertEqual(latest.metadata["price_semantics"], "betfair_available_to_back")
        self.assertEqual(latest.metadata["paper_fill_available_size"], "60.0")
        self.assertEqual(latest.metadata["betfair_last_traded_price"], "1.83")
        self.assertEqual(latest.metadata["betfair_price_ladder_type"], "CLASSIC")
        self.assertIs(latest.metadata["execution_price_ladder_verified"], True)
        self.assertIs(latest.metadata["execution_quote_verified"], True)
        self.assertIs(latest.metadata["paper_fill_eligible"], False)
        self.assertIs(latest.metadata["actual_fill_verified"], False)

    def test_definition_only_status_or_delay_transition_invalidates_prior_quote(self) -> None:
        transitions = (
            ({"status": "SUSPENDED"}, "suspended", "SUSPENDED", 0),
            ({"status": "OPEN", "betDelay": 2, "inPlay": True}, "open", "OPEN", 2),
        )
        for definition_change, event_status, source_status, expected_delay in transitions:
            with self.subTest(definition_change=definition_change), tempfile.TemporaryDirectory() as tmp:
                lines = _pro_stream()
                lines.insert(
                    2,
                    {
                        "op": "mcm",
                        "pt": _epoch_ms("2026-02-10T12:10:00Z"),
                        "mc": [
                            {
                                "id": "1.advanced",
                                "marketDefinition": definition_change,
                            }
                        ],
                    },
                )
                _output, events = self._import(Path(tmp), lines)

                invalidations = [
                    event for event in events if event.observed_ts == "2026-02-10T12:10:00Z"
                ]
                self.assertEqual(len(invalidations), 1)
                event = invalidations[0]
                self.assertEqual(event.status, event_status)
                self.assertEqual(event.decimal_odds, Decimal("1.85"))
                self.assertEqual(
                    event.metadata["price_semantics"],
                    "betfair_market_definition_state_transition",
                )
                self.assertEqual(event.metadata["provider_price_field"], "rc[].atb")
                self.assertEqual(event.metadata["betfair_market_status"], source_status)
                self.assertEqual(event.metadata["betfair_bet_delay_seconds"], expected_delay)
                self.assertIs(event.metadata["execution_quote_verified"], False)
                self.assertIs(event.metadata["paper_fill_eligible"], False)
                self.assertIs(event.metadata["paper_fill_capacity_verified"], False)

                book = PaperBook("10000")
                agent = PaperValueAgent(
                    {
                        event.quote_key: Forecast(
                            quote_key=event.quote_key,
                            probability=Decimal("0.90"),
                            model_id="definition-transition-test",
                            as_of_ts=event.observed_ts,
                        )
                    },
                    stake="10",
                    minimum_expected_profit_per_unit="0",
                )
                agent.on_market_event(event, AgentContext(paper_book=book))
                self.assertEqual(book.tickets, {})

    def test_open_market_quote_removal_preserves_market_status_and_marks_quote_unavailable(self) -> None:
        lines = _pro_stream()
        lines.insert(
            2,
            {
                "op": "mcm",
                "pt": _epoch_ms("2026-02-10T12:10:00Z"),
                "mc": [
                    {
                        "id": "1.advanced",
                        "rc": [{"id": 101, "atb": [[1.85, 0], [1.8, 0]]}],
                    }
                ],
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            _output, events = self._import(Path(tmp), lines)

        removed = [event for event in events if event.observed_ts == "2026-02-10T12:10:00Z"]
        self.assertEqual(len(removed), 1)
        event = removed[0]
        self.assertEqual(event.status, "open")
        self.assertEqual(event.decimal_odds, Decimal("1.85"))
        self.assertEqual(event.metadata["price_semantics"], "betfair_available_to_back_unavailable")
        self.assertEqual(event.metadata["betfair_price_ladder_type"], "CLASSIC")
        self.assertIs(event.metadata["execution_price_ladder_verified"], True)
        self.assertIs(event.metadata["execution_quote_verified"], False)
        self.assertIs(event.metadata["paper_fill_eligible"], False)
        self.assertIn("not a verified executable quote", paper_quote_rejection_reason(event, "1"))

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
        self.assertEqual(events[0].metadata["betfair_price_ladder_type"], "CLASSIC")
        self.assertIs(events[0].metadata["execution_price_ladder_verified"], True)
        self.assertIs(events[0].metadata["paper_fill_eligible"], False)

    def test_available_back_requires_explicit_supported_price_ladder(self) -> None:
        mutations = (
            (None, "requires explicit marketDefinition priceLadderDefinition"),
            ({"type": "FINEST"}, "unsupported Betfair execution price ladder FINEST"),
            ({"type": "LINE_RANGE"}, "unsupported Betfair execution price ladder LINE_RANGE"),
        )
        for ladder, expected in mutations:
            with self.subTest(ladder=ladder), tempfile.TemporaryDirectory() as tmp:
                lines = _pro_stream()
                definition = lines[0]["mc"][0]["marketDefinition"]
                if ladder is None:
                    definition.pop("priceLadderDefinition")
                else:
                    definition["priceLadderDefinition"] = ladder
                with self.assertRaisesRegex(ValueError, expected):
                    self._import(Path(tmp), lines)

    def test_classic_execution_prices_fail_closed_off_tick_or_out_of_range(self) -> None:
        cases = (
            ("atb", [[2.03, 10.0]], "2.03"),
            ("atb", [[1001.0, 10.0]], "1001"),
            ("batb", [[0, 2.03, 10.0]], "2.03"),
            ("batb", [[0, 1001.0, 10.0]], "1001"),
        )
        for field, ladder, rendered in cases:
            with self.subTest(field=field, ladder=ladder), tempfile.TemporaryDirectory() as tmp:
                lines = _pro_stream()
                runner = lines[0]["mc"][0]["rc"][0]
                runner.pop("atb")
                runner[field] = ladder
                with self.assertRaisesRegex(ValueError, rf"{rendered}.*outside the declared CLASSIC"):
                    self._import(Path(tmp), lines)

    def test_unknown_runner_change_is_rejected_before_cache_or_event_publication(self) -> None:
        lines = _pro_stream()
        lines[0]["mc"][0]["rc"][0]["id"] = 999
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, r"selection 999 is not declared by marketDefinition\.runners"):
                self._import(Path(tmp), lines)

    def test_latest_explicit_runner_roster_replaces_prior_membership(self) -> None:
        lines = _pro_stream()
        lines[0]["mc"][0]["marketDefinition"]["runners"].append(
            {"id": 999, "name": "Removed Runner", "status": "ACTIVE"}
        )
        lines.insert(
            1,
            {
                "op": "mcm",
                "pt": _epoch_ms("2026-02-10T12:02:00Z"),
                "mc": [
                    {
                        "id": "1.advanced",
                        "marketDefinition": {
                            "runners": [
                                {"id": 101, "name": "Player A", "status": "ACTIVE"}
                            ]
                        },
                    }
                ],
            },
        )
        lines[2]["mc"][0]["rc"][0]["id"] = 999

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                ValueError,
                r"selection 999 is not declared by marketDefinition\.runners",
            ):
                self._import(Path(tmp), lines)


if __name__ == "__main__":
    unittest.main()