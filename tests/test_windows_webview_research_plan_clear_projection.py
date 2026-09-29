from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def test_cleared_bound_research_plan_clears_the_visible_path() -> None:
    javascript = (
        _ROOT / "src" / "autosport" / "windows_web" / "app.js"
    ).read_text(encoding="utf-8")

    assert (
        'state.research_plan_path || byId("research-plan-path").value'
        not in javascript
    )
    assert "state.research_plan_path" in javascript
    assert 'document.activeElement !== byId("research-plan-path")' in javascript
