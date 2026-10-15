from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Final, Mapping


class AnnouncementPriority(str, Enum):
    """Effective assistive-technology announcement priority."""

    SILENT = "SILENT"
    POLITE = "POLITE"
    ASSERTIVE = "ASSERTIVE"


class AnnouncementKind(str, Enum):
    """Closed product event taxonomy for announcement policy."""

    PRICE_TICK = "PRICE_TICK"
    MARKET_REFRESH = "MARKET_REFRESH"
    PROGRESS_TICK = "PROGRESS_TICK"

    OPERATION_STARTED = "OPERATION_STARTED"
    OPERATION_COMPLETED = "OPERATION_COMPLETED"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOP_COMPLETED = "STOP_COMPLETED"

    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    CRITICAL_ERROR = "CRITICAL_ERROR"
    AUTHORITY_BLOCKED = "AUTHORITY_BLOCKED"


_PRIORITY_BY_KIND: Final[Mapping[AnnouncementKind, AnnouncementPriority]] = MappingProxyType(
    {
        AnnouncementKind.PRICE_TICK: AnnouncementPriority.SILENT,
        AnnouncementKind.MARKET_REFRESH: AnnouncementPriority.SILENT,
        AnnouncementKind.PROGRESS_TICK: AnnouncementPriority.SILENT,
        AnnouncementKind.OPERATION_STARTED: AnnouncementPriority.POLITE,
        AnnouncementKind.OPERATION_COMPLETED: AnnouncementPriority.POLITE,
        AnnouncementKind.STOP_REQUESTED: AnnouncementPriority.POLITE,
        AnnouncementKind.STOP_COMPLETED: AnnouncementPriority.POLITE,
        AnnouncementKind.RECOVERY_REQUIRED: AnnouncementPriority.ASSERTIVE,
        AnnouncementKind.CRITICAL_ERROR: AnnouncementPriority.ASSERTIVE,
        AnnouncementKind.AUTHORITY_BLOCKED: AnnouncementPriority.ASSERTIVE,
    }
)

if set(_PRIORITY_BY_KIND) != set(AnnouncementKind):
    raise RuntimeError("announcement policy must classify every AnnouncementKind exactly once")


_ACTIVITY_ID_PREFIX = "autosport:announcement:v1:"


@dataclass(frozen=True, slots=True)
class AnnouncementEvent:
    """One already-localized product status event presented to the policy.

    state_token is a stable product state-transition identity. Critical events
    additionally require episode_id so repeated projections of the same
    recovery/error/authority episode do not create repeated interruptions.

    This type intentionally has no caller-controlled priority field. Event kind
    owns priority. The policy is not a UIA emitter and does not prove screen
    reader speech.
    """

    kind: AnnouncementKind
    text: str
    state_token: str
    episode_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not AnnouncementKind:
            raise TypeError("kind must be AnnouncementKind")
        _require_trimmed("text", self.text)
        _require_trimmed("state_token", self.state_token)

        expected = _PRIORITY_BY_KIND[self.kind]
        if expected is AnnouncementPriority.ASSERTIVE:
            if self.episode_id is None:
                raise ValueError("assertive announcement event requires episode_id")
            _require_trimmed("episode_id", self.episode_id)
        elif self.episode_id is not None:
            raise ValueError("episode_id is valid only for assertive announcement events")


@dataclass(frozen=True, slots=True, init=False)
class AnnouncementDecision:
    """Product-issued policy result consumed by a future Windows emitter.

    Callers may inspect decisions but cannot construct an emitted decision
    directly at the normal API surface. AnnouncementGate is the issuance path.
    """

    emit: bool
    priority: AnnouncementPriority
    text: str | None
    reason: str
    activity_id: str | None = None
    move_focus: bool = False

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "AnnouncementDecision is product-issued; use AnnouncementGate.decide()"
        )

    def __post_init__(self) -> None:
        if type(self.emit) is not bool:
            raise TypeError("emit must be bool")
        if not isinstance(self.priority, AnnouncementPriority):
            raise TypeError("priority must be AnnouncementPriority")
        if type(self.move_focus) is not bool:
            raise TypeError("move_focus must be bool")
        if self.emit:
            if self.priority is AnnouncementPriority.SILENT:
                raise ValueError("emitted decision cannot be SILENT")
            _require_trimmed("text", self.text)
            _validate_activity_id(self.activity_id, self.priority)
        else:
            if self.priority is not AnnouncementPriority.SILENT:
                raise ValueError("suppressed decision must be SILENT")
            if self.text is not None:
                raise ValueError("suppressed decision must not carry announcement text")
            if self.activity_id is not None:
                raise ValueError("suppressed decision must not carry activity_id")
        if self.move_focus:
            raise ValueError("announcement policy must never request focus movement")
        _require_trimmed("reason", self.reason)


