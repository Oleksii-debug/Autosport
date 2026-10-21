"""Read-only evidence of actual members embedded in a PyInstaller one-file EXE.

Implements the bounded CArchive v6 table-of-contents layout using only Python
stdlib, so the independent ZIP verifier needs no PyInstaller installation.
This is an executable *member catalog*, not a wheel-level SBOM, software
license approval, authenticode verification, or code-signing assertion.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import re
import struct
from typing import BinaryIO

_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
_COOKIE = struct.Struct("!8sIIII64s")
_TOC_HEADER = struct.Struct("!IIIIBc")
_MAX_EXECUTABLE = 2 * 1024 * 1024 * 1024
_MAX_TOC = 32 * 1024 * 1024
_MAX_ITEMS = 20000
_MAX_NAME = 1024
_EVIDENCE_CLASS = "ACTUAL_PYINSTALLER_CARCHIVE_MEMBERS_ONLY"


def _last_cookie_offset(source: BinaryIO, file_size: int) -> int:
    end = file_size
    while end > 0:
        start = max(0, end - 65536)
        source.seek(start)
        segment = source.read(end - start)
        pos = segment.rfind(_MAGIC)
        if pos >= 0 and start + pos + _COOKIE.size <= file_size:
            return start + pos
        if start == 0:
            break
        end = start + len(_MAGIC) - 1
    raise ValueError("PyInstaller CArchive cookie is missing")


def _read_sha(source: BinaryIO, offset: int, length: int) -> str:
    source.seek(offset)
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        block = source.read(min(1024 * 1024, remaining))
        if not block:
            raise ValueError("PyInstaller member data truncated")
        digest.update(block)
        remaining -= len(block)
    return digest.hexdigest()


def _catalog(source: BinaryIO, source_sha: str) -> dict:
    if not isinstance(source_sha, str) or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("source_sha must be exact lower-case Git commit SHA")
    source.seek(0, io.SEEK_END)
    file_size = source.tell()
    if not 0 < file_size <= _MAX_EXECUTABLE:
        raise ValueError("PyInstaller executable size exceeds supported limit")
    exe_sha = _read_sha(source, 0, file_size)
    cookie_offset = _last_cookie_offset(source, file_size)
    source.seek(cookie_offset)
    cookie = source.read(_COOKIE.size)
    if len(cookie) != _COOKIE.size:
        raise ValueError("PyInstaller CArchive cookie is truncated")
    magic, archive_length, toc_offset, toc_length, python_version, libname = _COOKIE.unpack(cookie)
    archive_end = cookie_offset + _COOKIE.size
    archive_start = archive_end - archive_length
    if (
        magic != _MAGIC
        or archive_start < 0
        or archive_length < _COOKIE.size + _TOC_HEADER.size
        or toc_length < _TOC_HEADER.size
        or toc_length > _MAX_TOC
        or toc_offset + toc_length + _COOKIE.size != archive_length
        or not libname.strip(b"\x00")
        or not 200 <= python_version <= 999
    ):
        raise ValueError("PyInstaller CArchive cookie layout is invalid")
    toc_start = archive_start + toc_offset
    if toc_start < archive_start or toc_start + toc_length != cookie_offset:
        raise ValueError("PyInstaller CArchive TOC offsets are invalid")
    source.seek(toc_start)
    toc = source.read(toc_length)
    if len(toc) != toc_length:
        raise ValueError("PyInstaller CArchive TOC is truncated")
    entries: list[dict] = []
    ranges: list[tuple[int, int]] = []
    seen: set[str] = set()
    offset = 0
    while offset < toc_length:
        if len(entries) >= _MAX_ITEMS or toc_length - offset < _TOC_HEADER.size:
            raise ValueError("PyInstaller CArchive TOC entry count or width is invalid")
        width, member_offset, packed_len, unpacked_len, compression, raw_type = _TOC_HEADER.unpack_from(toc, offset)
        if width <= _TOC_HEADER.size or width > _TOC_HEADER.size + _MAX_NAME or width > toc_length - offset:
            raise ValueError("PyInstaller CArchive TOC member width is invalid")
        raw_name = toc[offset + _TOC_HEADER.size : offset + width]
        offset += width
        if b"\x00" not in raw_name:
            raise ValueError("PyInstaller CArchive TOC name is not NUL-terminated")
        encoded_name, _, padding = raw_name.partition(b"\x00")
        if not encoded_name or padding.strip(b"\x00"):
            raise ValueError("PyInstaller CArchive TOC name padding is malformed")
        try:
            name = encoded_name.decode("utf-8", errors="strict")
            typecode = raw_type.decode("ascii", errors="strict")
        except UnicodeError as exc:
            raise ValueError("PyInstaller CArchive TOC name/type is undecodable") from exc
        if (
            not typecode.isprintable()
            or len(typecode) != 1
            or len(name) > _MAX_NAME
            or any(ord(char) < 32 for char in name)
            or name.startswith(("/", "\\"))
            or ".." in name.replace("\\", "/").split("/")
        ):
            raise ValueError("PyInstaller CArchive TOC member name/type is unsafe")
        if name in seen and typecode != "o":
            raise ValueError("PyInstaller CArchive TOC contains duplicate member name")
        seen.add(name)
        if compression not in (0, 1) or member_offset + packed_len > toc_offset:
            raise ValueError("PyInstaller CArchive member boundaries are invalid")
        if packed_len:
            ranges.append((member_offset, member_offset + packed_len))
        entries.append({
            "name": name,
            "typecode": typecode,
            "packed_length": packed_len,
            "unpacked_length": unpacked_len,
            "compressed": bool(compression),
            "packed_sha256": _read_sha(source, archive_start + member_offset, packed_len),
        })
    previous_end = 0
    for first, last in sorted(ranges):
        if first < previous_end:
            raise ValueError("PyInstaller CArchive TOC contains overlapping payloads")
        previous_end = last
    if not entries:
        raise ValueError("PyInstaller CArchive TOC is empty")
    return {
        "schema_version": 1,
        "source_sha": source_sha,
        "evidence_class": _EVIDENCE_CLASS,
        "executable_sha256": exe_sha,
        "executable_size": file_size,
        "archive_offset": archive_start,
        "archive_length": archive_length,
        "python_version": python_version,
        "members": sorted(entries, key=lambda item: (item["typecode"], item["name"])),
    }


def capture_pyinstaller_member_catalog(executable: str | Path | bytes, source_sha: str) -> dict:
    """Read the actual EXE's embedded member bytes, without executing the EXE."""
    if isinstance(executable, bytes):
        return _catalog(io.BytesIO(executable), source_sha)
    with Path(executable).open("rb") as handle:
        return _catalog(handle, source_sha)


def verify_pyinstaller_member_catalog(
    executable: str | Path | bytes, evidence: dict, source_sha: str,
) -> None:
    if not isinstance(evidence, dict):
        raise ValueError("PyInstaller CArchive member catalog is missing")
    exact = capture_pyinstaller_member_catalog(executable, source_sha)
    if evidence != exact:
        raise ValueError("PyInstaller embedded member catalog differs from actual EXE bytes")
