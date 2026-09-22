"""Restart-safe durable history for the canonical incident/model-risk register.

This module persists only IncidentRiskEntry values produced by the canonical
register contract. It does not define a second incident/model-risk schema and does
not grant execution or release authority. Once non-empty history is committed, the
existing independent MonotonicWorkspaceAuthority prevents store-file deletion or a
stale store image from silently becoming healthy empty/current truth while that
machine-state authority survives.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

from .incident_risk_register import (
    IncidentRiskEntry,
    IncidentRiskRegisterError,
    validate_successor,
)
from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)


STORE_SCHEMA: Final = "autosport.incident_model_risk_store"
STORE_SCHEMA_VERSION: Final = 2
_AUTHORITY_DOMAIN: Final = "autosport.incident-model-risk-register"
_AUTHORITY_KEY: Final = "durable-history.v1"
_AUTHORITY_BINDING_SHA256: Final = hashlib.sha256(
    b"autosport.incident-model-risk-register.durable-history.monotonic.v1"
).hexdigest()
_ROOT_KEYS: Final = frozenset(
    {"schema", "schema_version", "histories", "availability", "content_sha256"}
)
_HISTORY_KEYS: Final = frozenset({"entry_id", "revisions"})
_AVAILABILITY_KEYS: Final = frozenset({"entry_id", "revision", "available_at"})


class IncidentRiskStoreError(ValueError):
    """Raised when durable incident/model-risk history is invalid or ambiguous."""


def _sha256_text(name: str, value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise IncidentRiskStoreError(
            f"{name} must be lowercase 64-character SHA-256 hex"
        )
    return value


def _canonical_utc_timestamp(value: object, name: str) -> datetime:
    if type(value) is not str or not value or value.strip() != value:
        raise IncidentRiskStoreError(
            f"{name} must be canonical trimmed ISO-8601 UTC text"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise IncidentRiskStoreError(f"{name} must be ISO-8601") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
        or parsed.isoformat() != value
    ):
        raise IncidentRiskStoreError(
            f"{name} must use datetime.isoformat() canonical UTC +00:00"
        )
    return parsed


def _canonical_as_of(value: object) -> datetime:
    return _canonical_utc_timestamp(value, "as_of")


def _availability_now() -> str:
    """Return the product-owned publication clock for a durable revision."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _validate_histories(
    histories: object,
) -> tuple[tuple[IncidentRiskEntry, ...], ...]:
    if type(histories) is not tuple:
        raise IncidentRiskStoreError("histories must be an exact tuple")

    entry_ids: list[str] = []
    normalized: list[tuple[IncidentRiskEntry, ...]] = []
    for history in histories:
        if type(history) is not tuple or not history:
            raise IncidentRiskStoreError(
                "each incident/model-risk history must be a non-empty exact tuple"
            )
        if any(type(entry) is not IncidentRiskEntry for entry in history):
            raise IncidentRiskStoreError(
                "durable history accepts exact IncidentRiskEntry values only"
            )

        first = history[0]
        if first.revision != 1:
            raise IncidentRiskStoreError(
                "durable incident/model-risk history must begin at revision 1"
            )
        if any(entry.entry_id != first.entry_id for entry in history):
            raise IncidentRiskStoreError(
                "one durable history cannot contain multiple entry_id values"
            )
        try:
            for previous, candidate in zip(history, history[1:]):
                validate_successor(previous, candidate)
        except (IncidentRiskRegisterError, TypeError, ValueError) as exc:
            raise IncidentRiskStoreError(
                "durable incident/model-risk revision chain is invalid"
            ) from exc

        entry_ids.append(first.entry_id)
        normalized.append(history)

    if entry_ids != sorted(set(entry_ids)):
        raise IncidentRiskStoreError(
            "durable histories must be sorted by unique entry_id"
        )
    return tuple(normalized)


def _histories_json(
    histories: tuple[tuple[IncidentRiskEntry, ...], ...],
) -> list[dict[str, object]]:
    return [
        {
            "entry_id": history[0].entry_id,
            "revisions": [entry.to_dict() for entry in history],
        }
        for history in histories
    ]


