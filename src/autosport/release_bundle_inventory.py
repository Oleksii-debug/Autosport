"""Fail-closed inventory of the actual PyInstaller CArchive inside a Windows EXE.

Evidence names and sizes are extracted from the executable, not from pip's build
host. The complete executable SHA-256 binds the bytes; this is NOT a software
bill of materials, license clearance, or proof of which Python wheels were used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_MAX_EXE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ENTRIES = 50000


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _name(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("invalid bundled entry name")
    if (
        value.startswith("/") or "\\" in value or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("unsafe bundled entry name")
    return value


def inventory_from_toc(
    toc: Mapping[str, Any], *, source_sha: str, exe_sha256: str, role: str,
    exe_size: int,
) -> dict[str, Any]:
    if not isinstance(source_sha, str) or not _SHA.fullmatch(source_sha):
        raise ValueError("invalid source SHA")
    if not isinstance(exe_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", exe_sha256) is None:
        raise ValueError("invalid executable SHA")
    if role not in {"gui", "data"}:
        raise ValueError("invalid executable role")
    if type(exe_size) is not int or not 0 < exe_size <= _MAX_EXE_BYTES:
        raise ValueError("invalid executable size")
    if not isinstance(toc, Mapping) or not 0 < len(toc) <= _MAX_ENTRIES:
        raise ValueError("invalid bundled entry count")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_name, record in toc.items():
        name = _name(raw_name)
        folded = name.casefold()
        if folded in seen:
            raise ValueError("ambiguous bundled entry name")
        seen.add(folded)
        if not isinstance(record, (tuple, list)) or len(record) != 5:
            raise ValueError("invalid bundled entry structure")
        offset, compressed, expanded, compressed_flag, kind = record
        if any(type(n) is not int or n < 0 for n in (offset, compressed, expanded)):
            raise ValueError("invalid bundled entry bounds")
        if (
            offset > exe_size or compressed > exe_size
            or offset + compressed > exe_size
            or compressed_flag not in (0, 1) or type(compressed_flag) is not int
            or not isinstance(kind, str) or len(kind) != 1
            or not kind.isascii() or not kind.isprintable()
        ):
            raise ValueError("unsafe bundled entry metadata")
        entries.append({
            "name": name,
            "typecode": kind,
            "offset": offset,
            "compressed_bytes": compressed,
            "expanded_bytes": expanded,
            "compressed": bool(compressed_flag),
        })
    entries.sort(key=lambda item: item["name"])
    return {
        "schema_version": 1,
        "source_sha": source_sha,
        "exe_sha256": exe_sha256,
        "exe_role": role,
        "evidence_class": "PYINSTALLER_CARCHIVE_NAMES_AND_SIZES_ONLY",
        "license_clearance": False,
        "bundled_distribution_sbom_verified": False,
        "entries": entries,
    }


def capture_inventory(path: Path, *, source_sha: str, role: str) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > _MAX_EXE_BYTES:
        raise ValueError("unsafe executable")
    first = _sha256_file(path)
    from PyInstaller.archive.readers import CArchiveReader
    reader = CArchiveReader(str(path))
    result = inventory_from_toc(
        reader.toc, source_sha=source_sha, exe_sha256=first,
        role=role, exe_size=info.st_size,
    )
    final = path.lstat()
    if (
        not stat.S_ISREG(final.st_mode)
        or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
        or _sha256_file(path) != first
    ):
        raise ValueError("executable changed during bundled inventory")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect bound PyInstaller executable")
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--role", choices=("gui", "data"), required=True)
    args = parser.parse_args()
    try:
        result = capture_inventory(args.exe, source_sha=args.source_sha, role=args.role)
    except (OSError, ValueError, ImportError, RuntimeError, KeyError, TypeError) as exc:
        raise SystemExit("Bundled executable inventory failed closed") from None
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
