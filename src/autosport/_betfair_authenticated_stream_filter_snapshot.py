"""Seal caller-owned Betfair market filters before subscription authority uses them.

The authenticated Stream composition hashes and later serializes a market filter into
an authority-bearing request.  A caller must not be able to mutate the original dict
between those operations and make the provider acknowledge bytes different from the
filter digest carried by the issued subscription.

This guard does not create another subscription/transport/freshness authority.  It
snapshots bounded canonical JSON once, validates the detached product-owned value, and
then delegates to the existing issuer with that value only.
"""

from __future__ import annotations

from functools import wraps
import json
from typing import Any

from . import betfair_authenticated_stream as _stream

_ORIGINAL_OPEN = _stream.open_authenticated_market_subscription
_CANONICAL_JSON_BYTES = _stream._canonical_json_bytes


def _sealed_market_filter(value: dict[str, Any]) -> dict[str, Any]:
    if type(value) is not dict or not value:
        raise ValueError("market_filter must be a non-empty exact dict")

    # Preserve the owning issuer's pre-serialization resource/secret fences, then
    # detach from caller ownership through the exact canonical bytes. Revalidate
    # the detached value so a concurrent mutation during the first traversal can
    # never smuggle an invalid/secret-bearing snapshot into positive authority.
    _stream._validate_json_bounds(value)
    _stream._reject_secret_keys(value)
    canonical = _CANONICAL_JSON_BYTES(value)
    if len(canonical) > _stream._MAX_SUBSCRIPTION_BYTES:
        raise ValueError("market_filter canonical snapshot exceeds bounded size")
    try:
        sealed = json.loads(canonical.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise ValueError("market_filter canonical snapshot is not valid JSON") from exc
    if type(sealed) is not dict or not sealed:
        raise ValueError("market_filter canonical snapshot must be a non-empty object")
    _stream._validate_json_bounds(sealed)
    _stream._reject_secret_keys(sealed)
    if _CANONICAL_JSON_BYTES(sealed) != canonical:
        raise ValueError("market_filter canonical snapshot is not stable")
    return sealed


@wraps(_ORIGINAL_OPEN)
def _open_with_sealed_market_filter(
    transport,
    *,
    provider_request_id: int,
    market_filter: dict[str, Any],
    market_data_fields: tuple[str, ...],
    ladder_levels: int | None,
    heartbeat_ms: int,
    conflate_ms: int,
):
    sealed_market_filter = _sealed_market_filter(market_filter)
    return _ORIGINAL_OPEN(
        transport,
        provider_request_id=provider_request_id,
        market_filter=sealed_market_filter,
        market_data_fields=market_data_fields,
        ladder_levels=ladder_levels,
        heartbeat_ms=heartbeat_ms,
        conflate_ms=conflate_ms,
    )


_open_with_sealed_market_filter._autosport_market_filter_snapshot_guard = True
_stream.open_authenticated_market_subscription = _open_with_sealed_market_filter
