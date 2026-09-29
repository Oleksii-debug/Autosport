"""Fail closed if a historical snapshot changes logical provider scope in flight.

The owning :mod:`historical_snapshot` module remains the one snapshot producer.
This composition guard does not create another downloader, evidence format, or
provider authority.  It freezes the logical request/canonicalization scope used by
one child capture and requires the same scope immediately after the child returns.

This matters for multi-snapshot acquisition bundles: the legacy child builds each
URL from mutable provider fields.  Without this fence a transport callback could
change regions/markets/base/source identity during snapshot N and snapshot N+1
would then use a different logical scope while the parent bundle still carried the
scope frozen before the loop.
"""

from __future__ import annotations

import sys
from typing import Any

from . import historical_snapshot as _snapshot
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


def _install() -> None:
    original = _snapshot.capture_historical_snapshot
    logical_scope = _logical_scope

    def guarded_capture(provider: Any, *args: Any, **kwargs: Any):
        expected_scope = logical_scope(provider)
        report = original(provider, *args, **kwargs)
        if logical_scope(provider) != expected_scope:
            raise ProviderPayloadError(
                "historical snapshot provider logical scope changed during acquisition"
            )
        return report

    guarded_capture.__name__ = original.__name__
    guarded_capture.__qualname__ = original.__qualname__
    guarded_capture.__doc__ = original.__doc__
    guarded_capture.__annotations__ = dict(original.__annotations__)
    _snapshot.capture_historical_snapshot = guarded_capture

    # If the bundle module was imported before this composition layer, update only
    # the exact child alias it captured.  Later imports naturally receive the
    # guarded function from historical_snapshot.
    acquisition = sys.modules.get(f"{__package__}.historical_acquisition")
    if (
        acquisition is not None
        and getattr(acquisition, "capture_historical_snapshot", None) is original
    ):
        acquisition.capture_historical_snapshot = guarded_capture


_install()
del _install


__all__: list[str] = []
