import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from inspect_sandbox_policy_prior import inspect
from autosport.paper_live_sandbox_campaign import BlindPaperSandboxCampaign
from test_paper_live_sandbox_campaign import _plan, _snapshot


def artifact(tmp_path, *, corrupt_protocol=False):
    root = tmp_path / "workspace"
    root.mkdir()
    campaign = BlindPaperSandboxCampaign(_plan())
    campaign.step(_snapshot(1), _snapshot(2))
    campaign.save(root)
    state = json.loads((root / "campaign_state.json").read_text("utf-8"))
    if corrupt_protocol:
        state["plan"]["selection_protocol"] = "forged"
    payloads = {"campaign_state.json": json.dumps(state).encode(),
                "paper_book.json": (root / "paper_book.json").read_bytes(),
                "endurance_summary.json": json.dumps(campaign.summary()).encode()}
    manifest = "".join(hashlib.sha256(value).hexdigest() + "  account-c-paper-evidence/" + name + "\n"
                       for name, value in sorted(payloads.items()))
    path = tmp_path / "artifact.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in payloads.items():
            archive.writestr(name, value)
        archive.writestr("artifact-sha256.txt", manifest)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_inspection_preserves_simulation_boundary_and_does_not_import(tmp_path):
    path, sha = artifact(tmp_path)
    result = inspect(path, expected_artifact_sha256=sha)
    assert result["source_generation"] == 1
    assert result["truth"] == "simulated"
    assert result["desktop_import_performed"] is False
    assert result["promotion_authorized"] is False
    assert result["source_authentication_independently_verified"] is False


def test_wrong_identity_and_forged_plan_fail_closed(tmp_path):
    path, sha = artifact(tmp_path, corrupt_protocol=True)
    with pytest.raises(ValueError, match="does not match"):
        inspect(path, expected_artifact_sha256="0" * 64)
    with pytest.raises(ValueError, match="not canonical"):
        inspect(path, expected_artifact_sha256=sha)
