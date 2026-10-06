from __future__ import annotations

import inspect
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from autosport.gui import AUTOMATION_IDS, AutosportApp
from autosport.ui_model import evaluation_lines


def _result(*, mode: str, result_path: Path):
    return SimpleNamespace(
        replay=SimpleNamespace(run_id="12345678-abcd", event_count=17),
        settled_ticket_ids=("t1", "t2"),
        evaluation=SimpleNamespace(
            initial_bankroll=Decimal("10000"),
            final_balance=Decimal("10025"),
            committed_stake=Decimal("0"),
            settled_stake=Decimal("100"),
            net_profit=Decimal("25"),
            roi=Decimal("0.25"),
            won=1,
            lost=1,
            void=0,
        ),
        portfolio=SimpleNamespace(
            mode=mode,
            scenario_count=4 if mode == "exact" else 20000,
            worst_case=Decimal("-10"),
            best_case=Decimal("30"),
            mean_case=Decimal("5"),
        ),
        result_path=result_path,
    )


def _write_run_summary(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "market_price_truth": {
                    "price_semantics": "unspecified_or_mixed_observation",
                    "executable_quote_verified": False,
                    "paper_fill_fidelity_verified": False,
                    "source_ids": [],
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_evaluation_lines_publish_terminal_paper_metrics_and_truth_boundary(tmp_path: Path):
    result_path = _write_run_summary(tmp_path / "run-summary.json")
    lines = evaluation_lines(_result(mode="exact", result_path=result_path))

    assert lines[0] == "Повтор 12345678 | події 17 | завершені квитки 2"
    assert "початковий 10000" in lines[1]
    assert "кінцевий 10025" in lines[1]
    assert "завершена сума ставок 100" in lines[1]
    assert "чистий результат 25" in lines[2]
    assert "ROI 0.25" in lines[2]
    assert "виграно 1" in lines[2]
    assert "програно 1" in lines[2]
    assert "точний — усі релевантні сценарії цього звіту портфеля перебрано" in lines[3]
    assert "сценарії 4" in lines[3]
    assert "виконувану котировку перевірено=ні" in lines[4]
    assert "відповідність паперового виконання перевірено=ні" in lines[4]
    assert "це оцінювання не є доказом майбутньої прибутковості" in lines[5]


def test_approximate_portfolio_never_presents_sampled_bounds_as_guarantees(tmp_path: Path):
    result_path = _write_run_summary(tmp_path / "run-summary.json")
    lines = evaluation_lines(_result(mode="approximate", result_path=result_path))

    assert "наближений — сценарії вибіркові" in lines[3]
    assert "гарантії найгіршого/найкращого не заявляються" in lines[3]
    assert "сценарії 20000" in lines[3]
    assert "виконувану котировку перевірено=ні" in lines[4]
    assert "відповідність паперового виконання перевірено=ні" in lines[4]
    assert "це оцінювання не є доказом майбутньої прибутковості" in lines[5]


def test_evaluation_lines_fail_closed_when_run_summary_evidence_is_missing(tmp_path: Path):
    lines = evaluation_lines(_result(mode="exact", result_path=tmp_path / "missing-run-summary.json"))

    assert lines[4] == "Істина ціни | ПОМИЛКА — доказ підсумку запуску відсутній або не читається."


def test_evaluation_lines_fail_closed_when_run_summary_evidence_is_unreadable(tmp_path: Path):
    lines = evaluation_lines(_result(mode="exact", result_path=tmp_path))

    assert lines[4] == "Істина ціни | ПОМИЛКА — доказ підсумку запуску відсутній або не читається."


def test_evaluation_lines_fail_closed_when_run_summary_is_not_utf8(tmp_path: Path):
    result_path = tmp_path / "corrupt-run-summary.json"
    result_path.write_bytes(b"\xff\xfe\x00\x80")

    lines = evaluation_lines(_result(mode="exact", result_path=result_path))

    assert lines[4] == "Істина ціни | ПОМИЛКА — доказ підсумку запуску відсутній або не читається."


def test_evaluation_lines_reject_duplicate_run_summary_keys(tmp_path: Path):
    result_path = tmp_path / "duplicate-run-summary.json"
    result_path.write_text(
        '{"market_price_truth": {}, "market_price_truth": {}}',
        encoding="utf-8",
    )

    lines = evaluation_lines(_result(mode="exact", result_path=result_path))

    assert lines[4] == (
        "Істина ціни | ПОМИЛКА — JSON підсумку запуску неоднозначний або неканонічний: "
        "duplicate object key: market_price_truth."
    )


def test_evaluation_lines_reject_nonfinite_json_constants(tmp_path: Path):
    result_path = tmp_path / "nonfinite-run-summary.json"
    result_path.write_text(
        '{"market_price_truth": {"price_semantics": NaN}}',
        encoding="utf-8",
    )

    lines = evaluation_lines(_result(mode="exact", result_path=result_path))

    assert lines[4] == (
        "Істина ціни | ПОМИЛКА — JSON підсумку запуску неоднозначний або неканонічний: "
        "non-finite JSON constant: NaN."
    )


def test_gui_wires_evaluation_to_keyboard_uia_and_terminal_result_without_tk_startup():
    build_source = inspect.getsource(AutosportApp._build)
    accessibility_source = inspect.getsource(AutosportApp._configure_accessibility)
    poll_source = inspect.getsource(AutosportApp._poll_replay_worker)

    assert AUTOMATION_IDS["evaluation"] == 204
    assert "self.evaluation = tk.Listbox" in build_source
    assert 'self.bind("<F8>"' in build_source
    assert 'text("ui.accessibility.evaluation.name")' in accessibility_source
    assert 'text("ui.accessibility.evaluation.description")' in accessibility_source
    assert 'AUTOMATION_IDS["evaluation"]' in accessibility_source
    assert "self._set_evaluation_lines(evaluation_lines(result))" in poll_source
    assert 'text("ui.evaluation.replay_failed")' in poll_source
