"""Bind one Betfair historical marketDefinition revision to authenticated file origin.

This is a composition boundary only.  It consumes the process-local provider-origin
capability issued by ``betfair_historical_entitlement`` and the causal replay boundary
from ``betfair_historical_causal_replay``.  It does not create a second downloader,
parser, outcome authority, settlement authority, or execution path.

Betfair historical ``pt`` is provider publication time inside an archive record.  It is
not rewritten into a product observation/acquisition timestamp: the later authenticated
download time remains separate and explicit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from .betfair_historical_causal_replay import (
    BetfairHistoricalReplayRecord,
    BetfairHistoricalReplaySource,
    HistoricalCompression,
    HistoricalPackageTier,
    HistoricalRepresentation,
    replay_betfair_historical_until,
)
from .betfair_historical_entitlement import HistoricalProviderOriginWitness


class BetfairHistoricalMarketDefinitionOriginError(ValueError):
    """Historical market-definition origin could not be proven at this boundary."""


_PARSER_REVISION = "autosport.betfair-historical-market-definition-origin.v1"
_TOKEN = object()
_HEX = frozenset("0123456789abcdef")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetfairHistoricalMarketDefinitionOriginError(
            f"{name} must be a non-empty canonical string"
        )
    value.encode("utf-8")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or text != text.lower() or any(ch not in _HEX for ch in text):
        raise BetfairHistoricalMarketDefinitionOriginError(
            f"{name} must be canonical SHA-256 hex"
        )
    return text


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _canonical_market_definition(value: object) -> tuple[str, str]:
    if type(value) is not dict:
        raise BetfairHistoricalMarketDefinitionOriginError(
            "marketDefinition must be an exact JSON object"
        )
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise BetfairHistoricalMarketDefinitionOriginError(
            "marketDefinition is not canonical finite JSON"
        ) from exc
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BetfairHistoricalMarketDefinitionOrigin:
    """One archive revision bound to an authenticated exact historical file."""

    provider_origin_witness_sha256: str
    transport_contract_sha256: str
    download_file_identity_sha256: str
    provider_path: str
    raw_file_sha256: str
    entitlement_snapshot_sha256: str
    download_retrieved_at: str
    replay_source_identity: str
    source_ordinal: int
    provider_pt_ms: int
    line_sha256: str
    record_payload_sha256: str
    market_id: str
    market_definition_sha256: str
    market_definition_json: str
    package_tier: HistoricalPackageTier
    representation: HistoricalRepresentation
    _witness: HistoricalProviderOriginWitness = field(repr=False, compare=False)
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise TypeError(
                "BetfairHistoricalMarketDefinitionOrigin must be issued by the origin binder"
            )
        for name in (
            "provider_origin_witness_sha256",
            "transport_contract_sha256",
            "download_file_identity_sha256",
            "raw_file_sha256",
            "entitlement_snapshot_sha256",
            "replay_source_identity",
            "line_sha256",
            "record_payload_sha256",
            "market_definition_sha256",
        ):
            _sha(getattr(self, name), name)
        _text(self.provider_path, "provider_path")
        _text(self.download_retrieved_at, "download_retrieved_at")
        _text(self.market_id, "market_id")
        if type(self.source_ordinal) is not int or self.source_ordinal < 1:
            raise BetfairHistoricalMarketDefinitionOriginError(
                "source_ordinal must be a positive exact integer"
            )
        if type(self.provider_pt_ms) is not int or self.provider_pt_ms < 0:
            raise BetfairHistoricalMarketDefinitionOriginError(
                "provider_pt_ms must be a non-negative exact integer"
            )
        if type(self._witness) is not HistoricalProviderOriginWitness:
            raise BetfairHistoricalMarketDefinitionOriginError(
                "origin evidence must retain the exact upstream provider witness"
            )
        if self.provider_origin_witness_sha256 != self._witness.witness_sha256:
            raise BetfairHistoricalMarketDefinitionOriginError(
                "retained provider witness identity changed"
            )
        try:
            decoded = json.loads(self.market_definition_json)
        except json.JSONDecodeError as exc:
            raise BetfairHistoricalMarketDefinitionOriginError(
                "market_definition_json is invalid"
            ) from exc
        canonical, digest = _canonical_market_definition(decoded)
        if canonical != self.market_definition_json or digest != self.market_definition_sha256:
            raise BetfairHistoricalMarketDefinitionOriginError(
                "marketDefinition canonical identity mismatch"
            )

    @property
    def provider_origin_verified(self) -> bool:
        try:
            self._witness.assert_authoritative()
        except Exception:
            return False
        return self._witness.witness_sha256 == self.provider_origin_witness_sha256

    @property
    def evidence_sha256(self) -> str:
        return _digest(self._identity_payload())

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "autosport.betfair-historical-market-definition-origin.v1",
            "provider_origin_witness_sha256": self.provider_origin_witness_sha256,
            "transport_contract_sha256": self.transport_contract_sha256,
            "download_file_identity_sha256": self.download_file_identity_sha256,
            "provider_path": self.provider_path,
            "raw_file_sha256": self.raw_file_sha256,
            "entitlement_snapshot_sha256": self.entitlement_snapshot_sha256,
            "download_retrieved_at": self.download_retrieved_at,
            "replay_source_identity": self.replay_source_identity,
            "source_ordinal": self.source_ordinal,
            "provider_pt_ms": self.provider_pt_ms,
            "line_sha256": self.line_sha256,
            "record_payload_sha256": self.record_payload_sha256,
            "market_id": self.market_id,
            "market_definition_sha256": self.market_definition_sha256,
            "market_definition_json": self.market_definition_json,
            "package_tier": self.package_tier.value,
            "representation": self.representation.value,
            "parser_revision": _PARSER_REVISION,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "evidence_sha256": self.evidence_sha256,
            "truth": {
                "provider_origin_verified": self.provider_origin_verified,
                "market_definition_record_bound": True,
                "historical_provider_publish_time_bound": True,
                "product_observation_time_backdated_from_provider_pt": False,
                "download_acquisition_time_kept_separate": True,
                "usage_rights_verified": False,
                "rights_revalidation_required": True,
                "source_stream_continuity_proven": False,
                "outcome_roster_completeness_proven": False,
                "live_quote_proven": False,
                "execution_authority": False,
                "promotion_authority": False,
                "real_money_execution": False,
            },
        }

    def market_definition(self) -> dict[str, object]:
        value = json.loads(self.market_definition_json)
        if type(value) is not dict:
            raise BetfairHistoricalMarketDefinitionOriginError(
                "bound marketDefinition is no longer an object"
            )
        return value

    def assert_provider_origin(self) -> None:
        if not self.provider_origin_verified:
            raise BetfairHistoricalMarketDefinitionOriginError(
                "upstream provider-origin capability is no longer authoritative"
            )

    def assert_published_by(self, cutoff_pt_ms: int) -> None:
        if type(cutoff_pt_ms) is not int or cutoff_pt_ms < 0:
            raise BetfairHistoricalMarketDefinitionOriginError(
                "cutoff_pt_ms must be a non-negative exact integer"
            )
        if self.provider_pt_ms > cutoff_pt_ms:
            raise BetfairHistoricalMarketDefinitionOriginError(
                "marketDefinition revision was published after the requested provider cutoff"
            )


def _definition_in_record(
    record: BetfairHistoricalReplayRecord,
    *,
    market_id: str,
) -> object | None:
    payload = record.payload()
    market_changes = payload.get("mc")
    if market_changes is None:
        return None
    if type(market_changes) is not list:
        raise BetfairHistoricalMarketDefinitionOriginError(
            "historical mcm.mc must be an exact JSON array"
        )
    matches: list[object] = []
    for item in market_changes:
        if type(item) is not dict:
            raise BetfairHistoricalMarketDefinitionOriginError(
                "historical mcm.mc entries must be exact JSON objects"
            )
        raw_id = item.get("id")
        if raw_id != market_id:
            continue
        if "marketDefinition" in item:
            matches.append(item["marketDefinition"])
    if len(matches) > 1:
        raise BetfairHistoricalMarketDefinitionOriginError(
            "one historical record contains duplicate marketDefinition revisions for market"
        )
    return matches[0] if matches else None


def bind_betfair_historical_market_definition_origin(
    *,
    witness: HistoricalProviderOriginWitness,
    raw_bytes: bytes,
    market_id: str,
    cutoff_pt_ms: int,
    package_tier: HistoricalPackageTier,
    representation: HistoricalRepresentation = HistoricalRepresentation.MARKET,
) -> BetfairHistoricalMarketDefinitionOrigin:
    """Bind the latest exact marketDefinition revision visible by provider ``pt`` cutoff.

    ``provider_pt_ms`` remains historical provider publication time.  Positive origin
    requires the live process-local #1345 provider capability for the exact bytes;
    the witness's real download ``retrieved_at`` remains the product acquisition time.
    """

    if type(witness) is not HistoricalProviderOriginWitness:
        raise TypeError("witness must be exact HistoricalProviderOriginWitness")
    try:
        witness.assert_authoritative()
    except Exception as exc:
        raise BetfairHistoricalMarketDefinitionOriginError(
            "historical file lacks live canonical provider-origin authority"
        ) from exc
    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes must be bytes")
    if len(raw_bytes) != witness.byte_length or hashlib.sha256(raw_bytes).hexdigest() != witness.raw_sha256:
        raise BetfairHistoricalMarketDefinitionOriginError(
            "raw bytes do not match the authoritative downloaded file"
        )
    market = _text(market_id, "market_id")
    if type(cutoff_pt_ms) is not int or cutoff_pt_ms < 0:
        raise BetfairHistoricalMarketDefinitionOriginError(
            "cutoff_pt_ms must be a non-negative exact integer"
        )
    if type(package_tier) is not HistoricalPackageTier:
        raise TypeError("package_tier must be exact HistoricalPackageTier")
    if type(representation) is not HistoricalRepresentation:
        raise TypeError("representation must be exact HistoricalRepresentation")

    source = BetfairHistoricalReplaySource(
        provider_file_identity=witness.provider_path,
        raw_file_sha256=witness.raw_sha256,
        entitlement_snapshot_sha256=witness.entitlement_snapshot_sha256,
        package_tier=package_tier,
        representation=representation,
        parser_revision=_PARSER_REVISION,
        compression=HistoricalCompression.BZ2,
    )
    window = replay_betfair_historical_until(
        raw_bytes,
        source,
        cutoff_pt_ms=cutoff_pt_ms,
    )
    selected: tuple[BetfairHistoricalReplayRecord, object] | None = None
    for record in window.records:
        definition = _definition_in_record(record, market_id=market)
        if definition is not None:
            selected = (record, definition)
    if selected is None:
        raise BetfairHistoricalMarketDefinitionOriginError(
            "no marketDefinition revision for market is visible by provider cutoff"
        )

    record, definition = selected
    canonical_definition, definition_sha = _canonical_market_definition(definition)
    return BetfairHistoricalMarketDefinitionOrigin(
        provider_origin_witness_sha256=witness.witness_sha256,
        transport_contract_sha256=witness.transport_contract_sha256,
        download_file_identity_sha256=witness.download_file_identity_sha256,
        provider_path=witness.provider_path,
        raw_file_sha256=witness.raw_sha256,
        entitlement_snapshot_sha256=witness.entitlement_snapshot_sha256,
        download_retrieved_at=witness.retrieved_at,
        replay_source_identity=source.source_identity,
        source_ordinal=record.source_ordinal,
        provider_pt_ms=record.provider_pt_ms,
        line_sha256=record.line_sha256,
        record_payload_sha256=record.payload_sha256,
        market_id=market,
        market_definition_sha256=definition_sha,
        market_definition_json=canonical_definition,
        package_tier=package_tier,
        representation=representation,
        _witness=witness,
        _token=_TOKEN,
    )


__all__ = [
    "BetfairHistoricalMarketDefinitionOrigin",
    "BetfairHistoricalMarketDefinitionOriginError",
    "bind_betfair_historical_market_definition_origin",
]
