"""Durable product-owned bookmaker account-snapshot acquisition authority.

This module composes the existing canonical Betfair read-only adapter with the
canonical integration evidence vocabulary.  Positive acquisition evidence can
only be created by invoking that adapter through this store; callers cannot
submit an already-constructed BookmakerAccountSnapshot for authorization.

The resulting receipt proves only that the configured product read path
observed the exact durable snapshot.  It never grants allocation, settlement,
execution, transfer, cross-provider atomicity, or real-money authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any

from ._campaign_provider_scope_devapp_identity import (
    _read_developer_account_identity,
)
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
)


_SCHEMA_VERSION = 1
_ALLOWED_SNAPSHOT_CAPABILITIES = frozenset(
    {
        BookmakerCapability.BALANCE_READ,
        BookmakerCapability.OPEN_POSITIONS_READ,
        BookmakerCapability.SETTLED_POSITIONS_READ,
    }
)

# Capture the canonical class dispatch surface at import time.  This prevents a
# caller from shadowing instance methods after constructing a canonical client
# and then using the acquisition store to bless caller-generated DTOs.
_CANONICAL_BETFAIR_METHODS = {
    name: getattr(BetfairReadOnlyClient, name)
    for name in (
        "read_account_snapshot",
        "read_account_details",
        "read_account_funds",
        "read_current_orders_page",
        "read_cleared_orders_page",
        "_read_all_current_orders_with_evidence",
        "_read_all_cleared_orders_with_evidence",
        "_to_position",
        "_rpc",
        "_observed_at",
    )
}
_CANONICAL_READ_ACCOUNT_SNAPSHOT = _CANONICAL_BETFAIR_METHODS[
    "read_account_snapshot"
]


class BookmakerAccountAcquisitionError(ValueError):
    """Raised when durable account acquisition evidence is invalid."""


@dataclass(frozen=True, slots=True)
class BookmakerAccountAcquisitionReceipt:
    """Audit receipt for one exact durable product-owned account snapshot.

    A receipt object by itself is not durable authority.  Authority-bearing use
    must resolve the receipt id through BookmakerAccountAcquisitionStore so the
    persisted record, digests, scope, integration evidence, and snapshot are
    all revalidated after restart.
    """

    receipt_id: str
    acquisition_id: str
    observation_key: str
    venue_id: str
    account_id: str
    authenticated_account_identity_sha256: str
    account_identity_observed_at: str
    adapter_id: str
    adapter_version: str
    profile_id: str
    integration_evidence_id: str
    integration_kind: BookmakerIntegrationKind
    requested_capabilities: tuple[BookmakerCapability, ...]
    provider_response_sha256: str
    snapshot_payload_sha256: str
    snapshot_semantic_sha256: str
    acquired_at: str
    provider_native_observed_at: str | None

    @property
    def source_authority_proven(self) -> bool:
        # A durable receipt is integrity/audit evidence, not remote-provider
        # provenance. Positive origin is an ephemeral exact-object capability.
        return False

    @property
    def allocation_authority_proven(self) -> bool:
        return False

    @property
    def atomicity_proven(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False

    def verify_snapshot(self, snapshot: BookmakerAccountSnapshot) -> None:
        """Verify that a supplied snapshot is the exact payload this receipt binds."""

        _validate_exact_snapshot(snapshot)
        payload = _snapshot_payload(snapshot)
        if _digest(payload) != self.snapshot_payload_sha256:
            raise BookmakerAccountAcquisitionError(
                "snapshot payload does not match acquisition receipt"
            )
        expected_scope = (
            self.venue_id,
            self.account_id,
            self.adapter_id,
            self.adapter_version,
            self.profile_id,
            self.provider_response_sha256,
        )
        actual_scope = (
            snapshot.profile.venue_id,
            snapshot.profile.account_id,
            snapshot.profile.adapter_id,
            snapshot.profile.adapter_version,
            snapshot.profile.profile_id,
            snapshot.profile.source_payload_sha256,
        )
        if actual_scope != expected_scope:
            raise BookmakerAccountAcquisitionError(
                "snapshot scope does not match acquisition receipt"
            )


@dataclass(frozen=True, slots=True, weakref_slot=True)
class AcquiredBookmakerAccountSnapshot:
    """Exact typed snapshot re-resolved from durable acquisition authority."""

    receipt: BookmakerAccountAcquisitionReceipt
    snapshot: BookmakerAccountSnapshot

    def __post_init__(self) -> None:
        if type(self.receipt) is not BookmakerAccountAcquisitionReceipt:
            raise BookmakerAccountAcquisitionError(
                "receipt must be an exact BookmakerAccountAcquisitionReceipt"
            )
        if type(self.snapshot) is not BookmakerAccountSnapshot:
            raise BookmakerAccountAcquisitionError(
                "snapshot must be an exact BookmakerAccountSnapshot"
            )
        self.receipt.verify_snapshot(self.snapshot)

    @property
    def source_authority_proven(self) -> bool:
        return _is_bookmaker_account_acquisition_authoritative(self)

    @property
    def allocation_authority_proven(self) -> bool:
        return False

    @property
    def atomicity_proven(self) -> bool:
        return False


def assert_bookmaker_account_acquisition_authoritative(
    acquired: AcquiredBookmakerAccountSnapshot,
) -> None:
    """Reject values that lack live provider-origin acquisition authority."""

    raise BookmakerAccountAcquisitionError(
        "bookmaker account acquisition has no live provider-origin authority"
    )


def _is_bookmaker_account_acquisition_authoritative(
    acquired: AcquiredBookmakerAccountSnapshot,
) -> bool:
    try:
        assert_bookmaker_account_acquisition_authoritative(acquired)
    except BookmakerAccountAcquisitionError:
        return False
    return True


class BookmakerAccountAcquisitionStore:
    """Append-only durable authority for product-owned account snapshot reads."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if self._path.exists() and self._path.is_dir():
            raise BookmakerAccountAcquisitionError(
                "acquisition store path must be a file"
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    def _acquire_betfair_unissued(
        self,
        client: BetfairReadOnlyClient,
        requested_capabilities: frozenset[BookmakerCapability],
        *,
        acquisition_id: str,
        authenticated_account_identity_sha256: str,
        account_identity_observed_at: str,
    ) -> AcquiredBookmakerAccountSnapshot:
        """Perform or idempotently resume one canonical provider acquisition.

        acquisition_id is a product retry/idempotency identity, not evidence of
        provider truth. Reusing an already-durable id resolves that exact acquisition
        before any network I/O. A genuinely new temporal read must use a new id even
        when the provider returns byte-identical content.
        """

        _assert_canonical_betfair_client(client)
        capabilities = _validate_requested_capabilities(requested_capabilities)
        acquisition_id = _exact_text(acquisition_id, "acquisition_id")
        authenticated_account_identity_sha256 = _sha256_hex(
            authenticated_account_identity_sha256,
            "authenticated_account_identity_sha256",
        )
        account_identity_observed_at = _exact_text(
            account_identity_observed_at,
            "account_identity_observed_at",
        )
        existing = self._load_by_acquisition_id(acquisition_id)
        if existing is not None:
            _validate_retry_request(existing, client, capabilities, acquisition_id)
            return existing

        # Do not dispatch through client.read_account_snapshot: a caller could
        # shadow that instance attribute.  The class surface itself is sealed
        # immediately above before this captured method is called.
        snapshot = _CANONICAL_READ_ACCOUNT_SNAPSHOT(client, capabilities)
        if type(snapshot) is not BookmakerAccountSnapshot:
            raise BookmakerAccountAcquisitionError(
                "canonical Betfair adapter returned a non-canonical snapshot"
            )
        _validate_exact_snapshot(snapshot)

        if snapshot.observed_capabilities != capabilities:
            raise BookmakerAccountAcquisitionError(
                "snapshot capability set differs from the requested acquisition set"
            )

        integration = _canonical_betfair_integration_evidence(snapshot)
        snapshot_payload = _snapshot_payload(snapshot)
        semantic_payload = _snapshot_semantic_payload(snapshot)
        snapshot_payload_sha256 = _digest(snapshot_payload)
        snapshot_semantic_sha256 = _digest(semantic_payload)
        observation_key = _observation_key(
            acquisition_id,
            snapshot,
            capabilities,
            integration.integration_kind,
        )

        record_without_receipt = {
            "schema_version": _SCHEMA_VERSION,
            "acquisition_id": acquisition_id,
            "observation_key": observation_key,
            "venue_id": snapshot.profile.venue_id,
            "account_id": snapshot.profile.account_id,
            "authenticated_account_identity_sha256": (
                authenticated_account_identity_sha256
            ),
            "account_identity_observed_at": account_identity_observed_at,
            "adapter_id": snapshot.profile.adapter_id,
            "adapter_version": snapshot.profile.adapter_version,
            "profile_id": snapshot.profile.profile_id,
            "integration_evidence": integration.to_canonical_dict(),
            "integration_evidence_id": integration.evidence_id,
            "integration_kind": integration.integration_kind.value,
            "requested_capabilities": [
                capability.value
                for capability in sorted(capabilities, key=lambda item: item.value)
            ],
            "provider_response_sha256": snapshot.profile.source_payload_sha256,
            "snapshot_payload": snapshot_payload,
            "snapshot_payload_sha256": snapshot_payload_sha256,
            "snapshot_semantic_sha256": snapshot_semantic_sha256,
            "acquired_at": snapshot.observed_at,
            # The current Betfair account snapshot schema contains local
            # receive/observation times but no typed provider-native account
            # snapshot timestamp.  Never substitute the local clock here.
            "provider_native_observed_at": None,
            "source_authority_proven": True,
            "allocation_authority_proven": False,
            "atomicity_proven": False,
            "execution_authorized": False,
            "real_money_execution": False,
        }
        receipt_id = _digest(record_without_receipt)
        record = dict(record_without_receipt)
        record["receipt_id"] = receipt_id
        record_json = _canonical_json(record)
        record_sha256 = sha256(record_json.encode("utf-8")).hexdigest()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT record_json, record_sha256
                FROM bookmaker_account_acquisition
                WHERE acquisition_id = ?
                """,
                (acquisition_id,),
            ).fetchone()
            if row is not None:
                connection.rollback()
                durable = _decode_record(row[0], row[1])
                _validate_retry_request(
                    durable,
                    client,
                    capabilities,
                    acquisition_id,
                )
                if (
                    durable.receipt.provider_response_sha256
                    != snapshot.profile.source_payload_sha256
                    or durable.receipt.snapshot_semantic_sha256
                    != snapshot_semantic_sha256
                ):
                    raise BookmakerAccountAcquisitionError(
                        "same acquisition_id produced conflicting provider evidence"
                    )
                return durable

            connection.execute(
                """
                INSERT INTO bookmaker_account_acquisition
                    (
                        receipt_id,
                        acquisition_id,
                        observation_key,
                        record_json,
                        record_sha256
                    )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    acquisition_id,
                    observation_key,
                    record_json,
                    record_sha256,
                ),
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

        return _decode_record(record_json, record_sha256)

    def resolve(self, receipt_id: str) -> AcquiredBookmakerAccountSnapshot:
        """Re-resolve one acquisition from durable bytes and verify all bindings."""

        _sha256_hex(receipt_id, "receipt_id")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT record_json, record_sha256
                FROM bookmaker_account_acquisition
                WHERE receipt_id = ?
                """,
                (receipt_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise BookmakerAccountAcquisitionError(
                "acquisition receipt does not exist"
            )
        return _decode_record(row[0], row[1])

    def count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM bookmaker_account_acquisition"
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        return int(row[0])

    def _load_by_acquisition_id(
        self, acquisition_id: str
    ) -> AcquiredBookmakerAccountSnapshot | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT record_json, record_sha256
                FROM bookmaker_account_acquisition
                WHERE acquisition_id = ?
                """,
                (acquisition_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return _decode_record(row[0], row[1])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10.0)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            version_row = connection.execute("PRAGMA user_version").fetchone()
            version = int(version_row[0]) if version_row is not None else 0
            if version not in (0, _SCHEMA_VERSION):
                raise BookmakerAccountAcquisitionError(
                    "unsupported acquisition store schema version"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bookmaker_account_acquisition (
                    receipt_id TEXT PRIMARY KEY,
                    acquisition_id TEXT NOT NULL UNIQUE,
                    observation_key TEXT NOT NULL UNIQUE,
                    record_json TEXT NOT NULL,
                    record_sha256 TEXT NOT NULL
                )
                """
            )
            if version == 0:
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.commit()
        finally:
            connection.close()


def _assert_canonical_betfair_client(client: object) -> None:
    if type(client) is not BetfairReadOnlyClient:
        raise BookmakerAccountAcquisitionError(
            "client must be an exact canonical BetfairReadOnlyClient"
        )
    instance_dict = vars(client)
    for name, expected in _CANONICAL_BETFAIR_METHODS.items():
        if name in instance_dict:
            raise BookmakerAccountAcquisitionError(
                f"canonical Betfair method {name} is shadowed on the client instance"
            )
        if getattr(BetfairReadOnlyClient, name) is not expected:
            raise BookmakerAccountAcquisitionError(
                f"canonical Betfair method {name} changed after acquisition authority loaded"
            )


def _client_scope(client: BetfairReadOnlyClient) -> tuple[str, str, str, str]:
    return (
        _exact_text(getattr(client, "_venue_id", None), "client.venue_id"),
        _exact_text(getattr(client, "_account_id", None), "client.account_id"),
        ADAPTER_ID,
        ADAPTER_VERSION,
    )


def _validate_retry_request(
    acquired: AcquiredBookmakerAccountSnapshot,
    client: BetfairReadOnlyClient,
    capabilities: frozenset[BookmakerCapability],
    acquisition_id: str,
) -> None:
    expected_capabilities = tuple(
        sorted(capabilities, key=lambda item: item.value)
    )
    if acquired.receipt.acquisition_id != acquisition_id:
        raise BookmakerAccountAcquisitionError(
            "durable acquisition id does not match retry request"
        )
    if (
        acquired.receipt.venue_id,
        acquired.receipt.account_id,
        acquired.receipt.adapter_id,
        acquired.receipt.adapter_version,
    ) != _client_scope(client):
        raise BookmakerAccountAcquisitionError(
            "acquisition_id cannot be reused for another provider/account/adapter scope"
        )
    if acquired.receipt.requested_capabilities != expected_capabilities:
        raise BookmakerAccountAcquisitionError(
            "acquisition_id cannot be reused for another capability request"
        )


def _validate_requested_capabilities(
    value: object,
) -> frozenset[BookmakerCapability]:
    if type(value) is not frozenset or not value:
        raise BookmakerAccountAcquisitionError(
            "requested_capabilities must be a non-empty exact frozenset"
        )
    for capability in value:
        if type(capability) is not BookmakerCapability:
            raise BookmakerAccountAcquisitionError(
                "requested_capabilities must contain exact BookmakerCapability values"
            )
        if capability not in _ALLOWED_SNAPSHOT_CAPABILITIES:
            raise BookmakerAccountAcquisitionError(
                f"{capability.value} is not typed account-snapshot acquisition evidence"
            )
    return frozenset(sorted(value, key=lambda item: item.value))


def _canonical_betfair_integration_evidence(
    snapshot: BookmakerAccountSnapshot,
) -> BookmakerIntegrationEvidence:
    descriptor = {
        "account_endpoint": ACCOUNT_JSON_RPC_ENDPOINT,
        "adapter_id": ADAPTER_ID,
        "adapter_version": ADAPTER_VERSION,
        "betting_endpoint": BETTING_JSON_RPC_ENDPOINT,
        "integration_kind": BookmakerIntegrationKind.OFFICIAL_API.value,
        "read_method": "BetfairReadOnlyClient.read_account_snapshot",
    }
    evidence = BookmakerIntegrationEvidence(
        venue_id=snapshot.profile.venue_id,
        adapter_id=snapshot.profile.adapter_id,
        adapter_version=snapshot.profile.adapter_version,
        profile_id=snapshot.profile.profile_id,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=snapshot.observed_at,
        source_ref=(
            f"autosport://canonical-bookmaker-adapter/"
            f"{ADAPTER_ID}/{ADAPTER_VERSION}/official-api"
        ),
        source_payload_sha256=_digest(descriptor),
    )
    evidence.verify_profile(snapshot.profile)
    return evidence


def _observation_key(
    acquisition_id: str,
    snapshot: BookmakerAccountSnapshot,
    capabilities: frozenset[BookmakerCapability],
    integration_kind: BookmakerIntegrationKind,
) -> str:
    # acquisition_id is the temporal attempt boundary. Provider bytes are bound
    # inside that attempt but never collapse two distinct reads: byte-identical
    # observations remain separate when their product acquisition ids differ.
    return _digest(
        {
            "acquisition_id": acquisition_id,
            "venue_id": snapshot.profile.venue_id,
            "account_id": snapshot.profile.account_id,
            "adapter_id": snapshot.profile.adapter_id,
            "adapter_version": snapshot.profile.adapter_version,
            "provider_response_sha256": snapshot.profile.source_payload_sha256,
            "requested_capabilities": [
                capability.value
                for capability in sorted(capabilities, key=lambda item: item.value)
            ],
            "integration_kind": integration_kind.value,
        }
    )


def _validate_exact_snapshot(snapshot: object) -> None:
    if type(snapshot) is not BookmakerAccountSnapshot:
        raise BookmakerAccountAcquisitionError(
            "snapshot must be an exact BookmakerAccountSnapshot"
        )
    if type(snapshot.profile) is not BookmakerCapabilityProfile:
        raise BookmakerAccountAcquisitionError(
            "snapshot profile must be an exact BookmakerCapabilityProfile"
        )
    for fact in snapshot.profile.facts:
        if type(fact) is not BookmakerCapabilityFact:
            raise BookmakerAccountAcquisitionError(
                "snapshot profile facts must be exact BookmakerCapabilityFact values"
            )
    for capability in snapshot.observed_capabilities:
        if type(capability) is not BookmakerCapability:
            raise BookmakerAccountAcquisitionError(
                "snapshot capabilities must be exact BookmakerCapability values"
            )
    if snapshot.balance is not None and type(snapshot.balance) is not BookmakerBalanceObservation:
        raise BookmakerAccountAcquisitionError(
            "snapshot balance must be an exact BookmakerBalanceObservation"
        )
    for position in snapshot.open_positions + snapshot.settled_positions:
        if type(position) is not BookmakerPositionObservation:
            raise BookmakerAccountAcquisitionError(
                "snapshot positions must be exact BookmakerPositionObservation values"
            )


def _snapshot_payload(snapshot: BookmakerAccountSnapshot) -> dict[str, Any]:
    return {
        "profile": {
            "venue_id": snapshot.profile.venue_id,
            "account_id": snapshot.profile.account_id,
            "adapter_id": snapshot.profile.adapter_id,
            "adapter_version": snapshot.profile.adapter_version,
            "profile_version": snapshot.profile.profile_version,
            "facts": [
                {
                    "capability": fact.capability.value,
                    "state": fact.state.value,
                }
                for fact in sorted(
                    snapshot.profile.facts,
                    key=lambda item: item.capability.value,
                )
            ],
            "observed_at": snapshot.profile.observed_at,
            "source_ref": snapshot.profile.source_ref,
            "source_payload_sha256": snapshot.profile.source_payload_sha256,
        },
        "observed_capabilities": sorted(
            capability.value for capability in snapshot.observed_capabilities
        ),
        "observed_at": snapshot.observed_at,
        "balance": (
            None
            if snapshot.balance is None
            else {
                "venue_id": snapshot.balance.venue_id,
                "account_id": snapshot.balance.account_id,
                "adapter_id": snapshot.balance.adapter_id,
                "observation_id": snapshot.balance.observation_id,
                "currency": snapshot.balance.currency,
                "available_balance": str(snapshot.balance.available_balance),
                "observed_at": snapshot.balance.observed_at,
                "source_payload_sha256": snapshot.balance.source_payload_sha256,
                "total_balance": _optional_decimal(snapshot.balance.total_balance),
                "total_balance_source_ref": snapshot.balance.total_balance_source_ref,
                "exposure": _optional_decimal(snapshot.balance.exposure),
                "retained_commission": _optional_decimal(
                    snapshot.balance.retained_commission
                ),
                "exposure_limit": _optional_decimal(snapshot.balance.exposure_limit),
            }
        ),
        "open_positions": [
            _position_payload(position) for position in snapshot.open_positions
        ],
        "settled_positions": [
            _position_payload(position) for position in snapshot.settled_positions
        ],
    }


def _snapshot_semantic_payload(
    snapshot: BookmakerAccountSnapshot,
) -> dict[str, Any]:
    payload = _snapshot_payload(snapshot)
    profile = dict(payload["profile"])
    profile.pop("observed_at")
    payload["profile"] = profile
    payload.pop("observed_at")
    balance = payload["balance"]
    if isinstance(balance, dict):
        balance = dict(balance)
        balance.pop("observed_at")
        payload["balance"] = balance
    for field in ("open_positions", "settled_positions"):
        payload[field] = [
            {key: value for key, value in position.items() if key != "observed_at"}
            for position in payload[field]
        ]
    return payload


def _position_payload(position: BookmakerPositionObservation) -> dict[str, Any]:
    return {
        "venue_id": position.venue_id,
        "account_id": position.account_id,
        "adapter_id": position.adapter_id,
        "observation_id": position.observation_id,
        "external_position_id": position.external_position_id,
        "state": position.state.value,
        "currency": position.currency,
        "observed_at": position.observed_at,
        "source_payload_sha256": position.source_payload_sha256,
        "provider_amount": str(position.provider_amount),
        "provider_amount_semantics": position.provider_amount_semantics,
        "provider_side": position.provider_side,
        "decimal_odds": _optional_decimal(position.decimal_odds),
        "gross_return": _optional_decimal(position.gross_return),
        "external_receipt_id": position.external_receipt_id,
    }


def _snapshot_from_payload(payload: object) -> BookmakerAccountSnapshot:
    snapshot = _exact_dict(
        payload,
        {
            "profile",
            "observed_capabilities",
            "observed_at",
            "balance",
            "open_positions",
            "settled_positions",
        },
        "snapshot_payload",
    )
    profile_raw = _exact_dict(
        snapshot["profile"],
        {
            "venue_id",
            "account_id",
            "authenticated_account_identity_sha256",
            "account_identity_observed_at",
            "adapter_id",
            "adapter_version",
            "profile_version",
            "facts",
            "observed_at",
            "source_ref",
            "source_payload_sha256",
        },
        "snapshot_payload.profile",
    )
    facts_raw = _exact_list(profile_raw["facts"], "snapshot_payload.profile.facts")
    facts: list[BookmakerCapabilityFact] = []
    for index, raw in enumerate(facts_raw):
        item = _exact_dict(
            raw,
            {"capability", "state"},
            f"snapshot_payload.profile.facts[{index}]",
        )
        facts.append(
            BookmakerCapabilityFact(
                BookmakerCapability(_exact_text(item["capability"], "capability")),
                BookmakerCapabilityState(_exact_text(item["state"], "state")),
            )
        )
    profile = BookmakerCapabilityProfile(
        venue_id=_exact_text(profile_raw["venue_id"], "venue_id"),
        account_id=_exact_text(profile_raw["account_id"], "account_id"),
        adapter_id=_exact_text(profile_raw["adapter_id"], "adapter_id"),
        adapter_version=_exact_text(profile_raw["adapter_version"], "adapter_version"),
        profile_version=_exact_int(profile_raw["profile_version"], "profile_version"),
        facts=tuple(facts),
        observed_at=_exact_text(profile_raw["observed_at"], "observed_at"),
        source_ref=_exact_text(profile_raw["source_ref"], "source_ref"),
        source_payload_sha256=_exact_text(
            profile_raw["source_payload_sha256"], "source_payload_sha256"
        ),
    )

    capabilities_raw = _exact_list(
        snapshot["observed_capabilities"],
        "snapshot_payload.observed_capabilities",
    )
    capabilities = frozenset(
        BookmakerCapability(_exact_text(value, "observed_capability"))
        for value in capabilities_raw
    )

    balance_raw = snapshot["balance"]
    balance: BookmakerBalanceObservation | None
    if balance_raw is None:
        balance = None
    else:
        item = _exact_dict(
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
            "snapshot_payload.balance",
        )
        total_balance_source_ref = item["total_balance_source_ref"]
        if total_balance_source_ref is not None:
            total_balance_source_ref = _exact_text(
                total_balance_source_ref, "total_balance_source_ref"
            )
        balance = BookmakerBalanceObservation(
            venue_id=_exact_text(item["venue_id"], "venue_id"),
            account_id=_exact_text(item["account_id"], "account_id"),
            adapter_id=_exact_text(item["adapter_id"], "adapter_id"),
            observation_id=_exact_text(item["observation_id"], "observation_id"),
            currency=_exact_text(item["currency"], "currency"),
            available_balance=_decimal(item["available_balance"], "available_balance"),
            observed_at=_exact_text(item["observed_at"], "observed_at"),
            source_payload_sha256=_exact_text(
                item["source_payload_sha256"], "source_payload_sha256"
            ),
            total_balance=_optional_decimal_from_json(
                item["total_balance"], "total_balance"
            ),
            total_balance_source_ref=total_balance_source_ref,
            exposure=_optional_decimal_from_json(item["exposure"], "exposure"),
            retained_commission=_optional_decimal_from_json(
                item["retained_commission"], "retained_commission"
            ),
            exposure_limit=_optional_decimal_from_json(
                item["exposure_limit"], "exposure_limit"
            ),
        )

    open_positions = tuple(
        _position_from_payload(value, f"open_positions[{index}]")
        for index, value in enumerate(
            _exact_list(snapshot["open_positions"], "snapshot_payload.open_positions")
        )
    )
    settled_positions = tuple(
        _position_from_payload(value, f"settled_positions[{index}]")
        for index, value in enumerate(
            _exact_list(
                snapshot["settled_positions"],
                "snapshot_payload.settled_positions",
            )
        )
    )
    result = BookmakerAccountSnapshot(
        profile=profile,
        observed_capabilities=capabilities,
        observed_at=_exact_text(snapshot["observed_at"], "observed_at"),
        balance=balance,
        open_positions=open_positions,
        settled_positions=settled_positions,
    )
    _validate_exact_snapshot(result)
    return result


def _position_from_payload(
    payload: object,
    field: str,
) -> BookmakerPositionObservation:
    item = _exact_dict(
        payload,
        {
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
        },
        field,
    )
    provider_side = item["provider_side"]
    external_receipt_id = item["external_receipt_id"]
    if provider_side is not None:
        provider_side = _exact_text(provider_side, "provider_side")
    if external_receipt_id is not None:
        external_receipt_id = _exact_text(
            external_receipt_id, "external_receipt_id"
        )
    return BookmakerPositionObservation(
        venue_id=_exact_text(item["venue_id"], "venue_id"),
        account_id=_exact_text(item["account_id"], "account_id"),
        adapter_id=_exact_text(item["adapter_id"], "adapter_id"),
        observation_id=_exact_text(item["observation_id"], "observation_id"),
        external_position_id=_exact_text(
            item["external_position_id"], "external_position_id"
        ),
        state=BookmakerPositionState(_exact_text(item["state"], "state")),
        currency=_exact_text(item["currency"], "currency"),
        observed_at=_exact_text(item["observed_at"], "observed_at"),
        source_payload_sha256=_exact_text(
            item["source_payload_sha256"], "source_payload_sha256"
        ),
        provider_amount=_decimal(item["provider_amount"], "provider_amount"),
        provider_amount_semantics=_exact_text(
            item["provider_amount_semantics"], "provider_amount_semantics"
        ),
        provider_side=provider_side,
        decimal_odds=_optional_decimal_from_json(
            item["decimal_odds"], "decimal_odds"
        ),
        gross_return=_optional_decimal_from_json(
            item["gross_return"], "gross_return"
        ),
        external_receipt_id=external_receipt_id,
    )


def _decode_record(
    record_json: object,
    record_sha256: object,
) -> AcquiredBookmakerAccountSnapshot:
    if type(record_json) is not str or type(record_sha256) is not str:
        raise BookmakerAccountAcquisitionError(
            "durable acquisition row has invalid storage types"
        )
    _sha256_hex(record_sha256, "record_sha256")
    if sha256(record_json.encode("utf-8")).hexdigest() != record_sha256:
        raise BookmakerAccountAcquisitionError(
            "durable acquisition record hash mismatch"
        )
    try:
        decoded = json.loads(record_json)
    except json.JSONDecodeError as exc:
        raise BookmakerAccountAcquisitionError(
            "durable acquisition record is not valid JSON"
        ) from exc
    if _canonical_json(decoded) != record_json:
        raise BookmakerAccountAcquisitionError(
            "durable acquisition record is not canonical JSON"
        )
    record = _exact_dict(
        decoded,
        {
            "schema_version",
            "acquisition_id",
            "observation_key",
            "venue_id",
            "account_id",
            "adapter_id",
            "adapter_version",
            "profile_id",
            "integration_evidence",
            "integration_evidence_id",
            "integration_kind",
            "requested_capabilities",
            "provider_response_sha256",
            "snapshot_payload",
            "snapshot_payload_sha256",
            "snapshot_semantic_sha256",
            "acquired_at",
            "provider_native_observed_at",
            "source_authority_proven",
            "allocation_authority_proven",
            "atomicity_proven",
            "execution_authorized",
            "real_money_execution",
            "receipt_id",
        },
        "acquisition_record",
    )
    if _exact_int(record["schema_version"], "schema_version") != _SCHEMA_VERSION:
        raise BookmakerAccountAcquisitionError(
            "unsupported acquisition record schema version"
        )
    for field, expected in (
        ("source_authority_proven", True),
        ("allocation_authority_proven", False),
        ("atomicity_proven", False),
        ("execution_authorized", False),
        ("real_money_execution", False),
    ):
        if type(record[field]) is not bool or record[field] is not expected:
            raise BookmakerAccountAcquisitionError(
                f"durable acquisition truth field {field} is invalid"
            )

    receipt_id = _exact_text(record["receipt_id"], "receipt_id")
    _sha256_hex(receipt_id, "receipt_id")
    unsigned = dict(record)
    unsigned.pop("receipt_id")
    if _digest(unsigned) != receipt_id:
        raise BookmakerAccountAcquisitionError(
            "durable acquisition receipt identity mismatch"
        )

    snapshot_payload = record["snapshot_payload"]
    snapshot_payload_sha256 = _exact_text(
        record["snapshot_payload_sha256"], "snapshot_payload_sha256"
    )
    _sha256_hex(snapshot_payload_sha256, "snapshot_payload_sha256")
    if _digest(snapshot_payload) != snapshot_payload_sha256:
        raise BookmakerAccountAcquisitionError(
            "durable snapshot payload digest mismatch"
        )
    snapshot = _snapshot_from_payload(snapshot_payload)
    snapshot_semantic_sha256 = _exact_text(
        record["snapshot_semantic_sha256"], "snapshot_semantic_sha256"
    )
    _sha256_hex(snapshot_semantic_sha256, "snapshot_semantic_sha256")
    if _digest(_snapshot_semantic_payload(snapshot)) != snapshot_semantic_sha256:
        raise BookmakerAccountAcquisitionError(
            "durable snapshot semantic digest mismatch"
        )

    integration_raw = _exact_dict(
        record["integration_evidence"],
        {
            "adapter_id",
            "adapter_version",
            "integration_kind",
            "observed_at",
            "profile_id",
            "schema_version",
            "source_payload_sha256",
            "source_ref",
            "venue_id",
        },
        "integration_evidence",
    )
    integration = BookmakerIntegrationEvidence(
        venue_id=_exact_text(integration_raw["venue_id"], "venue_id"),
        adapter_id=_exact_text(integration_raw["adapter_id"], "adapter_id"),
        adapter_version=_exact_text(
            integration_raw["adapter_version"], "adapter_version"
        ),
        profile_id=_exact_text(integration_raw["profile_id"], "profile_id"),
        integration_kind=BookmakerIntegrationKind(
            _exact_text(integration_raw["integration_kind"], "integration_kind")
        ),
        observed_at=_exact_text(integration_raw["observed_at"], "observed_at"),
        source_ref=_exact_text(integration_raw["source_ref"], "source_ref"),
        source_payload_sha256=_exact_text(
            integration_raw["source_payload_sha256"], "source_payload_sha256"
        ),
        schema_version=_exact_int(
            integration_raw["schema_version"], "integration_schema_version"
        ),
    )
    integration.verify_profile(snapshot.profile)
    if integration.integration_kind is not BookmakerIntegrationKind.OFFICIAL_API:
        raise BookmakerAccountAcquisitionError(
            "Betfair account acquisition must remain official-api evidence"
        )
    integration_evidence_id = _exact_text(
        record["integration_evidence_id"], "integration_evidence_id"
    )
    _sha256_hex(integration_evidence_id, "integration_evidence_id")
    if integration.evidence_id != integration_evidence_id:
        raise BookmakerAccountAcquisitionError(
            "integration evidence identity mismatch"
        )

    capabilities_raw = _exact_list(
        record["requested_capabilities"], "requested_capabilities"
    )
    capabilities = tuple(
        BookmakerCapability(_exact_text(value, "requested_capability"))
        for value in capabilities_raw
    )
    if tuple(sorted(capabilities, key=lambda item: item.value)) != capabilities:
        raise BookmakerAccountAcquisitionError(
            "requested capabilities are not canonical"
        )
    if frozenset(capabilities) != snapshot.observed_capabilities:
        raise BookmakerAccountAcquisitionError(
            "durable requested capabilities differ from snapshot capabilities"
        )

    provider_response_sha256 = _exact_text(
        record["provider_response_sha256"], "provider_response_sha256"
    )
    _sha256_hex(provider_response_sha256, "provider_response_sha256")
    if provider_response_sha256 != snapshot.profile.source_payload_sha256:
        raise BookmakerAccountAcquisitionError(
            "provider response identity differs from snapshot profile"
        )

    provider_native_observed_at = record["provider_native_observed_at"]
    if provider_native_observed_at is not None:
        provider_native_observed_at = _exact_text(
            provider_native_observed_at, "provider_native_observed_at"
        )

    receipt = BookmakerAccountAcquisitionReceipt(
        receipt_id=receipt_id,
        acquisition_id=_exact_text(record["acquisition_id"], "acquisition_id"),
        observation_key=_exact_text(record["observation_key"], "observation_key"),
        venue_id=_exact_text(record["venue_id"], "venue_id"),
        account_id=_exact_text(record["account_id"], "account_id"),
        authenticated_account_identity_sha256=_sha256_hex(
            record["authenticated_account_identity_sha256"],
            "authenticated_account_identity_sha256",
        ),
        account_identity_observed_at=_exact_text(
            record["account_identity_observed_at"],
            "account_identity_observed_at",
        ),
        adapter_id=_exact_text(record["adapter_id"], "adapter_id"),
        adapter_version=_exact_text(record["adapter_version"], "adapter_version"),
        profile_id=_exact_text(record["profile_id"], "profile_id"),
        integration_evidence_id=integration_evidence_id,
        integration_kind=integration.integration_kind,
        requested_capabilities=capabilities,
        provider_response_sha256=provider_response_sha256,
        snapshot_payload_sha256=snapshot_payload_sha256,
        snapshot_semantic_sha256=snapshot_semantic_sha256,
        acquired_at=_exact_text(record["acquired_at"], "acquired_at"),
        provider_native_observed_at=provider_native_observed_at,
    )
    if receipt.profile_id != snapshot.profile.profile_id:
        raise BookmakerAccountAcquisitionError(
            "receipt profile id differs from durable snapshot"
        )
    expected_observation_key = _observation_key(
        receipt.acquisition_id,
        snapshot,
        frozenset(capabilities),
        integration.integration_kind,
    )
    if receipt.observation_key != expected_observation_key:
        raise BookmakerAccountAcquisitionError(
            "durable acquisition observation identity mismatch"
        )
    return AcquiredBookmakerAccountSnapshot(receipt, snapshot)


def _exact_dict(
    value: object,
    keys: set[str],
    field: str,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise BookmakerAccountAcquisitionError(f"{field} must be an object")
    if set(value) != keys:
        raise BookmakerAccountAcquisitionError(
            f"{field} has an unexpected durable schema"
        )
    return value


def _exact_list(value: object, field: str) -> list[Any]:
    if type(value) is not list:
        raise BookmakerAccountAcquisitionError(f"{field} must be a list")
    return value


def _exact_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BookmakerAccountAcquisitionError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _exact_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise BookmakerAccountAcquisitionError(f"{field} must be an integer")
    return value


def _decimal(value: object, field: str) -> Decimal:
    text = _exact_text(value, field)
    try:
        decimal = Decimal(text)
    except InvalidOperation as exc:
        raise BookmakerAccountAcquisitionError(
            f"{field} must be a Decimal string"
        ) from exc
    if not decimal.is_finite():
        raise BookmakerAccountAcquisitionError(f"{field} must be finite")
    return decimal


def _optional_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _optional_decimal_from_json(
    value: object,
    field: str,
) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, field)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_hex(value: object, field: str) -> str:
    text = _exact_text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise BookmakerAccountAcquisitionError(
            f"{field} must be a lowercase SHA-256 hex digest"
        )
    return text


def _install_provider_origin_guard() -> None:
    """Issue positive source authority only from the fixed production entrypoint.

    This mirrors Autosport's existing complete-board trust-root model: local
    durable bytes prove integrity and identity but cannot recreate remote origin
    after process restart. Arbitrary in-process code injection/monkeypatching is
    outside this application authority boundary.
    """

    from weakref import ref

    issued: dict[
        str,
        tuple[object, str],
    ] = {}
    raw_acquire = BookmakerAccountAcquisitionStore._acquire_betfair_unissued

    def fingerprint(acquired: AcquiredBookmakerAccountSnapshot) -> str:
        return _digest(
            {
                "receipt_id": acquired.receipt.receipt_id,
                "snapshot_payload_sha256": acquired.receipt.snapshot_payload_sha256,
                "snapshot_semantic_sha256": acquired.receipt.snapshot_semantic_sha256,
            }
        )

    def forget(receipt_id: str, reference: object) -> None:
        current = issued.get(receipt_id)
        if current is not None and current[0] is reference:
            issued.pop(receipt_id, None)

    def issue(
        acquired: AcquiredBookmakerAccountSnapshot,
    ) -> AcquiredBookmakerAccountSnapshot:
        receipt_id = acquired.receipt.receipt_id
        reference = ref(
            acquired,
            lambda current, receipt_id=receipt_id: forget(receipt_id, current),
        )
        issued[receipt_id] = (reference, fingerprint(acquired))
        return acquired

    def current_issued(
        receipt_id: str,
    ) -> AcquiredBookmakerAccountSnapshot | None:
        current = issued.get(receipt_id)
        if current is None:
            return None
        value = current[0]()
        if value is None:
            issued.pop(receipt_id, None)
            return None
        if (
            type(value) is not AcquiredBookmakerAccountSnapshot
            or current[1] != fingerprint(value)
        ):
            issued.pop(receipt_id, None)
            return None
        return value

    def assert_authoritative(
        acquired: AcquiredBookmakerAccountSnapshot,
    ) -> None:
        if type(acquired) is not AcquiredBookmakerAccountSnapshot:
            raise BookmakerAccountAcquisitionError(
                "provider-origin authority requires exact acquired snapshot evidence"
            )
        current = current_issued(acquired.receipt.receipt_id)
        if current is not acquired:
            raise BookmakerAccountAcquisitionError(
                "account snapshot was not issued by live canonical provider acquisition"
            )

    def acquire_betfair(
        self: BookmakerAccountAcquisitionStore,
        credentials: BetfairSessionCredentials,
        requested_capabilities: frozenset[BookmakerCapability],
        *,
        acquisition_id: str,
        account_id: str = "default-account",
        timeout_seconds: float = 10.0,
    ) -> AcquiredBookmakerAccountSnapshot:
        if type(credentials) is not BetfairSessionCredentials:
            raise BookmakerAccountAcquisitionError(
                "credentials must be exact BetfairSessionCredentials"
            )
        capabilities = _validate_requested_capabilities(requested_capabilities)
        acquisition = _exact_text(acquisition_id, "acquisition_id")

        # Construct the canonical production client here. Consumers cannot inject
        # an alternate transport into the authority-bearing acquisition entrypoint.
        client = BetfairReadOnlyClient(
            credentials,
            timeout_seconds=timeout_seconds,
            venue_id="betfair",
            account_id=account_id,
        )
        _assert_canonical_betfair_client(client)

        existing = self._load_by_acquisition_id(acquisition)
        if existing is not None:
            _validate_retry_request(
                existing,
                client,
                capabilities,
                acquisition,
            )
            live = current_issued(existing.receipt.receipt_id)
            if live is not None:
                return live
            raise BookmakerAccountAcquisitionError(
                "durable acquisition cannot reissue provider-origin authority; "
                "use a new acquisition_id to reacquire provider evidence"
            )

        account_identity = _read_developer_account_identity(client)
        acquired = raw_acquire(
            self,
            client,
            capabilities,
            acquisition_id=acquisition,
            authenticated_account_identity_sha256=(
                account_identity.account_identity_sha256
            ),
            account_identity_observed_at=account_identity.observed_at,
        )
        return issue(acquired)

    globals()["assert_bookmaker_account_acquisition_authoritative"] = (
        assert_authoritative
    )
    BookmakerAccountAcquisitionStore.acquire_betfair = acquire_betfair


_install_provider_origin_guard()
del _install_provider_origin_guard
