import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from initialize_seeded_shadow import initialize
from autosport.shadow_strategy_memory import ShadowStrategyMemory
from test_recorded_live_flags import row


def dataset(tmp_path):
    rows = []
    for index in range(30):
        item = row(index, "1" if index % 3 else "2")
        item["kickoff_utc"] = (datetime(2026, 10, 2, tzinfo=timezone.utc) + timedelta(days=index // 5)).isoformat()
        rows.append(item)
    path = tmp_path / "source.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_seed_preserves_wins_and_losses_as_known_at_initialization(tmp_path):
    path, sha = dataset(tmp_path)
    root = tmp_path / "seeded"
    at = datetime(2026, 11, 1, tzinfo=timezone.utc)
    summary = initialize(path, root, expected_sha=sha, clock=lambda: at)
    memory = ShadowStrategyMemory.load(root / "memory.json")
    records = memory.payload()["records"]
    assert summary["historical_feedback_count"] == 30
    assert summary["research_preference"] == "PRICE_GAP"
    assert len(records) == 30
    assert sum(Decimal(r["net_returns"]["PRICE_GAP"]) < 0 for r in records) == 10
    assert all(r["result_available_at"] == at.isoformat() for r in records)
    assert memory.choose(as_of=at.isoformat(), admissible_actions=frozenset({"PRICE_GAP", "WAIT"})) == "PRICE_GAP"
    with pytest.raises(ValueError, match="past"):
        memory.choose(as_of=(at-timedelta(seconds=1)).isoformat(), admissible_actions=frozenset({"PRICE_GAP", "WAIT"}))
    with pytest.raises(FileExistsError):
        initialize(path, root, expected_sha=sha, clock=lambda: at)
    assert json.loads((root / "observer.json").read_text())["state"]["candidates"] == {}


def test_bad_source_and_recent_outcomes_do_not_create_workspace(tmp_path):
    path, sha = dataset(tmp_path)
    with pytest.raises(ValueError, match="digest"):
        initialize(path, tmp_path / "bad", expected_sha="0"*64)
    with pytest.raises(ValueError, match="too recent"):
        initialize(path, tmp_path / "early", expected_sha=sha,
                   clock=lambda: datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert not (tmp_path / "bad").exists()
    assert not (tmp_path / "early").exists()
