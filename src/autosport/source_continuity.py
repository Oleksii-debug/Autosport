from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads


_ALLOWED_CONTINUITY_STATUSES = frozenset({"unknown", "verified"})
_SCHEMA_VERSION = 1
_STATE_FIELDS = frozenset(
    {
        "source_id",
        "status",
        "trusted_token",
        "last_observed_cursor",
        "last_success_at",
        "last_failure_at",
        "reason",
    }
)


def _text(value: object, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or value.strip() != value:
        suffix = " or null" if nullable else ""
        raise ValueError(f"{name} must be a non-empty trimmed string{suffix}")
    return value


def _cursor(value: object) -> str | None:
    """Preserve the existing ProviderBatch cursor domain as opaque telemetry."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("cursor must be str or null")
    return value


def _instant(value: object, name: str, *, nullable: bool = False) -> str | None:
    raw = _text(value, name, nullable=nullable)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return raw


@dataclass(frozen=True, slots=True)
class ProviderContinuityWitness:
    """Provider-owned proof that one cursor transition covers the intervening interval."""

    previous_token: str | None
    current_token: str
    backfill_complete: bool = True

    def __post_init__(self) -> None:
        _text(self.previous_token, "previous_token", nullable=True)
        _text(self.current_token, "current_token")
        if type(self.backfill_complete) is not bool:
            raise TypeError("backfill_complete must be bool")


@dataclass(frozen=True, slots=True)
class SourceContinuityState:
    """Durable continuity truth kept separate from current-snapshot health."""

    source_id: str
    status: str = "unknown"
    trusted_token: str | None = None
    last_observed_cursor: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    reason: str = "no_continuity_evidence"

    def __post_init__(self) -> None:
        _text(self.source_id, "source_id")
        if self.status not in _ALLOWED_CONTINUITY_STATUSES:
            raise ValueError("invalid source continuity status")
        _text(self.trusted_token, "trusted_token", nullable=True)
        _cursor(self.last_observed_cursor)
        _instant(self.last_success_at, "last_success_at", nullable=True)
        _instant(self.last_failure_at, "last_failure_at", nullable=True)
        _text(self.reason, "reason")
        if self.status == "verified" and self.trusted_token is None:
            raise ValueError("verified continuity requires a trusted provider token")


class SourceContinuityStore:
    """Durable fail-closed provider continuity authority.

    Current snapshot health and historical continuity are deliberately independent:
    a successful snapshot without an explicit provider-owned continuity witness is
    stored as ``unknown`` rather than being promoted to continuous-history proof.
    Tokens are opaque identities; this contract never invents numeric contiguity.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with durable_path_lock(self.path):
            if not self.path.exists():
                atomic_write_json(
                    self.path,
                    {"schema_version": _SCHEMA_VERSION, "sources": {}},
                )
            self._read_locked()

    @staticmethod
    def _state_from_payload(source_id: str, payload: object) -> SourceContinuityState:
        if not isinstance(payload, dict) or set(payload) != _STATE_FIELDS:
            raise ValueError("invalid source continuity state fields")
        if payload.get("source_id") != source_id:
            raise ValueError("source continuity state identity mismatch")
        return SourceContinuityState(**payload)

    def _read_locked(self) -> dict[str, object]:
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("invalid source continuity store") from exc
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "sources"}
            or raw.get("schema_version") != _SCHEMA_VERSION
            or not isinstance(raw.get("sources"), dict)
        ):
            raise ValueError("invalid source continuity store")
        sources = raw["sources"]
        assert isinstance(sources, dict)
        for source_id, payload in sources.items():
            _text(source_id, "source_id")
            self._state_from_payload(source_id, payload)
        return raw

    def get(self, source_id: str) -> SourceContinuityState:
        source_id = _text(source_id, "source_id")
        assert isinstance(source_id, str)
        with durable_path_lock(self.path):
            raw = self._read_locked()
            sources = raw["sources"]
            assert isinstance(sources, dict)
            payload = sources.get(source_id)
            if payload is None:
                return SourceContinuityState(source_id=source_id)
            return self._state_from_payload(source_id, payload)

    def _write_state_locked(
        self,
        raw: dict[str, object],
        state: SourceContinuityState,
    ) -> SourceContinuityState:
        sources = raw["sources"]
        assert isinstance(sources, dict)
        sources[state.source_id] = asdict(state)
        atomic_write_json(self.path, raw)
        self._read_locked()
        return state

    def record_failure(self, source_id: str, *, now: str) -> SourceContinuityState:
        source_id = _text(source_id, "source_id")
        now = _instant(now, "now")
        assert isinstance(source_id, str)
        assert isinstance(now, str)
        with durable_path_lock(self.path):
            raw = self._read_locked()
            sources = raw["sources"]
            assert isinstance(sources, dict)
            payload = sources.get(source_id)
            before = (
                SourceContinuityState(source_id=source_id)
                if payload is None
                else self._state_from_payload(source_id, payload)
            )
            state = SourceContinuityState(
                source_id=source_id,
                status="unknown",
                trusted_token=before.trusted_token,
                last_observed_cursor=before.last_observed_cursor,
                last_success_at=before.last_success_at,
                last_failure_at=now,
                reason="provider_failure_since_last_continuity_proof",
            )
            return self._write_state_locked(raw, state)

    def record_success(
        self,
        source_id: str,
        *,
        now: str,
        cursor: str | None,
        witness: ProviderContinuityWitness | None,
    ) -> SourceContinuityState:
        source_id = _text(source_id, "source_id")
        now = _instant(now, "now")
        cursor = _cursor(cursor)
        if witness is not None and not isinstance(witness, ProviderContinuityWitness):
            raise TypeError("witness must be ProviderContinuityWitness or null")
        assert isinstance(source_id, str)
        assert isinstance(now, str)

        with durable_path_lock(self.path):
            raw = self._read_locked()
            sources = raw["sources"]
            assert isinstance(sources, dict)
            payload = sources.get(source_id)
            before = (
                SourceContinuityState(source_id=source_id)
                if payload is None
                else self._state_from_payload(source_id, payload)
            )

            trusted_token = before.trusted_token
            status = "unknown"
            reason = "provider_continuity_witness_absent"

            if witness is not None:
                if cursor is None or witness.current_token != cursor:
                    reason = "witness_current_token_mismatch"
                elif trusted_token is None:
                    trusted_token = witness.current_token
                    reason = "provider_anchor_established_without_prior_continuity"
                elif witness.previous_token != trusted_token:
                    reason = "witness_previous_token_mismatch"
                elif not witness.backfill_complete:
                    reason = "provider_backfill_incomplete"
                else:
                    trusted_token = witness.current_token
                    status = "verified"
                    reason = "provider_chain_and_backfill_verified"

            state = SourceContinuityState(
                source_id=source_id,
                status=status,
                trusted_token=trusted_token,
                last_observed_cursor=cursor,
                last_success_at=now,
                last_failure_at=before.last_failure_at,
                reason=reason,
            )
            return self._write_state_locked(raw, state)
