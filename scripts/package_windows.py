from __future__ import annotations

import argparse
from pathlib import Path

from autosport.release_package import build_windows_package


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--start-file", type=Path, required=True)
    parser.add_argument("--example-dir", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    output, digest = build_windows_package(
        args.exe, args.start_file, args.example_dir, args.diagnostic, args.output, args.source_sha
    )
    print(f"PACKAGE={output}")
    print(f"SHA256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
