"""Recover the frozen initial seeded challenger without restarting its memory."""
import argparse
from pathlib import Path

from restore_prospective_shadow import restore
from run_seeded_shadow import summarize

SHA256 = "008bfce01d071322249467377f4a9967a220763e11cd41598c4ff53c6a4b2f5e"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "examples/seeded-shadow-initial-20261028.zip"
    restore(source.read_bytes(), args.workspace, expected_sha256=SHA256)
    print(summarize(args.workspace))
