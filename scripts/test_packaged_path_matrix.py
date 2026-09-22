from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA_VERSION = 2
SEMANTIC_KEY = "windows.packaged-launch-path-matrix-v1"
AUTHORITY_FAMILY = "product.windows-packaging.path-compatibility"
LAUNCH_CWD_POLICY = "UNRELATED_CASE_DIRECTORY"
WORKSPACE_POLICY = "AUTOSPORT_WORKSPACE_PER_CASE_ABSOLUTE"

SCENARIOS: tuple[tuple[str, str], ...] = (
    ("ascii_control", "01_Autosport_QA"),
    ("spaces", "02_Autosport QA With Spaces"),
    ("cyrillic", "03_Автоспорт_Перевірка"),
    ("combined", "04_Автоспорт Перевірка With Spaces"),
)


class MatrixError(RuntimeError):
    pass


@dataclass(frozen=True)
class TreeEntry:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    path_component: str
    artifact_tree_sha256: str
    copied_bytes_equal: bool
    launched: bool
    startup_stable: bool
    exit_code: int | None
    probe_mode: str
    elapsed_seconds: float
    argument_count: int
    argument_sha256: str
    status: str
    output_captured: bool
    error_type: str | None


@dataclass(frozen=True)
class MatrixReport:
    schema_version: int
    semantic_key: str
    authority_family: str
    artifact_tree_sha256: str
    executable_relative_path: str
    probe_mode: str
    startup_seconds: float
    timeout_seconds: float
    launch_cwd_policy: str
    workspace_policy: str
    privilege_context: str
    non_admin_verified: bool
    scenarios: tuple[ScenarioResult, ...]
    matrix_status: str
    real_money_execution: bool = False
    human_tested: bool = False
    nvda_verified: bool = False
    whole_product_complete: bool = False


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_sha(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(payload)


def detect_privilege_context() -> str:
    """Classify the machine context without promoting CI to non-admin proof."""

    if os.name != "nt":
        return "NON_WINDOWS_TEST_CONTEXT"
    try:
        import ctypes

        return "ADMINISTRATOR" if bool(ctypes.windll.shell32.IsUserAnAdmin()) else "STANDARD_USER"
    except Exception:
        return "UNKNOWN"


def _safe_relative_path(path: Path) -> str:
    if path.is_absolute() or ".." in path.parts:
        raise MatrixError(f"executable path must be relative and traversal-free: {path}")
    return path.as_posix()


def tree_manifest(root: Path) -> tuple[TreeEntry, ...]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise MatrixError(f"artifact root must be a directory: {root}")
    all_paths = sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix())
    symlinks = [p.relative_to(root).as_posix() for p in all_paths if p.is_symlink()]
    if symlinks:
        raise MatrixError(f"artifact root contains unsupported symlink(s): {symlinks[:3]}")
    entries: list[TreeEntry] = []
    for path in (p for p in all_paths if p.is_file()):
        rel = path.relative_to(root).as_posix()
        stat_before = path.stat()
        file_sha = _sha256_file(path)
        stat_after = path.stat()
        if (stat_before.st_size, stat_before.st_mtime_ns) != (stat_after.st_size, stat_after.st_mtime_ns):
            raise MatrixError(f"artifact file changed while hashing: {rel}")
        entries.append(TreeEntry(rel, stat_after.st_size, file_sha))
    if not entries:
        raise MatrixError("artifact root contains no files")
    return tuple(entries)


def tree_manifest_sha(entries: Iterable[TreeEntry]) -> str:
    serial = [asdict(entry) for entry in entries]
    return _canonical_json_sha(serial)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def copy_tree_verified(source: Path, destination: Path, *, expected_source_sha: str | None = None) -> str:
    source = source.resolve(strict=True)
    _remove_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    dst_manifest = tree_manifest(destination)
    src_sha = expected_source_sha or tree_manifest_sha(tree_manifest(source))
    dst_sha = tree_manifest_sha(dst_manifest)
    if src_sha != dst_sha:
        raise MatrixError("copied package bytes differ from source artifact")
    return dst_sha


