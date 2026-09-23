from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .prophetx_marketdata import ProphetXRestMarketProvider
from .provider_sequence_authority import SQLiteProviderSequenceAuthority
from .providers import ProviderBatch, ProviderQuote


_SCHEMA_VERSION = 1
_SOURCE_ID = "prophetx:sandbox"
_SEQUENCE_SOURCE_ID = "prophetx:sandbox:rest:v3-affiliate-get-markets"
_MAX_SEQUENCE = (1 << 63) - 1


class ProphetXSnapshotPublicationError(RuntimeError):
    """Durable ProphetX publication identity cannot be established safely."""


@dataclass(frozen=True, slots=True)
class ProphetXSnapshotPublication:
    journal_authority_id: str
    sequence_authority_id: str
    acquisition_sequence: int
    request_fingerprint_sha256: str
    snapshot_fingerprint_sha256: str
    batch_sha256: str
    publication_fingerprint_sha256: str
    quote_count: int
    quality_flags: tuple[str, ...]
    provider_continuity_verified: bool = False
    grants_provider_origin_authority: bool = False
    grants_retention_rights: bool = False
    grants_execution_authority: bool = False

    def __post_init__(self) -> None:
        _identity(self.journal_authority_id, "journal_authority_id")
        _identity(self.sequence_authority_id, "sequence_authority_id")
        _sequence(self.acquisition_sequence)
        for name, value in (
            ("request_fingerprint_sha256", self.request_fingerprint_sha256),
            ("snapshot_fingerprint_sha256", self.snapshot_fingerprint_sha256),
            ("batch_sha256", self.batch_sha256),
            ("publication_fingerprint_sha256", self.publication_fingerprint_sha256),
        ):
            _sha256(value, name)
        if type(self.quote_count) is not int or self.quote_count < 0:
            raise ProphetXSnapshotPublicationError("quote_count must be non-negative int")
        if type(self.quality_flags) is not tuple:
            raise ProphetXSnapshotPublicationError("quality_flags must be tuple")
        if len(set(self.quality_flags)) != len(self.quality_flags):
            raise ProphetXSnapshotPublicationError("quality_flags contain duplicates")
        if any(type(flag) is not str or not flag or flag != flag.strip() for flag in self.quality_flags):
            raise ProphetXSnapshotPublicationError("quality_flags are not canonical")
        if any(
            flag is not False
            for flag in (
                self.provider_continuity_verified,
                self.grants_provider_origin_authority,
                self.grants_retention_rights,
                self.grants_execution_authority,
            )
        ):
            raise ProphetXSnapshotPublicationError(
                "publication cannot mint continuity/origin/rights/execution authority"
            )


@dataclass(frozen=True, slots=True)
class ProphetXPublicationResult:
    publication: ProphetXSnapshotPublication
    batch: ProviderBatch
    created: bool


