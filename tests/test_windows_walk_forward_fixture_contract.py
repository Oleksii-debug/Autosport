from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _ROOT / "scripts" / "build_windows.ps1"


def test_windows_walk_forward_smoke_fixture_has_single_outcomes_and_windows_keys() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")
    start = script.index("$walkForwardBundle = [ordered]@{")
    end = script.index("$walkForwardJson =", start)
    fixture = script[start:end]

    assert fixture.count("\n  outcomes = @(\n") == 1
    assert fixture.count("\n  windows = @(\n") == 1