def _validate_availability(
    histories: tuple[tuple[IncidentRiskEntry, ...], ...],
    availability: object,
) -> tuple[tuple[str, int, str], ...]:
    if type(availability) is not tuple:
        raise IncidentRiskStoreError("availability must be an exact tuple")

    expected = [
        (entry.entry_id, entry.revision)
        for history in histories
        for entry in history
    ]
    normalized: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int]] = set()
    for record in availability:
        if type(record) is not tuple or len(record) != 3:
            raise IncidentRiskStoreError(
                "availability records must be exact (entry_id, revision, available_at) tuples"
            )
        entry_id, revision, available_at = record
        if type(entry_id) is not str or not entry_id:
            raise IncidentRiskStoreError(
                "availability entry_id must be a non-empty string"
            )
        if type(revision) is not int or revision <= 0:
            raise IncidentRiskStoreError(
                "availability revision must be a positive exact integer"
            )
        _canonical_utc_timestamp(available_at, "available_at")
        key = (entry_id, revision)
        if key in seen:
            raise IncidentRiskStoreError(
                "availability cannot contain duplicate revision identities"
            )
        seen.add(key)
        normalized.append((entry_id, revision, available_at))

    keys = [(entry_id, revision) for entry_id, revision, _ in normalized]
    if keys != sorted(keys):
        raise IncidentRiskStoreError(
            "availability records must be sorted by entry_id and revision"
        )
    if set(keys) != set(expected) or len(keys) != len(expected):
        raise IncidentRiskStoreError(
            "availability must bind every durable revision exactly once"
        )

    by_key = {
        (entry_id, revision): _canonical_utc_timestamp(
            available_at, "available_at"
        )
        for entry_id, revision, available_at in normalized
    }
    for history in histories:
        prior: datetime | None = None
        for entry in history:
            current = by_key[(entry.entry_id, entry.revision)]
            entry_updated = _canonical_utc_timestamp(
                entry.updated_at, "entry.updated_at"
            )
            if current < entry_updated:
                raise IncidentRiskStoreError(
                    "revision availability cannot precede entry.updated_at"
                )
            if prior is not None and current < prior:
                raise IncidentRiskStoreError(
                    "revision availability must not move backwards"
                )
            prior = current

    return tuple(normalized)


def _availability_json(
    availability: tuple[tuple[str, int, str], ...],
) -> list[dict[str, object]]:
    return [
        {
            "entry_id": entry_id,
            "revision": revision,
            "available_at": available_at,
        }
        for entry_id, revision, available_at in availability
    ]


def _core_payload(
    histories: tuple[tuple[IncidentRiskEntry, ...], ...],
    availability: tuple[tuple[str, int, str], ...],
) -> dict[str, object]:
    return {
        "schema": STORE_SCHEMA,
        "schema_version": STORE_SCHEMA_VERSION,
        "histories": _histories_json(histories),
        "availability": _availability_json(availability),
    }