class SQLiteProphetXSnapshotPublicationJournal:
    """Restart-safe product-local publication journal for ProphetX REST snapshots.

    Product acquisition sequence orders Autosport acquisitions only. Sequence gaps,
    consecutive values, equal raw response hashes, and successful replay never prove
    provider causal continuity or retained-data rights.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        authority_id: str,
        sequence_authority: SQLiteProviderSequenceAuthority,
        event_ids: tuple[int, ...],
        create: bool,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        if type(create) is not bool:
            raise TypeError("create must be bool")
        if type(sequence_authority) is not SQLiteProviderSequenceAuthority:
            raise TypeError("sequence_authority must be exact SQLiteProviderSequenceAuthority")
        if type(event_ids) is not tuple or not event_ids:
            raise ValueError("event_ids must be a non-empty tuple")
        events = tuple(_event_id(value) for value in event_ids)
        if len(set(events)) != len(events):
            raise ValueError("event_ids must not contain duplicates")
        if isinstance(path, str) and path == ":memory:":
            raise ValueError("publication journal must use durable storage")
        if isinstance(busy_timeout_seconds, bool) or not isinstance(
            busy_timeout_seconds, (int, float)
        ):
            raise ValueError("busy_timeout_seconds must be finite and positive")
        timeout = float(busy_timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("busy_timeout_seconds must be finite and positive")

        self.path = Path(path).expanduser().resolve(strict=False)
        self.authority_id = _identity(authority_id, "authority_id")
        self.sequence_authority = sequence_authority
        self.sequence_authority_id = _identity(
            sequence_authority.authority_id, "sequence_authority_id"
        )
        self.event_ids = events
        self.request_fingerprint_sha256 = _request_fingerprint(events)
        self.busy_timeout_seconds = timeout
        self._busy_timeout_ms = max(1, int(timeout * 1000))

        if not self.path.parent.exists() or not self.path.parent.is_dir():
            raise ValueError("publication journal parent directory must exist")
        if self.path.exists() and self.path.is_dir():
            raise ValueError("publication journal path must be a file")
        if create and not self.path.exists():
            self._initialize()
        else:
            self._validate_existing()

    def acquire_and_publish(
        self,
        provider: ProphetXRestMarketProvider,
        *,
        max_items: int = 1000,
    ) -> ProphetXPublicationResult:
        if type(provider) is not ProphetXRestMarketProvider:
            raise TypeError("provider must be exact ProphetXRestMarketProvider")
        if type(max_items) is not int or max_items <= 0:
            raise ValueError("max_items must be positive non-boolean int")
        if provider.event_ids != self.event_ids:
            raise ProphetXSnapshotPublicationError("provider event scope does not match journal")
        if getattr(provider, "_sequence_authority", None) is not self.sequence_authority:
            raise ProphetXSnapshotPublicationError("provider sequence authority does not match journal")
        if getattr(provider, "_pending", None) is not None or getattr(provider, "_offset", None) != 0:
            raise ProphetXSnapshotPublicationError("provider is not at a snapshot boundary")

        chunks: list[ProviderBatch] = []
        cursor: str | None = None
        stable_flags: tuple[str, ...] | None = None
        while True:
            chunk = provider.read_batch(max_items=max_items)
            if chunk.source_id != _SOURCE_ID or chunk.cursor is None:
                raise ProphetXSnapshotPublicationError("ProphetX batch identity is incomplete")
            if cursor is None:
                cursor = chunk.cursor
            elif cursor != chunk.cursor:
                raise ProphetXSnapshotPublicationError("snapshot cursor changed between chunks")
            flags = tuple(flag for flag in chunk.quality_flags if flag != "TRUNCATED_BATCH")
            if stable_flags is None:
                stable_flags = flags
            elif stable_flags != flags:
                raise ProphetXSnapshotPublicationError("snapshot quality changed between chunks")
            chunks.append(chunk)
            if "TRUNCATED_BATCH" not in chunk.quality_flags:
                break

        assert cursor is not None and stable_flags is not None
        batch = ProviderBatch(
            source_id=_SOURCE_ID,
            quotes=tuple(quote for chunk in chunks for quote in chunk.quotes),
            cursor=cursor,
            quality_flags=stable_flags,
        )
        result = self._publish(batch, allow_create=True)
        if getattr(provider, "_pending", None) is not None or getattr(provider, "_offset", None) != 0:
            raise ProphetXSnapshotPublicationError("provider did not finish at snapshot boundary")
        return result

    def assert_idempotent_replay(self, batch: ProviderBatch) -> ProphetXPublicationResult:
        """Verify a replay against durable state. Replay can never create a row."""
        return self._publish(batch, allow_create=False)

    def resolve(self, acquisition_sequence: int) -> ProphetXSnapshotPublication:
        sequence = _sequence(acquisition_sequence)
        row = self._read_row(sequence)
        return self._publication(sequence, row)

    def resolve_payload(self, acquisition_sequence: int) -> bytes:
        sequence = _sequence(acquisition_sequence)
        row = self._read_row(sequence)
        publication = self._publication(sequence, row)
        payload = row[5]
        if not isinstance(payload, str):
            raise ProphetXSnapshotPublicationError("durable batch payload is malformed")
        raw = payload.encode("ascii")
        if hashlib.sha256(raw).hexdigest() != publication.batch_sha256:
            raise ProphetXSnapshotPublicationError("durable batch payload hash mismatch")
        return raw

    def _publish(self, batch: ProviderBatch, *, allow_create: bool) -> ProphetXPublicationResult:
        if type(batch) is not ProviderBatch:
            raise TypeError("batch must be ProviderBatch")
        sequence, snapshot_sha = self._validate_batch(batch)
        raw = _batch_bytes(batch)
        batch_sha = hashlib.sha256(raw).hexdigest()
        publication_sha = _publication_fingerprint(
            authority_id=self.authority_id,
            sequence_authority_id=self.sequence_authority_id,
            acquisition_sequence=sequence,
            request_sha=self.request_fingerprint_sha256,
            snapshot_sha=snapshot_sha,
            batch_sha=batch_sha,
        )
        flags_json = _json_text(list(batch.quality_flags))
        batch_json = raw.decode("ascii")

        connection = self._connect()
        try:
            self._configure(connection)
            connection.execute("BEGIN IMMEDIATE")
            self._validate(connection)
            row = connection.execute(
                """SELECT snapshot_fingerprint_sha256,batch_sha256,
                          publication_fingerprint_sha256,quote_count,quality_flags_json,batch_json
                   FROM prophetx_snapshot_publications_v1
                   WHERE acquisition_sequence=?""",
                (sequence,),
            ).fetchone()
            if row is None:
                if not allow_create:
                    raise ProphetXSnapshotPublicationError(
                        "replay sequence has no durable publication"
                    )
                connection.execute(
                    """INSERT INTO prophetx_snapshot_publications_v1(
                           acquisition_sequence,snapshot_fingerprint_sha256,batch_sha256,
                           publication_fingerprint_sha256,quote_count,quality_flags_json,batch_json
                       ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        sequence,
                        snapshot_sha,
                        batch_sha,
                        publication_sha,
                        len(batch.quotes),
                        flags_json,
                        batch_json,
                    ),
                )
                connection.commit()
                created = True
                row = (
                    snapshot_sha,
                    batch_sha,
                    publication_sha,
                    len(batch.quotes),
                    flags_json,
                    batch_json,
                )
            else:
                expected = (
                    snapshot_sha,
                    batch_sha,
                    publication_sha,
                    len(batch.quotes),
                    flags_json,
                    batch_json,
                )
                if tuple(row) != expected:
                    raise ProphetXSnapshotPublicationError(
                        "acquisition sequence already binds different publication evidence"
                    )
                connection.commit()
                created = False
        except ProphetXSnapshotPublicationError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ProphetXSnapshotPublicationError("publication journal unavailable") from exc
        finally:
            connection.close()

        return ProphetXPublicationResult(
            publication=self._publication(sequence, row),
            batch=batch,
            created=created,
        )

    def _validate_batch(self, batch: ProviderBatch) -> tuple[int, str]:
        if batch.source_id != _SOURCE_ID or batch.cursor is None:
            raise ProphetXSnapshotPublicationError("batch is not canonical ProphetX snapshot")
        if "PROPHETX_REST_SNAPSHOT" not in batch.quality_flags:
            raise ProphetXSnapshotPublicationError("snapshot quality flag is missing")
        if "PRODUCT_ACQUISITION_SEQUENCE_AUTHORITY" not in batch.quality_flags:
            raise ProphetXSnapshotPublicationError("sequence-authority quality flag is missing")
        if "TRUNCATED_BATCH" in batch.quality_flags:
            raise ProphetXSnapshotPublicationError("partial snapshot cannot be published")
        sequence, snapshot_sha = _cursor(batch.cursor)

        observed: str | None = None
        for quote in batch.quotes:
            if type(quote) is not ProviderQuote or quote.sequence != sequence:
                raise ProphetXSnapshotPublicationError("quote acquisition sequence mismatch")
            metadata = quote.metadata
            expected = {
                "product_acquisition_sequence": sequence,
                "sequence_authority_id": self.sequence_authority_id,
                "sequence_source_id": _SEQUENCE_SOURCE_ID,
                "request_fingerprint_sha256": self.request_fingerprint_sha256,
                "snapshot_fingerprint_sha256": snapshot_sha,
            }
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise ProphetXSnapshotPublicationError("quote publication lineage mismatch")
            if observed is None:
                observed = quote.observed_ts
            elif observed != quote.observed_ts:
                raise ProphetXSnapshotPublicationError(
                    "one acquisition cannot contain multiple receipt timestamps"
                )
        return sequence, snapshot_sha

    def _read_row(self, sequence: int) -> tuple[Any, ...]:
        connection = self._connect()
        try:
            self._configure(connection)
            self._validate(connection)
            row = connection.execute(
                """SELECT snapshot_fingerprint_sha256,batch_sha256,
                          publication_fingerprint_sha256,quote_count,quality_flags_json,batch_json
                   FROM prophetx_snapshot_publications_v1
                   WHERE acquisition_sequence=?""",
                (sequence,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ProphetXSnapshotPublicationError("publication journal unavailable") from exc
        finally:
            connection.close()
        if row is None:
            raise ProphetXSnapshotPublicationError("publication sequence is not retained")
        return tuple(row)

    def _publication(
        self, sequence: int, row: tuple[Any, ...]
    ) -> ProphetXSnapshotPublication:
        if len(row) != 6:
            raise ProphetXSnapshotPublicationError("publication row is malformed")
        snapshot_sha, batch_sha, publication_sha, quote_count, flags_json, batch_json = row
        _sha256(snapshot_sha, "snapshot_fingerprint_sha256")
        _sha256(batch_sha, "batch_sha256")
        _sha256(publication_sha, "publication_fingerprint_sha256")
        if type(quote_count) is not int or quote_count < 0:
            raise ProphetXSnapshotPublicationError("durable quote_count is malformed")
        if not isinstance(flags_json, str) or not isinstance(batch_json, str):
            raise ProphetXSnapshotPublicationError("durable publication payload is malformed")
        try:
            flags = json.loads(flags_json)
        except json.JSONDecodeError as exc:
            raise ProphetXSnapshotPublicationError("durable quality flags are malformed") from exc
        if type(flags) is not list or any(type(flag) is not str for flag in flags):
            raise ProphetXSnapshotPublicationError("durable quality flags are malformed")
        raw = batch_json.encode("ascii")
        if hashlib.sha256(raw).hexdigest() != batch_sha:
            raise ProphetXSnapshotPublicationError("durable batch payload hash mismatch")
        expected = _publication_fingerprint(
            authority_id=self.authority_id,
            sequence_authority_id=self.sequence_authority_id,
            acquisition_sequence=sequence,
            request_sha=self.request_fingerprint_sha256,
            snapshot_sha=snapshot_sha,
            batch_sha=batch_sha,
        )
        if publication_sha != expected:
            raise ProphetXSnapshotPublicationError("publication fingerprint does not verify")
        return ProphetXSnapshotPublication(
            journal_authority_id=self.authority_id,
            sequence_authority_id=self.sequence_authority_id,
            acquisition_sequence=sequence,
            request_fingerprint_sha256=self.request_fingerprint_sha256,
            snapshot_fingerprint_sha256=snapshot_sha,
            batch_sha256=batch_sha,
            publication_fingerprint_sha256=publication_sha,
            quote_count=quote_count,
            quality_flags=tuple(flags),
        )

    def _initialize(self) -> None:
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_seconds,
                isolation_level=None,
            )
            self._configure(connection)
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise ProphetXSnapshotPublicationError("publication journal requires WAL mode")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE prophetx_snapshot_publication_meta_v1(
                       singleton INTEGER PRIMARY KEY,
                       schema_version INTEGER NOT NULL,
                       journal_authority_id TEXT NOT NULL,
                       sequence_authority_id TEXT NOT NULL,
                       request_fingerprint_sha256 TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE TABLE prophetx_snapshot_publications_v1(
                       acquisition_sequence INTEGER PRIMARY KEY,
                       snapshot_fingerprint_sha256 TEXT NOT NULL,
                       batch_sha256 TEXT NOT NULL,
                       publication_fingerprint_sha256 TEXT NOT NULL,
                       quote_count INTEGER NOT NULL,
                       quality_flags_json TEXT NOT NULL,
                       batch_json TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """INSERT INTO prophetx_snapshot_publication_meta_v1
                   VALUES (1,?,?,?,?)""",
                (
                    _SCHEMA_VERSION,
                    self.authority_id,
                    self.sequence_authority_id,
                    self.request_fingerprint_sha256,
                ),
            )
            connection.commit()
        except (sqlite3.Error, ProphetXSnapshotPublicationError) as exc:
            try:
                if "connection" in locals() and connection.in_transaction:
                    connection.rollback()
            except sqlite3.Error:
                pass
            if isinstance(exc, ProphetXSnapshotPublicationError):
                raise
            raise ProphetXSnapshotPublicationError(
                "publication journal cannot be initialized"
            ) from exc
        finally:
            if "connection" in locals():
                connection.close()
        self._validate_existing()

    def _validate_existing(self) -> None:
        connection = self._connect()
        try:
            self._configure(connection)
            self._validate(connection)
        except sqlite3.Error as exc:
            raise ProphetXSnapshotPublicationError("publication journal unavailable") from exc
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists() or not self.path.is_file():
            raise ProphetXSnapshotPublicationError("publication journal is missing")
        try:
            return sqlite3.connect(
                self.path.as_uri() + "?mode=rw",
                uri=True,
                timeout=self.busy_timeout_seconds,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise ProphetXSnapshotPublicationError("publication journal cannot be opened") from exc

    def _configure(self, connection: sqlite3.Connection) -> None:
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA synchronous=FULL")

    def _validate(self, connection: sqlite3.Connection) -> None:
        mode = connection.execute("PRAGMA journal_mode").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            raise ProphetXSnapshotPublicationError("publication journal requires WAL mode")
        meta = connection.execute(
            """SELECT singleton,schema_version,journal_authority_id,
                      sequence_authority_id,request_fingerprint_sha256
               FROM prophetx_snapshot_publication_meta_v1"""
        ).fetchall()
        expected = [
            (
                1,
                _SCHEMA_VERSION,
                self.authority_id,
                self.sequence_authority_id,
                self.request_fingerprint_sha256,
            )
        ]
        if meta != expected:
            raise ProphetXSnapshotPublicationError("publication journal identity mismatch")
        triggers = connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='trigger'
                 AND tbl_name IN (
                    'prophetx_snapshot_publication_meta_v1',
                    'prophetx_snapshot_publications_v1'
                 )"""
        ).fetchall()
        if triggers:
            raise ProphetXSnapshotPublicationError("publication journal tables must not have triggers")


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty trimmed text")
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field} must not contain control characters")
    return value


def _event_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("event_ids must contain positive non-boolean integers")
    return value


def _sequence(value: object) -> int:
    if type(value) is not int or value <= 0 or value > _MAX_SEQUENCE:
        raise ProphetXSnapshotPublicationError(
            "acquisition sequence must be positive signed-64 int"
        )
    return value


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProphetXSnapshotPublicationError(f"{field} must be lowercase SHA-256")
    return value


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProphetXSnapshotPublicationError("value is not canonical JSON") from exc


def _json_text(value: Any) -> str:
    return _json_bytes(value).decode("ascii")


def _request_fingerprint(event_ids: tuple[int, ...]) -> str:
    value = {
        "schema": "autosport.prophetx-rest-request-binding.v1",
        "source_id": _SOURCE_ID,
        "sequence_source_id": _SEQUENCE_SOURCE_ID,
        "transport_surface": "v3_affiliate_get_markets",
        "event_ids": list(event_ids),
        "get_all_market": True,
    }
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _cursor(cursor: str) -> tuple[int, str]:
    if not isinstance(cursor, str):
        raise ProphetXSnapshotPublicationError("snapshot cursor is malformed")
    parts = cursor.split(":")
    if len(parts) != 4 or parts[0] != "acquisition" or parts[2] != "sha256":
        raise ProphetXSnapshotPublicationError("snapshot cursor is malformed")
    if not parts[1].isascii() or not parts[1].isdigit() or parts[1].startswith("0"):
        raise ProphetXSnapshotPublicationError("snapshot sequence is malformed")
    return _sequence(int(parts[1])), _sha256(parts[3], "snapshot_fingerprint_sha256")


def _metadata_value(value: Any, depth: int = 0) -> Any:
    if depth > 64:
        raise ProphetXSnapshotPublicationError("metadata nesting exceeds safe bound")
    if value is None or type(value) in (str, bool, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProphetXSnapshotPublicationError("metadata contains non-finite float")
        return value
    if type(value) is list:
        return [_metadata_value(item, depth + 1) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ProphetXSnapshotPublicationError("metadata contains non-string key")
        return {key: _metadata_value(item, depth + 1) for key, item in value.items()}
    raise ProphetXSnapshotPublicationError("metadata contains non-JSON value")


def _quote_value(quote: ProviderQuote) -> dict[str, Any]:
    if not isinstance(quote.decimal_odds, Decimal) or not quote.decimal_odds.is_finite():
        raise ProphetXSnapshotPublicationError("quote odds must be finite Decimal")
    return {
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
        "metadata": _metadata_value(quote.metadata),
        "sport": quote.sport,
    }


def _batch_bytes(batch: ProviderBatch) -> bytes:
    return _json_bytes(
        {
            "schema": "autosport.prophetx-snapshot-publication-batch.v1",
            "source_id": batch.source_id,
            "cursor": batch.cursor,
            "quality_flags": list(batch.quality_flags),
            "quotes": [_quote_value(quote) for quote in batch.quotes],
        }
    )


def _publication_fingerprint(
    *,
    authority_id: str,
    sequence_authority_id: str,
    acquisition_sequence: int,
    request_sha: str,
    snapshot_sha: str,
    batch_sha: str,
) -> str:
    return hashlib.sha256(
        _json_bytes(
            {
                "schema": "autosport.prophetx-snapshot-publication.v1",
                "journal_authority_id": authority_id,
                "source_id": _SOURCE_ID,
                "sequence_source_id": _SEQUENCE_SOURCE_ID,
                "sequence_authority_id": sequence_authority_id,
                "acquisition_sequence": acquisition_sequence,
                "request_fingerprint_sha256": request_sha,
                "snapshot_fingerprint_sha256": snapshot_sha,
                "batch_sha256": batch_sha,
            }
        )
    ).hexdigest()
