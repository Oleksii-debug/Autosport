from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from autosport.ui_model import evaluation_lines


def _result(result_path: Path):
    return SimpleNamespace(
        replay=SimpleNamespace(run_id="run-summary-depth-test", event_count=1),
        balance=10000,
        settled_ticket_ids=(),
        evaluation=SimpleNamespace(
            initial_bankroll=10000,
            final_balance=10000,
            committed_stake=0,
            settled_stake=0,
            net_profit=0,
            roi=0,
            won=0,
            lost=0,
            void=0,
        ),
        portfolio=SimpleNamespace(
            mode="exact",
            scenario_count=1,
            worst_case=0,
            best_case=0,
            mean_case=0,
        ),
        result_path=result_path,
    )


def test_evaluation_lines_fail_closed_on_excessive_json_nesting(tmp_path: Path) -> None:
    summary = tmp_path / "result.json"
    depth = 10_000
    summary.write_text(
        '{"market_price_truth":' + ('[' * depth) + '0' + (']' * depth) + '}',
        encoding="utf-8",
    )

    lines = evaluation_lines(_result(summary))

    assert "Істина ціни | ПОМИЛКА — вкладеність JSON підсумку запуску надто глибока." in lines
    assert lines[-1].startswith("Істина | лише паперова симуляція")


def test_evaluation_lines_fail_closed_on_standard_json_numeric_overflow(tmp_path: Path) -> None:
    summary = tmp_path / "result.json"
    summary.write_text(
        (
            '{"market_price_truth":{'
            '"price_semantics":"canonical_observation_quote",'
            '"executable_quote_verified":false,'
            '"paper_fill_fidelity_verified":false,'
            '"source_ids":[]},'
            '"unrelated_numeric_overflow":1e400}'
        ),
        encoding="utf-8",
    )

    lines = evaluation_lines(_result(summary))

    assert (
        "Істина ціни | ПОМИЛКА — JSON підсумку запуску неоднозначний або неканонічний: "
        "non-finite JSON number: 1e400."
    ) in lines
    assert lines[-1].startswith("Істина | лише паперова симуляція")


def test_evaluation_lines_preserve_canonical_price_truth_rendering(tmp_path: Path) -> None:
    summary = tmp_path / "result.json"
    summary.write_text(
        json.dumps(
            {
                "market_price_truth": {
                    "price_semantics": "canonical_observation_quote",
                    "executable_quote_verified": False,
                    "paper_fill_fidelity_verified": False,
                    "source_ids": [],
                }
            }
        ),
        encoding="utf-8",
    )

    lines = evaluation_lines(_result(summary))

    assert (
        "Істина ціни | canonical_observation_quote; виконувану котировку перевірено=ні; "
        "відповідність паперового виконання перевірено=ні."
    ) in lines