def render_arguments(arguments: Sequence[str], *, case_root: Path, package_root: Path) -> tuple[str, ...]:
    replacements = {
        "{CASE_ROOT}": str(case_root),
        "{PACKAGE_ROOT}": str(package_root),
        "{WORKSPACE_ROOT}": str(case_root / "workspace"),
    }
    rendered: list[str] = []
    for arg in arguments:
        value = arg
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        rendered.append(value)
    return tuple(rendered)


def command_for(executable: Path, arguments: Sequence[str], launcher: Sequence[str]) -> list[str]:
    exe = executable.resolve(strict=True)
    return [*launcher, str(exe), *arguments]


def probe_process(
    command: Sequence[str],
    *,
    cwd: Path,
    stdout_path: Path,
    mode: str,
    startup_seconds: float,
    timeout_seconds: float,
    capture_output: bool,
    environment: dict[str, str] | None = None,
) -> tuple[bool, bool, int | None, float, str, str | None]:
    if mode not in {"persistent", "exit-zero"}:
        raise MatrixError(f"unsupported probe mode: {mode}")
    started_at = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    launched = False
    stable = False
    exit_code: int | None = None
    status = "FAIL"
    error_type: str | None = None
    log_handle = None
    try:
        stdout_target = subprocess.DEVNULL
        if capture_output:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = stdout_path.open("wb")
            stdout_target = log_handle
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=stdout_target,
            stderr=subprocess.STDOUT,
            shell=False,
            env=(os.environ.copy() if environment is None else dict(environment)),
        )
        launched = True
        if mode == "persistent":
            deadline = time.monotonic() + startup_seconds
            while time.monotonic() < deadline:
                exit_code = process.poll()
                if exit_code is not None:
                    return launched, False, exit_code, time.monotonic() - started_at, "FAIL_EARLY_EXIT", None
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            stable = process.poll() is None
            status = "PASS" if stable else "FAIL_EARLY_EXIT"
        else:
            try:
                exit_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                status = "FAIL_TIMEOUT"
            else:
                stable = exit_code == 0
                status = "PASS" if stable else "FAIL_NONZERO_EXIT"
    except Exception as exc:
        error_type = type(exc).__name__
        status = "FAIL_LAUNCH"
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process is not None and exit_code is None:
            exit_code = process.poll()
        if log_handle is not None:
            log_handle.close()
    return launched, stable, exit_code, time.monotonic() - started_at, status, error_type


def classify_matrix(results: Sequence[ScenarioResult]) -> str:
    if not results or results[0].name != "ascii_control":
        raise MatrixError("ASCII control must be the first scenario")
    if results[0].status != "PASS":
        return "INCONCLUSIVE_CONTROL_FAILED"
    if all(result.status == "PASS" for result in results[1:]):
        return "PASS"
    return "FAIL_PATH_VARIANT"


