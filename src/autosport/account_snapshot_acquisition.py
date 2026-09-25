"""Durable product-owned acquisition authority for Betfair account snapshots.

This module closes the boundary between a real product-owned provider read and downstream
account-snapshot consumers.  It does not place/cancel bets, settle positions, infer P&L,
or grant execution authority.  Credentials remain memory-only and are never persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from weakref import ref

from ._campaign_provider_scope_devapp_identity import (
    _read_developer_account_identity,
)
from . import campaign_provider_scope_authority as _provider_scope
from .betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    ADAPTER_ID,
    ADAPTER_VERSION,
    BETTING_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from .bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    BookmakerPositionObservation,
    BookmakerPositionState,
)
from .bookmaker_integration_boundary import (
    BookmakerIntegrationEvidence,
    BookmakerIntegrationKind,
    bind_bookmaker_integration,
)


class AccountSnapshotAcquisitionError(RuntimeError):
    """Raised when durable acquisition authority cannot be established or verified."""


_ALLOWED_ACCOUNT_CAPABILITIES = frozenset(
    {
        BookmakerCapability.BALANCE_READ,
        BookmakerCapability.OPEN_POSITIONS_READ,
        BookmakerCapability.SETTLED_POSITIONS_READ,
    }
)
_SCHEMA_VERSION = 4
_INTEGRATION_SOURCE_REF = "autosport://betfair-account-readonly/official-api-v1"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _canonical_sha256(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _official_api_manifest_sha256() -> str:
    return _canonical_sha256(
        {
            "schema": "autosport.betfair-account-snapshot-integration",
            "schema_version": 1,
            "adapter_id": ADAPTER_ID,
            "adapter_version": ADAPTER_VERSION,
            "integration_kind": BookmakerIntegrationKind.OFFICIAL_API.value,
            "account_endpoint": ACCOUNT_JSON_RPC_ENDPOINT,
            "betting_endpoint": BETTING_JSON_RPC_ENDPOINT,
        }
    )


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AccountSnapshotAcquisitionError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _sha256_hex(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise AccountSnapshotAcquisitionError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return text


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AccountSnapshotAcquisitionError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AccountSnapshotAcquisitionError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _exact_keys(value: dict[str, object], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise AccountSnapshotAcquisitionError(
            f"{field} schema mismatch; missing={missing}, extra={extra}"
        )


def _load_json_object(text: str, field: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AccountSnapshotAcquisitionError(
                    f"{field} contains duplicate JSON key {key}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=reject_duplicates)
    except AccountSnapshotAcquisitionError:
        raise
    except (TypeError, json.JSONDecodeError) as exc:
        raise AccountSnapshotAcquisitionError(f"{field} is not valid JSON") from exc
    if type(value) is not dict:
        raise AccountSnapshotAcquisitionError(f"{field} must be a JSON object")
    return value


@dataclass(frozen=True, slots=True)
class AccountSnapshotAcquisitionReceipt:
    acquisition_id: str
    acquisition_request_id_sha256: str
    source_observation_id: str
    venue_id: str
    account_id: str
    authenticated_account_identity_sha256: str
    account_identity_observed_at: str
    adapter_id: str
    adapter_version: str
    integration_evidence_id: str
    integration_kind: str
    requested_capabilities: tuple[str, ...]
    snapshot_sha256: str
    snapshot_content_sha256: str
    source_payload_sha256: str
    acquired_at: str
    provider_observed_at: str | None
    source_authority_proven: bool
    provider_account_identity_proven: bool
    grants_execution_authority: bool
    grants_settlement_authority: bool
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field in (
            "acquisition_id",
            "acquisition_request_id_sha256",
            "source_observation_id",
            "authenticated_account_identity_sha256",
            "integration_evidence_id",
            "snapshot_sha256",
            "snapshot_content_sha256",
            "source_payload_sha256",
        ):
            _sha256_hex(getattr(self, field), field)
        for field in (
            "venue_id",
            "account_id",
            "adapter_id",
            "adapter_version",
            "integration_kind",
        ):
            _text(getattr(self, field), field)
        if self.integration_kind != BookmakerIntegrationKind.OFFICIAL_API.value:
            raise AccountSnapshotAcquisitionError(
                "integration_kind must be official_api for Betfair acquisition receipts"
            )
        if type(self.requested_capabilities) is not tuple or not self.requested_capabilities:
            raise AccountSnapshotAcquisitionError(
                "requested_capabilities must be a non-empty tuple"
            )
        if tuple(sorted(set(self.requested_capabilities))) != self.requested_capabilities:
            raise AccountSnapshotAcquisitionError(
                "requested_capabilities must be unique and sorted"
            )
        for capability in self.requested_capabilities:
            _text(capability, "requested_capabilities item")
        _timestamp(self.account_identity_observed_at, "account_identity_observed_at")
        _timestamp(self.acquired_at, "acquired_at")
        if self.provider_observed_at is not None:
            _timestamp(self.provider_observed_at, "provider_observed_at")
        if self.source_authority_proven is not False:
            raise AccountSnapshotAcquisitionError(
                "durable source_authority_proven must be exactly false"
            )
        if self.provider_account_identity_proven is not True:
            raise AccountSnapshotAcquisitionError(
                "provider_account_identity_proven must be exactly true"
            )
        for field in (
            "grants_execution_authority",
            "grants_settlement_authority",
        ):
            if getattr(self, field) is not False:
                raise AccountSnapshotAcquisitionError(f"{field} must be exactly false")
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise AccountSnapshotAcquisitionError(
                "schema_version must match the current acquisition schema"
            )


@dataclass(frozen=True, slots=True, weakref_slot=True)
class AuthoritativeAccountSnapshot:
    snapshot: BookmakerAccountSnapshot
    receipt: AccountSnapshotAcquisitionReceipt

    def __post_init__(self) -> None:
        if type(self.snapshot) is not BookmakerAccountSnapshot:
            raise AccountSnapshotAcquisitionError(
                "snapshot must be an exact BookmakerAccountSnapshot"
            )
        if type(self.receipt) is not AccountSnapshotAcquisitionReceipt:
            raise AccountSnapshotAcquisitionError(
                "receipt must be an exact AccountSnapshotAcquisitionReceipt"
            )

    @property
    def source_authority_proven(self) -> bool:
        try:
            assert_account_snapshot_acquisition_authoritative(self)
        except AccountSnapshotAcquisitionError:
            return False
        return True


def assert_account_snapshot_acquisition_authoritative(
    acquired: AuthoritativeAccountSnapshot,
) -> None:
    """Require live provider-origin issuance, not durable local self-attestation."""

    raise AccountSnapshotAcquisitionError(
        "account snapshot lacks live canonical provider-origin authority"
    )


class BetfairAccountSnapshotAcquirer:
    """Own the production Betfair read path and durable acquisition receipt store."""

    def __init__(
        self,
        database_path: str | Path,
        credentials: BetfairSessionCredentials,
        *,
        account_id: str = "default-account",
        timeout_seconds: float = 10.0,
    ) -> None:
        if type(credentials) is not BetfairSessionCredentials:
            raise TypeError("credentials must be an exact BetfairSessionCredentials")
        path = Path(database_path)
        if not path.name:
            raise ValueError("database_path must name a file")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._store = _AccountSnapshotStore(path)
        # No transport or clock injection is exposed here. Production authority is issued
        # only by the canonical client using its real default HTTPS transport and UTC clock.
        self._client = BetfairReadOnlyClient(
            credentials,
            timeout_seconds=timeout_seconds,
            venue_id="betfair",
            account_id=account_id,
        )

    def _read_provider_snapshot(
        self,
        client: BetfairReadOnlyClient,
        requested_capabilities: frozenset[BookmakerCapability],
        snapshot_reader: object,
    ) -> tuple[BookmakerAccountSnapshot, BookmakerIntegrationEvidence]:
        if type(requested_capabilities) is not frozenset:
            raise AccountSnapshotAcquisitionError(
                "requested_capabilities must be an exact frozenset"
            )
        if not requested_capabilities:
            raise AccountSnapshotAcquisitionError(
                "requested_capabilities must not be empty"
            )
        for capability in requested_capabilities:
            if type(capability) is not BookmakerCapability:
                raise AccountSnapshotAcquisitionError(
                    "requested_capabilities must contain exact BookmakerCapability values"
                )
        unsupported = requested_capabilities - _ALLOWED_ACCOUNT_CAPABILITIES
        if unsupported:
            names = ", ".join(sorted(capability.value for capability in unsupported))
            raise AccountSnapshotAcquisitionError(
                "account snapshot acquisition does not authorize capability: " + names
            )

        if type(client) is not BetfairReadOnlyClient:
            raise AccountSnapshotAcquisitionError(
                "account acquisition client is not the canonical BetfairReadOnlyClient"
            )
        if not callable(snapshot_reader):
            raise AccountSnapshotAcquisitionError(
                "canonical Betfair account snapshot reader is unavailable"
            )
        snapshot = snapshot_reader(client, requested_capabilities)
        if type(snapshot) is not BookmakerAccountSnapshot:
            raise AccountSnapshotAcquisitionError(
                "canonical Betfair client returned a non-canonical account snapshot"
            )
        if snapshot.observed_capabilities != requested_capabilities:
            raise AccountSnapshotAcquisitionError(
                "provider snapshot capability set drifted from the requested set"
            )
        if (
            snapshot.profile.adapter_id != ADAPTER_ID
            or snapshot.profile.adapter_version != ADAPTER_VERSION
        ):
            raise AccountSnapshotAcquisitionError(
                "provider snapshot adapter identity drifted from the canonical Betfair client"
            )

        integration = _official_api_integration(snapshot)
        return snapshot, integration

    def resolve(self, acquisition_id: str) -> AuthoritativeAccountSnapshot:
        return self._store.resolve(acquisition_id)

    def verify(
        self,
        snapshot: BookmakerAccountSnapshot,
        receipt: AccountSnapshotAcquisitionReceipt,
    ) -> None:
        if type(snapshot) is not BookmakerAccountSnapshot:
            raise AccountSnapshotAcquisitionError(
                "snapshot must be an exact BookmakerAccountSnapshot"
            )
        if type(receipt) is not AccountSnapshotAcquisitionReceipt:
            raise AccountSnapshotAcquisitionError(
                "receipt must be an exact AccountSnapshotAcquisitionReceipt"
            )
        resolved = self._store.resolve(receipt.acquisition_id)
        if resolved.receipt != receipt:
            raise AccountSnapshotAcquisitionError(
                "receipt does not match durable acquisition authority"
            )
        if _canonical_sha256(_snapshot_payload(snapshot, include_local_times=True)) != (
            resolved.receipt.snapshot_sha256
        ):
            raise AccountSnapshotAcquisitionError(
                "snapshot does not match durable acquisition receipt"
            )


def _official_api_integration(
    snapshot: BookmakerAccountSnapshot,
) -> BookmakerIntegrationEvidence:
    evidence = bind_bookmaker_integration(
        snapshot.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=snapshot.observed_at,
        source_ref=_INTEGRATION_SOURCE_REF,
        source_payload_sha256=_official_api_manifest_sha256(),
    )
    if evidence.integration_kind is not BookmakerIntegrationKind.OFFICIAL_API:
        raise AccountSnapshotAcquisitionError(
            "Betfair product acquisition must remain bound to the official API channel"
        )
    return evidence


class _AccountSnapshotStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS account_snapshot_acquisitions (
                    acquisition_request_id_sha256 TEXT PRIMARY KEY,
                    source_observation_id TEXT NOT NULL,
                    acquisition_id TEXT NOT NULL UNIQUE,
                    record_json TEXT NOT NULL,
                    record_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS account_snapshot_acquisitions_source_observation
                ON account_snapshot_acquisitions(source_observation_id);
                CREATE TRIGGER IF NOT EXISTS account_snapshot_acquisitions_no_update
                BEFORE UPDATE ON account_snapshot_acquisitions
                BEGIN
                    SELECT RAISE(ABORT, 'account snapshot acquisition rows are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS account_snapshot_acquisitions_no_delete
                BEFORE DELETE ON account_snapshot_acquisitions
                BEGIN
                    SELECT RAISE(ABORT, 'account snapshot acquisition rows are immutable');
                END;
                """
            )

    def record(
        self,
        snapshot: BookmakerAccountSnapshot,
        integration: BookmakerIntegrationEvidence,
        requested_capabilities: frozenset[BookmakerCapability],
        *,
        acquisition_request_id_sha256: str,
        authenticated_account_identity_sha256: str,
        account_identity_observed_at: str,
    ) -> AuthoritativeAccountSnapshot:
        if type(snapshot) is not BookmakerAccountSnapshot:
            raise AccountSnapshotAcquisitionError(
                "snapshot must be an exact BookmakerAccountSnapshot"
            )
        if type(integration) is not BookmakerIntegrationEvidence:
            raise AccountSnapshotAcquisitionError(
                "integration must be exact BookmakerIntegrationEvidence"
            )
        integration.verify_profile(snapshot.profile)
        acquisition_request_id_sha256 = _sha256_hex(
            acquisition_request_id_sha256,
            "acquisition_request_id_sha256",
        )
        authenticated_account_identity_sha256 = _sha256_hex(
            authenticated_account_identity_sha256,
            "authenticated_account_identity_sha256",
        )
        _timestamp(account_identity_observed_at, "account_identity_observed_at")

        requested = tuple(sorted(capability.value for capability in requested_capabilities))
        snapshot_payload = _snapshot_payload(snapshot, include_local_times=True)
        snapshot_content_payload = _snapshot_payload(snapshot, include_local_times=False)
        snapshot_sha256 = _canonical_sha256(snapshot_payload)
        snapshot_content_sha256 = _canonical_sha256(snapshot_content_payload)
        source_observation_id = _canonical_sha256(
            {
                "schema": "autosport.account-snapshot-source-observation",
                "schema_version": 1,
                "venue_id": snapshot.profile.venue_id,
                "account_id": snapshot.profile.account_id,
                "authenticated_account_identity_sha256": authenticated_account_identity_sha256,
                "adapter_id": snapshot.profile.adapter_id,
                "adapter_version": snapshot.profile.adapter_version,
                "integration_kind": integration.integration_kind.value,
                "requested_capabilities": list(requested),
                "source_payload_sha256": snapshot.profile.source_payload_sha256,
            }
        )
        base_record: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "acquisition_request_id_sha256": acquisition_request_id_sha256,
            "source_observation_id": source_observation_id,
            "venue_id": snapshot.profile.venue_id,
            "account_id": snapshot.profile.account_id,
            "authenticated_account_identity_sha256": authenticated_account_identity_sha256,
            "account_identity_observed_at": account_identity_observed_at,
            "adapter_id": snapshot.profile.adapter_id,
            "adapter_version": snapshot.profile.adapter_version,
            "integration_evidence": integration.to_canonical_dict(),
            "integration_evidence_id": integration.evidence_id,
            "requested_capabilities": list(requested),
            "snapshot": snapshot_payload,
            "snapshot_sha256": snapshot_sha256,
            "snapshot_content_sha256": snapshot_content_sha256,
            "source_payload_sha256": snapshot.profile.source_payload_sha256,
            "acquired_at": snapshot.observed_at,
            "provider_observed_at": None,
            # Durable local bytes prove integrity/history only. Remote provider
            # origin is an ephemeral exact-object capability issued below.
            "source_authority_proven": False,
            # Betfair account-details does not expose a first-party immutable account id.
            "provider_account_identity_proven": True,
            "grants_execution_authority": False,
            "grants_settlement_authority": False,
        }
        acquisition_id = _canonical_sha256(base_record)
        record = dict(base_record)
        record["acquisition_id"] = acquisition_id
        record_json = _canonical_json(record)
        record_sha256 = sha256(record_json.encode("utf-8")).hexdigest()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT acquisition_request_id_sha256, source_observation_id,
                       acquisition_id, record_json, record_sha256
                FROM account_snapshot_acquisitions
                WHERE acquisition_request_id_sha256 = ?
                """,
                (acquisition_request_id_sha256,),
            ).fetchone()
            if existing is not None:
                resolved = self._decode_row(existing)
                if (
                    resolved.receipt.source_observation_id != source_observation_id
                    or resolved.receipt.snapshot_sha256 != snapshot_sha256
                    or resolved.receipt.snapshot_content_sha256 != snapshot_content_sha256
                ):
                    raise AccountSnapshotAcquisitionError(
                        "same acquisition request id produced conflicting provider evidence"
                    )
                return resolved

            connection.execute(
                """
                INSERT INTO account_snapshot_acquisitions
                    (
                        acquisition_request_id_sha256,
                        source_observation_id,
                        acquisition_id,
                        record_json,
                        record_sha256
                    )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    acquisition_request_id_sha256,
                    source_observation_id,
                    acquisition_id,
                    record_json,
                    record_sha256,
                ),
            )
        return self.resolve(acquisition_id)

    def resolve(self, acquisition_id: str) -> AuthoritativeAccountSnapshot:
        acquisition_id = _sha256_hex(acquisition_id, "acquisition_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT acquisition_request_id_sha256, source_observation_id,
                       acquisition_id, record_json, record_sha256
                FROM account_snapshot_acquisitions
                WHERE acquisition_id = ?
                """,
                (acquisition_id,),
            ).fetchone()
        if row is None:
            raise AccountSnapshotAcquisitionError(
                "unknown durable account snapshot acquisition"
            )
        return self._decode_row(row)

    def resolve_request(
        self,
        acquisition_request_id_sha256: str,
    ) -> AuthoritativeAccountSnapshot | None:
        acquisition_request_id_sha256 = _sha256_hex(
            acquisition_request_id_sha256,
            "acquisition_request_id_sha256",
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT acquisition_request_id_sha256, source_observation_id,
                       acquisition_id, record_json, record_sha256
                FROM account_snapshot_acquisitions
                WHERE acquisition_request_id_sha256 = ?
                """,
                (acquisition_request_id_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_row(row)

    def _decode_row(self, row: tuple[object, ...]) -> AuthoritativeAccountSnapshot:
        if len(row) != 5 or any(type(value) is not str for value in row):
            raise AccountSnapshotAcquisitionError(
                "durable account snapshot acquisition row has invalid storage types"
            )
        (
            acquisition_request_id_sha256,
            source_observation_id,
            acquisition_id,
            record_json,
            record_sha256,
        ) = row
        assert isinstance(acquisition_request_id_sha256, str)
        assert isinstance(source_observation_id, str)
        assert isinstance(acquisition_id, str)
        assert isinstance(record_json, str)
        assert isinstance(record_sha256, str)

        _sha256_hex(
            acquisition_request_id_sha256,
            "acquisition_request_id_sha256",
        )
        _sha256_hex(source_observation_id, "source_observation_id")
        _sha256_hex(acquisition_id, "acquisition_id")
        _sha256_hex(record_sha256, "record_sha256")
        if sha256(record_json.encode("utf-8")).hexdigest() != record_sha256:
            raise AccountSnapshotAcquisitionError(
                "durable acquisition record digest mismatch"
            )

        record = _load_json_object(record_json, "record_json")
        expected_keys = {
            "schema_version",
            "acquisition_request_id_sha256",
            "source_observation_id",
            "acquisition_id",
            "venue_id",
            "account_id",
            "authenticated_account_identity_sha256",
            "account_identity_observed_at",
            "adapter_id",
            "adapter_version",
            "integration_evidence",
            "integration_evidence_id",
            "requested_capabilities",
            "snapshot",
            "snapshot_sha256",
            "snapshot_content_sha256",
            "source_payload_sha256",
            "acquired_at",
            "provider_observed_at",
            "source_authority_proven",
            "provider_account_identity_proven",
            "grants_execution_authority",
            "grants_settlement_authority",
        }
        _exact_keys(record, expected_keys, "record_json")
        if (
            type(record["schema_version"]) is not int
            or record["schema_version"] != _SCHEMA_VERSION
        ):
            raise AccountSnapshotAcquisitionError(
                "durable acquisition schema_version mismatch"
            )
        if (
            record["acquisition_request_id_sha256"]
            != acquisition_request_id_sha256
        ):
            raise AccountSnapshotAcquisitionError(
                "durable acquisition request id column mismatch"
            )
        if record["source_observation_id"] != source_observation_id:
            raise AccountSnapshotAcquisitionError(
                "durable acquisition source observation key mismatch"
            )
        if record["acquisition_id"] != acquisition_id:
            raise AccountSnapshotAcquisitionError(
                "durable acquisition id column mismatch"
            )

        base_record = dict(record)
        del base_record["acquisition_id"]
        if _canonical_sha256(base_record) != acquisition_id:
            raise AccountSnapshotAcquisitionError(
                "durable acquisition identity digest mismatch"
            )

        snapshot_raw = record["snapshot"]
        if type(snapshot_raw) is not dict:
            raise AccountSnapshotAcquisitionError("snapshot must be a JSON object")
        snapshot = _snapshot_from_payload(snapshot_raw)
        full_sha = _canonical_sha256(
            _snapshot_payload(snapshot, include_local_times=True)
        )
        content_sha = _canonical_sha256(
            _snapshot_payload(snapshot, include_local_times=False)
        )
        if record["snapshot_sha256"] != full_sha:
            raise AccountSnapshotAcquisitionError(
                "durable snapshot digest mismatch"
            )
        if record["snapshot_content_sha256"] != content_sha:
            raise AccountSnapshotAcquisitionError(
                "durable snapshot content digest mismatch"
            )

        integration_raw = record["integration_evidence"]
        if type(integration_raw) is not dict:
            raise AccountSnapshotAcquisitionError(
                "integration_evidence must be a JSON object"
            )
        integration = _integration_from_payload(integration_raw)
        integration.verify_profile(snapshot.profile)
        if record["integration_evidence_id"] != integration.evidence_id:
            raise AccountSnapshotAcquisitionError(
                "durable integration evidence digest mismatch"
            )
        if integration.integration_kind is not BookmakerIntegrationKind.OFFICIAL_API:
            raise AccountSnapshotAcquisitionError(
                "durable Betfair acquisition is not bound to official API evidence"
            )
        if (
            integration.source_ref != _INTEGRATION_SOURCE_REF
            or integration.source_payload_sha256 != _official_api_manifest_sha256()
        ):
            raise AccountSnapshotAcquisitionError(
                "durable Betfair integration manifest drifted from canonical authority"
            )

        requested_raw = record["requested_capabilities"]
        if type(requested_raw) is not list or not requested_raw:
            raise AccountSnapshotAcquisitionError(
                "requested_capabilities must be a non-empty JSON array"
            )
        if any(type(value) is not str for value in requested_raw):
            raise AccountSnapshotAcquisitionError(
                "requested_capabilities entries must be strings"
            )
        requested = tuple(requested_raw)
        if requested != tuple(sorted(set(requested))):
            raise AccountSnapshotAcquisitionError(
                "requested_capabilities must be sorted and unique"
            )
        try:
            requested_enums = frozenset(BookmakerCapability(value) for value in requested)
        except ValueError as exc:
            raise AccountSnapshotAcquisitionError(
                "requested_capabilities contains an unknown capability"
            ) from exc
        if requested_enums != snapshot.observed_capabilities:
            raise AccountSnapshotAcquisitionError(
                "durable requested capabilities do not match snapshot"
            )
        if requested_enums - _ALLOWED_ACCOUNT_CAPABILITIES:
            raise AccountSnapshotAcquisitionError(
                "durable snapshot contains a capability outside account acquisition authority"
            )

        expected_source_observation_id = _canonical_sha256(
            {
                "schema": "autosport.account-snapshot-source-observation",
                "schema_version": 1,
                "venue_id": snapshot.profile.venue_id,
                "account_id": snapshot.profile.account_id,
                "authenticated_account_identity_sha256": record["authenticated_account_identity_sha256"],
                "adapter_id": snapshot.profile.adapter_id,
                "adapter_version": snapshot.profile.adapter_version,
                "integration_kind": integration.integration_kind.value,
                "requested_capabilities": list(requested),
                "source_payload_sha256": snapshot.profile.source_payload_sha256,
            }
        )
        if source_observation_id != expected_source_observation_id:
            raise AccountSnapshotAcquisitionError(
                "durable source observation identity mismatch"
            )
        if record["source_payload_sha256"] != snapshot.profile.source_payload_sha256:
            raise AccountSnapshotAcquisitionError(
                "durable source payload digest drifted from snapshot profile"
            )

        receipt = AccountSnapshotAcquisitionReceipt(
            acquisition_id=acquisition_id,
            acquisition_request_id_sha256=acquisition_request_id_sha256,
            source_observation_id=source_observation_id,
            venue_id=_text(record["venue_id"], "venue_id"),
            account_id=_text(record["account_id"], "account_id"),
            authenticated_account_identity_sha256=_sha256_hex(
                record["authenticated_account_identity_sha256"],
                "authenticated_account_identity_sha256",
            ),
            account_identity_observed_at=_text(
                record["account_identity_observed_at"],
                "account_identity_observed_at",
            ),
            adapter_id=_text(record["adapter_id"], "adapter_id"),
            adapter_version=_text(record["adapter_version"], "adapter_version"),
            integration_evidence_id=_sha256_hex(
                record["integration_evidence_id"], "integration_evidence_id"
            ),
            integration_kind=integration.integration_kind.value,
            requested_capabilities=requested,
            snapshot_sha256=_sha256_hex(record["snapshot_sha256"], "snapshot_sha256"),
            snapshot_content_sha256=_sha256_hex(
                record["snapshot_content_sha256"], "snapshot_content_sha256"
            ),
            source_payload_sha256=_sha256_hex(
                record["source_payload_sha256"], "source_payload_sha256"
            ),
            acquired_at=_text(record["acquired_at"], "acquired_at"),
            provider_observed_at=record["provider_observed_at"],
            source_authority_proven=record["source_authority_proven"],
            provider_account_identity_proven=record[
                "provider_account_identity_proven"
            ],
            grants_execution_authority=record["grants_execution_authority"],
            grants_settlement_authority=record["grants_settlement_authority"],
            schema_version=record["schema_version"],
        )
        if (
            receipt.venue_id != snapshot.profile.venue_id
            or receipt.account_id != snapshot.profile.account_id
            or receipt.adapter_id != snapshot.profile.adapter_id
            or receipt.adapter_version != snapshot.profile.adapter_version
        ):
            raise AccountSnapshotAcquisitionError(
                "durable receipt identity does not match snapshot profile"
            )
        if receipt.acquired_at != snapshot.observed_at:
            raise AccountSnapshotAcquisitionError(
                "durable receipt acquired_at does not match snapshot receive time"
            )
        return AuthoritativeAccountSnapshot(snapshot, receipt)


def _integration_from_payload(
    payload: dict[str, object],
) -> BookmakerIntegrationEvidence:
    expected = {
        "adapter_id",
        "adapter_version",
        "integration_kind",
        "observed_at",
        "profile_id",
        "schema_version",
        "source_payload_sha256",
        "source_ref",
        "venue_id",
    }
    _exact_keys(payload, expected, "integration_evidence")
    try:
        kind = BookmakerIntegrationKind(payload["integration_kind"])
    except (TypeError, ValueError) as exc:
        raise AccountSnapshotAcquisitionError(
            "integration evidence has unknown integration_kind"
        ) from exc
    try:
        return BookmakerIntegrationEvidence(
            venue_id=payload["venue_id"],
            adapter_id=payload["adapter_id"],
            adapter_version=payload["adapter_version"],
            profile_id=payload["profile_id"],
            integration_kind=kind,
            observed_at=payload["observed_at"],
            source_ref=payload["source_ref"],
            source_payload_sha256=payload["source_payload_sha256"],
            schema_version=payload["schema_version"],
        )
    except (TypeError, ValueError) as exc:
        raise AccountSnapshotAcquisitionError(
            "integration evidence is invalid"
        ) from exc


def _snapshot_payload(
    snapshot: BookmakerAccountSnapshot,
    *,
    include_local_times: bool,
) -> dict[str, object]:
    if type(snapshot) is not BookmakerAccountSnapshot:
        raise AccountSnapshotAcquisitionError(
            "snapshot must be an exact BookmakerAccountSnapshot"
        )

    def observed(value: str) -> str | None:
        return value if include_local_times else None

    profile = snapshot.profile.to_canonical_dict()
    profile["observed_at"] = observed(snapshot.profile.observed_at)

    balance: dict[str, object] | None = None
    if snapshot.balance is not None:
        item = snapshot.balance
        balance = {
            "venue_id": item.venue_id,
            "account_id": item.account_id,
            "adapter_id": item.adapter_id,
            "observation_id": item.observation_id,
            "currency": item.currency,
            "available_balance": str(item.available_balance),
            "observed_at": observed(item.observed_at),
            "source_payload_sha256": item.source_payload_sha256,
            "total_balance": None if item.total_balance is None else str(item.total_balance),
            "total_balance_source_ref": item.total_balance_source_ref,
            "exposure": None if item.exposure is None else str(item.exposure),
            "retained_commission": (
                None
                if item.retained_commission is None
                else str(item.retained_commission)
            ),
            "exposure_limit": (
                None if item.exposure_limit is None else str(item.exposure_limit)
            ),
        }

    def position_payload(item: BookmakerPositionObservation) -> dict[str, object]:
        return {
            "venue_id": item.venue_id,
            "account_id": item.account_id,
            "adapter_id": item.adapter_id,
            "observation_id": item.observation_id,
            "external_position_id": item.external_position_id,
            "state": item.state.value,
            "currency": item.currency,
            "observed_at": observed(item.observed_at),
            "source_payload_sha256": item.source_payload_sha256,
            "provider_amount": str(item.provider_amount),
            "provider_amount_semantics": item.provider_amount_semantics,
            "provider_side": item.provider_side,
            "decimal_odds": None if item.decimal_odds is None else str(item.decimal_odds),
            "gross_return": None if item.gross_return is None else str(item.gross_return),
            "external_receipt_id": item.external_receipt_id,
        }

    open_positions = sorted(
        (position_payload(item) for item in snapshot.open_positions),
        key=lambda item: (str(item["external_position_id"]), str(item["observation_id"])),
    )
    settled_positions = sorted(
        (position_payload(item) for item in snapshot.settled_positions),
        key=lambda item: (str(item["external_position_id"]), str(item["observation_id"])),
    )
    return {
        "profile": profile,
        "observed_capabilities": sorted(
            capability.value for capability in snapshot.observed_capabilities
        ),
        "observed_at": observed(snapshot.observed_at),
        "balance": balance,
        "open_positions": open_positions,
        "settled_positions": settled_positions,
    }


def _snapshot_from_payload(payload: dict[str, object]) -> BookmakerAccountSnapshot:
    _exact_keys(
        payload,
        {
            "profile",
            "observed_capabilities",
            "observed_at",
            "balance",
            "open_positions",
            "settled_positions",
        },
        "snapshot",
    )
    profile_raw = payload["profile"]
    if type(profile_raw) is not dict:
        raise AccountSnapshotAcquisitionError("snapshot.profile must be an object")
    _exact_keys(
        profile_raw,
        {
            "account_id",
            "adapter_id",
            "adapter_version",
            "facts",
            "observed_at",
            "profile_version",
            "source_payload_sha256",
            "source_ref",
            "venue_id",
        },
        "snapshot.profile",
    )
    facts_raw = profile_raw["facts"]
    if type(facts_raw) is not list:
        raise AccountSnapshotAcquisitionError("snapshot.profile.facts must be an array")
    facts: list[BookmakerCapabilityFact] = []
    for raw in facts_raw:
        if type(raw) is not dict:
            raise AccountSnapshotAcquisitionError(
                "snapshot.profile.facts entries must be objects"
            )
        _exact_keys(raw, {"capability", "state"}, "snapshot.profile.fact")
        try:
            facts.append(
                BookmakerCapabilityFact(
                    BookmakerCapability(raw["capability"]),
                    BookmakerCapabilityState(raw["state"]),
                )
            )
        except (TypeError, ValueError) as exc:
            raise AccountSnapshotAcquisitionError(
                "snapshot.profile fact is invalid"
            ) from exc
    try:
        profile = BookmakerCapabilityProfile(
            venue_id=profile_raw["venue_id"],
            account_id=profile_raw["account_id"],
            adapter_id=profile_raw["adapter_id"],
            adapter_version=profile_raw["adapter_version"],
            profile_version=profile_raw["profile_version"],
            facts=tuple(facts),
            observed_at=profile_raw["observed_at"],
            source_ref=profile_raw["source_ref"],
            source_payload_sha256=profile_raw["source_payload_sha256"],
        )
    except (TypeError, ValueError) as exc:
        raise AccountSnapshotAcquisitionError("snapshot.profile is invalid") from exc

    capabilities_raw = payload["observed_capabilities"]
    if type(capabilities_raw) is not list:
        raise AccountSnapshotAcquisitionError(
            "snapshot.observed_capabilities must be an array"
        )
    try:
        capabilities = frozenset(BookmakerCapability(value) for value in capabilities_raw)
    except (TypeError, ValueError) as exc:
        raise AccountSnapshotAcquisitionError(
            "snapshot.observed_capabilities is invalid"
        ) from exc
    if len(capabilities) != len(capabilities_raw):
        raise AccountSnapshotAcquisitionError(
            "snapshot.observed_capabilities contains duplicates"
        )

    balance_raw = payload["balance"]
    balance = None
    if balance_raw is not None:
        if type(balance_raw) is not dict:
            raise AccountSnapshotAcquisitionError("snapshot.balance must be an object")
        _exact_keys(
            balance_raw,
            {
                "venue_id",
                "account_id",
                "adapter_id",
                "observation_id",
                "currency",
                "available_balance",
                "observed_at",
                "source_payload_sha256",
                "total_balance",
                "total_balance_source_ref",
                "exposure",
                "retained_commission",
                "exposure_limit",
            },
            "snapshot.balance",
        )
        try:
            balance = BookmakerBalanceObservation(
                venue_id=balance_raw["venue_id"],
                account_id=balance_raw["account_id"],
                adapter_id=balance_raw["adapter_id"],
                observation_id=balance_raw["observation_id"],
                currency=balance_raw["currency"],
                available_balance=Decimal(balance_raw["available_balance"]),
                observed_at=balance_raw["observed_at"],
                source_payload_sha256=balance_raw["source_payload_sha256"],
                total_balance=(
                    None
                    if balance_raw["total_balance"] is None
                    else Decimal(balance_raw["total_balance"])
                ),
                total_balance_source_ref=balance_raw["total_balance_source_ref"],
                exposure=(
                    None
                    if balance_raw["exposure"] is None
                    else Decimal(balance_raw["exposure"])
                ),
                retained_commission=(
                    None
                    if balance_raw["retained_commission"] is None
                    else Decimal(balance_raw["retained_commission"])
                ),
                exposure_limit=(
                    None
                    if balance_raw["exposure_limit"] is None
                    else Decimal(balance_raw["exposure_limit"])
                ),
            )
        except (TypeError, ValueError) as exc:
            raise AccountSnapshotAcquisitionError("snapshot.balance is invalid") from exc

    def positions(value: object, field: str) -> tuple[BookmakerPositionObservation, ...]:
        if type(value) is not list:
            raise AccountSnapshotAcquisitionError(f"snapshot.{field} must be an array")
        result: list[BookmakerPositionObservation] = []
        expected = {
            "venue_id",
            "account_id",
            "adapter_id",
            "observation_id",
            "external_position_id",
            "state",
            "currency",
            "observed_at",
            "source_payload_sha256",
            "provider_amount",
            "provider_amount_semantics",
            "provider_side",
            "decimal_odds",
            "gross_return",
            "external_receipt_id",
        }
        for raw in value:
            if type(raw) is not dict:
                raise AccountSnapshotAcquisitionError(
                    f"snapshot.{field} entries must be objects"
                )
            _exact_keys(raw, expected, f"snapshot.{field} entry")
            try:
                result.append(
                    BookmakerPositionObservation(
                        venue_id=raw["venue_id"],
                        account_id=raw["account_id"],
                        adapter_id=raw["adapter_id"],
                        observation_id=raw["observation_id"],
                        external_position_id=raw["external_position_id"],
                        state=BookmakerPositionState(raw["state"]),
                        currency=raw["currency"],
                        observed_at=raw["observed_at"],
                        source_payload_sha256=raw["source_payload_sha256"],
                        provider_amount=Decimal(raw["provider_amount"]),
                        provider_amount_semantics=raw["provider_amount_semantics"],
                        provider_side=raw["provider_side"],
                        decimal_odds=(
                            None
                            if raw["decimal_odds"] is None
                            else Decimal(raw["decimal_odds"])
                        ),
                        gross_return=(
                            None
                            if raw["gross_return"] is None
                            else Decimal(raw["gross_return"])
                        ),
                        external_receipt_id=raw["external_receipt_id"],
                    )
                )
            except (TypeError, ValueError) as exc:
                raise AccountSnapshotAcquisitionError(
                    f"snapshot.{field} entry is invalid"
                ) from exc
        return tuple(result)

    try:
        return BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=capabilities,
            observed_at=payload["observed_at"],
            balance=balance,
            open_positions=positions(payload["open_positions"], "open_positions"),
            settled_positions=positions(
                payload["settled_positions"], "settled_positions"
            ),
        )
    except (TypeError, ValueError) as exc:
        raise AccountSnapshotAcquisitionError("snapshot is invalid") from exc



# Bind positive acquisition authority to the exact product-owned provider read.  The raw
# provider-read function and raw durable-record function are captured only by this closure,
# then removed from their classes.  This mirrors the canonical execution-readback issuance
# pattern: callers can resolve/verify durable evidence, but cannot pass an arbitrary
# caller-constructed BookmakerAccountSnapshot to a minting function.
def _install_account_snapshot_acquisition_authority() -> None:
    issued: dict[int, tuple[object, _AccountSnapshotStore, BetfairReadOnlyClient]] = {}
    live_issued: dict[str, tuple[object, str]] = {}
    raw_init = BetfairAccountSnapshotAcquirer.__init__
    raw_read = BetfairAccountSnapshotAcquirer._read_provider_snapshot
    raw_record = _AccountSnapshotStore.record
    raw_resolve = _AccountSnapshotStore.resolve
    raw_resolve_request = _AccountSnapshotStore.resolve_request
    canonical_snapshot_read = BetfairReadOnlyClient.read_account_snapshot

    def live_fingerprint(acquired: AuthoritativeAccountSnapshot) -> str:
        return _canonical_sha256(
            {
                "acquisition_id": acquired.receipt.acquisition_id,
                "acquisition_request_id_sha256": (
                    acquired.receipt.acquisition_request_id_sha256
                ),
                "snapshot_sha256": acquired.receipt.snapshot_sha256,
            }
        )

    def forget_live(acquisition_id: str, reference: object) -> None:
        current = live_issued.get(acquisition_id)
        if current is not None and current[0] is reference:
            live_issued.pop(acquisition_id, None)

    def issue_live(
        acquired: AuthoritativeAccountSnapshot,
    ) -> AuthoritativeAccountSnapshot:
        acquisition_id = acquired.receipt.acquisition_id
        reference = ref(
            acquired,
            lambda current, acquisition_id=acquisition_id: forget_live(
                acquisition_id, current
            ),
        )
        live_issued[acquisition_id] = (
            reference,
            live_fingerprint(acquired),
        )
        return acquired

    def current_live(
        acquisition_id: str,
    ) -> AuthoritativeAccountSnapshot | None:
        current = live_issued.get(acquisition_id)
        if current is None:
            return None
        value = current[0]()
        if value is None:
            live_issued.pop(acquisition_id, None)
            return None
        if (
            type(value) is not AuthoritativeAccountSnapshot
            or current[1] != live_fingerprint(value)
        ):
            live_issued.pop(acquisition_id, None)
            return None
        return value

    def assert_live(
        acquired: AuthoritativeAccountSnapshot,
    ) -> None:
        if type(acquired) is not AuthoritativeAccountSnapshot:
            raise AccountSnapshotAcquisitionError(
                "provider-origin authority requires exact acquired snapshot evidence"
            )
        current = current_live(acquired.receipt.acquisition_id)
        if current is not acquired:
            raise AccountSnapshotAcquisitionError(
                "account snapshot was not issued by live canonical provider acquisition"
            )

    def state(
        self: BetfairAccountSnapshotAcquirer,
    ) -> tuple[_AccountSnapshotStore, BetfairReadOnlyClient]:
        record = issued.get(id(self))
        if record is None or record[0]() is not self:
            raise AccountSnapshotAcquisitionError(
                "account snapshot acquirer was not initialized by canonical product authority"
            )
        return record[1], record[2]

    def __init__(
        self: BetfairAccountSnapshotAcquirer,
        database_path: str | Path,
        credentials: BetfairSessionCredentials,
        *,
        account_id: str = "default-account",
        timeout_seconds: float = 10.0,
    ) -> None:
        raw_init(
            self,
            database_path,
            credentials,
            account_id=account_id,
            timeout_seconds=timeout_seconds,
        )
        store = self._store
        client = self._client
        del self._store
        del self._client
        instance_id = id(self)

        def forget(_weakref: object, *, key: int = instance_id) -> None:
            issued.pop(key, None)

        issued[instance_id] = (ref(self, forget), store, client)

    def acquire(
        self: BetfairAccountSnapshotAcquirer,
        requested_capabilities: frozenset[BookmakerCapability],
        *,
        acquisition_id: str,
    ) -> AuthoritativeAccountSnapshot:
        store, client = state(self)
        acquisition_id = _text(acquisition_id, "acquisition_id")
        if type(requested_capabilities) is not frozenset:
            raise AccountSnapshotAcquisitionError(
                "requested_capabilities must be an exact frozenset"
            )
        if not requested_capabilities:
            raise AccountSnapshotAcquisitionError(
                "requested_capabilities must not be empty"
            )
        for capability in requested_capabilities:
            if type(capability) is not BookmakerCapability:
                raise AccountSnapshotAcquisitionError(
                    "requested_capabilities must contain exact BookmakerCapability values"
                )
        unsupported = requested_capabilities - _ALLOWED_ACCOUNT_CAPABILITIES
        if unsupported:
            names = ", ".join(
                sorted(capability.value for capability in unsupported)
            )
            raise AccountSnapshotAcquisitionError(
                "account snapshot acquisition does not authorize capability: "
                + names
            )

        acquisition_request_id_sha256 = _canonical_sha256(
            {
                "schema": "autosport.account-snapshot-acquisition-request",
                "schema_version": 1,
                "acquisition_id": acquisition_id,
            }
        )

        existing = raw_resolve_request(
            store,
            acquisition_request_id_sha256,
        )
        if existing is not None:
            expected_requested = tuple(
                sorted(
                    capability.value
                    for capability in requested_capabilities
                )
            )
            if (
                existing.receipt.venue_id != getattr(client, "_venue_id", None)
                or existing.receipt.account_id != getattr(client, "_account_id", None)
                or existing.receipt.adapter_id != ADAPTER_ID
                or existing.receipt.adapter_version != ADAPTER_VERSION
                or existing.receipt.requested_capabilities != expected_requested
            ):
                raise AccountSnapshotAcquisitionError(
                    "acquisition_id cannot be reused for another provider/account/capability scope"
                )
            live = current_live(existing.receipt.acquisition_id)
            if live is not None:
                return live
            raise AccountSnapshotAcquisitionError(
                "durable acquisition cannot reissue provider-origin authority; "
                "use a new acquisition_id for a new provider read"
            )

        try:
            account_identity = _read_developer_account_identity(client)
        except _provider_scope.CampaignProviderScopeError as exc:
            raise AccountSnapshotAcquisitionError(
                "authenticated Betfair account identity is unavailable"
            ) from exc

        snapshot, integration = raw_read(
            self,
            client,
            requested_capabilities,
            canonical_snapshot_read,
        )
        return issue_live(
            raw_record(
                store,
                snapshot,
                integration,
                requested_capabilities,
                acquisition_request_id_sha256=acquisition_request_id_sha256,
                authenticated_account_identity_sha256=account_identity.account_identity_sha256,
                account_identity_observed_at=account_identity.observed_at,
            )
        )

    def resolve(
        self: BetfairAccountSnapshotAcquirer,
        acquisition_id: str,
    ) -> AuthoritativeAccountSnapshot:
        store, _ = state(self)
        return raw_resolve(store, acquisition_id)

    def verify(
        self: BetfairAccountSnapshotAcquirer,
        snapshot: BookmakerAccountSnapshot,
        receipt: AccountSnapshotAcquisitionReceipt,
    ) -> None:
        if type(snapshot) is not BookmakerAccountSnapshot:
            raise AccountSnapshotAcquisitionError(
                "snapshot must be an exact BookmakerAccountSnapshot"
            )
        if type(receipt) is not AccountSnapshotAcquisitionReceipt:
            raise AccountSnapshotAcquisitionError(
                "receipt must be an exact AccountSnapshotAcquisitionReceipt"
            )
        store, _ = state(self)
        resolved = raw_resolve(store, receipt.acquisition_id)
        if resolved.receipt != receipt:
            raise AccountSnapshotAcquisitionError(
                "receipt does not match durable acquisition authority"
            )
        if _canonical_sha256(_snapshot_payload(snapshot, include_local_times=True)) != (
            resolved.receipt.snapshot_sha256
        ):
            raise AccountSnapshotAcquisitionError(
                "snapshot does not match durable acquisition receipt"
            )

    globals()["assert_account_snapshot_acquisition_authoritative"] = assert_live
    BetfairAccountSnapshotAcquirer.__init__ = __init__
    BetfairAccountSnapshotAcquirer.acquire = acquire
    BetfairAccountSnapshotAcquirer.resolve = resolve
    BetfairAccountSnapshotAcquirer.verify = verify
    delattr(BetfairAccountSnapshotAcquirer, "_read_provider_snapshot")
    delattr(_AccountSnapshotStore, "record")
    delattr(_AccountSnapshotStore, "resolve_request")


_install_account_snapshot_acquisition_authority()
del _install_account_snapshot_acquisition_authority