def _core_sha256(
    histories: tuple[tuple[IncidentRiskEntry, ...], ...],
    availability: tuple[tuple[str, int, str], ...],
) -> str:
    payload = json.dumps(
        _core_payload(histories, availability),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _authority_tx_id(state_sha256: str) -> str:
    return f"incident-risk-store:{_sha256_text('state_sha256', state_sha256)}"


@dataclass(frozen=True, slots=True)
class IncidentRiskStoreSnapshot:
    """One verified durable history image.

    content_sha256 is deterministic local identity only. Rollback/deletion
    resistance is supplied independently by MonotonicWorkspaceAuthority.
    """

    histories: tuple[tuple[IncidentRiskEntry, ...], ...]
    availability: tuple[tuple[str, int, str], ...]
    content_sha256: str

    def __post_init__(self) -> None:
        normalized = _validate_histories(self.histories)
        if normalized != self.histories:
            raise IncidentRiskStoreError("histories are not canonical")
        normalized_availability = _validate_availability(
            normalized, self.availability
        )
        if normalized_availability != self.availability:
            raise IncidentRiskStoreError("availability is not canonical")
        digest = _sha256_text("content_sha256", self.content_sha256)
        if not hmac.compare_digest(
            digest,
            _core_sha256(normalized, normalized_availability),
        ):
            raise IncidentRiskStoreError(
                "incident/model-risk store content hash mismatch"
            )

    @property
    def current_entries(self) -> tuple[IncidentRiskEntry, ...]:
        return tuple(history[-1] for history in self.histories)

    def current_entries_as_of(
        self,
        as_of: str,
    ) -> tuple[IncidentRiskEntry, ...]:
        """Resolve the latest revision causally available by an exact UTC cutoff."""

        cutoff = _canonical_as_of(as_of)
        available_at = {
            (entry_id, revision): _canonical_utc_timestamp(
                timestamp, "available_at"
            )
            for entry_id, revision, timestamp in self.availability
        }
        visible: list[IncidentRiskEntry] = []
        for history in self.histories:
            latest_visible: IncidentRiskEntry | None = None
            for entry in history:
                if available_at[(entry.entry_id, entry.revision)] <= cutoff:
                    latest_visible = entry
                    continue
                break
            if latest_visible is not None:
                visible.append(latest_visible)
        return tuple(visible)

    def history(self, entry_id: str) -> tuple[IncidentRiskEntry, ...]:
        if type(entry_id) is not str or not entry_id:
            raise IncidentRiskStoreError("entry_id must be a non-empty string")
        for history in self.histories:
            if history[0].entry_id == entry_id:
                return history
        return ()


def _snapshot(
    histories: tuple[tuple[IncidentRiskEntry, ...], ...],
    availability: tuple[tuple[str, int, str], ...],
) -> IncidentRiskStoreSnapshot:
    normalized = _validate_histories(histories)
    normalized_availability = _validate_availability(
        normalized, availability
    )
    return IncidentRiskStoreSnapshot(
        histories=normalized,
        availability=normalized_availability,
        content_sha256=_core_sha256(
            normalized, normalized_availability
        ),
    )


def _payload(snapshot: IncidentRiskStoreSnapshot) -> dict[str, object]:
    if type(snapshot) is not IncidentRiskStoreSnapshot:
        raise IncidentRiskStoreError(
            "persistence requires an exact IncidentRiskStoreSnapshot"
        )
    return {
        **_core_payload(snapshot.histories, snapshot.availability),
        "content_sha256": snapshot.content_sha256,
    }


def _decode_snapshot(text: str) -> IncidentRiskStoreSnapshot:
    if type(text) is not str:
        raise IncidentRiskStoreError(
            "incident/model-risk store JSON must be text"
        )
    try:
        raw = strict_json_loads(text)
    except (TypeError, ValueError) as exc:
        raise IncidentRiskStoreError(
            "invalid incident/model-risk store JSON"
        ) from exc

    if type(raw) is not dict or set(raw) != _ROOT_KEYS:
        raise IncidentRiskStoreError(
            "incident/model-risk store must contain exactly canonical root fields"
        )
    if type(raw["schema"]) is not str or raw["schema"] != STORE_SCHEMA:
        raise IncidentRiskStoreError(
            "unsupported incident/model-risk store schema"
        )
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != STORE_SCHEMA_VERSION
    ):
        raise IncidentRiskStoreError(
            "unsupported incident/model-risk store schema_version"
        )

    stored_digest = _sha256_text(
        "content_sha256", raw["content_sha256"]
    )
    raw_histories = raw["histories"]
    if type(raw_histories) is not list:
        raise IncidentRiskStoreError("histories must be a JSON array")
    raw_availability = raw["availability"]
    if type(raw_availability) is not list:
        raise IncidentRiskStoreError("availability must be a JSON array")

    decoded: list[tuple[IncidentRiskEntry, ...]] = []
    for raw_history in raw_histories:
        if (
            type(raw_history) is not dict
            or set(raw_history) != _HISTORY_KEYS
        ):
            raise IncidentRiskStoreError(
                "history must contain exactly entry_id and revisions"
            )
        entry_id = raw_history["entry_id"]
        if type(entry_id) is not str or not entry_id:
            raise IncidentRiskStoreError(
                "history entry_id must be a non-empty string"
            )
        raw_revisions = raw_history["revisions"]
        if type(raw_revisions) is not list or not raw_revisions:
            raise IncidentRiskStoreError(
                "history revisions must be a non-empty JSON array"
            )
        entries: list[IncidentRiskEntry] = []
        for raw_entry in raw_revisions:
            try:
                entry = IncidentRiskEntry.from_dict(raw_entry)
            except (
                IncidentRiskRegisterError,
                TypeError,
                ValueError,
            ) as exc:
                raise IncidentRiskStoreError(
                    "durable history contains an invalid incident/model-risk entry"
                ) from exc
            if type(entry) is not IncidentRiskEntry:
                raise IncidentRiskStoreError(
                    "decoded durable history entry must be exact IncidentRiskEntry"
                )
            entries.append(entry)
        if entries[0].entry_id != entry_id:
            raise IncidentRiskStoreError(
                "history wrapper entry_id must match its canonical revisions"
            )
        decoded.append(tuple(entries))

    decoded_availability: list[tuple[str, int, str]] = []
    for raw_record in raw_availability:
        if (
            type(raw_record) is not dict
            or set(raw_record) != _AVAILABILITY_KEYS
        ):
            raise IncidentRiskStoreError(
                "availability record must contain exactly canonical fields"
            )
        entry_id = raw_record["entry_id"]
        revision = raw_record["revision"]
        available_at = raw_record["available_at"]
        if type(entry_id) is not str or not entry_id:
            raise IncidentRiskStoreError(
                "availability entry_id must be a non-empty string"
            )
        if type(revision) is not int or revision <= 0:
            raise IncidentRiskStoreError(
                "availability revision must be a positive exact integer"
            )
        _canonical_utc_timestamp(available_at, "available_at")
        decoded_availability.append((entry_id, revision, available_at))

    snapshot = _snapshot(
        tuple(decoded), tuple(decoded_availability)
    )
    if not hmac.compare_digest(
        stored_digest, snapshot.content_sha256
    ):
        raise IncidentRiskStoreError(
            "incident/model-risk store content hash mismatch"
        )
    return snapshot


