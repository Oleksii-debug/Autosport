from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from autosport.performance_qualification import (
    PerformanceBudget,
    PerformanceQualificationError,
    qualify_endurance_report,
)


def _read_json_object(path: Path, name: str) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PerformanceQualificationError(f"cannot read {name}: {exc}") from exc
    if type(raw) is not dict:
        raise PerformanceQualificationError(f"{name} must contain a JSON object")
    return raw


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _path_identity(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve(strict=False)))
    except OSError as exc:
        raise PerformanceQualificationError(
            f"cannot resolve qualification path {path}: {exc}"
        ) from exc


def _prepare_output(path: Path, *, inputs: tuple[Path, ...]) -> None:
    output_identity = _path_identity(path)
    if any(output_identity == _path_identity(input_path) for input_path in inputs):
        raise PerformanceQualificationError(
            "qualification output must not overwrite report or budget input"
        )
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise PerformanceQualificationError(
            f"cannot invalidate stale qualification output: {exc}"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Qualify one Autosport endurance report against an explicit machine-bound budget."
    )
    parser.add_argument("report", type=Path, help="endurance-report.json")
    parser.add_argument("budget", type=Path, help="explicit performance-budget JSON")
    parser.add_argument("--source-sha", required=True, help="exact source commit SHA")
    parser.add_argument("--machine-profile", required=True, help="declared machine/profile identity")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("endurance-performance-qualification.json"),
        help="qualification evidence JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _prepare_output(args.output, inputs=(args.report, args.budget))
        report = _read_json_object(args.report, "endurance report")
        budget = PerformanceBudget.from_dict(_read_json_object(args.budget, "performance budget"))
        qualification = qualify_endurance_report(
            report,
            budget,
            source_sha=args.source_sha,
            machine_profile=args.machine_profile,
        )
        _write_json_atomic(args.output, qualification.to_dict())
    except (PerformanceQualificationError, OSError) as exc:
        print(f"performance_qualification=INVALID error={exc}")
        return 3

    print(
        f"performance_qualification={qualification.status} "
        f"source_sha={qualification.source_sha} "
        f"machine_profile={qualification.machine_profile} "
        f"qualification_id={qualification.qualification_id} "
        "target_machine_acceptance=false"
    )
    print(f"evidence={args.output}")
    return 0 if qualification.status == "PASS" else 5


if __name__ == "__main__":
    raise SystemExit(main())
