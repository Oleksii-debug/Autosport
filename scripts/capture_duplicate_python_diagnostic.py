"""Failure-only, nonblocking Python stack locations; never product acceptance.

Raw py-spy output (including names, paths, command lines and locals) is never
persisted or forwarded. Only checkout-owned module basenames and numeric lines
survive. py-spy 0.4.2 dump JSON schema is pinned by the workflow.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

MODULE = re.compile(r"[A-Za-z0-9_]+[.]py\Z")
STATUSES = {"CAPTURED", "SAMPLER_ERROR", "SAMPLER_TIMEOUT", "INVALID_PAYLOAD",
            "PAYLOAD_LIMIT", "UNAVAILABLE"}


def sanitize(payload: object, allowed: set[str]) -> dict:
    if type(payload) is not list or not 1 <= len(payload) <= 128:
        raise ValueError("invalid trace envelope")
    locations = []
    seen = set()
    omitted = 0
    for trace in payload:
        if type(trace) is not dict or type(trace.get("frames")) is not list:
            raise ValueError("invalid thread frames")
        if len(trace["frames"]) > 4096:
            raise ValueError("frame limit")
        for frame in trace["frames"]:
            if type(frame) is not dict or frame.get("locals") is not None:
                raise ValueError("locals or malformed frame")
            filename = frame.get("filename")
            line = frame.get("line")
            if type(filename) is not str or type(line) is not int or not 0 <= line <= 1_000_000:
                raise ValueError("invalid frame location")
            name = filename.replace("\\", "/").rsplit("/", 1)[-1]
            if filename in {"<frozen importlib._bootstrap>", "<frozen importlib._bootstrap_external>"}:
                name = "IMPORT_BOOTSTRAP"
            elif not MODULE.fullmatch(name) or name not in allowed:
                omitted += 1
                continue
            key = (name, line)
            if key not in seen:
                seen.add(key)
                if len(locations) < 32:
                    locations.append({"module": name, "line": line})
                else:
                    omitted += 1
    return {"status": "CAPTURED", "thread_count": len(payload),
            "locations": locations, "omitted_frames": omitted}


def capture(sampler: str, pid: int, source_root: Path) -> dict:
    if type(pid) is not int or pid <= 0 or not source_root.is_dir():
        return {"status": "UNAVAILABLE"}
    modules = {p.name for p in source_root.glob("*.py")
               if p.is_file() and not p.is_symlink() and MODULE.fullmatch(p.name)}
    if not modules or len(modules) > 4096:
        return {"status": "UNAVAILABLE"}
    try:
        result = subprocess.run(
            [sampler, "dump", "--json", "--nonblocking", "--pid", str(pid)],
            capture_output=True, timeout=3, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"status": "SAMPLER_TIMEOUT"}
    except OSError:
        return {"status": "SAMPLER_ERROR"}
    if result.returncode != 0:
        return {"status": "SAMPLER_ERROR"}
    if len(result.stdout) > 1_000_000:
        return {"status": "PAYLOAD_LIMIT"}
    try:
        return sanitize(json.loads(result.stdout), modules)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return {"status": "INVALID_PAYLOAD"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sampler", required=True)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--source-root", required=True, type=Path)
    args = parser.parse_args()
    report = capture(args.sampler, args.pid, args.source_root)
    report.update(evidence_class="FAILURE_ONLY_PYTHON_STACK_LOCATIONS_NOT_ACCEPTANCE",
                  human_tested=False, nvda_verified=False, real_money_execution=False)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