def _issue_announcement_decision(
    *,
    emit: bool,
    priority: AnnouncementPriority,
    text: str | None,
    reason: str,
    activity_id: str | None = None,
    move_focus: bool = False,
) -> AnnouncementDecision:
    """Issue one validated decision from the product-owned policy path."""

    decision = object.__new__(AnnouncementDecision)
    for name, value in (
        ("emit", emit),
        ("priority", priority),
        ("text", text),
        ("reason", reason),
        ("activity_id", activity_id),
        ("move_focus", move_focus),
    ):
        object.__setattr__(decision, name, value)
    decision.__post_init__()
    return decision


class AnnouncementGate:
    """Bound and deduplicate machine announcement decisions.

    This class is deliberately transport-free. It classifies and suppresses
    events only. Integration with a real Windows UIA live-region/notification
    emitter, external-client verification, human testing, and NVDA speech
    verification are separate evidence gates.

    max_history bounds in-memory deduplication state. Once an oldest key is
    evicted it may be emitted again; callers must still supply stable
    product-owned transition/episode identities rather than minting IDs merely
    to bypass deduplication.
    """

    __slots__ = ("_max_history", "_history")

    def __init__(self, *, max_history: int = 128) -> None:
        if type(max_history) is not int or max_history <= 0:
            raise ValueError("max_history must be a positive integer")
        self._max_history = max_history
        self._history: OrderedDict[tuple[str, ...], None] = OrderedDict()

    @property
    def history_size(self) -> int:
        return len(self._history)

    @property
    def max_history(self) -> int:
        return self._max_history

    def decide(self, event: AnnouncementEvent) -> AnnouncementDecision:
        if type(event) is not AnnouncementEvent:
            raise TypeError("event must be AnnouncementEvent")

        intended = _PRIORITY_BY_KIND[event.kind]
        if intended is AnnouncementPriority.SILENT:
            return _suppressed("HIGH_FREQUENCY_CHURN")

        if intended is AnnouncementPriority.ASSERTIVE:
            assert event.episode_id is not None
            key = ("ASSERTIVE", event.episode_id)
            duplicate_reason = "DUPLICATE_CRITICAL_EPISODE"
        else:
            key = ("POLITE", event.state_token)
            duplicate_reason = "DUPLICATE_STATE_TRANSITION"

        if key in self._history:
            self._history.move_to_end(key)
            return _suppressed(duplicate_reason)

        self._remember(key)
        return _issue_announcement_decision(
            emit=True,
            priority=intended,
            text=event.text,
            reason="EMIT",
            activity_id=_activity_id_for_key(key),
            move_focus=False,
        )

    def _remember(self, key: tuple[str, ...]) -> None:
        self._history[key] = None
        while len(self._history) > self._max_history:
            self._history.popitem(last=False)


def priority_for_kind(kind: AnnouncementKind) -> AnnouncementPriority:
    """Return product-owned priority without allowing caller escalation."""

    if type(kind) is not AnnouncementKind:
        raise TypeError("kind must be AnnouncementKind")
    return _PRIORITY_BY_KIND[kind]


def _activity_id_for_key(key: tuple[str, str]) -> str:
    """Derive a stable opaque non-localized UIA activity identity."""

    identity_kind, identity = key
    payload = identity_kind.encode("ascii") + b"\0" + identity.encode("utf-8")
    digest = sha256(payload).hexdigest()
    return f"{_ACTIVITY_ID_PREFIX}{identity_kind.lower()}:sha256:{digest}"


def _validate_activity_id(
    value: object,
    priority: AnnouncementPriority,
) -> str:
    _require_trimmed("activity_id", value)
    assert type(value) is str
    if not value.isascii() or not value.startswith(_ACTIVITY_ID_PREFIX):
        raise ValueError("activity_id must be a product-issued non-localized ASCII identity")
    suffix = value[len(_ACTIVITY_ID_PREFIX):]
    try:
        identity_kind, algorithm, digest = suffix.split(":", 2)
    except ValueError as exc:
        raise ValueError("activity_id has invalid product-issued format") from exc
    if (
        identity_kind not in {"polite", "assertive"}
        or identity_kind != priority.value.lower()
        or algorithm != "sha256"
    ):
        raise ValueError("activity_id has invalid product-issued format")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("activity_id has invalid product-issued digest")
    return value


def _suppressed(reason: str) -> AnnouncementDecision:
    return _issue_announcement_decision(
        emit=False,
        priority=AnnouncementPriority.SILENT,
        text=None,
        reason=reason,
        activity_id=None,
        move_focus=False,
    )


def _require_trimmed(name: str, value: object) -> None:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty trimmed string")
