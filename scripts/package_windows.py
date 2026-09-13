from __future__ import annotations

import argparse
import json
from pathlib import Path

from autosport.release_package import build_windows_package, verify_windows_package


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--start-file", type=Path, required=True)
    parser.add_argument("--example-dir", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--accessibility-audit", type=Path, required=True)
    parser.add_argument("--keyboard-audit", type=Path, required=True)
    parser.add_argument("--restart-recovery-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--verification-output", type=Path)
    args = parser.parse_args()
    output, digest = build_windows_package(
        args.exe,
        args.start_file,
        args.example_dir,
        args.diagnostic,
        args.accessibility_audit,
        args.keyboard_audit,
        args.restart_recovery_audit,
        args.output,
        args.source_sha,
    )
    verification = verify_windows_package(output, expected_source_sha=args.source_sha)
    if args.verification_output is not None:
        args.verification_output.parent.mkdir(parents=True, exist_ok=True)
        args.verification_output.write_text(
            json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(f"PACKAGE={output}")
    print(f"SHA256={digest}")
    print("PACKAGE_VERIFICATION=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
