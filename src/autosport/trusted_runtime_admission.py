"""Serialized admission for the existing trusted runtime code-profile authority.

This module does not create a second runtime authority.  It holds the exact lock
owned by ``trusted_runtime_code_profile`` while a caller consumes one exact active
profile, so canonical ProductGuiWorker STOP/revocation cannot win between the final
profile check and the protected operation.

The lease is intentionally generic and grants no provider-write or REAL-money
permission by itself.  Callers must compose it with their own economic, provider,
execution-scope, and STOP authorities.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import trusted_runtime_code_profile as _authority
from .trusted_runtime_code_profile import TrustedRuntimeCodeProfile


_CANONICAL_LOCK = _authority._LOCK
_CANONICAL_REQUIRE = _authority.require_authoritative_trusted_runtime_code_profile
_CANONICAL_REQUIRE_CODE = getattr(_CANONICAL_REQUIRE, "__code__", None)

if _CANONICAL_REQUIRE_CODE is None:
    raise RuntimeError("canonical trusted runtime profile authority is unavailable")


@contextmanager
def trusted_runtime_code_profile_admission(
    value: object,
    *,
    workspace: str | Path,
) -> Iterator[TrustedRuntimeCodeProfile]:
    """Hold the canonical profile authority across one protected operation."""

    if (
        _authority._LOCK is not _CANONICAL_LOCK
        or _authority.require_authoritative_trusted_runtime_code_profile
        is not _CANONICAL_REQUIRE
        or getattr(_CANONICAL_REQUIRE, "__code__", None)
        is not _CANONICAL_REQUIRE_CODE
    ):
        raise _authority.TrustedRuntimeCodeProfileError(
            "trusted runtime profile admission authority changed"
        )

    with _CANONICAL_LOCK:
        admitted = _CANONICAL_REQUIRE(value, workspace=workspace)
        yield admitted
        # Catch lifecycle drift not serialized through ProductGuiWorker (for example,
        # a canonical runtime that has already left RUNNING for another reason).
        # ProductGuiWorker STOP/revoke itself is serialized by the held authority lock.
        _CANONICAL_REQUIRE(admitted, workspace=workspace)
