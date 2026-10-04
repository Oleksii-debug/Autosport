from __future__ import annotations

import hashlib
import json
import re

from .domain import MarketEvent


PROPHETX_REST_MARKET_STATE_CONTRACT = "autosport.prophetx-rest-market-state.v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROPHETX_SOURCE_ID = "prophetx:sandbox"
_PROPHETX_PROVIDER = "prophetx"
_PROPHETX_ENVIRONMENT = "sandbox"
_PROPHETX_TRANSPORT_SURFACE = "v3_affiliate_get_markets"

# These values prove one local acquisition/provenance instance rather than the
# normalized provider market state. They intentionally remain durable on the
# MarketEvent, but do not create another semantic state transition when every
# market/economic/provider field is otherwise unchanged.
_LOCAL_ACQUISITION_METADATA_FIELDS = frozenset(
    {
        "product_acquisition_sequence",
        "response_sha256",
        "snapshot_fingerprint_sha256",
    }
)


class MarketStateIdentityError(ValueError):
    """A claimed product-owned semantic market-state identity is malformed."""


def _prophetx_rest_state_material(event: MarketEvent) -> dict[str, object]:
    if event.source_id != _PROPHETX_SOURCE_ID:
        raise MarketStateIdentityError(
            "ProphetX REST market-state contract requires the canonical source_id"
        )

    payload = event.to_dict()
    metadata = payload.get("metadata")
    if type(metadata) is not dict:
        raise MarketStateIdentityError("market-state metadata must be a JSON object")

    if metadata.get("provider") != _PROPHETX_PROVIDER:
        raise MarketStateIdentityError(
            "ProphetX REST market-state contract requires provider=prophetx"
        )
    if metadata.get("environment") != _PROPHETX_ENVIRONMENT:
        raise MarketStateIdentityError(
            "ProphetX REST market-state contract requires sandbox environment"
        )
    if metadata.get("transport_surface") != _PROPHETX_TRANSPORT_SURFACE:
        raise MarketStateIdentityError(
            "ProphetX REST market-state contract requires the canonical transport surface"
        )

    request_fingerprint = metadata.get("request_fingerprint_sha256")
    if (
        type(request_fingerprint) is not str
        or _SHA256_RE.fullmatch(request_fingerprint) is None
    ):
        raise MarketStateIdentityError(
            "ProphetX REST market-state contract requires a canonical request fingerprint"
        )

    acquisition_sequence = metadata.get("product_acquisition_sequence")
    if (
        type(acquisition_sequence) is not int
        or acquisition_sequence <= 0
        or acquisition_sequence > (1 << 63) - 1
        or acquisition_sequence != event.sequence
    ):
        raise MarketStateIdentityError(
            "ProphetX REST market-state acquisition sequence is inconsistent"
        )

    for field_name in ("response_sha256", "snapshot_fingerprint_sha256"):
        value = metadata.get(field_name)
        if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
            raise MarketStateIdentityError(
                f"ProphetX REST market-state contract requires canonical {field_name}"
            )

    sequence_authority_id = metadata.get("sequence_authority_id")
    sequence_source_id = metadata.get("sequence_source_id")
    if (
        type(sequence_authority_id) is not str
        or not sequence_authority_id
        or sequence_authority_id.strip() != sequence_authority_id
    ):
        raise MarketStateIdentityError(
            "ProphetX REST market-state contract requires sequence authority identity"
        )
    if (
        type(sequence_source_id) is not str
        or not sequence_source_id
        or sequence_source_id.strip() != sequence_source_id
    ):
        raise MarketStateIdentityError(
            "ProphetX REST market-state contract requires sequence source identity"
        )

    # Product receipt clocks and acquisition ordering are deliberately excluded.
    # Provider/source time, if a future compatible contract ever supplies it, remains
    # in the material because it is provider-state truth rather than local liveness.
    payload.pop("observed_ts", None)
    payload.pop("ingest_ts", None)
    payload.pop("sequence", None)

    semantic_metadata = dict(metadata)
    for field_name in _LOCAL_ACQUISITION_METADATA_FIELDS:
        semantic_metadata.pop(field_name, None)
    payload["metadata"] = semantic_metadata
    return payload


def semantic_market_state_identity(event: MarketEvent) -> str | None:
    """Return a versioned semantic-state digest for supported durable market events.

    Absence of a contract means the event uses ordinary event-by-event storage
    semantics. A claimed but unsupported/malformed contract fails closed instead of
    becoming a caller-mintable duplicate-suppression hint.
    """

    if not isinstance(event, MarketEvent):
        raise TypeError("event must be MarketEvent")

    contract = event.metadata.get("semantic_state_contract")
    if contract is None:
        return None
    if type(contract) is not str:
        raise MarketStateIdentityError("semantic_state_contract must be text")
    if contract != PROPHETX_REST_MARKET_STATE_CONTRACT:
        raise MarketStateIdentityError(
            "semantic market-state contract is unsupported by this product build"
        )

    material = _prophetx_rest_state_material(event)
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{contract}:{hashlib.sha256(encoded).hexdigest()}"


def same_semantic_market_state(previous: MarketEvent, incoming: MarketEvent) -> bool:
    """Return True only for two supported, exact same-state durable events."""

    if not isinstance(previous, MarketEvent) or not isinstance(incoming, MarketEvent):
        raise TypeError("previous and incoming must be MarketEvent")
    previous_identity = semantic_market_state_identity(previous)
    incoming_identity = semantic_market_state_identity(incoming)
    if previous_identity is None or incoming_identity is None:
        return False
    return previous_identity == incoming_identity
