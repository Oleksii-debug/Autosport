from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/create_legacy_market_store_fixture.py"
SPEC = importlib.util.spec_from_file_location("legacy_fixture", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_fixture_has_exact_legacy_projection_and_canonical_history(tmp_path: Path) -> None:
    database = tmp_path / "legacy state.db"
    report = MODULE.create_legacy_market_store_fixture(database, source_sha="a" * 40)

    assert report["status"] == "PASS"
    assert report["history_count"] == report["current_count"] == 1
    assert report["real_money_execution"] is False
    assert report["human_tested"] is False
    assert report["nvda_verified"] is False
    with sqlite3.connect(database) as connection:
        columns = tuple(
            row[1] for row in connection.execute(
                "PRAGMA table_xinfo(current_quotes)"
            ).fetchall()
        )
    assert columns == ("quote_key", "observed_ts", "sequence", "payload_json")


def test_fixture_rejects_overwrite_and_invalid_source(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    MODULE.create_legacy_market_store_fixture(database, source_sha="b" * 40)

    for path, source_sha in ((database, "b" * 40), (tmp_path / "new.db", "bad")):
        try:
            MODULE.create_legacy_market_store_fixture(path, source_sha=source_sha)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe fixture request unexpectedly accepted")
