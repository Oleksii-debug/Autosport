"""Resume the latest campaign checkpoint; never silently start empty."""
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
            if len({entry.filename for entry in entries}) != len(entries) or sum(entry.file_size for entry in entries) > 1024 * 1024 * 1024:
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


def select_checkpoint(artifacts, *, run_id, attempt, segment=None):
    prefix = f"prospective-shadow-{run_id}-attempt-{attempt}"
    if segment is not None:
        if not 1 <= segment <= 6:
            raise ValueError("invalid previous campaign segment")
        expected = prefix if segment == 6 else f"{prefix}-segment-{segment}"
        eligible = [(segment, artifact) for artifact in artifacts
                    if artifact["name"] == expected]
    else:
        eligible = []
        for artifact in artifacts:
            name = artifact["name"]
            if name == prefix:
                eligible.append((6, artifact))
            elif name.startswith(prefix + "-segment-"):
                suffix = name[len(prefix + "-segment-"):]
                if suffix in {"1", "2", "3", "4", "5"}:
                    eligible.append((int(suffix), artifact))
    if not eligible:
        raise ValueError("latest campaign checkpoint unavailable; refusing rollback")
    latest = max(rank for rank, _ in eligible)
    matches = [artifact for rank, artifact in eligible if rank == latest]
    if len(matches) != 1:
        raise ValueError("ambiguous campaign checkpoint")
    if matches[0]["expired"]:
        raise ValueError("latest campaign checkpoint expired; refusing rollback")
    return matches[0]


def latest_started_segment(jobs):
    segments = []
    for job in jobs:
        name = job.get("name", "")
        suffix = name.removeprefix("segment-")
        if name.startswith("segment-") and suffix in {"1", "2", "3", "4", "5", "6"} and job.get("conclusion") != "skipped":
            segments.append(int(suffix))
    if segments:
        return max(segments)
    if any(job.get("name") == "public-shadow-learning" and job.get("conclusion") != "skipped" for job in jobs):
        return 6  # legacy single-segment campaigns used the final artifact name
    raise ValueError("previous campaign has no started learning segment")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--previous-segment", type=int)
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = "work/agent-shadow-learning-20261027"
    if args.previous_segment is not None:
        run = api(f"repos/{repo}/actions/runs/{int(os.environ['GITHUB_RUN_ID'])}")
        if run["head_branch"] != branch or run["event"] != "push":
            raise ValueError("current campaign identity mismatch")
    else:
        runs = api(f"repos/{repo}/actions/workflows/prospective-shadow-observer.yml/runs?status=completed&per_page=30")["workflow_runs"]
        eligible = [run for run in runs if run["head_branch"] == branch and run["event"] == "push"]
        if not eligible:
            raise ValueError("previous campaign not found; explicit recovery required")
        run = max(eligible, key=lambda value: value["run_number"])
    artifacts = api(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
    previous_segment = args.previous_segment
    if previous_segment is None:
        jobs = api(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        previous_segment = latest_started_segment(jobs)
    artifact = select_checkpoint(artifacts, run_id=run["id"], attempt=run["run_attempt"], segment=previous_segment)
    if artifact["size_in_bytes"] > 200 * 1024 * 1024 or not artifact.get("digest", "").startswith("sha256:"):
        raise ValueError("campaign archive lacks bounded digest evidence")
    raw = api(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", binary=True)
    restore(raw, args.workspace, expected_sha256=artifact["digest"][7:])
    provenance = {"run_id": run["id"], "source_sha": run["head_sha"],
                  "artifact_id": artifact["id"], "archive_sha256": artifact["digest"][7:]}
    (args.workspace / "restore-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", "utf-8")
    print(json.dumps({"event": "campaign_restored", **provenance}))


if __name__ == "__main__":
    main()
