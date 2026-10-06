from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from itertools import islice
from pathlib import Path
from typing import Callable

from .causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    StreamCheckpoint,
    canonical_event_digest,
    digest_source_payload,
)
from .domain import MarketEvent, MarketType, utc_now_iso
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
from .parlayapi_provider import ParlayApiTableTennisProvider
from .providers import CanonicalNormalizer, MarketProvider, ProviderBatch, ProviderQuote
from .workspace_lock import (
    WorkspaceEconomicLock,
    WorkspaceEconomicLockError,
    _open_read_only_descriptor,
)


class ProductSourceError(RuntimeError):
    """The production source cannot prove a safe causal provider boundary."""


class ProductSourceStateError(ProductSourceError):
    """Durable source state conflicts with collector/catalog truth."""


class ProductSourcePayloadError(ProductSourceError):
    """Provider evidence is insufficient or causally contradictory."""


Clock = Callable[[], str]

_CANONICAL_COLLECTOR_STORE_TYPE = CollectorDeltaStore
_CANONICAL_PATH_EQ = Path.__eq__
_CANONICAL_PATH_EQ_CODE = getattr(_CANONICAL_PATH_EQ, "__code__", None)
_CANONICAL_STORE_MIGRATE_LEGACY = CollectorDeltaStore.migrate_legacy_event_payloads
_CANONICAL_STORE_EVENT_DIGEST_MAPS = CollectorDeltaStore.event_digest_maps
_CANONICAL_STORE_RESOLVE_EVENT = CollectorDeltaStore.resolve_event
_CANONICAL_STORE_SURFACE = tuple(
    (
        name,
        member,
        getattr(member, "__code__", None),
    )
    for name, member in (
        ("migrate_legacy_event_payloads", _CANONICAL_STORE_MIGRATE_LEGACY),
        ("event_digest_maps", _CANONICAL_STORE_EVENT_DIGEST_MAPS),
        ("resolve_event", _CANONICAL_STORE_RESOLVE_EVENT),
    )
)
# The captured public history methods still call these helpers through self/cls.
# Seal that transitive graph as well: an exact store instance with a shadowed _connect
# must not redirect canonical history into another SQLite authority while the public
# method identities remain unchanged.
_CANONICAL_STORE_TRANSITIVE_SURFACE = tuple(
    (
        name,
        descriptor,
        descriptor.__func__
        if isinstance(descriptor, (classmethod, staticmethod))
        else descriptor,
        getattr(
            descriptor.__func__
            if isinstance(descriptor, (classmethod, staticmethod))
            else descriptor,
            "__code__",
            None,
        ),
    )
    for name in (
        "_stat_file_identity",
        "_path_file_identity",
        "_connect",
        "_canonical_event_payload",
        "_append_event_payload_connection",
        "_bounded_identity_keys",
        "_delta_by_id",
        "_row_delta",
    )
    for descriptor in (vars(_CANONICAL_COLLECTOR_STORE_TYPE)[name],)
)


def _same_canonical_collector_store_path(observed: object, expected: Path) -> bool:
    """Compare store paths without ambient pathlib late dispatch."""

    if type(observed) is not type(expected):
        return False
    current_eq = Path.__eq__
    if (
        current_eq is not _CANONICAL_PATH_EQ
        or (
            _CANONICAL_PATH_EQ_CODE is not None
            and getattr(_CANONICAL_PATH_EQ, "__code__", None)
            is not _CANONICAL_PATH_EQ_CODE
        )
    ):
        raise ProductSourceStateError(
            "canonical collector-store path comparison dispatch was replaced"
        )
    equal = _CANONICAL_PATH_EQ(observed, expected)
    if (
        Path.__eq__ is not _CANONICAL_PATH_EQ
        or (
            _CANONICAL_PATH_EQ_CODE is not None
            and getattr(_CANONICAL_PATH_EQ, "__code__", None)
            is not _CANONICAL_PATH_EQ_CODE
        )
    ):
        raise ProductSourceStateError(
            "canonical collector-store path comparison dispatch was replaced"
        )
    return equal is True


def _canonical_collector_store_dispatch(
    store: CollectorDeltaStore,
):
    """Return exact durable-history callables without instance late dispatch."""

    if type(store) is not _CANONICAL_COLLECTOR_STORE_TYPE:
        raise ProductSourceStateError(
            "collector store is not the exact canonical durable-history authority"
        )
    class_dict = vars(_CANONICAL_COLLECTOR_STORE_TYPE)
    instance_dict = vars(store)
    for (
        name,
        expected_descriptor,
        expected_callable,
        expected_code,
    ) in _CANONICAL_STORE_TRANSITIVE_SURFACE:
        current_descriptor = class_dict.get(name)
        current_callable = (
            current_descriptor.__func__
            if isinstance(current_descriptor, (classmethod, staticmethod))
            else current_descriptor
        )
        if (
            name in instance_dict
            or current_descriptor is not expected_descriptor
            or current_callable is not expected_callable
            or (
                expected_code is not None
                and getattr(current_callable, "__code__", None) is not expected_code
            )
        ):
            raise ProductSourceStateError(
                "canonical collector-store durable-history dispatch was replaced"
            )
    for name, expected, expected_code in _CANONICAL_STORE_SURFACE:
        current = class_dict.get(name)
        if (
            current is not expected
            or (
                expected_code is not None
                and getattr(current, "__code__", None) is not expected_code
            )
        ):
            raise ProductSourceStateError(
                "canonical collector-store durable-history dispatch was replaced"
            )
    return (
        _CANONICAL_STORE_MIGRATE_LEGACY,
        _CANONICAL_STORE_EVENT_DIGEST_MAPS,
        _CANONICAL_STORE_RESOLVE_EVENT,
    )


