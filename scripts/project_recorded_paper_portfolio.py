"""Produce a virtual assumed-fill portfolio from a retained observer workspace."""
import argparse
import json
from pathlib import Path

from autosport.recorded_paper_projection import project


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = project(args.workspace)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({key: value for key, value in result.items() if key != "events"}))
