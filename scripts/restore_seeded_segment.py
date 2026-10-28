"""Restore the exact previous segment of this seeded research campaign."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from restore_prospective_shadow import api, restore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--previous-segment", type=int, required=True, choices=range(1, 6))
    args = parser.parse_args()
    repo, run_id = os.environ["GITHUB_REPOSITORY"], int(os.environ["GITHUB_RUN_ID"])
    run = api(f"repos/{repo}/actions/runs/{run_id}")
    if run["head_branch"] != "research/seeded-shadow-20261028" or run["event"] != "push":
        raise ValueError("seeded campaign run identity mismatch")
    artifacts = api(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")["artifacts"]
    name = f"seeded-shadow-{run_id}-attempt-{run['run_attempt']}-segment-{args.previous_segment}"
    found = [artifact for artifact in artifacts if artifact["name"] == name]
    if len(found) != 1 or found[0]["expired"]:
        raise ValueError("exact prior seeded segment is unavailable")
    artifact = found[0]
    if artifact["size_in_bytes"] > 200 * 1024 * 1024 or not artifact.get("digest", "").startswith("sha256:"):
        raise ValueError("seeded archive lacks bounded digest")
    raw = api(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", binary=True)
    restore(raw, args.workspace, expected_sha256=artifact["digest"][7:])
    print(json.dumps({"event": "seeded_segment_restored", "run_id": run_id,
                      "segment": args.previous_segment, "artifact_id": artifact["id"],
                      "archive_sha256": artifact["digest"][7:]}))


if __name__ == "__main__":
    main()
