#!/usr/bin/env python3
"""Fail-closed Plan-6 atomic closure preflight for a *resolved* merge tree.

This is a source-authority check, not a Windows/UIA/CI/NVDA acceptance oracle.
Supports both in-progress and terminal aggregate states without asserting acceptance.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

REGISTRY = "MULTI_PLAN_CLOSURE_STATE.md"
ATOMIC = "ATOMIC_OPEN_SECTIONS_2026-10-13.md"
REQUIRED_ATOMIC = ("2А", "2Б", "2В")
GATE = "SOURCE_AUTHORITY_ONLY_NOT_WINDOWS_OR_NVDA"
MAX_BYTES = 2_000_000


class AuthorityError(ValueError):
    """An untrusted/missing source-authority input cannot be evaluated."""


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _check_identity(root: Path, name: str, expected: tuple[int, int, int, int, int]) -> None:
    """Ensure an earlier authority input has not changed while reading its peer."""
    try:
        current = (root / name).lstat()
    except OSError as exc:
        raise AuthorityError(f"{name}: vanished between authority reads") from exc
    if not stat.S_ISREG(current.st_mode) or _identity(current) != expected:
        raise AuthorityError(f"{name}: changed between authority reads")


def _read_regular(root: Path, name: str) -> tuple[str, tuple[int, int, int, int, int]]:
    """Pin the opened regular file; reject symlink swaps and concurrent writes.

    The path is checked both before and after reading. The descriptor, not a
    second path lookup, supplies the bytes. This is an integrity preflight,
    not a substitute for a frozen Git commit/tree identity.
    """
    path = root / name
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise AuthorityError(f"{name}: missing, symlink, or not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise AuthorityError(f"{name}: not a regular opened file")
            if opened.st_size > MAX_BYTES:
                raise AuthorityError(f"{name}: unreasonably large authority file")
            if _identity(before) != _identity(opened):
                raise AuthorityError(f"{name}: file changed before open")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(65536, MAX_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_BYTES:
                    raise AuthorityError(f"{name}: unreasonably large authority file")
            after_fd = os.fstat(fd)
            after_path = path.lstat()
            if (not stat.S_ISREG(after_path.st_mode) or
                _identity(opened) != _identity(after_path) or
                _identity(opened) != _identity(after_fd)):
                raise AuthorityError(f"{name}: file changed during read")
            return b"".join(chunks).decode("utf-8", errors="strict"), _identity(opened)
        finally:
            os.close(fd)
    except (OSError, UnicodeError) as exc:
        raise AuthorityError(f"{name}: cannot read stable valid UTF-8") from exc


def _one_section(text: str, start_pattern: str, next_pattern: str, label: str) -> str:
    matches = list(re.finditer(start_pattern, text, re.MULTILINE))
    if len(matches) != 1:
        raise AuthorityError(f"{label}: expected one canonical section heading, found {len(matches)}")
    start = matches[0].end()
    following = re.search(next_pattern, text[start:], re.MULTILINE)
    end = start + following.start() if following else len(text)
    return text[start:end]


def _rows(section: str, key_pattern: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for match in re.finditer(r"^\|\s*(" + key_pattern + r")\s*\|\s*([^|\n]+)\|", section, re.MULTILINE):
        result.setdefault(match.group(1).strip(), []).append(match.group(2).strip())
    return result


def _atomic_statuses(section: str) -> dict[str, list[str]]:
    """Read the *third* column, never the description or another plan's rows."""
    result: dict[str, list[str]] = {}
    for line in section.splitlines():
        match = re.match(r"^\|\s*(\d+[А-ЯІЇЄҐ])\s*\|", line)
        if not match:
            continue
        key = match.group(1)
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3 or cells[0] != key or not cells[1] or not cells[2]:
            result.setdefault(key, []).append("")
        else:
            result.setdefault(key, []).append(cells[2])
    return result


def _is_terminal_done(value: str) -> bool:
    # A description containing the word DONE is not an acceptance record.
    normalized = re.sub(r"^(?:Статус(?: міграції)?\s*:\s*)", "", value).strip()
    return bool(re.match(r"^DONE(?:\s*(?:—|$))", normalized)) and not re.search(
        r"\bNOT[ _-]?DONE\b", normalized, re.IGNORECASE
    )


def inspect(root: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise AuthorityError("root must be a directory")
    registry, registry_identity = _read_regular(root, REGISTRY)
    atomic, atomic_identity = _read_regular(root, ATOMIC)
    # A file may be replaced after its own descriptor was closed but before
    # its peer was read. Fail closed on mixed-epoch source authority.
    _check_identity(root, REGISTRY, registry_identity)
    _check_identity(root, ATOMIC, atomic_identity)
    failures: list[str] = []
    if not re.match(r"\A## Перебудова незакритих розділів — 2026-10-13\s*\n", registry):
        failures.append("missing 2026-10-13 owner directive at start of registry")
    if ATOMIC not in registry.split("# Autosport — Multi-Plan Closure State", 1)[0]:
        failures.append("owner directive does not reference canonical atomic map")
    if not re.match(r"\A# Autosport — повний реєстр самостійних розділів", atomic):
        failures.append("atomic map has no canonical registry title")
    parent_state: str | None = None
    atomic_states: dict[str, str] = {}
    try:
        plan6 = _one_section(registry, r"^### Plan 6\s+—[^\n]*$", r"^### Plan \d+\s+—", "registry Plan 6")
        parent = _rows(plan6, r"\d+").get("2", [])
        if len(parent) != 1 or parent[0] not in {"IN_PROGRESS_NOT_DONE", "DONE"}:
            failures.append("Plan 6 aggregate Section 2 must have exactly one recognized state row")
        else:
            parent_state = parent[0]
    except AuthorityError as exc:
        failures.append(str(exc))
    try:
        plan6_atomic = _one_section(atomic, r"^## План 6:\s*\d+ самостійних розділів\s*$", r"^## План \d+:", "atomic Plan 6")
        if not re.search(r"^\|\s*Розділ\s*\|\s*Зміст\s*\|\s*Стан\s*\|", plan6_atomic, re.MULTILINE):
            failures.append("atomic Plan 6 section table header missing")
        atomic_rows = _atomic_statuses(plan6_atomic)
        for key in REQUIRED_ATOMIC:
            values = atomic_rows.get(key, [])
            if len(values) != 1 or not values[0]:
                failures.append(f"Plan 6 {key}: expected one valid three-column row, found {len(values)}")
            else:
                atomic_states[key] = values[0]
        if parent_state == "DONE" and len(atomic_states) == len(REQUIRED_ATOMIC):
            incomplete = [key for key, value in atomic_states.items() if not _is_terminal_done(value)]
            if incomplete:
                failures.append("Plan 6 aggregate DONE conflicts with unfinished atomic rows: " + ", ".join(incomplete))
    except AuthorityError as exc:
        failures.append(str(exc))
    return {"ok": not failures, "gate": GATE, "required_sections": list(REQUIRED_ATOMIC), "failures": failures}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved-tree", type=Path, required=True, help="Checked-out resolved integration tree, not stale PR head")
    args = parser.parse_args(argv)
    try:
        report = inspect(args.resolved_tree)
    except (AuthorityError, OSError) as exc:
        report = {"ok": False, "gate": GATE, "failures": [str(exc)]}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
