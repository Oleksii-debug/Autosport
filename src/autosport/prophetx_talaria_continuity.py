"""Fail-closed, transport-independent ProphetX Talaria continuity state.

Market frames are dirty signals, not provider-sequenced state. This module does
not open sockets, hold credentials, prove provider origin, mutate quote state,
or authorize decisions/writes. REBASED only means that every declared scope has
an exact post-generation snapshot reference supplied by a higher authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

_PROTOCOL = "prophetx-talaria-continuity-v1"
_EVENTS = frozenset({
    "pusher:connection_established", "pusher:signin_success",
    "pusher_internal:subscription_succeeded", "pusher:error", "market_selections",
})


class ProphetXTalariaError(RuntimeError):
    pass


class ProphetXTalariaContractError(ProphetXTalariaError):
    pass


class ProphetXTalariaGenerationError(ProphetXTalariaError):
    pass


class TalariaContinuityState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    REGISTERED = "registered"
    SIGNED_IN = "signed_in"
    REBASE_REQUIRED = "rebase_required"
    REBASED = "rebased"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class TalariaScope:
    event_id: str
    sub_type: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _event_id(self.event_id))
        if self.sub_type is not None:
            object.__setattr__(self, "sub_type", _sub_type(self.sub_type))

    @property
    def canonical_id(self) -> str:
        if self.sub_type is None:
            return f"event:{self.event_id}"
        return f"event_subtype:{self.event_id}:{self.sub_type}"


@dataclass(frozen=True, slots=True)
class TalariaFrame:
    event: str
    channel: str | None
    data: Mapping[str, Any]
    frame_sha256: str


@dataclass(frozen=True, slots=True)
class TalariaGenerationEvidence:
    generation_id: str
    socket_id_sha256: str
    config_sha256: str
    started_at: str
    subscription_digest: str | None
    coverage_generation_id: str | None
    registration_evidence_sha256: str | None
    rebased_snapshots: tuple[tuple[str, str, str], ...]
    declared_scopes: tuple[TalariaScope, ...]
    acknowledged_scopes: tuple[TalariaScope, ...]
    rebase_required_scopes: tuple[TalariaScope, ...]
    dirty_scopes: tuple[TalariaScope, ...]
    state: TalariaContinuityState

    @property
    def gap_free_continuity_proven(self) -> bool:
        return False

    @property
    def provider_origin_proven(self) -> bool:
        return False

    @property
    def canonical_market_state_authority(self) -> bool:
        return False

    @property
    def decision_eligible(self) -> bool:
        return False

    @property
    def provider_write_authority(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False


def _strict_json(text: str, label: str) -> Any:
    if type(text) is not str:
        raise ProphetXTalariaContractError(f"{label} must be JSON text")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise ProphetXTalariaContractError(f"{label} has duplicate key")
            out[key] = value
        return out

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda _v: (_ for _ in ()).throw(
                ProphetXTalariaContractError(f"{label} has non-finite JSON")
            ),
        )
    except ProphetXTalariaContractError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProphetXTalariaContractError(f"{label} is invalid JSON") from exc


def parse_talaria_frame(raw: bytes | str) -> TalariaFrame:
    if type(raw) is bytes:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProphetXTalariaContractError("frame is not UTF-8") from exc
    elif type(raw) is str:
        text = raw
    else:
        raise ProphetXTalariaContractError("frame must be bytes or text")
    if not text or len(text.encode("utf-8")) > 8 * 1024 * 1024:
        raise ProphetXTalariaContractError("frame is empty or too large")
    outer = _strict_json(text, "outer frame")
    if type(outer) is not dict or set(outer) - {"event", "data", "channel"}:
        raise ProphetXTalariaContractError("outer frame shape is invalid")
    event, data_text, channel = outer.get("event"), outer.get("data"), outer.get("channel")
    if type(event) is not str or event not in _EVENTS:
        raise ProphetXTalariaContractError("unsupported event")
    if type(data_text) is not str:
        raise ProphetXTalariaContractError("data must be double-JSON text")
    if channel is not None and (type(channel) is not str or not channel.strip()):
        raise ProphetXTalariaContractError("channel is invalid")
    data = _strict_json(data_text, "inner data")
    if type(data) is not dict:
        raise ProphetXTalariaContractError("inner data must be an object")
    return TalariaFrame(event, channel, data, sha256(text.encode()).hexdigest())


def _event_id(value: object) -> str:
    if type(value) is int and value >= 0:
        return str(value)
    if type(value) is str and value == value.strip() and value.isascii() and value.isdigit():
        return value
    raise ProphetXTalariaContractError("event_id is invalid")


def _sub_type(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProphetXTalariaContractError("sub_type is invalid")
    if len(value) > 512 or any(ch in value for ch in "\r\n\x00"):
        raise ProphetXTalariaContractError("sub_type is invalid")
    return value


def _scope_from_market(data: Mapping[str, Any]) -> TalariaScope:
    scope = data.get("scope")
    if type(scope) is not dict or set(scope) - {"event_id", "sub_type"} or "event_id" not in scope:
        raise ProphetXTalariaContractError("market payload scope is invalid")
    subtype = scope.get("sub_type")
    return TalariaScope(scope["event_id"], None if subtype is None else _sub_type(subtype))


def _time(value: object, name: str) -> datetime:
    if type(value) is not str or value != value.strip() or not value:
        raise ProphetXTalariaContractError(f"{name} is invalid")
    value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProphetXTalariaContractError(f"{name} is invalid") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ProphetXTalariaContractError(f"{name} must be timezone-aware")
    return dt.astimezone(timezone.utc)


def _ctime(value: object, name: str) -> str:
    return _time(value, name).isoformat()


def _digest(value: object, name: str) -> str:
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ProphetXTalariaContractError(f"{name} must be lowercase SHA-256")
    return value


def _scope_key(scope: TalariaScope) -> tuple[str, str]:
    return scope.event_id, scope.sub_type or ""


def _scope_set(values: Iterable[TalariaScope]) -> tuple[TalariaScope, ...]:
    values = tuple(values)
    if not values or any(type(v) is not TalariaScope for v in values) or len(values) != len(set(values)):
        raise ProphetXTalariaContractError("subscription scopes are invalid")
    return tuple(sorted(values, key=_scope_key))


def _scope_digest(values: tuple[TalariaScope, ...]) -> str:
    body = json.dumps([v.canonical_id for v in values], separators=(",", ":")).encode()
    return sha256(b"autosport.talaria.scope.v1\x00" + body).hexdigest()


class ProphetXTalariaContinuity:
    def __init__(self) -> None:
        self._generation_id: str | None = None
        self._socket_hash: str | None = None
        self._config: str | None = None
        self._started: str | None = None
        self._scopes: tuple[TalariaScope, ...] = ()
        self._subscription_digest: str | None = None
        self._coverage_id: str | None = None
        self._registration_digest: str | None = None
        self._channels: dict[str, TalariaScope] = {}
        self._acks: set[TalariaScope] = set()
        self._rebase: set[TalariaScope] = set()
        self._dirty: set[TalariaScope] = set()
        self._seen_dirty_frames: set[tuple[TalariaScope, str]] = set()
        self._snapshots: dict[TalariaScope, tuple[str, str]] = {}
        self._signin = False
        self._state = TalariaContinuityState.DISCONNECTED
        self._generation_counter = 0
        self._coverage_counter = 0
        self._last_time: str | None = None

    @property
    def state(self) -> TalariaContinuityState:
        return self._state

    @property
    def generation_id(self) -> str | None:
        return self._generation_id

    def _clock(self, value: object) -> str:
        current = _ctime(value, "observed_at")
        if self._last_time and _time(current, "observed_at") < _time(self._last_time, "last_observed_at"):
            raise ProphetXTalariaContractError("product clock moved backwards")
        self._last_time = current
        return current

    def _require(self, generation_id: object) -> None:
        if self._generation_id is None or self._state is TalariaContinuityState.DISCONNECTED:
            raise ProphetXTalariaGenerationError("no active generation")
        if type(generation_id) is not str or generation_id != self._generation_id:
            raise ProphetXTalariaGenerationError("stale generation")

    def establish_generation(self, raw_frame: bytes | str, *, config_sha256: str, observed_at: str) -> str:
        frame = parse_talaria_frame(raw_frame)
        if frame.event != "pusher:connection_established":
            raise ProphetXTalariaContractError("generation requires connection_established")
        socket_id = frame.data.get("socket_id")
        if type(socket_id) is not str or not socket_id or socket_id != socket_id.strip() or len(socket_id) > 512:
            raise ProphetXTalariaContractError("socket_id is invalid")
        config, observed = _digest(config_sha256, "config_sha256"), self._clock(observed_at)
        self._generation_counter += 1
        socket_hash = sha256(socket_id.encode()).hexdigest()
        seed = f"{_PROTOCOL}\x00{self._generation_counter}\x00{config}\x00{socket_hash}\x00{observed}".encode()
        self._generation_id = sha256(seed).hexdigest()
        self._socket_hash, self._config, self._started = socket_hash, config, observed
        self._scopes, self._subscription_digest, self._coverage_id, self._registration_digest = (), None, None, None
        self._channels.clear(); self._acks.clear(); self._rebase.clear(); self._dirty.clear(); self._seen_dirty_frames.clear(); self._snapshots.clear()
        self._signin = False
        self._state = TalariaContinuityState.CONNECTED
        return self._generation_id

    def bind_registration(self, generation_id: str, *, declared_scopes: Iterable[TalariaScope], channel_scope: Mapping[str, TalariaScope], channel_limit: int, registration_evidence_sha256: str, observed_at: str) -> str:
        self._require(generation_id); self._clock(observed_at)
        scopes = _scope_set(declared_scopes)
        reg = _digest(registration_evidence_sha256, "registration_evidence_sha256")
        if type(channel_limit) is not int or channel_limit < 1 or len(scopes) > channel_limit:
            raise ProphetXTalariaContractError("channel_limit is invalid or exceeded")
        if type(channel_scope) is not dict or not channel_scope:
            raise ProphetXTalariaContractError("authorized channels are required")
        channels: dict[str, TalariaScope] = {}
        for channel, scope in channel_scope.items():
            if type(channel) is not str or not channel or channel != channel.strip() or type(scope) is not TalariaScope:
                raise ProphetXTalariaContractError("authorized channel mapping is invalid")
            channels[channel] = scope
        if len(channels) != len(scopes) or set(channels.values()) != set(scopes):
            raise ProphetXTalariaContractError("authorized channels must exactly cover scopes")
        self._scopes, self._subscription_digest, self._registration_digest = scopes, _scope_digest(scopes), reg
        self._coverage_counter += 1
        seed = f"{_PROTOCOL}\x00coverage\x00{generation_id}\x00{self._coverage_counter}\x00{self._subscription_digest}\x00{reg}".encode()
        self._coverage_id = sha256(seed).hexdigest()
        self._channels = channels
        self._acks.clear(); self._rebase = set(scopes); self._dirty = set(scopes); self._seen_dirty_frames.clear(); self._snapshots.clear()
        self._signin = False
        self._state = TalariaContinuityState.REGISTERED
        return self._subscription_digest

    def _handshake(self) -> bool:
        return self._signin and bool(self._scopes) and self._acks == set(self._scopes)

    def _degrade(self) -> None:
        self._state = TalariaContinuityState.DEGRADED
        self._dirty.update(self._scopes); self._rebase.update(self._scopes)

    def handle_frame(self, generation_id: str, raw_frame: bytes | str, *, observed_at: str) -> TalariaScope | None:
        self._require(generation_id)
        if self._state is TalariaContinuityState.DEGRADED:
            raise ProphetXTalariaGenerationError("degraded generation must reconnect")
        frame = parse_talaria_frame(raw_frame); self._clock(observed_at)
        if frame.event == "pusher:connection_established":
            raise ProphetXTalariaContractError("new connection requires new generation")
        if frame.event == "pusher:error":
            self._degrade(); return None
        if frame.event == "pusher:signin_success":
            if self._state not in {TalariaContinuityState.REGISTERED, TalariaContinuityState.SIGNED_IN}:
                raise ProphetXTalariaContractError("signin_success before registration")
            self._signin = True; self._state = TalariaContinuityState.SIGNED_IN
            if self._handshake(): self._state = TalariaContinuityState.REBASE_REQUIRED
            return None
        if frame.event == "pusher_internal:subscription_succeeded":
            if not self._signin:
                raise ProphetXTalariaContractError("subscription ack before signin")
            if frame.channel is None or frame.channel not in self._channels:
                raise ProphetXTalariaContractError("subscription ack for undeclared channel")
            scope = self._channels[frame.channel]; self._acks.add(scope)
            if self._handshake(): self._state = TalariaContinuityState.REBASE_REQUIRED
            return scope
        if frame.event != "market_selections":
            raise ProphetXTalariaContractError("unsupported event")
        if not self._handshake():
            raise ProphetXTalariaContractError("market update before complete handshake")
        if frame.channel is None or frame.channel not in self._channels:
            self._degrade(); raise ProphetXTalariaContractError("market update on undeclared channel")
        scope = _scope_from_market(frame.data)
        if scope != self._channels[frame.channel]:
            self._degrade(); raise ProphetXTalariaContractError("provider scope mismatch")
        marker = (scope, frame.frame_sha256)
        if marker not in self._seen_dirty_frames:
            self._seen_dirty_frames.add(marker); self._dirty.add(scope); self._rebase.add(scope)
            self._state = TalariaContinuityState.REBASE_REQUIRED
        return scope

    def acknowledge_rebase(self, generation_id: str, *, scope: TalariaScope, snapshot_evidence_sha256: str, snapshot_available_at: str, observed_at: str) -> None:
        self._require(generation_id)
        if self._state is TalariaContinuityState.DEGRADED:
            raise ProphetXTalariaGenerationError("degraded generation must reconnect")
        if not self._handshake() or type(scope) is not TalariaScope or scope not in set(self._scopes):
            raise ProphetXTalariaContractError("rebase preconditions failed")
        digest = _digest(snapshot_evidence_sha256, "snapshot_evidence_sha256")
        snapshot_time, observed = _ctime(snapshot_available_at, "snapshot_available_at"), self._clock(observed_at)
        assert self._started is not None
        if _time(snapshot_time, "snapshot_available_at") < _time(self._started, "started_at"):
            raise ProphetXTalariaContractError("pre-generation snapshot cannot clear rebase")
        if _time(snapshot_time, "snapshot_available_at") > _time(observed, "observed_at"):
            raise ProphetXTalariaContractError("future snapshot cannot clear rebase")
        self._snapshots[scope] = (digest, snapshot_time); self._rebase.discard(scope); self._dirty.discard(scope)
        self._seen_dirty_frames = {x for x in self._seen_dirty_frames if x[0] != scope}
        self._state = TalariaContinuityState.REBASED if not self._rebase else TalariaContinuityState.REBASE_REQUIRED

    def disconnect(self, generation_id: str, *, observed_at: str) -> None:
        self._require(generation_id); self._clock(observed_at)
        self._dirty.update(self._scopes); self._rebase.update(self._scopes); self._state = TalariaContinuityState.DISCONNECTED

    def invalidate_config(self, generation_id: str, *, new_config_sha256: str, observed_at: str) -> None:
        self._require(generation_id); new = _digest(new_config_sha256, "new_config_sha256"); self._clock(observed_at)
        if new != self._config: self._degrade()

    def evidence(self) -> TalariaGenerationEvidence:
        if not all((self._generation_id, self._socket_hash, self._config, self._started)):
            raise ProphetXTalariaGenerationError("no generation evidence")
        return TalariaGenerationEvidence(
            self._generation_id, self._socket_hash, self._config, self._started,
            self._subscription_digest, self._coverage_id, self._registration_digest,
            tuple((s.canonical_id, *self._snapshots[s]) for s in sorted(self._snapshots, key=_scope_key)),
            self._scopes, tuple(sorted(self._acks, key=_scope_key)), tuple(sorted(self._rebase, key=_scope_key)),
            tuple(sorted(self._dirty, key=_scope_key)), self._state,
        )
