#!/usr/bin/env python3
"""Fail-closed Plan-6 atomic closure preflight for a *resolved* merge tree.

This is a source-authority check, not a Windows/UIA/CI/NVDA acceptance oracle.
Use it while the aggregate Plan-6 Section 2 is still IN_PROGRESS_NOT_DONE.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REGISTRY = "MULTI_PLAN_CLOSURE_STATE.md"
ATOMIC = "ATOMIC_OPEN_SECTIONS_2026-10-13.md"
REQUIRED_ATOMIC = ("2А", "2Б", "2В")
GATE = "SOURCE_AUTHORITY_ONLY_NOT_WINDOWS_OR_NVDA"
MAX_BYTES = 2_000_000


class AuthorityError(ValueError):
    """An untrusted/missing source-authority input cannot be evaluated."""


def _read_regular(root: Path, name: str) -> str:
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise AuthorityError(f"{name}: missing, symlink, or not a regular file")
    try:
        if path.stat().st_size > MAX_BYTES:
            raise AuthorityError(f"{name}: unreasonably large authority file")
        return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise AuthorityError(f"{name}: cannot read valid UTF-8") from exc


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


def inspect(root: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise AuthorityError("root must be a directory")
    registry = _read_regular(root, REGISTRY)
    atomic = _read_regular(root, ATOMIC)
    failures: list[str] = []
    if not re.match(r"\A## Перебудова незакритих розділів — 2026-10-13\s*\n", registry):
        failures.append("missing 2026-10-13 owner directive at start of registry")
    if ATOMIC not in registry.split("# Autosport — Multi-Plan Closure State", 1)[0]:
        failures.append("owner directive does not reference canonical atomic map")
    if not re.match(r"\A# Autosport — повний реєстр самостійних розділів", atomic):
        failures.append("atomic map has no canonical registry title")
    try:
        plan6 = _one_section(registry, r"^### Plan 6\s+—[^\n]*$", r"^### Plan \d+\s+—", "registry Plan 6")
        parent = _rows(plan6, r"\d+").get("2", [])
        if parent != ["IN_PROGRESS_NOT_DONE"]:
            failures.append("Plan 6 aggregate Section 2 must have exactly one IN_PROGRESS_NOT_DONE row before closure")
    except AuthorityError as exc:
        failures.append(str(exc))
    try:
        plan6_atomic = _one_section(atomic, r"^## План 6:\s*\d+ самостійних розділів\s*$", r"^## План \d+:", "atomic Plan 6")
        if not re.search(r"^\|\s*Розділ\s*\|\s*Зміст\s*\|\s*Стан\s*\|", plan6_atomic, re.MULTILINE):
            failures.append("atomic Plan 6 section table header missing")
        atomic_rows = _rows(plan6_atomic, r"\d+[А-ЯІЇЄҐ]")
        for key in REQUIRED_ATOMIC:
            if len(atomic_rows.get(key, [])) != 1:
                failures.append(f"Plan 6 {key}: expected one canonical row, found {len(atomic_rows.get(key, []))}")
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
