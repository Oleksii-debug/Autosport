from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport.dataset import load_dataset
from autosport.recovery import reconcile_late_crashes
from autosport.run_registry import UnresolvedExperimentError
from autosport.run_transaction import RunTransaction
from autosport.session import AutosportSession


def test_terminal_registry_with_unfinished_manifest_blocks_next_run_until_recovery(
    tmp_path: Path,
) -> None:
    dataset = load_dataset(Path("examples/tt_demo"))
    session = AutosportSession(tmp_path, "10000")

    with patch.object(
        RunTransaction,
        "mark_registry_completed",
        side_effect=OSError("simulated terminal manifest publication failure"),
    ):
        with pytest.raises(
            OSError,
            match="simulated terminal manifest publication failure",
        ):
            session.run_dataset(dataset)

    transaction_root = tmp_path / RunTransaction.ROOT_NAME
    transaction_dirs = sorted(transaction_root.iterdir(), key=lambda path: path.name)
    assert len(transaction_dirs) == 1
    manifest_path = transaction_dirs[0] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["phase"] == "canonical_committed"
    experiment_key = manifest["experiment_key"]
    assert session.registry.get(experiment_key)["status"] == "completed"
    assert session.registry.in_progress() == ()

    with pytest.raises(
        UnresolvedExperimentError,
        match="unresolved transaction history",
    ):
        session.run_dataset(dataset, allow_repeat=True)

    assert sorted(transaction_root.iterdir(), key=lambda path: path.name) == transaction_dirs
    assert session.registry.in_progress() == ()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["phase"] == "canonical_committed"

    report = reconcile_late_crashes(tmp_path)
    assert report.reconciled_keys == (experiment_key,)
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["phase"] == "completed"

    repeated = session.run_dataset(dataset, allow_repeat=True)
    assert repeated.experiment_key != experiment_key
    session.close()