class ParlayApiProductSource:
    """Restart-safe, read-only ProductCollectorSource over the Parlay API adapter.

    One provider acquisition becomes a durable pending snapshot.  That snapshot is
    replayed until both canonical downstream checkpoints prove the exact page/deltas
    that were persisted.  Positions alone never acknowledge source work: catalog
    cursor+page digest and collector cursor+delta id are checked exactly.
    """

    _SCHEMA = "autosport.parlay_product_source"
    _VERSION = 3
    _STREAM_EPOCH = "parlayapi-table-tennis-product-v1"
    _READ_BATCH_ITEMS = 1000
    _MAX_SNAPSHOT_ITEMS = 50_000
    _MAX_STATE_BYTES = 512 * 1024 * 1024
    _LEGACY_HISTORY_VERIFY_CHUNK = 10_000
    _LEGACY_EVENT_MIGRATION_CHUNK = 1_000
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
        self.stream_epoch = self._STREAM_EPOCH
        self.workspace = workspace_path
        self._collector_store_path = self.workspace / "collector_deltas.json"
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
        self._collector_store: CollectorDeltaStore | None = None
        self._legacy_oversized_state = False
        self._initialize_state()
        try:
            self._legacy_oversized_state = (
                self.state_path.stat().st_size > self._MAX_STATE_BYTES
            )
        except OSError as exc:
            raise ProductSourceStateError(
                "cannot verify durable product source state"
            ) from exc
        self._read_state(
            allow_oversized_legacy=self._legacy_oversized_state
        )

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
        fractional = re.search(r"\d{2}:?\d{2}:?\d{2}[.,](\d+)", raw)
        if fractional is not None:
            digits = fractional.group(1)
            if len(digits) > 6 and any(digit != "0" for digit in digits[6:]):
                raise ProductSourcePayloadError(
                    f"{field} has non-zero precision finer than microseconds"
                )
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

    @staticmethod
    def _stable_state_metadata(left: os.stat_result, right: os.stat_result) -> bool:
        return (
            left.st_mode == right.st_mode
            and left.st_size == right.st_size
            and left.st_mtime_ns == right.st_mtime_ns
            and left.st_ctime_ns == right.st_ctime_ns
        )

    def _read_state_text_bounded(
        self,
        *,
        allow_oversized_legacy: bool = False,
    ) -> tuple[str, bool]:
        path = self.state_path
        try:
            path_before = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise ProductSourceStateError(
                "cannot verify durable product source state"
            ) from exc
        oversized = path_before.st_size > self._MAX_STATE_BYTES
        if (
            not stat.S_ISREG(path_before.st_mode)
            or path_before.st_nlink != 1
            or (oversized and not allow_oversized_legacy)
        ):
            raise ProductSourceStateError(
                "durable product source state is not a bounded canonical file"
            )

        # The only exception to the current-state product cap is one upgrade read of
        # a pre-existing structurally-unbounded legacy history.  Read exactly the
        # stable observed extent (plus one byte to catch growth); parsed state is
        # accepted below only if it actually contains migratable legacy history.
        read_limit = (
            path_before.st_size if oversized else self._MAX_STATE_BYTES
        )
        try:
            descriptor = _open_read_only_descriptor(path)
        except OSError as exc:
            raise ProductSourceStateError(
                "cannot verify durable product source state"
            ) from exc
        try:
            handle = os.fdopen(descriptor, "rb", closefd=True)
        except BaseException:
            os.close(descriptor)
            raise

        with handle:
            verification_descriptor: int | None = None
            final_descriptor: int | None = None
            try:
                opened_before = os.fstat(handle.fileno())
                current = os.stat(path, follow_symlinks=False)
                verification_descriptor = _open_read_only_descriptor(path)
                verification_stat = os.fstat(verification_descriptor)
                same_file = os.path.sameopenfile(
                    handle.fileno(),
                    verification_descriptor,
                )
                if (
                    not same_file
                    or not stat.S_ISREG(opened_before.st_mode)
                    or not stat.S_ISREG(verification_stat.st_mode)
                    or opened_before.st_nlink != 1
                    or verification_stat.st_nlink != 1
                    or not self._stable_state_metadata(path_before, current)
                ):
                    raise ProductSourceStateError(
                        "durable product source state changed while validating"
                    )

                payload = handle.read(read_limit + 1)
                opened_after = os.fstat(handle.fileno())
                current_after = os.stat(path, follow_symlinks=False)
                final_descriptor = _open_read_only_descriptor(path)
                final_stat = os.fstat(final_descriptor)
                same_final_file = os.path.sameopenfile(
                    handle.fileno(),
                    final_descriptor,
                )
                if (
                    not same_final_file
                    or not stat.S_ISREG(opened_after.st_mode)
                    or not stat.S_ISREG(final_stat.st_mode)
                    or opened_after.st_nlink != 1
                    or final_stat.st_nlink != 1
                    or len(payload) > read_limit
                    or not self._stable_state_metadata(opened_before, opened_after)
                    or not self._stable_state_metadata(path_before, current_after)
                ):
                    raise ProductSourceStateError(
                        "durable product source state changed or exceeded its byte bound"
                    )
            except ProductSourceStateError:
                raise
            except OSError as exc:
                raise ProductSourceStateError(
                    "cannot verify durable product source state"
                ) from exc
            finally:
                for candidate in (final_descriptor, verification_descriptor):
                    if candidate is not None:
                        try:
                            os.close(candidate)
                        except OSError:
                            pass

        try:
            return payload.decode("utf-8"), oversized
        except UnicodeDecodeError as exc:
            raise ProductSourceStateError(
                "durable product source state is not valid UTF-8"
            ) from exc

    def _read_state_unlocked(
        self,
        *,
        allow_oversized_legacy: bool = False,
    ) -> dict[str, object]:
        try:
            state_text, oversized = self._read_state_text_bounded(
                allow_oversized_legacy=allow_oversized_legacy
            )
            raw = strict_json_loads(state_text)
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
        if oversized and not any(
            raw[name]
            for name in (
                "event_cache",
                "last_committed_quote_digests",
                "last_committed_dedupe_digests",
            )
        ):
            raise ProductSourceStateError(
                "oversized product source state has no migratable legacy history"
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
            # Rendering and the product byte ceiling are pure preconditions.  Prove
            # them before preparing the external monotonic authority so an
            # intrinsically unpublishable candidate cannot leave a prepared
            # transition that recovery would later have to unwind.
            try:
                rendered = (
                    json.dumps(
                        sealed,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                        allow_nan=False,
                    )
                    + "\n"
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeError) as exc:
                raise ProductSourceStateError(
                    "product source state cannot be serialized"
                ) from exc
            if len(rendered) > self._MAX_STATE_BYTES:
                raise ProductSourceStateError(
                    "product source state exceeds bounded checkpoint capacity"
                )

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
                    raw = self._read_state_unlocked(
                        allow_oversized_legacy=True
                    )
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

    def _read_state(
        self,
        *,
        allow_oversized_legacy: bool = False,
    ) -> dict[str, object]:
        if self._legacy_oversized_state and not allow_oversized_legacy:
            self._require_collector_store()
        try:
            with WorkspaceEconomicLock(self.state_dir):
                raw = self._read_state_unlocked(
                    allow_oversized_legacy=allow_oversized_legacy
                )
                self._recover_authority_locked(raw)
                return raw
        except ProductSourceStateError:
            raise
        except WorkspaceEconomicLockError as exc:
            raise ProductSourceStateError(
                "cannot acquire canonical product source journal lock"
            ) from exc

    def _write_state(
        self,
        raw: dict[str, object],
        *,
        allow_oversized_current: bool = False,
    ) -> None:
        expected = raw.get("state_sha256")
        if not self._is_digest(expected):
            raise ProductSourceStateError(
                "product source write is missing an exact previous state digest"
            )
        assert isinstance(expected, str)
        try:
            with WorkspaceEconomicLock(self.state_dir):
                current = self._read_state_unlocked(
                    allow_oversized_legacy=allow_oversized_current
                )
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
                self._legacy_oversized_state = False
        except ProductSourceStateError:
            raise
        except WorkspaceEconomicLockError as exc:
            raise ProductSourceStateError(
                "cannot acquire canonical product source journal lock"
            ) from exc

    def bind_collector_store(self, store: CollectorDeltaStore) -> None:
        # Historical-event migration/lookup is durable evidence authority.  A subclass
        # could override migration or digest-resolution methods while still passing an
        # isinstance check, so canonical product composition requires the exact store.
        if type(store) is not _CANONICAL_COLLECTOR_STORE_TYPE:
            raise TypeError("store must be exact canonical CollectorDeltaStore")
        if not _same_canonical_collector_store_path(
            store.path,
            self._collector_store_path,
        ):
            raise ProductSourceStateError(
                "product source collector store must be the canonical workspace store"
            )
        current = self._collector_store
        if current is not None and current is not store:
            raise ProductSourceStateError(
                "product source collector store authority cannot be replaced"
            )
        if current is store:
            self._migrate_legacy_history_to_collector_store()
            return

        # Treat first binding as an in-memory authority transition: migration must
        # succeed before this source retains the store.  A failed/backpressured
        # upgrade may be retried by a fresh canonical composition without inheriting
        # a half-established store authority.
        self._collector_store = store
        try:
            self._migrate_legacy_history_to_collector_store()
        except Exception:
            self._collector_store = None
            raise

    def _require_collector_store(self) -> CollectorDeltaStore:
        store = self._collector_store
        if store is None:
            # Canonical production composition binds a budgeted store through
            # HeadlessCollectorService before any source I/O.  Keep direct/unit
            # source use available without mutating collector storage merely by
            # constructing the source.
            store = _CANONICAL_COLLECTOR_STORE_TYPE(self._collector_store_path)
            self._collector_store = store
            try:
                self._migrate_legacy_history_to_collector_store()
            except Exception:
                self._collector_store = None
                raise
        if not _same_canonical_collector_store_path(
            store.path,
            self._collector_store_path,
        ):
            raise ProductSourceStateError(
                "product source collector store authority changed"
            )
        return store

    def _migrate_legacy_history_to_collector_store(self) -> None:
        store = self._require_collector_store()
        state = self._read_state(allow_oversized_legacy=True)
        cache = state["event_cache"]
        quote_history = state["last_committed_quote_digests"]
        dedupe_history = state["last_committed_dedupe_digests"]
        assert isinstance(cache, dict)
        assert isinstance(quote_history, dict)
        assert isinstance(dedupe_history, dict)
        if not cache and not quote_history and not dedupe_history:
            return

        pending_delta_ids: set[str] = set()
        pending = state["pending"]
        if isinstance(pending, dict) and pending["assigned"]:
            for item in pending["items"]:
                delta = CollectorDelta.from_dict(item["delta"])
                pending_delta_ids.add(delta.delta_id)

        # Record only whether the exact legacy *final* digest for each identity
        # was witnessed among canonically retired events.  This avoids materializing
        # every historical retired digest while correctly allowing multiple prices
        # for one quote_key.  Retained archive evidence remains authoritative below.
        retired_quote_matches: set[str] = set()
        retired_dedupe_matches: set[str] = set()

        def remember_retired_match(
            matches: set[str],
            history: dict[str, object],
            *,
            key: str,
            digest: str,
        ) -> None:
            if history.get(key) == digest:
                matches.add(key)

        cache_items = iter(cache.items())
        while True:
            cache_chunk = tuple(
                islice(
                    cache_items,
                    self._LEGACY_EVENT_MIGRATION_CHUNK,
                )
            )
            if not cache_chunk:
                break
            migrate: dict[str, MarketEvent] = {}
            for delta_id, event_raw in cache_chunk:
                try:
                    migrate[delta_id] = MarketEvent.from_dict(event_raw)
                except (TypeError, ValueError) as exc:
                    raise ProductSourceStateError(
                        "legacy historical event cache is invalid"
                    ) from exc
            try:
                migrate_legacy, _, _ = _canonical_collector_store_dispatch(store)
                _, retired_delta_ids = migrate_legacy(
                    store,
                    migrate,
                    source_id=self.source_id,
                    stream_epoch=self.stream_epoch,
                    allow_missing_delta_ids=tuple(sorted(pending_delta_ids)),
                )
            except (TypeError, ValueError) as exc:
                raise ProductSourceStateError(
                    "legacy historical event cache conflicts with canonical collector retention"
                ) from exc

            for delta_id in retired_delta_ids:
                event = migrate[delta_id]
                digest = canonical_event_digest(event)
                remember_retired_match(
                    retired_quote_matches,
                    quote_history,
                    key=event.quote_key,
                    digest=digest,
                )
                remember_retired_match(
                    retired_dedupe_matches,
                    dedupe_history,
                    key=event.dedupe_key,
                    digest=digest,
                )

        def verify_history(
            history: dict[str, object],
            *,
            quote: bool,
        ) -> None:
            items = iter(history.items())
            retired_matches = (
                retired_quote_matches if quote else retired_dedupe_matches
            )
            while True:
                chunk = tuple(
                    islice(
                        items,
                        self._LEGACY_HISTORY_VERIFY_CHUNK,
                    )
                )
                if not chunk:
                    break
                keys = tuple(str(key) for key, _ in chunk)
                try:
                    _, event_digest_maps, _ = _canonical_collector_store_dispatch(store)
                    quote_map, dedupe_map = event_digest_maps(
                        store,
                        source_id=self.source_id,
                        stream_epoch=self.stream_epoch,
                        quote_keys=keys if quote else (),
                        dedupe_keys=() if quote else keys,
                    )
                except (TypeError, ValueError) as exc:
                    raise ProductSourceStateError(
                        "legacy digest history cannot be verified against collector retention"
                    ) from exc
                observed = quote_map if quote else dedupe_map
                for key, digest in chunk:
                    retained_digest = observed.get(key)
                    if retained_digest == digest:
                        continue
                    if key in retired_matches and (quote or retained_digest is None):
                        continue
                    kind = "quote" if quote else "dedupe"
                    raise ProductSourceStateError(
                        f"legacy {kind} history conflicts with canonical collector history"
                    )

        verify_history(quote_history, quote=True)
        verify_history(dedupe_history, quote=False)

        state["last_committed_quote_digests"] = {}
        state["last_committed_dedupe_digests"] = {}
        state["event_cache"] = {}
        self._write_state(
            state,
            allow_oversized_current=self._legacy_oversized_state,
        )

    def _validate_pending(self, pending: object) -> None:
        base_fields = {
            "catalog_cursor",
            "catalog_position",
            "catalog_events",
            "quality_flags",
            "items",
            "assigned",
            "confirmed",
        }
        provenance_fields = {"lawful_terms_ref", "retention_ref"}
        if type(pending) is not dict:
            raise ProductSourceStateError("pending product snapshot fields mismatch")
        pending_fields = set(pending)
        if pending_fields not in (base_fields, base_fields | provenance_fields):
            raise ProductSourceStateError("pending product snapshot fields mismatch")
        try:
            self._text(pending["catalog_cursor"], "pending.catalog_cursor")
        except ValueError as exc:
            raise ProductSourceStateError("pending catalog cursor is invalid") from exc
        if type(pending["catalog_position"]) is not int or pending["catalog_position"] < 0:
            raise ProductSourceStateError("pending catalog position must be non-negative")
        if type(pending["assigned"]) is not bool or type(pending["confirmed"]) is not bool:
            raise ProductSourceStateError("pending assignment flags must be booleans")
        has_acquisition_provenance = provenance_fields.issubset(pending_fields)
        acquisition_lawful_terms_ref: str | None = None
        acquisition_retention_ref: str | None = None
        if has_acquisition_provenance:
            try:
                acquisition_lawful_terms_ref = self._text(
                    pending["lawful_terms_ref"],
                    "pending.lawful_terms_ref",
                )
                acquisition_retention_ref = self._text(
                    pending["retention_ref"],
                    "pending.retention_ref",
                )
            except ValueError as exc:
                raise ProductSourceStateError(
                    "pending acquisition compliance provenance is invalid"
                ) from exc
        elif not pending["assigned"]:
            raise ProductSourceStateError(
                "unassigned pending source snapshot lacks acquisition compliance provenance"
            )
        if pending["confirmed"] and not pending["assigned"]:
            raise ProductSourceStateError("pending snapshot cannot confirm before assignment")
        if type(pending["catalog_events"]) is not list:
            raise ProductSourceStateError("pending catalog_events must be a list")
        catalog_identities: set[str] = set()
        for event_raw in pending["catalog_events"]:
            try:
                catalog_event = CatalogEvent.from_dict(event_raw)
            except (TypeError, ValueError) as exc:
                raise ProductSourceStateError("pending catalog event is invalid") from exc
            if catalog_event.source_id != self.source_id:
                raise ProductSourceStateError(
                    "pending catalog event source conflicts with product source"
                )
            catalog_identities.add(catalog_event.identity)
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
            if event.source_id != self.source_id:
                raise ProductSourceStateError(
                    "pending market event source conflicts with product source"
                )
            if event.event_id not in catalog_identities:
                raise ProductSourceStateError(
                    "pending market event is absent from catalog snapshot"
                )
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
                    or delta.source_observed_at != event.observed_ts
                    or delta.event_dedupe_key != event.dedupe_key
                    or delta.event_id != event.event_id
                    or delta.canonical_event_digest != item["canonical_digest"]
                    or delta.source_payload_digest != item["source_payload_digest"]
                    or delta.quality_flags != tuple(flags)
                    or (
                        has_acquisition_provenance
                        and (
                            delta.lawful_terms_ref != acquisition_lawful_terms_ref
                            or delta.retention_ref != acquisition_retention_ref
                        )
                    )
                ):
                    raise ProductSourceStateError("pending delta is not bound to source evidence")
                if prior_position is not None and delta.cursor_position != prior_position + 1:
                    raise ProductSourceStateError("pending delta positions are not contiguous")
                prior_position = delta.cursor_position
            elif item["delta"] is not None:
                raise ProductSourceStateError("unassigned pending item cannot contain a delta")

    @classmethod
    def _snapshot_provider_metadata(
        cls,
        value: object,
        *,
        path: str = "metadata",
        depth: int = 0,
        active: set[int] | None = None,
    ) -> object:
        """Copy only exact canonical JSON value types without coercion."""

        if depth > 64:
            raise ProductSourcePayloadError(
                "provider metadata exceeds acquisition snapshot depth"
            )
        if value is None or type(value) in (str, bool, int):
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise ProductSourcePayloadError(
                    f"{path} contains non-finite JSON number"
                )
            return value
        if type(value) not in (list, dict):
            raise ProductSourcePayloadError(
                f"{path} contains non-canonical JSON value type"
            )

        if active is None:
            active = set()
        marker = id(value)
        if marker in active:
            raise ProductSourcePayloadError(
                f"{path} contains cyclic JSON container"
            )
        active.add(marker)
        try:
            if type(value) is list:
                return [
                    cls._snapshot_provider_metadata(
                        item,
                        path=f"{path}[{index}]",
                        depth=depth + 1,
                        active=active,
                    )
                    for index, item in enumerate(value)
                ]

            result: dict[str, object] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise ProductSourcePayloadError(
                        f"{path} contains non-canonical JSON object key"
                    )
                result[key] = cls._snapshot_provider_metadata(
                    item,
                    path=f"{path}.{key}",
                    depth=depth + 1,
                    active=active,
                )
            return result
        finally:
            active.remove(marker)

    @classmethod
    def _snapshot_provider_quote(cls, quote: object) -> ProviderQuote:
        """Revalidate one provider DTO at the product-source acquisition boundary.

        ProviderQuote is frozen for ordinary callers, but frozen dataclasses can still
        be mutated through low-level object.__setattr__ and subclasses can replace
        descriptors. Constructor-time provider validation therefore is not sufficient
        authority for bytes received later from an external provider adapter.
        """

        if type(quote) is not ProviderQuote:
            raise ProductSourcePayloadError(
                "provider batch requires exact ProviderQuote evidence"
            )
        required_text = (
            quote.provider_event_id,
            quote.provider_market_id,
            quote.provider_selection_id,
            quote.observed_ts,
            quote.status,
        )
        optional_text = (
            quote.source_ts,
            quote.score_state,
            quote.sport,
            quote.exchange_side,
        )
        if (
            any(type(value) is not str for value in required_text)
            or any(value is not None and type(value) is not str for value in optional_text)
            or type(quote.decimal_odds) is not Decimal
            or type(quote.sequence) is not int
            or type(quote.market_type) is not MarketType
            or type(quote.metadata) is not dict
        ):
            raise ProductSourcePayloadError(
                "provider quote uses non-canonical acquisition value types"
            )
        cls._instant(quote.observed_ts, "provider quote observed_ts")
        if quote.source_ts is not None:
            cls._instant(quote.source_ts, "provider quote source_ts")
        try:
            metadata = cls._snapshot_provider_metadata(quote.metadata)
            if type(metadata) is not dict:
                raise TypeError("provider quote metadata must be an object")
            # Canonical JSON encode/decode is now only a determinism check/copy
            # witness; the exact-type traversal above has already rejected values
            # that JSON would otherwise coerce (for example tuple -> array).
            metadata = strict_json_loads(cls._canonical_json(metadata))
            if type(metadata) is not dict:
                raise TypeError("provider quote metadata must be an object")
            return ProviderQuote(
                provider_event_id=quote.provider_event_id,
                provider_market_id=quote.provider_market_id,
                provider_selection_id=quote.provider_selection_id,
                decimal_odds=quote.decimal_odds,
                observed_ts=quote.observed_ts,
                sequence=quote.sequence,
                market_type=quote.market_type,
                status=quote.status,
                source_ts=quote.source_ts,
                score_state=quote.score_state,
                metadata=metadata,
                sport=quote.sport,
                exchange_side=quote.exchange_side,
            )
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ProductSourcePayloadError(
                "provider quote failed acquisition-boundary validation"
            ) from exc

    @classmethod
    def _snapshot_provider_batch(cls, batch: object) -> ProviderBatch:
        """Copy provider output into exact canonical DTOs before product use."""

        if type(batch) is not ProviderBatch:
            raise ProductSourcePayloadError(
                "provider.read_batch must return exact ProviderBatch"
            )
        if (
            type(batch.source_id) is not str
            or type(batch.quotes) is not tuple
            or (batch.cursor is not None and type(batch.cursor) is not str)
            or type(batch.quality_flags) is not tuple
            or any(type(flag) is not str for flag in batch.quality_flags)
        ):
            raise ProductSourcePayloadError(
                "provider batch uses non-canonical acquisition value types"
            )
        try:
            quotes = tuple(cls._snapshot_provider_quote(quote) for quote in batch.quotes)
            return ProviderBatch(
                source_id=batch.source_id,
                quotes=quotes,
                cursor=batch.cursor,
                quality_flags=batch.quality_flags,
            )
        except ProductSourcePayloadError:
            raise
        except (TypeError, ValueError) as exc:
            raise ProductSourcePayloadError(
                "provider batch failed acquisition-boundary validation"
            ) from exc

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
            "exchange_side": quote.exchange_side,
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

    def _read_provider_snapshot(
        self,
        *,
        provider: MarketProvider,
        read_batch: Callable[[int], ProviderBatch],
        source_id: str,
    ) -> tuple[str, tuple[ProviderQuote, ...], tuple[str, ...]]:
        quotes: list[ProviderQuote] = []
        cursor: str | None = None
        flags: set[str] = set()
        while True:
            if self.provider is not provider:
                raise ProductSourcePayloadError(
                    "product source provider changed during acquisition"
                )
            batch = read_batch(self._READ_BATCH_ITEMS)
            if self.provider is not provider:
                raise ProductSourcePayloadError(
                    "product source provider changed during acquisition"
                )
            batch = self._snapshot_provider_batch(batch)
            if getattr(provider, "source_id", None) != source_id:
                raise ProductSourcePayloadError(
                    "provider source_id changed during acquisition"
                )
            if batch.source_id != source_id:
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
            truncated = "TRUNCATED_BATCH" in batch.quality_flags
            if truncated and len(batch.quotes) != self._READ_BATCH_ITEMS:
                raise ProductSourcePayloadError(
                    "truncated provider batch must fill requested acquisition page"
                )
            quotes.extend(batch.quotes)
            if len(quotes) > self._MAX_SNAPSHOT_ITEMS or (
                truncated and len(quotes) == self._MAX_SNAPSHOT_ITEMS
            ):
                raise ProductSourcePayloadError(
                    "provider snapshot exceeds bounded source capacity"
                )
            flags.update(flag for flag in batch.quality_flags if flag != "TRUNCATED_BATCH")
            if not truncated:
                break
        assert cursor is not None
        return cursor, tuple(quotes), tuple(sorted(flags))

    @classmethod
    def _scheduled_start(cls, event: MarketEvent) -> str:
        raw = event.metadata.get("commence_time")
        try:
            value = cls._text(raw, "commence_time")
            cls._instant(value, "commence_time")
        except ValueError as exc:
            raise ProductSourcePayloadError(
                "provider event requires timezone-aware commence_time"
            ) from exc
        return value

    def _catalog_events(
        self,
        quotes: tuple[ProviderQuote, ...],
        *,
        source_id: str,
        normalize: Callable[[str, ProviderQuote], MarketEvent],
    ) -> tuple[CatalogEvent, ...]:
        values: dict[str, CatalogEvent] = {}
        for quote in quotes:
            event = normalize(source_id, quote)
            if event.sport is None:
                raise ProductSourcePayloadError("provider quote requires canonical sport identity")
            scheduled = self._scheduled_start(event)
            phase = (
                EventPhase.PRE_MATCH
                if self._instant(event.observed_ts, "observed_ts")
                < self._instant(scheduled, "commence_time")
                else EventPhase.LIVE
            )
            candidate = CatalogEvent(
                source_id=source_id,
                sport=event.sport,
                event_id=quote.provider_event_id,
                phase=phase,
                available_at=event.observed_ts,
                scheduled_start_at=scheduled,
            )
            candidate.validate()
            previous = values.get(quote.provider_event_id)
            if previous is not None and previous != candidate:
                raise ProductSourcePayloadError(
                    "provider snapshot contradicts lifecycle metadata within one event"
                )
            values[quote.provider_event_id] = candidate
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
        try:
            acquisition_lawful_terms_ref = self._text(
                self.lawful_terms_ref,
                "lawful_terms_ref",
            )
            acquisition_retention_ref = self._text(
                self.retention_ref,
                "retention_ref",
            )
        except ValueError as exc:
            raise ProductSourceStateError(
                "product source acquisition compliance provenance is invalid"
            ) from exc

        provider = self.provider
        source_id = self.source_id
        stream_epoch = self.stream_epoch
        normalizer = self.normalizer
        if type(normalizer) is not CanonicalNormalizer:
            raise ProductSourceStateError(
                "product source normalizer is not canonical"
            )
        read_batch = getattr(provider, "read_batch", None)
        if not callable(read_batch):
            raise ProductSourceStateError(
                "product source provider read authority is unavailable"
            )
        read_batch_func = getattr(read_batch, "__func__", read_batch)
        read_batch_self = getattr(read_batch, "__self__", None)
        read_batch_code = getattr(read_batch_func, "__code__", None)
        read_batch_defaults = getattr(read_batch_func, "__defaults__", None)
        read_batch_kwdefaults = getattr(read_batch_func, "__kwdefaults__", None)
        read_batch_kwitems = (
            tuple(read_batch_kwdefaults.items())
            if read_batch_kwdefaults is not None
            else ()
        )
        normalize = normalizer.normalize
        normalize_func = getattr(normalize, "__func__", normalize)
        normalize_self = getattr(normalize, "__self__", None)
        normalize_code = getattr(normalize_func, "__code__", None)
        normalize_defaults = getattr(normalize_func, "__defaults__", None)
        normalize_kwdefaults = getattr(normalize_func, "__kwdefaults__", None)
        normalize_kwitems = (
            tuple(normalize_kwdefaults.items())
            if normalize_kwdefaults is not None
            else ()
        )
        if read_batch_code is None or normalize_code is None:
            raise ProductSourceStateError(
                "product source acquisition executable authority is unavailable"
            )

        def callable_metadata_current(
            *,
            owner: object,
            name: str,
            expected_self: object,
            expected_func: object,
            expected_code: object,
            expected_defaults: object,
            expected_kwdefaults: object,
            expected_kwitems: tuple[tuple[str, object], ...],
        ) -> bool:
            rebound = getattr(owner, name, None)
            rebound_func = getattr(rebound, "__func__", rebound)
            current_kwdefaults = getattr(expected_func, "__kwdefaults__", None)
            return (
                callable(rebound)
                and getattr(rebound, "__self__", None) is expected_self
                and rebound_func is expected_func
                and getattr(expected_func, "__code__", None) is expected_code
                and getattr(expected_func, "__defaults__", None) is expected_defaults
                and current_kwdefaults is expected_kwdefaults
                and (
                    expected_kwdefaults is None
                    or (
                        len(current_kwdefaults) == len(expected_kwitems)
                        and all(
                            key in current_kwdefaults
                            and current_kwdefaults[key] is value
                            for key, value in expected_kwitems
                        )
                    )
                )
            )

        def provider_read(max_items: int) -> ProviderBatch:
            if not callable_metadata_current(
                owner=provider,
                name="read_batch",
                expected_self=read_batch_self,
                expected_func=read_batch_func,
                expected_code=read_batch_code,
                expected_defaults=read_batch_defaults,
                expected_kwdefaults=read_batch_kwdefaults,
                expected_kwitems=read_batch_kwitems,
            ):
                raise ProductSourcePayloadError(
                    "product source provider read executable changed during acquisition"
                )
            result = read_batch(max_items)
            if not callable_metadata_current(
                owner=provider,
                name="read_batch",
                expected_self=read_batch_self,
                expected_func=read_batch_func,
                expected_code=read_batch_code,
                expected_defaults=read_batch_defaults,
                expected_kwdefaults=read_batch_kwdefaults,
                expected_kwitems=read_batch_kwitems,
            ):
                raise ProductSourcePayloadError(
                    "product source provider read executable changed during acquisition"
                )
            return result

        def canonical_normalize(current_source_id: str, quote: ProviderQuote) -> MarketEvent:
            if (
                self.normalizer is not normalizer
                or not callable_metadata_current(
                    owner=normalizer,
                    name="normalize",
                    expected_self=normalize_self,
                    expected_func=normalize_func,
                    expected_code=normalize_code,
                    expected_defaults=normalize_defaults,
                    expected_kwdefaults=normalize_kwdefaults,
                    expected_kwitems=normalize_kwitems,
                )
            ):
                raise ProductSourceStateError(
                    "product source normalizer executable changed during acquisition"
                )
            event = normalize(current_source_id, quote)
            if (
                self.normalizer is not normalizer
                or not callable_metadata_current(
                    owner=normalizer,
                    name="normalize",
                    expected_self=normalize_self,
                    expected_func=normalize_func,
                    expected_code=normalize_code,
                    expected_defaults=normalize_defaults,
                    expected_kwdefaults=normalize_kwdefaults,
                    expected_kwitems=normalize_kwitems,
                )
            ):
                raise ProductSourceStateError(
                    "product source normalizer executable changed during acquisition"
                )
            return event

        cursor, quotes, quality_flags = self._read_provider_snapshot(
            provider=provider,
            read_batch=provider_read,
            source_id=source_id,
        )
        if (
            self.provider is not provider
            or self.source_id != source_id
            or self.stream_epoch != stream_epoch
        ):
            raise ProductSourceStateError(
                "product source identity changed during acquisition"
            )
        if self.normalizer is not normalizer:
            raise ProductSourceStateError(
                "product source normalizer changed during acquisition"
            )
        catalog_events = self._catalog_events(
            quotes,
            source_id=source_id,
            normalize=canonical_normalize,
        )

        normalized: list[tuple[ProviderQuote, MarketEvent, str]] = []
        seen_quotes: dict[str, str] = {}
        seen_dedupes: dict[str, str] = {}
        for quote in quotes:
            event = canonical_normalize(source_id, quote)
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
            normalized.append((quote, event, digest))

        store = self._require_collector_store()
        try:
            _, event_digest_maps, _ = _canonical_collector_store_dispatch(store)
            committed_quotes, committed_dedupes = event_digest_maps(
                store,
                source_id=self.source_id,
                stream_epoch=self.stream_epoch,
                quote_keys=tuple(seen_quotes),
                dedupe_keys=tuple(seen_dedupes),
            )
        except (TypeError, ValueError) as exc:
            raise ProductSourceStateError(
                "cannot resolve bounded canonical collector event history"
            ) from exc

        items: list[dict[str, object]] = []
        for quote, event, digest in normalized:
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
            "lawful_terms_ref": acquisition_lawful_terms_ref,
            "retention_ref": acquisition_retention_ref,
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
        try:
            acquisition_lawful_terms_ref = self._text(
                pending.get("lawful_terms_ref"),
                "pending.lawful_terms_ref",
            )
            acquisition_retention_ref = self._text(
                pending.get("retention_ref"),
                "pending.retention_ref",
            )
        except ValueError as exc:
            raise ProductSourceStateError(
                "pending source snapshot lacks acquisition compliance provenance"
            ) from exc
        confirmed = int(state["last_confirmed_delta_position"])
        if checkpoint_position != confirmed:
            raise ProductSourceStateError(
                "collector advanced beyond source state before snapshot assignment"
            )
        assigned_at = self.clock()
        self._instant(assigned_at, "collector_received_at")
        items = pending["items"]
        assert isinstance(items, list)
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
                lawful_terms_ref=acquisition_lawful_terms_ref,
                retention_ref=acquisition_retention_ref,
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
        pending = state["pending"]
        if isinstance(pending, dict) and pending["assigned"]:
            for item in pending["items"]:
                pending_delta = CollectorDelta.from_dict(item["delta"])
                if pending_delta.delta_id != delta.delta_id:
                    continue
                if pending_delta != delta:
                    raise ProductSourceStateError(
                        "pending event conflicts with collector delta"
                    )
                event = MarketEvent.from_dict(item["event"])
                if (
                    event.event_id != delta.event_id
                    or event.dedupe_key != delta.event_dedupe_key
                    or canonical_event_digest(event) != delta.canonical_event_digest
                ):
                    raise ProductSourceStateError(
                        "pending event conflicts with collector delta"
                    )
                return event

        try:
            store = self._require_collector_store()
            _, _, resolve_event = _canonical_collector_store_dispatch(store)
            return resolve_event(store, delta)
        except (TypeError, ValueError) as exc:
            raise ProductSourceStateError(
                "canonical event payload is absent or invalid in collector retention"
            ) from exc



def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value or value.strip() != value:
        raise ProductSourceError(f"required product source environment variable {name} is missing")
    return value


def _capture_parlay_product_source_factory(
    *,
    provider_type=ParlayApiTableTennisProvider,
    source_type=ParlayApiProductSource,
    required_env=_required_env,
):
    """Bind shipped source constructors once at import composition."""

    def create_parlay_product_source() -> ParlayApiProductSource:
        """Construct the supported read-only Parlay source from closed dependencies."""

        provider = provider_type(
            api_key=required_env("AUTOSPORT_PARLAY_API_KEY")
        )
        return source_type(
            provider,
            workspace=required_env("AUTOSPORT_PRODUCT_WORKSPACE"),
            lawful_terms_ref=required_env("AUTOSPORT_PARLAY_LAWFUL_TERMS_REF"),
            retention_ref=required_env("AUTOSPORT_PARLAY_RETENTION_REF"),
        )

    return create_parlay_product_source


create_parlay_product_source = _capture_parlay_product_source_factory()
