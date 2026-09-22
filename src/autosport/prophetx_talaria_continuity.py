from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_ID_RE = re.compile(r"^[1-9][0-9]*$")
_ALLOWED_SUBSCRIPTION_TYPES = frozenset({"event", "event_subtype"})


class TalariaProtocolError(ValueError):
    """Provider wire evidence is malformed or insufficient for causal publication."""


class TalariaContinuityError(RuntimeError):
    """Local connection-generation or continuity precondition was violated."""


def _strict_json(raw: str, *, field: str) -> Any:
    if not isinstance(raw, str):
        raise TalariaProtocolError(f"{field} must be JSON text")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TalariaProtocolError(f"{field} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> None:
        raise TalariaProtocolError(
            f"{field} contains non-standard JSON constant {value!r}"
        )

    try:
        return json.loads(
            raw,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise TalariaProtocolError(f"{field} is invalid JSON") from exc


def _trimmed(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TalariaProtocolError(f"{field} must be a non-empty trimmed string")
    return value


def _event_id(value: object, *, field: str = "event_id") -> str:
    if isinstance(value, bool):
        raise TalariaProtocolError(f"{field} must be a positive provider event id")
    if isinstance(value, int):
        value = str(value)
    text = _trimmed(value, field=field)
    if not _EVENT_ID_RE.fullmatch(text):
        raise TalariaProtocolError(f"{field} must be a positive provider event id")
    return text


def _positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TalariaProtocolError(f"{field} must be a positive non-boolean integer")
    return value


def _sha256(value: object, *, field: str) -> str:
    text = _trimmed(value, field=field)
    if not _SHA256_RE.fullmatch(text):
        raise TalariaProtocolError(f"{field} must be lowercase SHA-256 hex")
    return text


def _canonical_json_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise TalariaProtocolError(
            "connection config must be canonical JSON evidence"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class TalariaFrame:
    event: str
    channel: str | None
    data: Mapping[str, Any]


def parse_talaria_frame(raw: str) -> TalariaFrame:
    """Decode one strict double-JSON Talaria/Pusher frame."""

    outer = _strict_json(raw, field="Talaria frame")
    if not isinstance(outer, dict):
        raise TalariaProtocolError("Talaria frame must be a JSON object")

    event = _trimmed(outer.get("event"), field="Talaria event")
    channel_raw = outer.get("channel")
    channel = (
        None
        if channel_raw is None
        else _trimmed(channel_raw, field="Talaria channel")
    )

    data_raw = outer.get("data")
    data = _strict_json(data_raw, field="Talaria data")
    if not isinstance(data, dict):
        raise TalariaProtocolError("Talaria data must decode to a JSON object")
    return TalariaFrame(event=event, channel=channel, data=data)


@dataclass(frozen=True, slots=True, order=True)
class ProphetXSubscription:
    kind: str
    value: str

    @classmethod
    def event(cls, event_id: int | str) -> "ProphetXSubscription":
        return cls(kind="event", value=_event_id(event_id))

    @classmethod
    def event_subtype(
        cls,
        event_id: int | str,
        sub_type: str,
    ) -> "ProphetXSubscription":
        event = _event_id(event_id)
        subtype = _trimmed(sub_type, field="sub_type")
        if ":" in subtype:
            raise TalariaProtocolError(
                "sub_type must not contain the event/subtype delimiter ':'"
            )
        return cls(kind="event_subtype", value=f"{event}:{subtype}")

    def __post_init__(self) -> None:
        if self.kind not in _ALLOWED_SUBSCRIPTION_TYPES:
            raise TalariaProtocolError("unsupported ProphetX subscription type")
        value = _trimmed(self.value, field="subscription value")
        if self.kind == "event":
            _event_id(value, field="event subscription id")
        else:
            event, delimiter, subtype = value.partition(":")
            if delimiter != ":" or not event or not subtype or ":" in subtype:
                raise TalariaProtocolError(
                    "event_subtype subscription must be '<event_id>:<sub_type>'"
                )
            _event_id(event, field="event_subtype event id")
            _trimmed(subtype, field="event_subtype sub_type")


def canonical_subscriptions(
    values: Iterable[ProphetXSubscription],
) -> tuple[ProphetXSubscription, ...]:
    subscriptions = tuple(values)
    if not subscriptions:
        raise TalariaProtocolError("at least one market-data subscription is required")
    for item in subscriptions:
        if not isinstance(item, ProphetXSubscription):
            raise TalariaProtocolError(
                "subscriptions must contain ProphetXSubscription values"
            )
    if len(set(subscriptions)) != len(subscriptions):
        raise TalariaProtocolError("subscriptions must not contain duplicates")
    return tuple(sorted(subscriptions))


def build_registration_request(
    socket_id: str,
    subscriptions: Iterable[ProphetXSubscription],
) -> dict[str, Any]:
    """Build the complete declarative subscription set for one socket generation."""

    socket = _trimmed(socket_id, field="socket_id")
    normalized = canonical_subscriptions(subscriptions)

    grouped: dict[str, list[str]] = {}
    for item in normalized:
        grouped.setdefault(item.kind, []).append(item.value)

    return {
        "socket_id": socket,
        "service": "pusher",
        "subscriptions": [
            {"type": kind, "ids": tuple(ids)}
            for kind, ids in sorted(grouped.items())
        ],
    }


def _binding_event_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TalariaProtocolError("binding_events must be a list")
    names: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            name = _trimmed(entry, field="binding event")
        elif isinstance(entry, dict):
            name = _trimmed(entry.get("name"), field="binding event name")
        else:
            raise TalariaProtocolError(
                "binding_events entries must be strings or objects"
            )
        names.append(name)
    if len(set(names)) != len(names):
        raise TalariaProtocolError("binding_events must not contain duplicates")
    return tuple(names)


@dataclass(frozen=True, slots=True)
class AuthorizedChannel:
    channel_name: str
    auth: str
    binding_events: tuple[str, ...]
    market_data: bool


@dataclass(frozen=True, slots=True)
class RegistrationEvidence:
    channel_limit: int
    authenticated: Mapping[str, Any]
    channels: tuple[AuthorizedChannel, ...]

    @property
    def market_channels(self) -> tuple[str, ...]:
        return tuple(
            channel.channel_name for channel in self.channels if channel.market_data
        )


def parse_registration_evidence(raw: Mapping[str, Any]) -> RegistrationEvidence:
    """Validate registration evidence without guessing undocumented channel names."""

    if not isinstance(raw, Mapping):
        raise TalariaProtocolError("registration response must be an object")

    payload: object
    if "data" in raw:
        if any(key in raw for key in ("channel_limit", "authenticated", "authorized_channel")):
            raise TalariaProtocolError(
                "registration response must not mix top-level and data-wrapped evidence"
            )
        payload = raw["data"]
    else:
        payload = raw

    if not isinstance(payload, Mapping):
        raise TalariaProtocolError("registration response evidence must be an object")

    channel_limit = _positive_int(
        payload.get("channel_limit"),
        field="channel_limit",
    )
    authenticated = payload.get("authenticated")
    if not isinstance(authenticated, Mapping) or not authenticated:
        raise TalariaProtocolError(
            "registration response requires non-empty authenticated evidence"
        )

    authorized = payload.get("authorized_channel")
    if not isinstance(authorized, list) or not authorized:
        raise TalariaProtocolError(
            "registration response requires non-empty authorized_channel"
        )

    channels: list[AuthorizedChannel] = []
    seen_names: set[str] = set()
    for entry in authorized:
        if not isinstance(entry, Mapping):
            raise TalariaProtocolError("authorized_channel entries must be objects")
        channel_name = _trimmed(entry.get("channel_name"), field="channel_name")
        auth = _trimmed(entry.get("auth"), field="channel auth")
        if channel_name in seen_names:
            raise TalariaProtocolError("authorized_channel contains duplicate channel")
        seen_names.add(channel_name)
        binding_events = _binding_event_names(entry.get("binding_events"))
        channels.append(
            AuthorizedChannel(
                channel_name=channel_name,
                auth=auth,
                binding_events=binding_events,
                market_data="market_selections" in binding_events,
            )
        )

    if len(channels) > channel_limit:
        raise TalariaProtocolError(
            "registration evidence exceeds provider-reported channel_limit"
        )
    if not any(channel.market_data for channel in channels):
        raise TalariaProtocolError(
            "registration response authorizes no market_selections channel"
        )

    return RegistrationEvidence(
        channel_limit=channel_limit,
        authenticated=dict(authenticated),
        channels=tuple(channels),
    )


@dataclass(frozen=True, slots=True)
class TalariaMarketEnvelope:
    generation: int
    socket_id: str
    channel: str
    event_id: str
    sub_type: str | None
    payload: Mapping[str, Any]
    continuity_eligible: bool


@dataclass(frozen=True, slots=True)
class TalariaContinuitySnapshot:
    generation: int
    transport_open: bool
    socket_id: str | None
    signed_in: bool
    channel_limit: int | None
    market_channels: tuple[str, ...]
    subscribed_market_channels: tuple[str, ...]
    rebase_required: bool
    rebase_evidence_sha256: str | None
    config_sha256: str | None
    quality_flags: tuple[str, ...]

    @property
    def decision_eligible(self) -> bool:
        return (
            self.transport_open
            and self.socket_id is not None
            and self.signed_in
            and bool(self.market_channels)
            and self.subscribed_market_channels == self.market_channels
            and not self.rebase_required
            and self.rebase_evidence_sha256 is not None
            and not self.quality_flags
        )


class ProphetXTalariaContinuity:
    """Generation-fenced continuity authority for ProphetX market-data frames."""

    def __init__(self, subscriptions: Sequence[ProphetXSubscription]) -> None:
        self._subscriptions = canonical_subscriptions(subscriptions)
        self._generation = 0
        self._transport_open = False
        self._socket_id: str | None = None
        self._signed_in = False
        self._registration: RegistrationEvidence | None = None
        self._subscribed_market_channels: set[str] = set()
        self._rebase_required = True
        self._rebase_evidence_sha256: str | None = None
        self._config_sha256: str | None = None
        self._last_failure: str | None = None

    @property
    def subscriptions(self) -> tuple[ProphetXSubscription, ...]:
        return self._subscriptions

    @property
    def generation(self) -> int:
        return self._generation

    def open_generation(self, connection_config: Mapping[str, Any]) -> int:
        if not isinstance(connection_config, Mapping) or not connection_config:
            raise TalariaProtocolError(
                "connection_config must be non-empty provider evidence"
            )
        digest = _canonical_json_digest(dict(connection_config))
        self._generation += 1
        self._transport_open = True
        self._socket_id = None
        self._signed_in = False
        self._registration = None
        self._subscribed_market_channels.clear()
        self._rebase_required = True
        self._rebase_evidence_sha256 = None
        self._config_sha256 = digest
        self._last_failure = None
        return self._generation

    def observe_connection_config(self, connection_config: Mapping[str, Any]) -> bool:
        if not isinstance(connection_config, Mapping) or not connection_config:
            raise TalariaProtocolError(
                "connection_config must be non-empty provider evidence"
            )
        digest = _canonical_json_digest(dict(connection_config))
        if self._config_sha256 is None:
            self._config_sha256 = digest
            return False
        if digest == self._config_sha256:
            return False
        self._transport_open = False
        self._signed_in = False
        self._subscribed_market_channels.clear()
        self._rebase_required = True
        self._rebase_evidence_sha256 = None
        self._last_failure = "CONNECTION_CONFIG_CHANGED"
        self._config_sha256 = digest
        return True

    def registration_request(self, generation: int) -> dict[str, Any]:
        self._require_generation(generation)
        if not self._transport_open:
            raise TalariaContinuityError("transport is not open")
        if self._socket_id is None:
            raise TalariaContinuityError(
                "socket_id is unavailable before connection_established"
            )
        return build_registration_request(self._socket_id, self._subscriptions)

    def accept_registration(
        self,
        generation: int,
        evidence: RegistrationEvidence,
    ) -> None:
        self._require_generation(generation)
        if not self._transport_open or self._socket_id is None:
            raise TalariaContinuityError(
                "registration evidence requires an established current socket"
            )
        if not isinstance(evidence, RegistrationEvidence):
            raise TypeError("evidence must be RegistrationEvidence")
        self._registration = evidence
        self._signed_in = False
        self._subscribed_market_channels.clear()
        self._rebase_required = True
        self._rebase_evidence_sha256 = None

    def disconnect(self, generation: int, reason: str = "SOCKET_DISCONNECTED") -> None:
        self._require_generation(generation)
        normalized = _trimmed(reason, field="disconnect reason")
        self._transport_open = False
        self._signed_in = False
        self._subscribed_market_channels.clear()
        self._rebase_required = True
        self._rebase_evidence_sha256 = None
        self._last_failure = normalized

    def mark_rebase_complete(self, generation: int, evidence_sha256: str) -> None:
        self._require_generation(generation)
        digest = _sha256(evidence_sha256, field="rebase evidence digest")
        if not self._handshake_complete():
            raise TalariaContinuityError(
                "rebase cannot complete before current market subscriptions are acknowledged"
            )
        self._rebase_required = False
        self._rebase_evidence_sha256 = digest
        self._last_failure = None

    def ingest_frame(
        self,
        generation: int,
        raw: str,
    ) -> TalariaMarketEnvelope | None:
        self._require_generation(generation)
        if not self._transport_open:
            raise TalariaContinuityError("current Talaria transport is not open")

        frame = parse_talaria_frame(raw)

        if frame.event == "pusher:connection_established":
            self._accept_connection_established(frame)
            return None

        if frame.event == "pusher:signin_success":
            if self._registration is None:
                raise TalariaContinuityError(
                    "signin_success arrived before registration evidence"
                )
            self._signed_in = True
            return None

        if frame.event == "pusher_internal:subscription_succeeded":
            self._accept_subscription_success(frame)
            return None

        if frame.event == "pusher:error":
            message = frame.data.get("message")
            reason = (
                "PROVIDER_ERROR"
                if message is None
                else f"PROVIDER_ERROR:{_trimmed(message, field='provider error message')}"
            )
            self.disconnect(generation, reason=reason)
            return None

        if frame.event == "market_selections":
            return self._market_envelope(frame)

        return None

    def snapshot(self) -> TalariaContinuitySnapshot:
        market_channels = self._market_channels()
        subscribed = tuple(
            channel
            for channel in market_channels
            if channel in self._subscribed_market_channels
        )
        flags: list[str] = []
        if not self._transport_open:
            flags.append("PROPHETX_TALARIA_DISCONNECTED")
        if self._socket_id is None:
            flags.append("PROPHETX_TALARIA_SOCKET_UNESTABLISHED")
        if not self._signed_in:
            flags.append("PROPHETX_TALARIA_NOT_SIGNED_IN")
        if not market_channels or subscribed != market_channels:
            flags.append("PROPHETX_TALARIA_SUBSCRIPTIONS_INCOMPLETE")
        if self._rebase_required:
            flags.append("PROPHETX_TALARIA_REBASE_REQUIRED")
        if self._last_failure is not None:
            flags.append("PROPHETX_TALARIA_FAILURE_EVIDENCE")

        return TalariaContinuitySnapshot(
            generation=self._generation,
            transport_open=self._transport_open,
            socket_id=self._socket_id,
            signed_in=self._signed_in,
            channel_limit=(
                None if self._registration is None else self._registration.channel_limit
            ),
            market_channels=market_channels,
            subscribed_market_channels=subscribed,
            rebase_required=self._rebase_required,
            rebase_evidence_sha256=self._rebase_evidence_sha256,
            config_sha256=self._config_sha256,
            quality_flags=tuple(flags),
        )

    def _require_generation(self, generation: int) -> None:
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise TalariaContinuityError("generation must be a non-boolean integer")
        if generation != self._generation or generation <= 0:
            raise TalariaContinuityError(
                "frame/action belongs to a stale or unopened Talaria generation"
            )

    def _accept_connection_established(self, frame: TalariaFrame) -> None:
        socket_id = _trimmed(frame.data.get("socket_id"), field="socket_id")
        if frame.channel is not None:
            raise TalariaProtocolError(
                "connection_established must not be scoped to a channel"
            )
        if self._socket_id is not None and self._socket_id != socket_id:
            self._transport_open = False
            self._rebase_required = True
            self._rebase_evidence_sha256 = None
            self._last_failure = "SOCKET_ID_CHANGED_WITHIN_GENERATION"
            raise TalariaContinuityError(
                "socket_id changed inside one local connection generation"
            )
        self._socket_id = socket_id

    def _accept_subscription_success(self, frame: TalariaFrame) -> None:
        if not self._signed_in:
            raise TalariaContinuityError(
                "subscription acknowledgement arrived before signin success"
            )
        if self._registration is None:
            raise TalariaContinuityError(
                "subscription acknowledgement arrived before registration evidence"
            )
        channel = _trimmed(frame.channel, field="subscription channel")
        market_channels = set(self._registration.market_channels)
        if channel not in market_channels:
            raise TalariaProtocolError(
                "subscription acknowledgement is not for an authorized market channel"
            )
        if channel in self._subscribed_market_channels:
            raise TalariaProtocolError(
                "duplicate market subscription acknowledgement"
            )
        self._subscribed_market_channels.add(channel)

    def _market_envelope(self, frame: TalariaFrame) -> TalariaMarketEnvelope:
        if self._registration is None or self._socket_id is None:
            raise TalariaContinuityError(
                "market frame arrived before current socket registration"
            )
        channel = _trimmed(frame.channel, field="market frame channel")
        if channel not in set(self._registration.market_channels):
            raise TalariaProtocolError(
                "market frame is not on an authorized market_selections channel"
            )

        scope = frame.data.get("scope")
        if not isinstance(scope, Mapping):
            raise TalariaProtocolError(
                "market_selections requires explicit provider scope"
            )
        event = _event_id(scope.get("event_id"), field="scope event_id")
        subtype_raw = scope.get("sub_type")
        subtype = (
            None
            if subtype_raw is None
            else _trimmed(subtype_raw, field="scope sub_type")
        )
        if subtype is not None and ":" in subtype:
            raise TalariaProtocolError("scope sub_type contains reserved ':' delimiter")

        requested = set(self._subscriptions)
        if subtype is None:
            expected = ProphetXSubscription.event(event)
        else:
            expected = ProphetXSubscription.event_subtype(event, subtype)
        if expected not in requested:
            raise TalariaProtocolError(
                "market frame scope is outside the declarative subscription set"
            )

        return TalariaMarketEnvelope(
            generation=self._generation,
            socket_id=self._socket_id,
            channel=channel,
            event_id=event,
            sub_type=subtype,
            payload=dict(frame.data),
            continuity_eligible=self.snapshot().decision_eligible,
        )

    def _market_channels(self) -> tuple[str, ...]:
        if self._registration is None:
            return ()
        return tuple(sorted(self._registration.market_channels))

    def _handshake_complete(self) -> bool:
        if (
            not self._transport_open
            or self._socket_id is None
            or not self._signed_in
            or self._registration is None
        ):
            return False
        market_channels = set(self._registration.market_channels)
        return bool(market_channels) and self._subscribed_market_channels == market_channels
