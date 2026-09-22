from __future__ import annotations

import hashlib
import json
import os
import uuid
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
from .monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .json_integrity import strict_json_loads
from .parlay_sport_provider import ParlayApiSportProvider, _canonical_sport_key
from .parlayapi_provider import ParlayApiTableTennisProvider
from .providers import (
    CanonicalNormalizer,
    MarketProvider,
    ProviderBatch,
    ProviderQuote,
    _validate_sport,
)
from .workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


class ProductSourceError(RuntimeError):
    """The production source cannot prove a safe causal provider boundary."""


class ProductSourceStateError(ProductSourceError):
    """Durable source state conflicts with collector/catalog truth."""


class ProductSourcePayloadError(ProductSourceError):
    """Provider evidence is insufficient or causally contradictory."""


Clock = Callable[[], str]


class ParlayApiProductSource:
    """Restart-safe, read-only ProductCollectorSource over the Parlay API adapter.

    One provider acquisition becomes a durable pending snapshot.  That snapshot is
    replayed until both canonical downstream checkpoints prove the exact page/deltas
    that were persisted.  Positions alone never acknowledge source work: catalog
    cursor+page digest and collector cursor+delta id are checked exactly.
    """

    _SCHEMA = "autosport.parlay_product_source"
    _VERSION = 3
    _STREAM_EPOCH = "parlayapi-table-tennis-product-v1"  # durable legacy identity
    _SOURCE_PREFIX = "parlayapi:"
    _READ_BATCH_ITEMS = 1000
    _MAX_SNAPSHOT_ITEMS = 50_000
    _STATE_FIELDS = {
        "schema",
        "schema_version",
        "source_id",
        "stream_epoch",
        "workspace_instance_id",
        "generation",
        "authority_tx_id",
        "last_catalog_position",
        "last_catalog_cursor",
        "last_catalog_page_sha256",
        "last_confirmed_delta_position",
        "last_confirmed_delta_cursor",
        "last_confirmed_delta_id",
        "last_committed_quote_digests",
        "last_committed_dedupe_digests",
        "pending",
        "event_cache",
        "state_sha256",
    }

    def __init__(
        self,
        provider: MarketProvider,
        *,
        workspace: str | Path,
        lawful_terms_ref: str,
        retention_ref: str,
        authority_root: str | Path | None = None,
        clock: Clock = utc_now_iso,
    ) -> None:
        source_id = getattr(provider, "source_id", None)
        if type(source_id) is not str or not source_id or source_id.strip() != source_id:
            raise ValueError("provider.source_id must be a non-empty trimmed string")
        if not callable(getattr(provider, "read_batch", None)):
            raise TypeError("provider.read_batch must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        try:
            workspace_path = Path(workspace).expanduser().resolve(strict=False)
        except (TypeError, ValueError, OSError, RuntimeError) as exc:
            raise ProductSourceStateError("product source workspace cannot be resolved") from exc
        if not workspace_path.is_absolute():
            raise ProductSourceStateError("product source workspace must be absolute")
        self.provider = provider
        self.source_id = source_id
        self.sport_key = self._sport_from_source_id(source_id)
        self.stream_epoch = self._stream_epoch_for_sport(self.sport_key)
        self.workspace = workspace_path
        self.lawful_terms_ref = self._text(lawful_terms_ref, "lawful_terms_ref")
        self.retention_ref = self._text(retention_ref, "retention_ref")
        self.clock = clock
        self.normalizer = CanonicalNormalizer()
        try:
            self._authority = MonotonicWorkspaceAuthority(
                workspace=self.workspace,
                domain="autosport.parlay_product_source.v3",
                key=f"{self.source_id}|{self.stream_epoch}",
                authority_root=authority_root,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProductSourceStateError(
                "cannot bind product source to canonical workspace authority"
            ) from exc
        self.workspace_instance_id = self._authority.workspace_instance_id
        source_namespace = hashlib.sha256(
            f"{self.source_id}\0{self.stream_epoch}".encode("utf-8")
        ).hexdigest()
        self.state_dir = (
            self.workspace / ".autosport" / "product-sources" / source_namespace
        )
        self.state_path = self.state_dir / "state.json"
        self._authority_binding_sha256 = hashlib.sha256(
            self._canonical_json(
                {
                    "schema": self._SCHEMA,
                    "schema_version": self._VERSION,
                    "workspace_instance_id": self.workspace_instance_id,
                    "source_id": self.source_id,
                    "stream_epoch": self.stream_epoch,
                    "state_path": self.state_path.relative_to(self.workspace).as_posix(),
                }
            ).encode("utf-8")
        ).hexdigest()
        self._initialize_state()
        self._read_state()

    @classmethod
    def _sport_from_source_id(cls, source_id: str) -> str:
        if not source_id.startswith(cls._SOURCE_PREFIX):
            raise ValueError("provider.source_id must use parlayapi:<sport_key> identity")
        raw = source_id[len(cls._SOURCE_PREFIX) :]
        try:
            sport_key = _canonical_sport_key(raw)
            _validate_sport(sport_key)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "provider.source_id must contain one canonical Parlay sport identity"
            ) from exc
        if source_id != f"{cls._SOURCE_PREFIX}{sport_key}":
            raise ValueError("provider.source_id sport identity is not canonical")
        return sport_key

    @classmethod
    def _stream_epoch_for_sport(cls, sport_key: str) -> str:
        # Existing table-tennis workspaces must reopen with their exact durable epoch.
        if sport_key == "table_tennis":
            return cls._STREAM_EPOCH
        return f"parlayapi-{sport_key}-product-v1"

    @staticmethod
    def _text(value: object, field: str) -> str:
        if type(value) is not str or not value or value.strip() != value:
            raise ValueError(f"{field} must be a non-empty trimmed string")
        return value

    @classmethod
    def _instant(cls, value: object, field: str) -> datetime:
        raw = cls._text(value, field)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProductSourcePayloadError(f"{field} must be valid ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ProductSourcePayloadError(f"{field} must be timezone-aware ISO-8601")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _is_digest(value: object) -> bool:
        if type(value) is not str or len(value) != 64:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        return value == value.lower()

    @staticmethod
    def _is_tx_id(value: object) -> bool:
        if type(value) is not str or len(value) != 32:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        return value == value.lower()

    @staticmethod
    def _canonical_json(value: object) -> str:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ProductSourceStateError("product source state is not canonical JSON") from exc

    @classmethod
    def _state_digest(cls, raw: dict[str, object]) -> str:
        bare = {key: value for key, value in raw.items() if key != "state_sha256"}
        return hashlib.sha256(cls._canonical_json(bare).encode("utf-8")).hexdigest()

    @classmethod
    def _seal_state(cls, raw: dict[str, object]) -> dict[str, object]:
        sealed = dict(raw)
        sealed["state_sha256"] = cls._state_digest(sealed)
        return sealed

    def _empty_state(self) -> dict[str, object]:
        return {
            "schema": self._SCHEMA,
            "schema_version": self._VERSION,
            "source_id": self.source_id,
            "stream_epoch": self.stream_epoch,
            "workspace_instance_id": self.workspace_instance_id,
            "generation": 0,
            "authority_tx_id": uuid.uuid4().hex,
            "last_catalog_position": -1,
            "last_catalog_cursor": None,
            "last_catalog_page_sha256": None,
            "last_confirmed_delta_position": -1,
            "last_confirmed_delta_cursor": None,
            "last_confirmed_delta_id": None,
            "last_committed_quote_digests": {},
            "last_committed_dedupe_digests": {},
            "pending": None,
            "event_cache": {},
        }

    def _read_state_unlocked(self) -> dict[str, object]:
        try:
            raw = strict_json_loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ProductSourceStateError("cannot verify durable product source state") from exc
        if (
            type(raw) is not dict
            or set(raw) != self._STATE_FIELDS
            or raw.get("schema") != self._SCHEMA
            or raw.get("schema_version") != self._VERSION
            or raw.get("source_id") != self.source_id
            or raw.get("stream_epoch") != self.stream_epoch
            or raw.get("workspace_instance_id") != self.workspace_instance_id
            or type(raw.get("generation")) is not int
            or raw.get("generation") < 0
            or not self._is_tx_id(raw.get("authority_tx_id"))
            or not self._is_digest(raw.get("state_sha256"))
            or raw.get("state_sha256") != self._state_digest(raw)
        ):
            raise ProductSourceStateError("product source state identity/schema/digest mismatch")

        catalog_position = raw["last_catalog_position"]
        delta_position = raw["last_confirmed_delta_position"]
        if type(catalog_position) is not int or catalog_position < -1:
            raise ProductSourceStateError("invalid last_catalog_position")
        if type(delta_position) is not int or delta_position < -1:
            raise ProductSourceStateError("invalid last_confirmed_delta_position")
        self._validate_checkpoint_pair(
            position=catalog_position,
            cursor=raw["last_catalog_cursor"],
            identity=raw["last_catalog_page_sha256"],
            digest_identity=True,
            label="catalog",
        )
        self._validate_checkpoint_pair(
            position=delta_position,
            cursor=raw["last_confirmed_delta_cursor"],
            identity=raw["last_confirmed_delta_id"],
            digest_identity=False,
            label="collector",
        )

        for name in (
            "last_committed_quote_digests",
            "last_committed_dedupe_digests",
            "event_cache",
        ):
            if type(raw[name]) is not dict:
                raise ProductSourceStateError(f"invalid {name}")
        for name in ("last_committed_quote_digests", "last_committed_dedupe_digests"):
            mapping = raw[name]
            assert isinstance(mapping, dict)
            for key, digest in mapping.items():
                if type(key) is not str or not key or not self._is_digest(digest):
                    raise ProductSourceStateError(f"invalid {name} entry")
        event_cache = raw["event_cache"]
        assert isinstance(event_cache, dict)
        for delta_id, event_raw in event_cache.items():
            if type(delta_id) is not str or not delta_id:
                raise ProductSourceStateError("invalid product source event cache key")
            try:
                MarketEvent.from_dict(event_raw)
            except (TypeError, ValueError) as exc:
                raise ProductSourceStateError("invalid product source event cache") from exc

        pending = raw["pending"]
        if pending is not None:
            self._validate_pending(pending)
            assert isinstance(pending, dict)
            page = self._pending_page(pending)
            if pending["confirmed"]:
                if (
                    catalog_position != page.position
                    or raw["last_catalog_cursor"] != page.cursor
                    or raw["last_catalog_page_sha256"] != page.digest
                ):
                    raise ProductSourceStateError(
                        "confirmed pending page conflicts with durable catalog acknowledgement"
                    )
            elif page.position != catalog_position + 1:
                raise ProductSourceStateError("pending catalog position is not contiguous")
            if pending["confirmed"] and pending["items"]:
                final = CollectorDelta.from_dict(pending["items"][-1]["delta"])
                if (
                    delta_position != final.cursor_position
                    or raw["last_confirmed_delta_cursor"] != final.source_cursor
                    or raw["last_confirmed_delta_id"] != final.delta_id
                ):
                    raise ProductSourceStateError(
                        "confirmed pending deltas conflict with durable collector acknowledgement"
                    )
        return raw

    def _validate_checkpoint_pair(
        self,
        *,
        position: int,
        cursor: object,
        identity: object,
        digest_identity: bool,
        label: str,
    ) -> None:
        if position == -1:
            if cursor is not None or identity is not None:
                raise ProductSourceStateError(f"empty {label} checkpoint has evidence")
            return
        try:
            self._text(cursor, f"{label}.cursor")
            self._text(identity, f"{label}.identity")
        except ValueError as exc:
            raise ProductSourceStateError(f"incomplete {label} checkpoint") from exc
        if digest_identity and not self._is_digest(identity):
            raise ProductSourceStateError("catalog checkpoint digest is invalid")

    def _recover_authority_locked(self, raw: dict[str, object]) -> None:
        observed = raw["state_sha256"]
        tx_id = raw["authority_tx_id"]
        assert isinstance(observed, str)
        assert isinstance(tx_id, str)
        try:
            recovery = self._authority.recover(
                observed_state_sha256=observed,
                tx_id=tx_id,
                semantic_binding_sha256=self._authority_binding_sha256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProductSourceStateError(
                "product source state is stale, rolled back, or outside workspace authority"
            ) from exc
        if recovery.committed_state_sha256 != observed:
            raise ProductSourceStateError(
                "product source authority does not match durable state"
            )

    def _publish_state_locked(
        self,
        sealed: dict[str, object],
        *,
        observed_state_sha256: str | None,
    ) -> None:
        intended = sealed["state_sha256"]
        tx_id = sealed["authority_tx_id"]
        assert isinstance(intended, str)
        assert isinstance(tx_id, str)
        try:
            prepared = self._authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed_state_sha256,
                intended_state_sha256=intended,
                semantic_binding_sha256=self._authority_binding_sha256,
            )
            if prepared.intended_state_sha256 != intended or prepared.tx_id != tx_id:
                raise ProductSourceStateError(
                    "product source authority prepared a different state transition"
                )
            atomic_write_json(self.state_path, sealed)
            verified = self._read_state_unlocked()
            if verified["state_sha256"] != intended or verified["authority_tx_id"] != tx_id:
                raise ProductSourceStateError(
                    "product source state failed exact durable re-read"
                )
            self._authority.commit(
                tx_id=tx_id,
                observed_state_sha256=intended,
                semantic_binding_sha256=self._authority_binding_sha256,
            )
        except ProductSourceStateError:
            raise
        except (MonotonicWorkspaceAuthorityError, OSError) as exc:
            raise ProductSourceStateError(
                "cannot publish product source state under workspace authority"
            ) from exc

    def _initialize_state(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with WorkspaceEconomicLock(self.state_dir):
                if self.state_path.exists():
                    raw = self._read_state_unlocked()
                    self._recover_authority_locked(raw)
                    return
                try:
                    recovery = self._authority.recover(observed_state_sha256=None)
                except MonotonicWorkspaceAuthorityError as exc:
                    raise ProductSourceStateError(
                        "product source state is missing but workspace authority is not pristine"
                    ) from exc
                if recovery.committed_state_sha256 is not None:
                    raise ProductSourceStateError(
                        "product source state deletion/rollback detected"
                    )
                initial = self._seal_state(self._empty_state())
                self._publish_state_locked(initial, observed_state_sha256=None)
        except ProductSourceStateError:
            raise
        except (WorkspaceEconomicLockError, OSError) as exc:
            raise ProductSourceStateError(
                "cannot initialize canonical product source journal"
            ) from exc

    def _read_state(self) -> dict[str, object]:
        try:
            with WorkspaceEconomicLock(self.state_dir):
                raw = self._read_state_unlocked()
                self._recover_authority_locked(raw)
                return raw
        except ProductSourceStateError:
            raise
        except WorkspaceEconomicLockError as exc:
            raise ProductSourceStateError(
                "cannot acquire canonical product source journal lock"
            ) from exc

    def _write_state(self, raw: dict[str, object]) -> None:
        expected = raw.get("state_sha256")
        if not self._is_digest(expected):
            raise ProductSourceStateError(
                "product source write is missing an exact previous state digest"
            )
        assert isinstance(expected, str)
        try:
            with WorkspaceEconomicLock(self.state_dir):
                current = self._read_state_unlocked()
                self._recover_authority_locked(current)
                if current["state_sha256"] != expected:
                    raise ProductSourceStateError(
                        "stale product source writer generation detected"
                    )
                candidate = dict(raw)
                candidate["generation"] = int(current["generation"]) + 1
                candidate["authority_tx_id"] = uuid.uuid4().hex
                sealed = self._seal_state(candidate)
                self._publish_state_locked(
                    sealed,
                    observed_state_sha256=expected,
                )
        except ProductSourceStateError:
            raise
        except WorkspaceEconomicLockError as exc:
            raise ProductSourceStateError(
                "cannot acquire canonical product source journal lock"
            ) from exc

    def _validate_pending(self, pending: object) -> None:
        if type(pending) is not dict or set(pending) != {
            "catalog_cursor",
            "catalog_position",
            "catalog_events",
            "quality_flags",
            "items",
            "assigned",
            "confirmed",
        }:
            raise ProductSourceStateError("pending product snapshot fields mismatch")
        try:
            self._text(pending["catalog_cursor"], "pending.catalog_cursor")
        except ValueError as exc:
            raise ProductSourceStateError("pending catalog cursor is invalid") from exc
        if type(pending["catalog_position"]) is not int or pending["catalog_position"] < 0:
            raise ProductSourceStateError("pending catalog position must be non-negative")
        if type(pending["assigned"]) is not bool or type(pending["confirmed"]) is not bool:
            raise ProductSourceStateError("pending assignment flags must be booleans")
        if pending["confirmed"] and not pending["assigned"]:
            raise ProductSourceStateError("pending snapshot cannot confirm before assignment")
        if type(pending["catalog_events"]) is not list:
            raise ProductSourceStateError("pending catalog_events must be a list")
        for event_raw in pending["catalog_events"]:
            try:
                CatalogEvent.from_dict(event_raw)
            except (TypeError, ValueError) as exc:
                raise ProductSourceStateError("pending catalog event is invalid") from exc
        flags = pending["quality_flags"]
        if (
            type(flags) is not list
            or any(type(flag) is not str or not flag or flag.strip() != flag for flag in flags)
            or len(set(flags)) != len(flags)
        ):
            raise ProductSourceStateError("pending quality_flags are invalid")
        items = pending["items"]
        if type(items) is not list:
            raise ProductSourceStateError("pending items must be a list")
        prior_position: int | None = None
        for item in items:
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
            if not self._is_digest(item["source_payload_digest"]):
                raise ProductSourceStateError("pending source payload digest is invalid")
            if pending["assigned"]:
                try:
                    delta = CollectorDelta.from_dict(item["delta"])
                except (TypeError, ValueError) as exc:
                    raise ProductSourceStateError("pending collector delta is invalid") from exc
                if (
                    delta.source_id != self.source_id
                    or delta.stream_epoch != self.stream_epoch
                    or delta.source_cursor != pending["catalog_cursor"]
                    or delta.event_dedupe_key != event.dedupe_key
                    or delta.event_id != event.event_id
                    or delta.canonical_event_digest != item["canonical_digest"]
                    or delta.source_payload_digest != item["source_payload_digest"]
                    or delta.quality_flags != tuple(flags)
                ):
                    raise ProductSourceStateError("pending delta is not bound to source evidence")
                if prior_position is not None and delta.cursor_position != prior_position + 1:
                    raise ProductSourceStateError("pending delta positions are not contiguous")
                prior_position = delta.cursor_position
            elif item["delta"] is not None:
                raise ProductSourceStateError("unassigned pending item cannot contain a delta")

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
            try:
                batch_cursor = self._text(batch.cursor, "provider cursor")
            except ValueError as exc:
                raise ProductSourcePayloadError(
                    "provider snapshot requires a non-empty durable cursor"
                ) from exc
            if cursor is None:
                cursor = batch_cursor
            elif batch_cursor != cursor:
                raise ProductSourcePayloadError(
                    "provider snapshot cursor changed while draining one snapshot"
                )
            quotes.extend(batch.quotes)
            if len(quotes) > self._MAX_SNAPSHOT_ITEMS:
                raise ProductSourcePayloadError("provider snapshot exceeds bounded source capacity")
            flags.update(flag for flag in batch.quality_flags if flag != "TRUNCATED_BATCH")
            if "TRUNCATED_BATCH" not in batch.quality_flags:
                break
            if not batch.quotes:
                raise ProductSourcePayloadError("provider returned empty truncated snapshot page")
        assert cursor is not None
        return cursor, tuple(quotes), tuple(sorted(flags))

    @classmethod
    def _scheduled_start(cls, event: MarketEvent) -> str | None:
        if "commence_time" not in event.metadata:
            raise ProductSourcePayloadError("provider event requires commence_time field")
        raw = event.metadata["commence_time"]
        if raw is None:
            return None
        try:
            value = cls._text(raw, "commence_time")
            cls._instant(value, "commence_time")
        except ValueError as exc:
            raise ProductSourcePayloadError(
                "provider event requires timezone-aware commence_time when reported"
            ) from exc
        return value

    def _catalog_events(self, quotes: tuple[ProviderQuote, ...]) -> tuple[CatalogEvent, ...]:
        values: dict[str, CatalogEvent] = {}
        scheduled_by_event: dict[str, str | None] = {}
        for quote in quotes:
            event = self.normalizer.normalize(self.source_id, quote)
            if event.sport is None:
                raise ProductSourcePayloadError("provider quote requires canonical sport identity")
            if event.sport != self.sport_key:
                raise ProductSourcePayloadError(
                    "provider quote sport conflicts with product source identity"
                )
            scheduled = self._scheduled_start(event)
            event_id = quote.provider_event_id
            if event_id in scheduled_by_event and scheduled_by_event[event_id] != scheduled:
                raise ProductSourcePayloadError(
                    "provider snapshot contradicts commence_time evidence within one event"
                )
            scheduled_by_event[event_id] = scheduled
            if scheduled is None:
                continue
            phase = (
                EventPhase.PRE_MATCH
                if self._instant(event.observed_ts, "observed_ts")
                < self._instant(scheduled, "commence_time")
                else EventPhase.LIVE
            )
            candidate = CatalogEvent(
                source_id=self.source_id,
                sport=event.sport,
                event_id=event_id,
                phase=phase,
                available_at=event.observed_ts,
                scheduled_start_at=scheduled,
            )
            candidate.validate()
            previous = values.get(event_id)
            if previous is not None and previous != candidate:
                raise ProductSourcePayloadError(
                    "provider snapshot contradicts lifecycle metadata within one event"
                )
            values[event_id] = candidate
        return tuple(values[key] for key in sorted(values))

    def _pending_page(self, pending: dict[str, object]) -> CatalogPage:
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor=str(pending["catalog_cursor"]),
            position=int(pending["catalog_position"]),
            events=tuple(CatalogEvent.from_dict(raw) for raw in pending["catalog_events"]),
        )

    @staticmethod
    def _checkpoint_matches_page(checkpoint: CatalogCheckpoint, page: CatalogPage) -> bool:
        return (
            checkpoint.source_id == page.source_id
            and checkpoint.stream_epoch == page.stream_epoch
            and checkpoint.cursor == page.cursor
            and checkpoint.position == page.position
            and checkpoint.page_sha256 == page.digest
        )

    def _require_last_catalog_checkpoint(
        self,
        state: dict[str, object],
        checkpoint: CatalogCheckpoint | None,
    ) -> None:
        position = int(state["last_catalog_position"])
        if position == -1:
            if checkpoint is not None:
                raise ProductSourceStateError("unexpected catalog checkpoint before first source page")
            return
        if checkpoint is None:
            raise ProductSourceStateError("catalog checkpoint rollback detected")
        if (
            checkpoint.source_id != self.source_id
            or checkpoint.stream_epoch != self.stream_epoch
            or checkpoint.position != position
            or checkpoint.cursor != state["last_catalog_cursor"]
            or checkpoint.page_sha256 != state["last_catalog_page_sha256"]
        ):
            raise ProductSourceStateError(
                "catalog checkpoint conflicts with exact durable page evidence"
            )

    def _new_pending(self, checkpoint: CatalogCheckpoint | None) -> dict[str, object]:
        state = self._read_state()
        self._require_last_catalog_checkpoint(state, checkpoint)
        position = int(state["last_catalog_position"]) + 1
        cursor, quotes, quality_flags = self._read_provider_snapshot()
        catalog_events = self._catalog_events(quotes)
        committed_quotes = state["last_committed_quote_digests"]
        committed_dedupes = state["last_committed_dedupe_digests"]
        assert isinstance(committed_quotes, dict)
        assert isinstance(committed_dedupes, dict)
        seen_quotes: dict[str, str] = {}
        seen_dedupes: dict[str, str] = {}
        items: list[dict[str, object]] = []
        for quote in quotes:
            event = self.normalizer.normalize(self.source_id, quote)
            digest = canonical_event_digest(event)
            previous_quote = seen_quotes.get(event.quote_key)
            if previous_quote is not None:
                if previous_quote != digest:
                    raise ProductSourcePayloadError(
                        "provider snapshot conflicts for one quote identity"
                    )
                continue
            seen_quotes[event.quote_key] = digest
            previous_dedupe = seen_dedupes.get(event.dedupe_key)
            if previous_dedupe is not None and previous_dedupe != digest:
                raise ProductSourcePayloadError(
                    "provider snapshot reuses one causal dedupe identity"
                )
            seen_dedupes[event.dedupe_key] = digest
            committed_dedupe = committed_dedupes.get(event.dedupe_key)
            if committed_dedupe is not None and committed_dedupe != digest:
                raise ProductSourcePayloadError(
                    "provider changed canonical quote without advancing its causal identity"
                )
            if committed_quotes.get(event.quote_key) == digest:
                continue
            items.append(
                {
                    "event": event.to_dict(),
                    "quote_key": event.quote_key,
                    "dedupe_key": event.dedupe_key,
                    "canonical_digest": digest,
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

    def fetch_catalog_page(self, checkpoint: CatalogCheckpoint | None) -> CatalogPage:
        state = self._read_state()
        pending = state["pending"]
        if pending is None:
            return self._pending_page(self._new_pending(checkpoint))
        assert isinstance(pending, dict)
        page = self._pending_page(pending)
        if checkpoint is not None and checkpoint.position == page.position:
            if not self._checkpoint_matches_page(checkpoint, page):
                raise ProductSourceStateError(
                    "catalog checkpoint conflicts with pending exact page evidence"
                )
            if pending["confirmed"]:
                state["pending"] = None
                self._write_state(state)
                return self._pending_page(self._new_pending(checkpoint))
            return page
        self._require_last_catalog_checkpoint(state, checkpoint)
        if pending["confirmed"]:
            raise ProductSourceStateError(
                "confirmed source page is absent from canonical catalog checkpoint"
            )
        return page

    def _expected_collector_identity(
        self,
        state: dict[str, object],
        position: int,
    ) -> tuple[str, str] | None:
        if position == state["last_confirmed_delta_position"] and position >= 0:
            return str(state["last_confirmed_delta_cursor"]), str(
                state["last_confirmed_delta_id"]
            )
        pending = state["pending"]
        if isinstance(pending, dict) and pending["assigned"]:
            for item in pending["items"]:
                delta = CollectorDelta.from_dict(item["delta"])
                if delta.cursor_position == position:
                    return delta.source_cursor, delta.delta_id
        return None

    def _checkpoint_position(self, checkpoint: StreamCheckpoint | None) -> int:
        state = self._read_state()
        confirmed = int(state["last_confirmed_delta_position"])
        if checkpoint is None:
            if confirmed != -1:
                raise ProductSourceStateError("collector checkpoint rollback detected")
            return -1
        checkpoint.validate()
        if checkpoint.source_id != self.source_id or checkpoint.stream_epoch != self.stream_epoch:
            raise ProductSourceStateError("collector checkpoint identity mismatch")
        if checkpoint.last_position < confirmed:
            raise ProductSourceStateError("collector checkpoint rollback detected")
        expected = self._expected_collector_identity(state, checkpoint.last_position)
        if expected is None:
            raise ProductSourceStateError(
                "collector checkpoint advances beyond durable source delta evidence"
            )
        if (checkpoint.last_cursor, checkpoint.last_delta_id) != expected:
            raise ProductSourceStateError(
                "collector checkpoint conflicts with exact durable delta evidence"
            )
        return checkpoint.last_position

    def _assign_pending(self, state: dict[str, object], checkpoint_position: int) -> None:
        pending = state["pending"]
        assert isinstance(pending, dict)
        if pending["assigned"]:
            return
        confirmed = int(state["last_confirmed_delta_position"])
        if checkpoint_position != confirmed:
            raise ProductSourceStateError(
                "collector advanced beyond source state before snapshot assignment"
            )
        assigned_at = self.clock()
        self._instant(assigned_at, "collector_received_at")
        items = pending["items"]
        assert isinstance(items, list)
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
            seed = "|".join(
                (
                    self.source_id,
                    self.stream_epoch,
                    str(pending["catalog_cursor"]),
                    str(position),
                    event.dedupe_key,
                    str(item["canonical_digest"]),
                )
            )
            delta_id = "parlay-product:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()
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
            self._confirm_pending(state)
            return
        self._write_state(state)

    def _record_catalog_confirmation(
        self, state: dict[str, object], pending: dict[str, object]
    ) -> None:
        page = self._pending_page(pending)
        state["last_catalog_position"] = page.position
        state["last_catalog_cursor"] = page.cursor
        state["last_catalog_page_sha256"] = page.digest

    def _confirm_pending(self, state: dict[str, object]) -> None:
        pending = state["pending"]
        assert isinstance(pending, dict)
        if pending["confirmed"]:
            return
        items = pending["items"]
        assert isinstance(items, list)
        if items:
            final = CollectorDelta.from_dict(items[-1]["delta"])
            state["last_confirmed_delta_position"] = final.cursor_position
            state["last_confirmed_delta_cursor"] = final.source_cursor
            state["last_confirmed_delta_id"] = final.delta_id
        committed_quotes = state["last_committed_quote_digests"]
        committed_dedupes = state["last_committed_dedupe_digests"]
        assert isinstance(committed_quotes, dict)
        assert isinstance(committed_dedupes, dict)
        for item in items:
            committed_quotes[str(item["quote_key"])] = str(item["canonical_digest"])
            committed_dedupes[str(item["dedupe_key"])] = str(item["canonical_digest"])
        self._record_catalog_confirmation(state, pending)
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
        if state["pending"] is None:
            raise ProductSourceStateError(
                "fetch_deltas requires a catalog snapshot from fetch_catalog_page"
            )
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
        confirmed = int(state["last_confirmed_delta_position"])
        if checkpoint_position > final_position:
            raise ProductSourceStateError(
                "collector checkpoint advanced beyond pending source snapshot"
            )
        if checkpoint_position == final_position:
            if not pending["confirmed"]:
                self._confirm_pending(state)
            return ()
        if checkpoint_position < confirmed:
            raise ProductSourceStateError("collector checkpoint rollback detected")
        return tuple(
            delta for delta in deltas if delta.cursor_position > checkpoint_position
        )[:max_items]

    def resolve_event(self, delta: CollectorDelta) -> MarketEvent:
        if not isinstance(delta, CollectorDelta):
            raise TypeError("delta must be CollectorDelta")
        delta.validate()
        if delta.source_id != self.source_id or delta.stream_epoch != self.stream_epoch:
            raise ProductSourceStateError("delta identity does not belong to this source")
        state = self._read_state()
        event_cache = state["event_cache"]
        assert isinstance(event_cache, dict)
        event_raw = event_cache.get(delta.delta_id)
        if event_raw is None:
            raise ProductSourceStateError(
                "canonical event payload is absent from durable source state"
            )
        event = MarketEvent.from_dict(event_raw)
        if (
            event.event_id != delta.event_id
            or event.dedupe_key != delta.event_dedupe_key
            or canonical_event_digest(event) != delta.canonical_event_digest
        ):
            raise ProductSourceStateError("durable event conflicts with collector delta")
        return event


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value or value.strip() != value:
        raise ProductSourceError(f"required product source environment variable {name} is missing")
    return value


def create_parlay_product_source() -> ParlayApiProductSource:
    """Construct the legacy table-tennis product source from secret-safe environment."""

    provider = ParlayApiTableTennisProvider(api_key=_required_env("AUTOSPORT_PARLAY_API_KEY"))
    return ParlayApiProductSource(
        provider,
        workspace=_required_env("AUTOSPORT_PRODUCT_WORKSPACE"),
        lawful_terms_ref=_required_env("AUTOSPORT_PARLAY_LAWFUL_TERMS_REF"),
        retention_ref=_required_env("AUTOSPORT_PARLAY_RETENTION_REF"),
    )


def create_parlay_sport_product_source(sport_key: str) -> ParlayApiProductSource:
    """Construct one authenticated sport-scoped read-only Parlay product source.

    This only composes the configured read adapter with the durable product-source
    boundary. It does not prove catalog activity, endpoint capability, entitlement,
    or any provider-write/execution authority.
    """

    canonical = _canonical_sport_key(sport_key)
    try:
        _validate_sport(canonical)
    except (TypeError, ValueError) as exc:
        raise ValueError("sport_key is not a canonical product sport identity") from exc
    provider = ParlayApiSportProvider(
        canonical,
        api_key=_required_env("AUTOSPORT_PARLAY_API_KEY"),
    )
    return ParlayApiProductSource(
        provider,
        workspace=_required_env("AUTOSPORT_PRODUCT_WORKSPACE"),
        lawful_terms_ref=_required_env("AUTOSPORT_PARLAY_LAWFUL_TERMS_REF"),
        retention_ref=_required_env("AUTOSPORT_PARLAY_RETENTION_REF"),
    )
