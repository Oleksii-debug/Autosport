from __future__ import annotations

import contextlib
import io

import pytest

from autosport.cli import build_parser


def _subcommand_help() -> str:
    output = io.StringIO()
    parser = build_parser()
    with contextlib.redirect_stdout(output), pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["calculate-dataset-quote", "--help"])
    assert exc_info.value.code == 0
    return output.getvalue()


def test_dataset_calculation_help_is_ukrainian_first() -> None:
    help_text = _subcommand_help()

    assert "перевіреного запечатаного набору даних" in help_text
    assert "точний ідентифікатор події" in help_text
    assert "каузальний час відсічення" in help_text
    assert "формат виводу" in help_text
    assert "calculate one exact selected quote from a verified sealed dataset" not in help_text

    for operation in (
        "odds-conversion",
        "implied-probability",
        "expected-return",
        "paper-payout",
        "fractional-kelly",
    ):
        assert operation in help_text
    assert "text" in help_text
    assert "json" in help_text


def test_dataset_calculation_help_preserves_machine_cli_contract() -> None:
    help_text = _subcommand_help()

    for option in (
        "--event-id",
        "--market-id",
        "--selection-id",
        "--source-id",
        "--sequence",
        "--cutoff",
        "--operation",
        "--probability",
        "--stake",
        "--fraction",
        "--cap",
        "--format",
    ):
        assert option in help_text

    parser = build_parser()
    args = parser.parse_args(
        [
            "calculate-dataset-quote",
            "dataset",
            "--event-id",
            "event-1",
            "--market-id",
            "market-1",
            "--selection-id",
            "selection-1",
            "--source-id",
            "source-1",
            "--sequence",
            "7",
            "--cutoff",
            "2026-09-22T09:00:00+00:00",
            "--operation",
            "implied-probability",
        ]
    )

    assert args.command == "calculate-dataset-quote"
    assert args.event_id == "event-1"
    assert args.market_id == "market-1"
    assert args.selection_id == "selection-1"
    assert args.source_id == "source-1"
    assert args.sequence == 7
    assert args.operation == "implied-probability"
    assert args.stake == "1"
    assert args.fraction == "1"
    assert args.cap == "1"
    assert args.format == "text"
