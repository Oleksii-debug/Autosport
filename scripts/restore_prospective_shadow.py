"""Resume the latest completed same-branch campaign; never silently start empty."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import zipfile

from autosport.shadow_strategy_memory import ShadowStrategyMemory, digest


def api(path, *, binary=False):
    result = subprocess.run(["gh", "api", path], check=True, capture_output=True, timeout=120)
    return result.stdout if binary else json.loads(result.stdout)


def restore(raw: bytes, destination: Path, *, expected_sha256: str):
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("campaign archive digest mismatch")
    if destination.exists():
        raise ValueError("refusing to overwrite an existing campaign")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".shadow-restore-", dir=destination.parent))
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len({entry.filename for entry in entries}) != len(entries) or sum(entry.file_size for entry in entries) > 500 * 1024 * 1024:
                raise ValueError("ambiguous or oversized campaign archive")
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or ":" in entry.filename or stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError("unsafe campaign archive member")
            archive.extractall(temporary)
        wrapper = json.loads((temporary / "observer.json").read_text("utf-8"))
        if digest(wrapper["state"]) != wrapper["sha256"]:
            raise ValueError("observer checkpoint integrity mismatch")
        memory = ShadowStrategyMemory.load(temporary / "memory.json")
        sources = [(wrapper["state"]["last_forecast_sha256"], ".json"),
                   (wrapper["state"]["last_results_sha256"], ".csv")]
        sources.extend((row["source_sha256"], ".csv") for row in memory.payload()["records"])
        sources.extend((row["forecast_sha256"], ".json") for row in wrapper["state"]["candidates"].values())
        forecast_path = temporary / "forecast-learning.json"
        if forecast_path.exists():
            forecast = json.loads(forecast_path.read_text("utf-8"))
            if digest(forecast["state"]) != forecast["sha256"]:
                raise ValueError("forecast checkpoint integrity mismatch")
            sources.extend((row["forecast_sha256"], ".json") for row in forecast["state"]["receipts"])
            sources.extend((row["results_sha256"], ".csv") for row in forecast["state"]["receipts"])
        for sha, suffix in sources:
            if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha) or not (temporary / "raw" / (sha + suffix)).is_file():
                raise ValueError("required source evidence missing")
        for path in (temporary / "raw").iterdir():
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != path.stem:
                raise ValueError("retained source integrity mismatch")
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = "work/agent-shadow-learning-20261027"
    runs = api(f"repos/{repo}/actions/workflows/prospective-shadow-observer.yml/runs?status=completed&per_page=30")["workflow_runs"]
    eligible = [run for run in runs if run["head_branch"] == branch and run["event"] == "push"]
    if not eligible:
        raise ValueError("previous campaign not found; explicit recovery required")
    run = max(eligible, key=lambda value: value["run_number"])
    artifacts = api(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
    name = f"prospective-shadow-{run['id']}-attempt-{run['run_attempt']}"
    matches = [artifact for artifact in artifacts if artifact["name"] == name and not artifact["expired"]]
    if len(matches) != 1:
        raise ValueError("latest completed campaign checkpoint unavailable; refusing rollback")
    artifact = matches[0]
    if artifact["size_in_bytes"] > 50 * 1024 * 1024 or not artifact.get("digest", "").startswith("sha256:"):
        raise ValueError("campaign archive lacks bounded digest evidence")
    raw = api(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", binary=True)
    restore(raw, args.workspace, expected_sha256=artifact["digest"][7:])
    provenance = {"run_id": run["id"], "source_sha": run["head_sha"],
                  "artifact_id": artifact["id"], "archive_sha256": artifact["digest"][7:]}
    (args.workspace / "restore-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", "utf-8")
    print(json.dumps({"event": "campaign_restored", **provenance}))


if __name__ == "__main__":
    main()
