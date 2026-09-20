from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .causal_collector import (
    CollectorDelta,
    StreamCheckpoint,
    canonical_event_digest,
    digest_source_payload,
)
from .domain import MarketEvent, utc_now_iso
from .event_lifecycle import (
    CatalogCheckpoint,
    CatalogEvent,
    CatalogPage,
    EventLifecycleRecord,
    EventPhase,
)
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .parlayapi_provider import ParlayApiTableTennisProvider
from .providers import CanonicalNormalizer, MarketProvider, ProviderBatch, ProviderQuote


class ProductSourceError(RuntimeError):
    """The production source cannot prove a safe causal provider boundary."""


class ProductSourceStateError(ProductSourceError):
    """Durable source state conflicts with collector/catalog truth."""


class ProductSourcePayloadError(ProductSourceError):
    """Provider evidence is insufficient or causally contradictory."""


Clock = Callable[[], str]


class ParlayApiProductSource:
    """Durable read-only ProductCollectorSource over the Parlay API adapter.

    The adapter deliberately owns no betting/execution capability. It turns one real
    provider snapshot into catalog evidence plus restart-safe CollectorDelta values,
    and durably retains the exact canonical event used by ``resolve_event``. A source
    snapshot is not retired until the next call observes that every delta assigned to
    it is present in the canonical collector checkpoint; this avoids losing a quote at
    the crash boundary between source preparation and collector persistence.
    """

    _SCHEMA = "autosport.parlay_product_source"
    _VERSION = 1
    _STREAM_EPOCH = "parlayapi-table-tennis-product-v1"
    _READ_BATCH_ITEMS = 1000
    _MAX_SNAPSHOT_ITEMS = 50_000

    def __init__(
        self,
        provider: MarketProvider,
        *,
        state_path: str | Path,
        lawful_terms_ref: str,
        retention_ref: str,
        clock: Clock = utc_now_iso,
    ) -> None:
        source_id = getattr(provider, "source_id", None)
        if type(source_id) is not str or not source_id or source_id.strip() != source_id:
            raise ValueError("provider.source_id must be a non-empty trimmed string")
        if not callable(getattr(provider, "read_batch", None)):
            raise TypeError("provider.read_batch must be callable")
        self.provider = provider
        self.source_id = source_id
        self.stream_epoch = self._STREAM_EPOCH
        self.state_path = Path(state_path)
        self.lawful_terms_ref = self._text(lawful_terms_ref, "lawful_terms_ref")
        self.retention_ref = self._text(retention_ref, "retention_ref")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.clock = clock
        self.normalizer = CanonicalNormalizer()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            atomic_write_json(self.state_path, self._empty_state())
        self._read_state()

    @staticmethod
    def _text(value: object, field: str) -> str:
        if type(value) is not str or not value or value.strip() != value:
            raise ValueError(f"{field} must be a non-empty trimmed string")
        return value

    @staticmethod
    def _instant(value: object, field: str) -> datetime:
        raw = ParlayApiProductSource._text(value, field)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProductSourcePayloadError(f"{field} must be valid ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ProductSourcePayloadError(f"{field} must be timezone-aware ISO-8601")
        return parsed.astimezone(timezone.utc)

    def _empty_state(self) -> dict[str, object]:
        return {
            "schema": self._SCHEMA,
            "schema_version": self._VERSION,
            "source_id": self.source_id,
            "stream_epoch": self.stream_epoch,
            "last_catalog_position": -1,
            "last_confirmed_delta_position": -1,
            "last_committed_quote_digests": {},
            "last_committed_dedupe_digests": {},
            "pending": None,
            "event_cache": {},
        }

    def _read_state(self) -> dict[str, object]:
        try:
            raw = strict_json_loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ProductSourceStateError("cannot verify durable product source state") from exc
        expected = {
            "schema",
            "schema_version",
            "source_id",
            "stream_epoch",
            "last_catalog_position",
            "last_confirmed_delta_position",
            "last_committed_quote_digests",
            "last_committed_dedupe_digests",
            "pending",
            "event_cache",
        }
        if (
            type(raw) is not dict
            or set(raw) != expected
            or raw.get("schema") != self._SCHEMA
            or raw.get("schema_version") != self._VERSION
            or raw.get("source_id") != self.source_id
            or raw.get("stream_epoch") != self.stream_epoch
        ):
            raise ProductSourceStateError("product source state identity/schema mismatch")
        for field in ("last_catalog_position", "last_confirmed_delta_position"):
            value = raw[field]
            if type(value) is not int or value < -1:
                raise ProductSourceStateError(f"product source state has invalid {field}")
        for field in (
            "last_committed_quote_digests",
            "last_committed_dedupe_digests",
            "event_cache",
        ):
            if type(raw[field]) is not dict:
                raise ProductSourceStateError(f"product source state has invalid {field}")
        for mapping_name in (
            "last_committed_quote_digests",
            "last_committed_dedupe_digests",
        ):
            mapping = raw[mapping_name]
            assert isinstance(mapping, dict)
            for key, digest in mapping.items():
                if type(key) is not str or not key or type(digest) is not str or not self._is_digest(digest):
                    raise ProductSourceStateError(
                        f"product source state has invalid {mapping_name} entry"
                    )
        event_cache = raw["event_cache"]
        assert isinstance(event_cache, dict)
        for delta_id, event_raw in event_cache.items():
            if type(delta_id) is not str or not delta_id:
                raise ProductSourceStateError("product source event cache has invalid delta id")
            try:
                MarketEvent.from_dict(event_raw)
            except (TypeError, ValueError) as exc:
                raise ProductSourceStateError("product source event cache is invalid") from exc
        pending = raw["pending"]
        if pending is not None:
            self._validate_pending(pending)
        return raw

    @staticmethod
    def _is_digest(value: str) -> bool:
        if len(value) != 64:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        return value == value.lower()

    def _write_state(self, raw: dict[str, object]) -> None:
        atomic_write_json(self.state_path, raw)
        self._read_state()

    def _validate_pending(self, pending: object) -> None:
        if type(pending) is not dict:
            raise ProductSourceStateError("pending product snapshot must be an object")
        expected = {
            "catalog_cursor",
            "catalog_position",
            "catalog_events",
            "quality_flags",
            "items",
            "assigned",
            "confirmed",
        }
        if set(pending) != expected:
            raise ProductSourceStateError("pending product snapshot fields mismatch")
        self._text(pending["catalog_cursor"], "pending.catalog_cursor")
        if type(pending["catalog_position"]) is not int or pending["catalog_position"] < 0:
            raise ProductSourceStateError("pending catalog position must be non-negative")
        if type(pending["assigned"]) is not bool or type(pending["confirmed"]) is not bool:
            raise ProductSourceStateError("pending assignment/confirmation flags must be booleans")
        if type(pending["catalog_events"]) is not list:
            raise ProductSourceStateError("pending catalog_events must be a list")
        for event_raw in pending["catalog_events"]:
            try:
                CatalogEvent.from_dict(event_raw)
            except (TypeError, ValueError) as exc:
                raise ProductSourceStateError("pending catalog event is invalid") from exc
        if type(pending["quality_flags"]) is not list or any(
            type(flag) is not str or not flag or flag.strip() != flag
            for flag in pending["quality_flags"]
        ):
            raise ProductSourceStateError("pending quality_flags are invalid")
        if len(set(pending["quality_flags"])) != len(pending["quality_flags"]):
            raise ProductSourceStateError("pending quality_flags contain duplicates")
        if type(pending["items"]) is not list:
            raise ProductSourceStateError("pending items must be a list")
        for item in pending["items"]:
            if type(item) is not dict or set(item) != {
                "event",
                "quote_key",
                "dedupe_key",
                "canonical_digest",
                "source_payload_digest",
                "delta",
            }:
                raise ProductSourceStateError("pending item fields mismatch")
            try:
                event = MarketEvent.from_dict(item["event"])
            except (TypeError, ValueError) as exc:
                raise ProductSourceStateError("pending canonical event is invalid") from exc
            if item["quote_key"] != event.quote_key or item["dedupe_key"] != event.dedupe_key:
                raise ProductSourceStateError("pending event identity cache mismatch")
            if item["canonical_digest"] != canonical_event_digest(event):
                raise ProductSourceStateError("pending canonical event digest mismatch")
            if type(item["source_payload_digest"]) is not str or not self._is_digest(item["source_payload_digest"]):
                raise ProductSourceStateError("pending source payload digest is invalid")
            if pending["assigned"]:
                try:
                    delta = CollectorDelta.from_dict(item["delta"])
                except (TypeError, ValueError) as exc:
                    raise ProductSourceStateError("pending collector delta is invalid") from exc
                if (
                    delta.source_id != self.source_id
                    or delta.stream_epoch != self.stream_epoch
                    or delta.event_dedupe_key != event.dedupe_key
                    or delta.event_id != event.event_id
                    or delta.canonical_event_digest != item["canonical_digest"]
                    or delta.source_payload_digest != item["source_payload_digest"]
                ):
                    raise ProductSourceStateError("pending delta is not bound to canonical event")
            elif item["delta"] is not None:
                raise ProductSourceStateError("unassigned pending item cannot contain a delta")
        if pending["confirmed"] and not pending["assigned"] and pending["items"]:
            raise ProductSourceStateError("non-empty pending snapshot cannot confirm before assignment")

    @staticmethod
    def _quote_payload_bytes(quote: ProviderQuote) -> bytes:
        payload = {
            "provider_event_id": quote.provider_event_id,
            "provider_market_id": quote.provider_market_id,
            "provider_selection_id": quote.provider_selection_id,
            "decimal_odds": str(quote.decimal_odds),
            "observed_ts": quote.observed_ts,
            "sequence": quote.sequence,
            "market_type": quote.market_type.value,
            "status": quote.status,
            "source_ts": quote.source_ts,
            "score_state": quote.score_state,
            "metadata": quote.metadata,
            "sport": quote.sport,
        }
        try:
            return json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ProductSourcePayloadError("provider quote is not canonical JSON evidence") from exc

    def _read_provider_snapshot(self) -> tuple[str, tuple[ProviderQuote, ...], tuple[str, ...]]:
        quotes: list[ProviderQuote] = []
        cursor: str | None = None
        flags: set[str] = set()
        while True:
            batch = self.provider.read_batch(self._READ_BATCH_ITEMS)
            if not isinstance(batch, ProviderBatch):
                raise ProductSourcePayloadError("provider.read_batch must return ProviderBatch")
            if batch.source_id != self.source_id:
                raise ProductSourcePayloadError("provider batch source_id changed")
            if type(batch.cursor) is not str or not batch.cursor or batch.cursor.strip() != batch.cursor:
                raise ProductSourcePayloadError("provider snapshot requires a non-empty durable cursor")
            if cursor is None:
                cursor = batch.cursor
            elif batch.cursor != cursor:
                raise ProductSourcePayloadError("provider snapshot cursor changed while draining one snapshot")
            quotes.extend(batch.quotes)
            if len(quotes) > self._MAX_SNAPSHOT_ITEMS:
                raise ProductSourcePayloadError("provider snapshot exceeds bounded product source capacity")
            flags.update(flag for flag in batch.quality_flags if flag != "TRUNCATED_BATCH")
            if "TRUNCATED_BATCH" not in batch.quality_flags:
                break
            if not batch.quotes:
                raise ProductSourcePayloadError("provider returned empty truncated snapshot page")
        assert cursor is not None
        return cursor, tuple(quotes), tuple(sorted(flags))

    @staticmethod
    def _scheduled_start(event: MarketEvent) -> str:
        raw = event.metadata.get("commence_time")
        if type(raw) is not str or not raw or raw.strip() != raw:
            raise ProductSourcePayloadError(
                "provider event requires timezone-aware commence_time for product lifecycle"
            )
        ParlayApiProductSource._instant(raw, "commence_time")
        return raw

    def _catalog_events(self, quotes: tuple[ProviderQuote, ...]) -> tuple[CatalogEvent, ...]:
        by_provider_event: dict[str, CatalogEvent] = {}
        for quote in quotes:
            event = self.normalizer.normalize(self.source_id, quote)
            if event.sport is None:
                raise ProductSourcePayloadError("provider quote requires canonical sport identity")
            scheduled = self._scheduled_start(event)
            observed = self._instant(event.observed_ts, "observed_ts")
            starts = self._instant(scheduled, "commence_time")
            phase = EventPhase.PRE_MATCH if observed < starts else EventPhase.LIVE
            candidate = CatalogEvent(
                source_id=self.source_id,
                sport=event.sport,
                event_id=quote.provider_event_id,
                phase=phase,
                available_at=event.observed_ts,
                scheduled_start_at=scheduled,
            )
            candidate.validate()
            previous = by_provider_event.get(quote.provider_event_id)
            if previous is not None and previous != candidate:
                raise ProductSourcePayloadError(
                    "provider snapshot contradicts event lifecycle metadata within one event"
                )
            by_provider_event[quote.provider_event_id] = candidate
        return tuple(by_provider_event[key] for key in sorted(by_provider_event))

    def _new_pending(self, checkpoint: CatalogCheckpoint | None) -> dict[str, object]:
        state = self._read_state()
        last_catalog_position = state["last_catalog_position"]
        assert isinstance(last_catalog_position, int)
        if checkpoint is None:
            if last_catalog_position != -1:
                raise ProductSourceStateError("catalog checkpoint rollback detected")
            position = 0
        else:
            if (
                checkpoint.source_id != self.source_id
                or checkpoint.stream_epoch != self.stream_epoch
                or checkpoint.position != last_catalog_position
            ):
                raise ProductSourceStateError("catalog checkpoint conflicts with product source state")
            position = checkpoint.position + 1

        cursor, quotes, quality_flags = self._read_provider_snapshot()
        catalog_events = self._catalog_events(quotes)
        committed_quote_digests = state["last_committed_quote_digests"]
        committed_dedupe_digests = state["last_committed_dedupe_digests"]
        assert isinstance(committed_quote_digests, dict)
        assert isinstance(committed_dedupe_digests, dict)
        items: list[dict[str, object]] = []
        snapshot_quotes: dict[str, str] = {}
        snapshot_dedupes: dict[str, str] = {}
        for quote in quotes:
            event = self.normalizer.normalize(self.source_id, quote)
            canonical_digest = canonical_event_digest(event)
            quote_key = event.quote_key
            dedupe_key = event.dedupe_key
            existing_snapshot = snapshot_quotes.get(quote_key)
            if existing_snapshot is not None:
                if existing_snapshot != canonical_digest:
                    raise ProductSourcePayloadError(
                        "provider snapshot contains conflicting values for one quote identity"
                    )
                continue
            snapshot_quotes[quote_key] = canonical_digest
            existing_dedupe = snapshot_dedupes.get(dedupe_key)
            if existing_dedupe is not None and existing_dedupe != canonical_digest:
                raise ProductSourcePayloadError(
                    "provider snapshot reuses one causal dedupe identity for conflicting values"
                )
            snapshot_dedupes[dedupe_key] = canonical_digest
            committed_dedupe = committed_dedupe_digests.get(dedupe_key)
            if committed_dedupe is not None and committed_dedupe != canonical_digest:
                raise ProductSourcePayloadError(
                    "provider changed canonical quote without advancing its causal identity"
                )
            if committed_quote_digests.get(quote_key) == canonical_digest:
                continue
            items.append(
                {
                    "event": event.to_dict(),
                    "quote_key": quote_key,
                    "dedupe_key": dedupe_key,
                    "canonical_digest": canonical_digest,
                    "source_payload_digest": digest_source_payload(
                        self._quote_payload_bytes(quote)
                    ),
                    "delta": None,
                }
            )
        pending: dict[str, object] = {
            "catalog_cursor": cursor,
            "catalog_position": position,
            "catalog_events": [event.to_dict() for event in catalog_events],
            "quality_flags": list(quality_flags),
            "items": items,
            "assigned": False,
            "confirmed": False,
        }
        state["pending"] = pending
        self._write_state(state)
        return pending

    def _pending_page(self, pending: dict[str, object]) -> CatalogPage:
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor=self._text(pending["catalog_cursor"], "pending.catalog_cursor"),
            position=int(pending["catalog_position"]),
            events=tuple(CatalogEvent.from_dict(raw) for raw in pending["catalog_events"]),
        )

    def fetch_catalog_page(self, checkpoint: CatalogCheckpoint | None) -> CatalogPage:
        state = self._read_state()
        pending = state["pending"]
        if pending is not None:
            assert isinstance(pending, dict)
            pending_position = int(pending["catalog_position"])
            if pending["confirmed"]:
                if checkpoint is None or checkpoint.position != pending_position:
                    raise ProductSourceStateError(
                        "confirmed source snapshot is absent from durable catalog checkpoint"
                    )
                state["pending"] = None
                self._write_state(state)
                return self._pending_page(self._new_pending(checkpoint))
            if checkpoint is not None and checkpoint.position not in {
                pending_position - 1,
                pending_position,
            }:
                raise ProductSourceStateError("catalog/source pending position mismatch")
            if checkpoint is None and pending_position != 0:
                raise ProductSourceStateError("catalog checkpoint rollback detected")
            return self._pending_page(pending)
        return self._pending_page(self._new_pending(checkpoint))

    def _checkpoint_position(self, checkpoint: StreamCheckpoint | None) -> int:
        state = self._read_state()
        last_confirmed = state["last_confirmed_delta_position"]
        assert isinstance(last_confirmed, int)
        if checkpoint is None:
            if last_confirmed != -1:
                raise ProductSourceStateError("collector checkpoint rollback detected")
            return -1
        if checkpoint.source_id != self.source_id or checkpoint.stream_epoch != self.stream_epoch:
            raise ProductSourceStateError("collector checkpoint identity mismatch")
        if checkpoint.last_position < last_confirmed:
            raise ProductSourceStateError("collector checkpoint rollback detected")
        return checkpoint.last_position

    def _assign_pending(self, state: dict[str, object], checkpoint_position: int) -> None:
        pending = state["pending"]
        assert isinstance(pending, dict)
        if pending["assigned"]:
            return
        last_confirmed = state["last_confirmed_delta_position"]
        assert isinstance(last_confirmed, int)
        if checkpoint_position != last_confirmed:
            raise ProductSourceStateError(
                "collector advanced beyond product source state before snapshot assignment"
            )
        items = pending["items"]
        assert isinstance(items, list)
        assigned_at = self.clock()
        self._instant(assigned_at, "collector_received_at")
        event_cache = state["event_cache"]
        assert isinstance(event_cache, dict)
        for offset, item in enumerate(items, start=1):
            assert isinstance(item, dict)
            event = MarketEvent.from_dict(item["event"])
            if self._instant(event.observed_ts, "source_observed_at") > self._instant(
                assigned_at, "collector_received_at"
            ):
                raise ProductSourcePayloadError(
                    "provider observation cannot be after collector receipt time"
                )
            position = checkpoint_position + offset
            delta_seed = "|".join(
                (
                    self.source_id,
                    self.stream_epoch,
                    str(pending["catalog_cursor"]),
                    str(position),
                    event.dedupe_key,
                    str(item["canonical_digest"]),
                )
            )
            delta_id = "parlay-product:" + hashlib.sha256(
                delta_seed.encode("utf-8")
            ).hexdigest()
            delta = CollectorDelta(
                schema_version=1,
                delta_id=delta_id,
                source_id=self.source_id,
                lawful_terms_ref=self.lawful_terms_ref,
                retention_ref=self.retention_ref,
                stream_epoch=self.stream_epoch,
                source_cursor=str(pending["catalog_cursor"]),
                cursor_position=position,
                event_dedupe_key=event.dedupe_key,
                event_id=event.event_id,
                source_payload_digest=str(item["source_payload_digest"]),
                canonical_event_digest=str(item["canonical_digest"]),
                source_observed_at=event.observed_ts,
                collector_received_at=assigned_at,
                collector_committed_at=assigned_at,
                desktop_available_at=assigned_at,
                quality_flags=tuple(pending["quality_flags"]),
            )
            delta.validate()
            existing = event_cache.get(delta_id)
            if existing is not None and existing != event.to_dict():
                raise ProductSourceStateError("event cache conflicts for deterministic delta id")
            event_cache[delta_id] = event.to_dict()
            item["delta"] = delta.to_dict()
        pending["assigned"] = True
        if not items:
            pending["confirmed"] = True
            state["last_catalog_position"] = pending["catalog_position"]
        self._write_state(state)

    def _confirm_pending(self, state: dict[str, object]) -> None:
        pending = state["pending"]
        assert isinstance(pending, dict)
        items = pending["items"]
        assert isinstance(items, list)
        if items:
            final_delta = CollectorDelta.from_dict(items[-1]["delta"])
            state["last_confirmed_delta_position"] = final_delta.cursor_position
        for item in items:
            assert isinstance(item, dict)
            state["last_committed_quote_digests"][item["quote_key"]] = item[
                "canonical_digest"
            ]
            state["last_committed_dedupe_digests"][item["dedupe_key"]] = item[
                "canonical_digest"
            ]
        state["last_catalog_position"] = pending["catalog_position"]
        pending["confirmed"] = True
        self._write_state(state)

    def fetch_deltas(
        self,
        checkpoint: StreamCheckpoint | None,
        records: tuple[EventLifecycleRecord, ...],
        max_items: int,
    ) -> tuple[CollectorDelta, ...]:
        if type(max_items) is not int or max_items <= 0:
            raise ValueError("max_items must be a positive non-boolean integer")
        if type(records) is not tuple:
            raise TypeError("records must be a tuple")
        state = self._read_state()
        pending = state["pending"]
        if pending is None:
            raise ProductSourceStateError(
                "fetch_deltas requires the catalog snapshot prepared by fetch_catalog_page"
            )
        assert isinstance(pending, dict)
        checkpoint_position = self._checkpoint_position(checkpoint)
        state = self._read_state()
        pending = state["pending"]
        assert isinstance(pending, dict)
        if not pending["assigned"]:
            self._assign_pending(state, checkpoint_position)
            state = self._read_state()
            pending = state["pending"]
            assert isinstance(pending, dict)
        items = pending["items"]
        assert isinstance(items, list)
        if not items:
            if not pending["confirmed"]:
                self._confirm_pending(state)
            return ()
        deltas = tuple(CollectorDelta.from_dict(item["delta"]) for item in items)
        final_position = deltas[-1].cursor_position
        last_confirmed = state["last_confirmed_delta_position"]
        assert isinstance(last_confirmed, int)
        if checkpoint_position > final_position:
            raise ProductSourceStateError("collector checkpoint advanced beyond pending source snapshot")
        if checkpoint_position == final_position:
            self._confirm_pending(state)
            return ()
        if checkpoint_position < last_confirmed:
            raise ProductSourceStateError("collector checkpoint rollback detected")
        return tuple(
            delta
            for delta in deltas
            if delta.cursor_position > checkpoint_position
        )[:max_items]

    def resolve_event(self, delta: CollectorDelta) -> MarketEvent:
        if not isinstance(delta, CollectorDelta):
            raise TypeError("delta must be CollectorDelta")
        delta.validate()
        if delta.source_id != self.source_id or delta.stream_epoch != self.stream_epoch:
            raise ProductSourceStateError("delta identity does not belong to this product source")
        state = self._read_state()
        event_raw = state["event_cache"].get(delta.delta_id)
        if event_raw is None:
            raise ProductSourceStateError(
                "canonical event payload is absent from durable product source state"
            )
        event = MarketEvent.from_dict(event_raw)
        if (
            event.event_id != delta.event_id
            or event.dedupe_key != delta.event_dedupe_key
            or canonical_event_digest(event) != delta.canonical_event_digest
        ):
            raise ProductSourceStateError("durable event payload conflicts with collector delta")
        return event


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value or value.strip() != value:
        raise ProductSourceError(f"required product source environment variable {name} is missing")
    return value


def create_parlay_product_source() -> ParlayApiProductSource:
    """Create the supported real Parlay product source from secret-safe environment.

    Credentials are intentionally never accepted by the product CLI. The operator must
    also provide explicit lawful-terms and retention references; the product will not
    invent licence/retention authority on the operator's behalf.
    """

    api_key = _required_env("AUTOSPORT_PARLAY_API_KEY")
    state_path = _required_env("AUTOSPORT_PRODUCT_SOURCE_STATE")
    lawful_terms_ref = _required_env("AUTOSPORT_PARLAY_LAWFUL_TERMS_REF")
    retention_ref = _required_env("AUTOSPORT_PARLAY_RETENTION_REF")
    provider = ParlayApiTableTennisProvider(api_key=api_key)
    return ParlayApiProductSource(
        provider,
        state_path=state_path,
        lawful_terms_ref=lawful_terms_ref,
        retention_ref=retention_ref,
    )
