import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport import cli


class HistoricalSnapshotCliTests(unittest.TestCase):
    def test_parser_exposes_canonical_historical_snapshot_command(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "historical-snapshot",
                "--at",
                "2026-09-12T10:03:00Z",
                "--regions",
                "eu,us",
                "--markets",
                "h2h,totals",
            ]
        )
        self.assertEqual(args.command, "historical-snapshot")
        self.assertEqual(args.at, "2026-09-12T10:03:00Z")
        self.assertEqual(args.regions, "eu,us")
        self.assertEqual(args.markets, "h2h,totals")
        self.assertEqual(args.output, Path(".autosport-workspace/historical-snapshot.jsonl"))
        self.assertIsNone(args.evidence)

    def test_main_dispatches_exact_snapshot_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "market.jsonl"
            evidence = Path(temp) / "evidence.json"
            with patch("autosport.cli.run_historical_snapshot", return_value=6) as run:
                code = cli.main(
                    [
                        "historical-snapshot",
                        "--at",
                        "2026-09-12T10:03:00Z",
                        "--output",
                        str(output),
                        "--evidence",
                        str(evidence),
                        "--regions",
                        "eu,us",
                        "--markets",
                        "h2h,totals",
                    ]
                )
        self.assertEqual(code, 6)
        run.assert_called_once_with(
            requested_at="2026-09-12T10:03:00Z",
            output=output,
            evidence=evidence,
            regions="eu,us",
            markets="h2h,totals",
        )

    def test_missing_key_blocks_before_provider_construction(self) -> None:
        constructed = False

        def provider_factory(*args, **kwargs):
            nonlocal constructed
            constructed = True
            raise AssertionError("provider must not be constructed without a key")

        with patch.dict(os.environ, {}, clear=True):
            code = cli.run_historical_snapshot(
                requested_at="2026-09-12T10:03:00Z",
                output=Path("unused.jsonl"),
                evidence=None,
                regions="us",
                markets="h2h,spreads,totals",
                provider_factory=provider_factory,
            )
        self.assertEqual(code, 2)
        self.assertFalse(constructed)

    def test_empty_region_or_market_selection_fails_closed(self) -> None:
        with patch.dict(os.environ, {"AUTOSPORT_PARLAYAPI_KEY": "secret"}, clear=True):
            self.assertEqual(
                cli.run_historical_snapshot(
                    requested_at="2026-09-12T10:03:00Z",
                    output=Path("unused.jsonl"),
                    evidence=None,
                    regions="",
                    markets="h2h",
                ),
                3,
            )
            self.assertEqual(
                cli.run_historical_snapshot(
                    requested_at="2026-09-12T10:03:00Z",
                    output=Path("unused.jsonl"),
                    evidence=None,
                    regions="us",
                    markets="",
                ),
                3,
            )


if __name__ == "__main__":
    unittest.main()
