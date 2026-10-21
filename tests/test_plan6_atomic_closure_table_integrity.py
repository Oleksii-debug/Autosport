"""Plan 6 atomic status/immutable-lock parity (not Windows or NVDA evidence)."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {f"{number}{suffix}" for number in range(1, 8) for suffix in "АБВ"}
def _atomic_rows() -> dict[str, str]:
    text = (ROOT / "MULTI_PLAN_CLOSURE_STATE.md").read_text(encoding="utf-8")
    table = text.split("### Plan 6 —", 1)[1].split("### Plan 7 —", 1)[0]
    rows: dict[str, str] = {}
    for section, status in re.findall(r"^\|([^|]+)\|([^|]+)\|", table, re.MULTILINE):
        if section not in EXPECTED:
            continue
        assert section not in rows, f"duplicate Plan 6 atomic status: {section}"
        rows[section] = status
    return rows


def test_plan6_21_atomic_rows_are_explicit_and_consistent():
    states = _atomic_rows()
    assert set(states) == EXPECTED
    assert all(status.strip() for status in states.values())


def test_plan6_done_rows_match_terminal_immutable_locks():
    states = _atomic_rows()
    text = (ROOT / "SECTION_CLOSURE_IMMUTABILITY.md").read_text(encoding="utf-8")
    table = text.split("## Plan 6 atomic terminal locks", 1)[1]
    locked = set(re.findall(r"^\|\s*([1-7][АБВ])\s*\|\s*DONE_IMMUTABLE\b", table, re.MULTILINE))
    assert locked == {name for name, status in states.items() if status == "DONE"}
