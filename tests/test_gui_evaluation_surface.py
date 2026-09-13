from __future__ import annotations

import inspect
from decimal import Decimal
from types import SimpleNamespace

from autosport.gui import AUTOMATION_IDS, AutosportApp
from autosport.ui_model import evaluation_lines


def _result(*, mode: str):
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
    )


def test_evaluation_lines_publish_terminal_paper_metrics_and_truth_boundary():
    lines = evaluation_lines(_result(mode="exact"))

    assert lines[0] == "Replay 12345678 | events 17 | settled tickets 2"
    assert "initial 10000" in lines[1]
    assert "final 10025" in lines[1]
    assert "settled stake 100" in lines[1]
    assert "net 25" in lines[2]
    assert "ROI 0.25" in lines[2]
    assert "won 1" in lines[2]
    assert "lost 1" in lines[2]
    assert "exact — усі релевантні сценарії цього portfolio report перебрано" in lines[3]
    assert "scenarios 4" in lines[3]
    assert "не є доказом майбутньої profitability" in lines[4]


def test_approximate_portfolio_never_presents_sampled_bounds_as_guarantees():
    lines = evaluation_lines(_result(mode="approximate"))

    assert "approximate — сценарії sampled" in lines[3]
    assert "гарантії worst/best не заявляються" in lines[3]
    assert "scenarios 20000" in lines[3]


def test_gui_wires_evaluation_to_keyboard_uia_and_terminal_result_without_tk_startup():
    build_source = inspect.getsource(AutosportApp._build)
    accessibility_source = inspect.getsource(AutosportApp._configure_accessibility)
    poll_source = inspect.getsource(AutosportApp._poll_replay_worker)

    assert AUTOMATION_IDS["evaluation"] == 204
    assert "self.evaluation = tk.Listbox" in build_source
    assert 'self.bind("<F8>"' in build_source
    assert '"Evaluation і portfolio evidence"' in accessibility_source
    assert 'AUTOMATION_IDS["evaluation"]' in accessibility_source
    assert "self._set_evaluation_lines(evaluation_lines(result))" in poll_source
    assert "Evaluation недоступна" in poll_source
