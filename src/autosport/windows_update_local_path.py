"""Lexical Windows update input path policy; no filesystem access.

Use before opening an operator-supplied request, package or manifest. This
policy does not prove a trust root or authorize an installation.
"""

from __future__ import annotations

import os
import re
from pathlib import PureWindowsPath


class UnsafeUpdatePath(ValueError):
    """An operator path could resolve to a device, remote share or alias."""


_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{n}" for n in range(1, 10)),
    *(f"lpt{n}" for n in range(1, 10)),
    "com¹", "com²", "com³", "lpt¹", "lpt²", "lpt³",
})
_INVALID = frozenset('<>:"|?*')


def require_local_update_path(value: str) -> str:
    """Return *value* unchanged, or reject ambiguous Windows spellings.

    The caller must separately verify regular-file identity, held descriptors,
    SHA-256 and product-owned signature/trust authority. This helper performs
    no I/O and cannot grant permission to install or update.
    """
    if type(value) is not str or not value or "\x00" in value:
        raise UnsafeUpdatePath("invalid update path")
    if value.startswith(("//", "\\")):
        raise UnsafeUpdatePath("network or device path is not allowed")
    # PureWindowsPath normalizes dot components and repeated separators. Reject
    # them from the raw spelling first so different operator strings cannot
    # silently alias the same candidate path.
    raw_parts = re.split(r"[\\/]", value)
    if any(part in (".", "..") for part in raw_parts):
        raise UnsafeUpdatePath("noncanonical relative path component")
    if any(not part for part in raw_parts[1:]):
        raise UnsafeUpdatePath("repeated or trailing path separator")
    win = PureWindowsPath(value)
    if os.name == "nt" and win.root and not win.drive:
        raise UnsafeUpdatePath("current-drive rooted path is not allowed")
    if win.drive and (not win.root or os.name != "nt"):
        raise UnsafeUpdatePath("drive-relative or foreign drive path is not allowed")
    parts = win.parts[1:] if win.drive or win.root else win.parts
    for part in parts:
        if (
            part in (".", "..")
            or part.endswith((".", " "))
            or any(ord(char) < 32 or char in _INVALID for char in part)
            or part.split(".", 1)[0].rstrip(" ").casefold() in _RESERVED
        ):
            raise UnsafeUpdatePath("Windows device or ambiguous path is not allowed")
    return value
