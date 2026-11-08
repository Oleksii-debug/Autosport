import importlib.util
import json
from pathlib import Path
import sys

import pytest

from autosport.shadow_strategy_memory import digest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from restore_prospective_shadow import restore
from run_seeded_shadow import summarize

SEED_SHA = "008bfce01d071322249467377f4a9967a220763e11cd41598c4ff53c6a4b2f5e"


def initial(root):
    archive = Path(__file__).parents[1] / "examples/seeded-shadow-initial-20261028.zip"
    restore(archive.read_bytes(), root, expected_sha256=SEED_SHA)


def test_real_seed_archive_keeps_earlier_learning_separate_from_future_performance(tmp_path):
    root = tmp_path / "seeded"
    initial(root)
    report = summarize(root)
    assert report["historical_feedback_count"] == 69
    assert report["future_candidates"] == report["future_selected"] == 3
    assert report["future_selected_settled"] == 0
    assert report["future_hypothetical_net_units"] == "0"
    assert report["historical_return_included"] is False
    assert report["profit_earned"] is None


def test_changed_seeded_feedback_is_rejected_even_if_checkpoint_rehashed(tmp_path):
    root = tmp_path / "seeded"
    initial(root)
    path = root / "memory.json"
    wrapper = json.loads(path.read_text())
    wrapper["payload"]["records"][0]["net_returns"]["PRICE_GAP"] = "2"
    wrapper["sha256"] = digest(wrapper["payload"])
    path.write_text(json.dumps(wrapper))
    with pytest.raises(ValueError, match="historical memory changed"):
        summarize(root)


def test_seeded_segment_restores_exact_predecessor(tmp_path, monkeypatch):
    module_path = Path(__file__).parents[1] / "scripts/restore_seeded_segment.py"
    spec = importlib.util.spec_from_file_location("restore_seeded", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archive = (Path(__file__).parents[1] / "examples/seeded-shadow-initial-20261028.zip").read_bytes()
    def api(path, **kwargs):
        if path.endswith("/runs/42"):
            return {"head_branch": "research/seeded-shadow-20261028", "event": "push", "run_attempt": 2}
        if "/runs/42/artifacts?" in path:
            return {"artifacts": [{"name": "seeded-shadow-42-attempt-2-segment-1", "expired": False,
                    "id": 7, "size_in_bytes": len(archive), "digest": "sha256:" + SEED_SHA}]}
        assert path.endswith("/artifacts/7/zip") and kwargs["binary"]
        return archive
    monkeypatch.setattr(module, "api", api)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setattr(sys, "argv", ["restore", "--workspace", str(tmp_path / "restored"), "--previous-segment", "1"])
    module.main()
    assert summarize(tmp_path / "restored")["future_selected"] == 3
