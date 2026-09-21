"""Durable Betfair stream reconnect continuity witness.

The witness records structurally complete provider-specific resume/catch-up evidence
for one exact subscription. Structural validity never grants product live-data
authority and does not prove preservation of every raw intermediate tick, market
causality, execution truth, settlement truth, or money-moving authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads


_SCHEMA = "autosport.betfair_stream_resume"
_SCHEMA_VERSION = 1
_TOP_LEVEL_KEYS = frozenset({"schema", "schema_version", "records"})
_RECORD_KEYS = frozenset(
    {"previous_record_sha256", "witness", "witness_id", "record_sha256"}
)
_HEX = frozenset("0123456789abcdef")


class BetfairStreamResumeError(ValueError):
    """Raised when resume evidence is ambiguous, conflicting, or corrupt."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairStreamResumeError(
            f"{field} must be a non-empty canonical string"
        )
    if "\x00" in value:
        raise BetfairStreamResumeError(f"{field} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise BetfairStreamResumeError(
            f"{field} must be valid UTF-8 text"
        ) from exc
    return value


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in _HEX for character in digest)
    ):
        raise BetfairStreamResumeError(
            f"{field} must be a lowercase SHA-256 hex digest"
        )
    return digest


def _instant(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairStreamResumeError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairStreamResumeError(
            f"{field} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise BetfairStreamResumeError(
            f"{field} must be a non-negative integer"
        )
    return value


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BetfairStreamResumeWitness:
    """Structural evidence envelope for one exact RESUB_DELTA resume."""

    provider_id: str
    account_id: str
    environment: str
    stream_endpoint: str
    subscription_sha256: str
    pre_disconnect_initial_clk: str
    pre_disconnect_clk: str
    pre_disconnect_pt_ms: int
    reconnect_id: str
    reconnect_started_at: str
    resent_subscription_sha256: str
    first_change_type: str
    resumed_clk: str
    resumed_pt_ms: int
    resub_delta_payload_sha256: str
    cache_before_sha256: str
    cache_after_sha256: str
    image_replacement_seen: bool
    conflated_seen: bool
    received_at: str

    def __post_init__(self) -> None:
        for field in (
            "provider_id",
            "account_id",
            "environment",
            "stream_endpoint",
            "pre_disconnect_initial_clk",
            "pre_disconnect_clk",
            "reconnect_id",
            "resumed_clk",
        ):
            _text(getattr(self, field), field)
        _sha256(self.subscription_sha256, "subscription_sha256")
        _sha256(
            self.resent_subscription_sha256,
            "resent_subscription_sha256",
        )
        _sha256(
            self.resub_delta_payload_sha256,
            "resub_delta_payload_sha256",
        )
        _sha256(self.cache_before_sha256, "cache_before_sha256")
        _sha256(self.cache_after_sha256, "cache_after_sha256")
        _nonnegative_int(self.pre_disconnect_pt_ms, "pre_disconnect_pt_ms")
        _nonnegative_int(self.resumed_pt_ms, "resumed_pt_ms")

        if self.subscription_sha256 != self.resent_subscription_sha256:
            raise BetfairStreamResumeError(
                "reconnect must resend the exact durable subscription"
            )
        if self.first_change_type != "RESUB_DELTA":
            raise BetfairStreamResumeError(
                "first accepted reconnect change must be RESUB_DELTA"
            )
        if self.resumed_pt_ms < self.pre_disconnect_pt_ms:
            raise BetfairStreamResumeError(
                "resumed provider publish time regresses before disconnect state"
            )
        started = _instant(self.reconnect_started_at, "reconnect_started_at")
        received = _instant(self.received_at, "received_at")
        if received < started:
            raise BetfairStreamResumeError(
                "received_at cannot precede reconnect_started_at"
            )
        if type(self.image_replacement_seen) is not bool:
            raise BetfairStreamResumeError(
                "image_replacement_seen must be boolean"
            )
        if type(self.conflated_seen) is not bool:
            raise BetfairStreamResumeError("conflated_seen must be boolean")

    @property
    def witness_id(self) -> str:
        return _digest(self.to_payload())

    @property
    def grants_product_live_authority(self) -> bool:
        """Structural/caller-created evidence can never authorize live product use."""
        return False

    @property
    def raw_tick_completeness_proven(self) -> bool:
        return False

    def require_raw_tick_completeness(self) -> None:
        raise BetfairStreamResumeError(
            "Betfair reconnect continuity does not prove preservation "
            "of every raw intermediate tick"
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "stream_endpoint": self.stream_endpoint,
            "subscription_sha256": self.subscription_sha256,
            "pre_disconnect_initial_clk": self.pre_disconnect_initial_clk,
            "pre_disconnect_clk": self.pre_disconnect_clk,
            "pre_disconnect_pt_ms": self.pre_disconnect_pt_ms,
            "reconnect_id": self.reconnect_id,
            "reconnect_started_at": self.reconnect_started_at,
            "resent_subscription_sha256": self.resent_subscription_sha256,
            "first_change_type": self.first_change_type,
            "resumed_clk": self.resumed_clk,
            "resumed_pt_ms": self.resumed_pt_ms,
            "resub_delta_payload_sha256": self.resub_delta_payload_sha256,
            "cache_before_sha256": self.cache_before_sha256,
            "cache_after_sha256": self.cache_after_sha256,
            "image_replacement_seen": self.image_replacement_seen,
            "conflated_seen": self.conflated_seen,
            "received_at": self.received_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "BetfairStreamResumeWitness":
        if type(payload) is not dict:
            raise BetfairStreamResumeError(
                "resume witness payload must be an object"
            )
        expected = frozenset(cls.__dataclass_fields__)
        if frozenset(payload) != expected:
            raise BetfairStreamResumeError(
                "resume witness payload has unexpected or missing fields"
            )
        return cls(**payload)


class BetfairStreamResumeStore:
    """Append-only logical store for reconnect witnesses.

    Publication uses Autosport's durable atomic JSON helper under its per-path
    cross-process lock. Each logical record also binds the previous record digest
    so accidental/corrupt middle-of-history rewrites fail closed on reopen.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(
        self, witness: BetfairStreamResumeWitness
    ) -> BetfairStreamResumeWitness:
        if not isinstance(witness, BetfairStreamResumeWitness):
            raise TypeError("witness must be BetfairStreamResumeWitness")

        with durable_path_lock(self.path):
            records = self._load_records_unlocked()
            existing = self._find_reconnect_id(records, witness.reconnect_id)
            if existing is not None:
                if existing.witness_id != witness.witness_id:
                    raise BetfairStreamResumeError(
                        "reconnect_id already has conflicting durable evidence"
                    )
                return existing

            previous = (
                None
                if not records
                else _sha256(records[-1]["record_sha256"], "record_sha256")
            )
            base_record: dict[str, object] = {
                "previous_record_sha256": previous,
                "witness": witness.to_payload(),
                "witness_id": witness.witness_id,
            }
            record = {
                **base_record,
                "record_sha256": _digest(base_record),
            }
            records.append(record)
            atomic_write_json(
                self.path,
                {
                    "schema": _SCHEMA,
                    "schema_version": _SCHEMA_VERSION,
                    "records": records,
                },
            )
        return witness

    def witnesses(self) -> tuple[BetfairStreamResumeWitness, ...]:
        with durable_path_lock(self.path):
            records = self._load_records_unlocked()
        return tuple(
            BetfairStreamResumeWitness.from_payload(record["witness"])
            for record in records
        )

    def latest_for_scope(
        self,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
        stream_endpoint: str,
        subscription_sha256: str,
    ) -> BetfairStreamResumeWitness | None:
        scope = (
            _text(provider_id, "provider_id"),
            _text(account_id, "account_id"),
            _text(environment, "environment"),
            _text(stream_endpoint, "stream_endpoint"),
            _sha256(subscription_sha256, "subscription_sha256"),
        )
        matches = [
            witness
            for witness in self.witnesses()
            if (
                witness.provider_id,
                witness.account_id,
                witness.environment,
                witness.stream_endpoint,
                witness.subscription_sha256,
            )
            == scope
        ]
        return matches[-1] if matches else None

    def _load_records_unlocked(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise BetfairStreamResumeError(
                "resume witness store must be UTF-8 JSON"
            ) from exc
        try:
            payload = strict_json_loads(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise BetfairStreamResumeError(
                "resume witness store is not strict JSON"
            ) from exc
        if type(payload) is not dict or frozenset(payload) != _TOP_LEVEL_KEYS:
            raise BetfairStreamResumeError(
                "resume witness store has invalid top-level schema"
            )
        if payload["schema"] != _SCHEMA:
            raise BetfairStreamResumeError(
                "resume witness store schema is unsupported"
            )
        if payload["schema_version"] != _SCHEMA_VERSION:
            raise BetfairStreamResumeError(
                "resume witness store schema_version is unsupported"
            )
        raw_records = payload["records"]
        if type(raw_records) is not list:
            raise BetfairStreamResumeError(
                "resume witness store records must be a list"
            )

        records: list[dict[str, object]] = []
        seen_reconnect_ids: dict[str, str] = {}
        previous: str | None = None
        for raw in raw_records:
            if type(raw) is not dict or frozenset(raw) != _RECORD_KEYS:
                raise BetfairStreamResumeError(
                    "resume witness record has invalid schema"
                )
            if raw["previous_record_sha256"] != previous:
                raise BetfairStreamResumeError(
                    "resume witness record chain is broken"
                )
            witness = BetfairStreamResumeWitness.from_payload(raw["witness"])
            witness_id = _sha256(raw["witness_id"], "witness_id")
            if witness_id != witness.witness_id:
                raise BetfairStreamResumeError(
                    "resume witness_id does not match witness payload"
                )
            record_sha256 = _sha256(
                raw["record_sha256"], "record_sha256"
            )
            base_record = {
                "previous_record_sha256": previous,
                "witness": witness.to_payload(),
                "witness_id": witness_id,
            }
            if record_sha256 != _digest(base_record):
                raise BetfairStreamResumeError(
                    "resume witness record digest mismatch"
                )
            known = seen_reconnect_ids.get(witness.reconnect_id)
            if known is not None:
                if known != witness_id:
                    raise BetfairStreamResumeError(
                        "reconnect_id has conflicting persisted evidence"
                    )
                raise BetfairStreamResumeError(
                    "duplicate reconnect_id record is not canonical"
                )
            seen_reconnect_ids[witness.reconnect_id] = witness_id
            records.append(
                {
                    "previous_record_sha256": previous,
                    "witness": witness.to_payload(),
                    "witness_id": witness_id,
                    "record_sha256": record_sha256,
                }
            )
            previous = record_sha256
        return records

    @staticmethod
    def _find_reconnect_id(
        records: list[dict[str, object]], reconnect_id: str
    ) -> BetfairStreamResumeWitness | None:
        reconnect_id = _text(reconnect_id, "reconnect_id")
        for record in records:
            witness = BetfairStreamResumeWitness.from_payload(
                record["witness"]
            )
            if witness.reconnect_id == reconnect_id:
                return witness
        return None
