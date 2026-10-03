from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from autosport.performance_qualification import (
    PerformanceBudget,
    PerformanceQualificationError,
    qualify_endurance_report,
)


_DEFAULT_OUTPUT = Path("endurance-performance-qualification.json")


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
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with handle:
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


def _preflight_parser() -> argparse.ArgumentParser:
    """Resolve evidence paths without turning malformed CLI into authority."""

    parser = argparse.ArgumentParser(
        add_help=False,
        allow_abbrev=False,
        exit_on_error=False,
    )
    parser.add_argument("report", type=Path, nargs="?")
    parser.add_argument("budget", type=Path, nargs="?")
    parser.add_argument("--source-sha")
    parser.add_argument("--machine-profile")
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    return parser


def _invalidate_preexisting_output(argv: list[str] | None) -> None:
    """Invalidate known stale evidence before strict argparse can terminate.

    This pass grants no validity to the CLI. It acts only when both input paths can
    be resolved, so output/input alias protection remains in force. The canonical
    parser still decides whether the invocation is valid afterwards.
    """

    tokens = list(sys.argv[1:] if argv is None else argv)
    try:
        preview, _unknown = _preflight_parser().parse_known_args(tokens)
    except argparse.ArgumentError:
        return
    if preview.report is None or preview.budget is None:
        return
    _prepare_output(
        preview.output,
        inputs=(preview.report, preview.budget),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify one Autosport endurance report against an explicit "
            "machine-bound budget."
        )
    )
    parser.add_argument("report", type=Path, help="endurance-report.json")
    parser.add_argument("budget", type=Path, help="explicit performance-budget JSON")
    parser.add_argument("--source-sha", required=True, help="exact source commit SHA")
    parser.add_argument(
        "--machine-profile",
        required=True,
        help="declared machine/profile identity",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help="qualification evidence JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        _invalidate_preexisting_output(argv)
    except (PerformanceQualificationError, OSError) as exc:
        print(f"performance_qualification=INVALID error={exc}")
        return 3

    args = _parser().parse_args(argv)
    try:
        # Repeat against the authoritative parse. This is idempotent and covers
        # valid argument layouts that the conservative preflight did not resolve.
        _prepare_output(args.output, inputs=(args.report, args.budget))
        report = _read_json_object(args.report, "endurance report")
        budget = PerformanceBudget.from_dict(
            _read_json_object(args.budget, "performance budget")
        )
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
