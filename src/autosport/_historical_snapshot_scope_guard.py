"""Fail closed on mutable snapshot scope and destructive historical pair reuse.

The owning :mod:`historical_snapshot` and :mod:`historical_matches` modules remain
the only producers.  This composition guard does not create another downloader,
evidence format, generation store, or provider authority.

For snapshot children it freezes the logical request/canonicalization scope used by
one child capture and requires the same scope immediately after the child returns.
For historical match/result artifacts it serializes one exact output/evidence pair
and refuses to overwrite either existing destination.  The latter preserves an
already-valid evidence pair if a caller retries the same product path: replacing the
capture first and then failing the evidence write must never destroy prior evidence.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import historical_matches as _matches
from . import historical_snapshot as _snapshot
from .integrity import durable_path_lock
from .parlayapi_provider import ProviderPayloadError


_CANONICAL_SPORT_KEY = "table_tennis"
_CANONICAL_SOURCE_ID = "parlayapi:table_tennis"


def _logical_scope(provider: Any) -> tuple[str, str, str, tuple[object, ...], tuple[object, ...]]:
    sport_key = provider.sport_key
    source_id = provider.source_id
    base_url = provider.base_url
    regions = tuple(provider.regions)
    markets = tuple(provider.markets)
    if sport_key != _CANONICAL_SPORT_KEY:
        raise ProviderPayloadError(
            "historical snapshot requires the canonical table-tennis sport scope"
        )
    if source_id != _CANONICAL_SOURCE_ID:
        raise ProviderPayloadError(
            "historical snapshot requires the canonical provider source identity"
        )
    if type(base_url) is not str or not base_url:
        raise ProviderPayloadError("historical snapshot provider base_url must be text")
    return sport_key, source_id, base_url, regions, markets


def _pair_lock_path(output: Path, evidence: Path) -> Path:
    identities = sorted(
        os.path.normcase(str(path.resolve(strict=False)))
        for path in (output, evidence)
    )
    digest = hashlib.sha256("\0".join(identities).encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / "autosport-historical-match-pair-locks" / digest


def _destination_exists(path: Path) -> bool:
    # lexists rejects broken symlinks too; an evidence path is immutable once any
    # filesystem entry occupies that exact destination.
    return os.path.lexists(path)


def _install() -> None:
    original_snapshot = _snapshot.capture_historical_snapshot
    original_matches = _matches.capture_historical_matches
    logical_scope = _logical_scope
    pair_lock_path = _pair_lock_path
    destination_exists = _destination_exists
    pair_lock = durable_path_lock

    def guarded_snapshot(provider: Any, *args: Any, **kwargs: Any):
        expected_scope = logical_scope(provider)
        report = original_snapshot(provider, *args, **kwargs)
        if logical_scope(provider) != expected_scope:
            raise ProviderPayloadError(
                "historical snapshot provider logical scope changed during acquisition"
            )
        return report

    def guarded_matches(
        provider: Any,
        *,
        requested_date: str,
        output_path: str | Path,
        evidence_path: str | Path | None = None,
        priced_only: bool = False,
    ):
        output = Path(output_path)
        evidence = (
            Path(evidence_path)
            if evidence_path is not None
            else output.with_suffix(output.suffix + ".evidence.json")
        )
        with pair_lock(pair_lock_path(output, evidence)):
            if destination_exists(output) or destination_exists(evidence):
                raise ProviderPayloadError(
                    "historical match capture destinations already exist"
                )
            return original_matches(
                provider,
                requested_date=requested_date,
                output_path=output,
                evidence_path=evidence,
                priced_only=priced_only,
            )

    guarded_snapshot.__name__ = original_snapshot.__name__
    guarded_snapshot.__qualname__ = original_snapshot.__qualname__
    guarded_snapshot.__doc__ = original_snapshot.__doc__
    guarded_snapshot.__annotations__ = dict(original_snapshot.__annotations__)
    _snapshot.capture_historical_snapshot = guarded_snapshot

    guarded_matches.__name__ = original_matches.__name__
    guarded_matches.__qualname__ = original_matches.__qualname__
    guarded_matches.__doc__ = original_matches.__doc__
    guarded_matches.__annotations__ = dict(original_matches.__annotations__)
    _matches.capture_historical_matches = guarded_matches

    # If the bundle module was imported before this composition layer, update only
    # exact child aliases it captured. Later imports naturally receive the guards.
    acquisition = sys.modules.get(f"{__package__}.historical_acquisition")
    if acquisition is not None:
        if getattr(acquisition, "capture_historical_snapshot", None) is original_snapshot:
            acquisition.capture_historical_snapshot = guarded_snapshot
        if getattr(acquisition, "capture_historical_matches", None) is original_matches:
            acquisition.capture_historical_matches = guarded_matches


_install()
del _install


__all__: list[str] = []
