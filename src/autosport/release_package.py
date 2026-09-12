from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_windows_package(
    exe_path: str | Path,
    start_file: str | Path,
    example_dir: str | Path,
    diagnostic_path: str | Path,
    output_zip: str | Path,
    source_sha: str,
) -> tuple[Path, str]:
    exe_path = Path(exe_path)
    start_file = Path(start_file)
    example_dir = Path(example_dir)
    diagnostic_path = Path(diagnostic_path)
    output_zip = Path(output_zip)
    package_dir = output_zip.parent / "Autosport-V1"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True)
    shutil.copy2(exe_path, package_dir / "Autosport.exe")
    shutil.copy2(start_file, package_dir / "WINDOWS_START_HERE.txt")
    shutil.copy2(diagnostic_path, package_dir / "packaged-diagnostic.json")
    shutil.copytree(example_dir, package_dir / "examples" / example_dir.name)

    build_info = {
        "product": "Autosport",
        "version": "0.1.0-v1-prehuman",
        "source_sha": source_sha,
        "autosport_exe_sha256": sha256_file(package_dir / "Autosport.exe"),
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }
    _write_json(package_dir / "BUILD_INFO.json", build_info)

    payload_hashes = {}
    for path in sorted(item for item in package_dir.rglob("*") if item.is_file()):
        relative = path.relative_to(package_dir).as_posix()
        if relative == "SHA256SUMS.txt":
            continue
        payload_hashes[relative] = sha256_file(path)
    _write_json(package_dir / "PACKAGE_MANIFEST.json", {"schema_version": 1, "files": payload_hashes})

    lines = []
    for path in sorted(item for item in package_dir.rglob("*") if item.is_file() and item.name != "SHA256SUMS.txt"):
        relative = path.relative_to(package_dir).as_posix()
        lines.append(f"{sha256_file(path)}  {relative}")
    (package_dir / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if output_zip.exists():
        output_zip.unlink()
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(item for item in package_dir.rglob("*") if item.is_file()):
            relative = Path("Autosport-V1") / path.relative_to(package_dir)
            info = zipfile.ZipInfo(relative.as_posix(), _FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if path.name.lower().endswith(".exe") else 0o644) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output_zip, sha256_file(output_zip)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