def run_matrix(
    *,
    artifact_root: Path,
    executable_relative_path: Path,
    output_dir: Path,
    arguments: Sequence[str],
    launcher: Sequence[str],
    mode: str,
    startup_seconds: float,
    timeout_seconds: float,
    keep_copies: bool,
    capture_output: bool = False,
) -> MatrixReport:
    artifact_root = artifact_root.resolve(strict=True)
    output_dir = output_dir.resolve()
    try:
        output_dir.relative_to(artifact_root)
    except ValueError:
        pass
    else:
        raise MatrixError("output directory must not be inside the artifact root")
    rel = _safe_relative_path(executable_relative_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "path-matrix-report.json"
    _remove_path(report_path)
    _remove_path(output_dir / "logs")

    source_exe = artifact_root / executable_relative_path
    if not source_exe.is_file():
        raise MatrixError(f"executable does not exist inside artifact root: {rel}")
    original_manifest = tree_manifest(artifact_root)
    artifact_sha = tree_manifest_sha(original_manifest)
    privilege_context = detect_privilege_context()

    results: list[ScenarioResult] = []
    for scenario_name, component in SCENARIOS:
        case_root = output_dir / "cases" / component
        _remove_path(case_root)
        package_root = case_root / "package"
        log_path = output_dir / "logs" / f"{scenario_name}.log"
        copied_sha = ""
        copied_equal = False
        launched = False
        stable = False
        exit_code: int | None = None
        elapsed = 0.0
        status = "FAIL_SETUP"
        error_type: str | None = None
        rendered_args: tuple[str, ...] = ()
        try:
            copied_sha = copy_tree_verified(artifact_root, package_root, expected_source_sha=artifact_sha)
            copied_equal = copied_sha == artifact_sha
            launch_cwd = case_root / "launch-cwd"
            launch_cwd.mkdir(parents=True, exist_ok=False)
            workspace_root = (case_root / "workspace").resolve()
            child_environment = os.environ.copy()
            child_environment["AUTOSPORT_WORKSPACE"] = str(workspace_root)
            executable = package_root / executable_relative_path
            if not executable.is_file():
                raise MatrixError(f"copied executable missing: {rel}")
            rendered_args = render_arguments(arguments, case_root=case_root, package_root=package_root)
            cmd = command_for(executable, rendered_args, launcher)
            launched, stable, exit_code, elapsed, status, error_type = probe_process(
                cmd,
                cwd=launch_cwd,
                stdout_path=log_path,
                mode=mode,
                startup_seconds=startup_seconds,
                timeout_seconds=timeout_seconds,
                capture_output=capture_output,
                environment=child_environment,
            )
        except Exception as exc:
            error_type = type(exc).__name__
            status = "FAIL_SETUP"
        results.append(
            ScenarioResult(
                name=scenario_name,
                path_component=component,
                artifact_tree_sha256=copied_sha,
                copied_bytes_equal=copied_equal,
                launched=launched,
                startup_stable=stable,
                exit_code=exit_code,
                probe_mode=mode,
                elapsed_seconds=round(elapsed, 3),
                argument_count=len(rendered_args),
                argument_sha256=_canonical_json_sha(list(rendered_args)),
                status=status,
                output_captured=capture_output,
                error_type=error_type,
            )
        )
        if not keep_copies and case_root.exists():
            shutil.rmtree(case_root, ignore_errors=True)

    matrix_status = classify_matrix(results)
    report = MatrixReport(
        schema_version=SCHEMA_VERSION,
        semantic_key=SEMANTIC_KEY,
        authority_family=AUTHORITY_FAMILY,
        artifact_tree_sha256=artifact_sha,
        executable_relative_path=rel,
        probe_mode=mode,
        startup_seconds=startup_seconds,
        timeout_seconds=timeout_seconds,
        launch_cwd_policy=LAUNCH_CWD_POLICY,
        workspace_policy=WORKSPACE_POLICY,
        privilege_context=privilege_context,
        non_admin_verified=privilege_context == "STANDARD_USER",
        scenarios=tuple(results),
        matrix_status=matrix_status,
    )
    report_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qualify one packaged Autosport artifact under Windows paths with spaces and Cyrillic characters.")
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--executable-relative-path", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--argument", action="append", default=[], help="Application argument. Tokens {CASE_ROOT}, {PACKAGE_ROOT}, and {WORKSPACE_ROOT} are expanded per case. Repeat as needed.")
    parser.add_argument("--launcher", action="append", default=[], help="Optional launcher prefix, intended for test/dev use. Omit for the packaged .exe.")
    parser.add_argument("--mode", choices=("persistent", "exit-zero"), default="persistent")
    parser.add_argument("--startup-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--keep-copies", action="store_true")
    parser.add_argument("--capture-output", action="store_true", help="Opt in to persisting candidate stdout/stderr logs. Disabled by default to reduce accidental sensitive-output retention.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_matrix(
            artifact_root=args.artifact_root,
            executable_relative_path=args.executable_relative_path,
            output_dir=args.output_dir,
            arguments=args.argument,
            launcher=args.launcher,
            mode=args.mode,
            startup_seconds=args.startup_seconds,
            timeout_seconds=args.timeout_seconds,
            keep_copies=args.keep_copies,
            capture_output=args.capture_output,
        )
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}", file=sys.stderr)
        return 3
    print(report.matrix_status)
    print(str((args.output_dir.resolve() / "path-matrix-report.json")))
    return 0 if report.matrix_status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