class IncidentRiskStore:
    """Atomic restart-safe persistence for canonical incident/model-risk history."""

    FILE_NAME: Final = "incident_model_risk_store.json"

    def __init__(
        self,
        workspace: str | Path,
        *,
        authority_root: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().absolute()
        self.path = self.workspace / self.FILE_NAME
        try:
            self._authority = MonotonicWorkspaceAuthority(
                workspace=self.workspace,
                domain=_AUTHORITY_DOMAIN,
                key=_AUTHORITY_KEY,
                authority_root=authority_root,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise IncidentRiskStoreError(
                "cannot initialize independent incident/model-risk continuity authority"
            ) from exc

    @staticmethod
    def empty_snapshot() -> IncidentRiskStoreSnapshot:
        return _snapshot((), ())

    def _read_unlocked(
        self,
    ) -> tuple[IncidentRiskStoreSnapshot, str | None]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self.empty_snapshot(), None
        except (OSError, UnicodeError) as exc:
            raise IncidentRiskStoreError(
                "cannot read durable incident/model-risk store"
            ) from exc
        snapshot = _decode_snapshot(text)
        return snapshot, snapshot.content_sha256

    def _recover_authority_unlocked(
        self,
        *,
        observed_state_sha256: str | None,
    ) -> None:
        try:
            self._authority.recover(
                observed_state_sha256=observed_state_sha256,
                tx_id=(
                    None
                    if observed_state_sha256 is None
                    else _authority_tx_id(observed_state_sha256)
                ),
                semantic_binding_sha256=_AUTHORITY_BINDING_SHA256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise IncidentRiskStoreError(
                "incident/model-risk continuity authority rejects missing, stale, or unproven store state"
            ) from exc

    def _load_unlocked(self) -> IncidentRiskStoreSnapshot:
        snapshot, observed = self._read_unlocked()
        self._recover_authority_unlocked(observed_state_sha256=observed)
        return snapshot

    def load(self) -> IncidentRiskStoreSnapshot:
        with durable_path_lock(self.path):
            return self._load_unlocked()

    def load_as_of(self, as_of: str) -> tuple[IncidentRiskEntry, ...]:
        """Load verified durable truth and project only revisions known by cutoff."""

        return self.load().current_entries_as_of(as_of)

    def append(
        self, entry: IncidentRiskEntry
    ) -> IncidentRiskStoreSnapshot:
        """Atomically append one first revision or exact contiguous successor."""

        if type(entry) is not IncidentRiskEntry:
            raise IncidentRiskStoreError(
                "persistence accepts exact IncidentRiskEntry values only"
            )

        with durable_path_lock(self.path):
            before, observed_before = self._read_unlocked()
            self._recover_authority_unlocked(
                observed_state_sha256=observed_before
            )
            histories = list(before.histories)
            index = next(
                (
                    position
                    for position, history in enumerate(histories)
                    if history[0].entry_id == entry.entry_id
                ),
                None,
            )

            if index is None:
                if entry.revision != 1:
                    raise IncidentRiskStoreError(
                        "a new durable entry must begin at revision 1"
                    )
                histories.append((entry,))
            else:
                history = histories[index]
                latest = history[-1]
                if entry.revision == latest.revision:
                    if hmac.compare_digest(
                        entry.fingerprint_sha256,
                        latest.fingerprint_sha256,
                    ):
                        return before
                    raise IncidentRiskStoreError(
                        "same durable revision cannot be rebound to different content"
                    )
                try:
                    validate_successor(latest, entry)
                except (
                    IncidentRiskRegisterError,
                    TypeError,
                    ValueError,
                ) as exc:
                    raise IncidentRiskStoreError(
                        "durable append must be the exact contiguous successor"
                    ) from exc
                histories[index] = history + (entry,)

            histories.sort(key=lambda history: history[0].entry_id)

            available_at = _availability_now()
            available_time = _canonical_utc_timestamp(
                available_at, "available_at"
            )
            entry_updated_at = _canonical_utc_timestamp(
                entry.updated_at, "entry.updated_at"
            )
            if available_time < entry_updated_at:
                raise IncidentRiskStoreError(
                    "durable revision availability cannot precede entry.updated_at"
                )
            if before.availability:
                latest_available = max(
                    _canonical_utc_timestamp(
                        timestamp, "available_at"
                    )
                    for _, _, timestamp in before.availability
                )
                if available_time < latest_available:
                    raise IncidentRiskStoreError(
                        "durable revision availability clock moved backwards"
                    )
            availability = list(before.availability)
            availability.append(
                (entry.entry_id, entry.revision, available_at)
            )
            availability.sort(key=lambda item: (item[0], item[1]))

            intended = _snapshot(
                tuple(histories), tuple(availability)
            )
            tx_id = _authority_tx_id(intended.content_sha256)
            try:
                self._authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=observed_before,
                    intended_state_sha256=intended.content_sha256,
                    semantic_binding_sha256=_AUTHORITY_BINDING_SHA256,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise IncidentRiskStoreError(
                    "incident/model-risk continuity authority rejected publication prepare"
                ) from exc

            atomic_write_json(self.path, _payload(intended))
            published, observed_published = self._read_unlocked()
            if (
                observed_published is None
                or not hmac.compare_digest(
                    published.content_sha256,
                    intended.content_sha256,
                )
            ):
                raise IncidentRiskStoreError(
                    "published incident/model-risk store does not match intended history"
                )
            try:
                self._authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=observed_published,
                    semantic_binding_sha256=_AUTHORITY_BINDING_SHA256,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise IncidentRiskStoreError(
                    "incident/model-risk continuity authority rejected publication commit"
                ) from exc
            return published
