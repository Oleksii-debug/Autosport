from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .domain import _canonical_sport_value, _quote_identity


class OutcomeUniverseError(ValueError):
    """Authoritative market outcome-universe contract violation."""


class AuthorityKind(str, Enum):
    PROVIDER_MARKET_DEFINITION = "provider_market_definition"
    PROVIDER_MARKET_DEFINITION_WITH_SETTLEMENT_RULES = (
        "provider_market_definition_with_settlement_rules"
    )
    VERIFIED_PROVIDER_ADAPTER = "verified_provider_adapter"


class MarketLifecycle(str, Enum):
    OPEN = "open"
    SUSPENDED = "suspended"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class OutcomeAvailability(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class SettlementResolution(str, Enum):
    WIN = "win"
    LOSS = "loss"
    VOID = "void"


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise OutcomeUniverseError(f"{field} must be a non-empty trimmed string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OutcomeUniverseError(f"{field} must be UTF-8 encodable") from exc
    return value


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutcomeUniverseError(f"{field} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OutcomeUniverseError(f"{field} must be timezone-aware ISO-8601")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise OutcomeUniverseError(f"{field} must be a lowercase SHA-256 hex digest")
    return text


def _sequence(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise OutcomeUniverseError(f"{field} must be a non-negative non-boolean integer")
    return value


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MarketAuthorityEvidence:
    provider_source_id: str
    source_revision: str
    source_sequence: int
    source_ts: str
    observed_ts: str
    payload_sha256: str
    authority_kind: AuthorityKind
    roster_complete: bool
    settlement_semantics_complete: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_source_id",
            _text(self.provider_source_id, "provider_source_id"),
        )
        object.__setattr__(
            self,
            "source_revision",
            _text(self.source_revision, "source_revision"),
        )
        object.__setattr__(
            self,
            "source_sequence",
            _sequence(self.source_sequence, "source_sequence"),
        )
        object.__setattr__(self, "source_ts", _timestamp(self.source_ts, "source_ts"))
        object.__setattr__(
            self,
            "observed_ts",
            _timestamp(self.observed_ts, "observed_ts"),
        )
        object.__setattr__(
            self,
            "payload_sha256",
            _sha256(self.payload_sha256, "payload_sha256"),
        )
        if not isinstance(self.authority_kind, AuthorityKind):
            raise OutcomeUniverseError("authority_kind must be an AuthorityKind")
        if type(self.roster_complete) is not bool:
            raise OutcomeUniverseError("roster_complete must be boolean")
        if type(self.settlement_semantics_complete) is not bool:
            raise OutcomeUniverseError("settlement_semantics_complete must be boolean")
        if self.settlement_semantics_complete and not self.roster_complete:
            raise OutcomeUniverseError(
                "complete settlement semantics require a complete authoritative roster"
            )
        if _as_datetime(self.source_ts) > _as_datetime(self.observed_ts):
            raise OutcomeUniverseError("source_ts must not be later than observed_ts")

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_source_id": self.provider_source_id,
            "source_revision": self.source_revision,
            "source_sequence": self.source_sequence,
            "source_ts": self.source_ts,
            "observed_ts": self.observed_ts,
            "payload_sha256": self.payload_sha256,
            "authority_kind": self.authority_kind.value,
            "roster_complete": self.roster_complete,
            "settlement_semantics_complete": self.settlement_semantics_complete,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "MarketAuthorityEvidence":
        expected = {
            "provider_source_id",
            "source_revision",
            "source_sequence",
            "source_ts",
            "observed_ts",
            "payload_sha256",
            "authority_kind",
            "roster_complete",
            "settlement_semantics_complete",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise OutcomeUniverseError(
                "market authority evidence must contain canonical fields"
            )
        try:
            authority_kind = AuthorityKind(raw["authority_kind"])
        except (TypeError, ValueError) as exc:
            raise OutcomeUniverseError("unsupported authority_kind") from exc
        return cls(
            provider_source_id=raw["provider_source_id"],
            source_revision=raw["source_revision"],
            source_sequence=raw["source_sequence"],
            source_ts=raw["source_ts"],
            observed_ts=raw["observed_ts"],
            payload_sha256=raw["payload_sha256"],
            authority_kind=authority_kind,
            roster_complete=raw["roster_complete"],
            settlement_semantics_complete=raw["settlement_semantics_complete"],
        )


@dataclass(frozen=True, slots=True)
class MarketOutcomeSelection:
    selection_id: str
    availability: OutcomeAvailability
    lifecycle_ts: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selection_id",
            _text(self.selection_id, "selection_id"),
        )
        if not isinstance(self.availability, OutcomeAvailability):
            raise OutcomeUniverseError(
                "selection availability must be an OutcomeAvailability"
            )
        object.__setattr__(
            self,
            "lifecycle_ts",
            _timestamp(self.lifecycle_ts, "lifecycle_ts"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "selection_id": self.selection_id,
            "availability": self.availability.value,
            "lifecycle_ts": self.lifecycle_ts,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "MarketOutcomeSelection":
        if type(raw) is not dict or set(raw) != {
            "selection_id",
            "availability",
            "lifecycle_ts",
        }:
            raise OutcomeUniverseError(
                "market outcome selection must contain canonical fields"
            )
        try:
            availability = OutcomeAvailability(raw["availability"])
        except (TypeError, ValueError) as exc:
            raise OutcomeUniverseError("unsupported outcome availability") from exc
        return cls(
            selection_id=raw["selection_id"],
            availability=availability,
            lifecycle_ts=raw["lifecycle_ts"],
        )


@dataclass(frozen=True, slots=True)
class TerminalResolution:
    selection_id: str
    resolution: SettlementResolution

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selection_id",
            _text(self.selection_id, "terminal resolution selection_id"),
        )
        if not isinstance(self.resolution, SettlementResolution):
            raise OutcomeUniverseError(
                "terminal resolution must be a SettlementResolution"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "selection_id": self.selection_id,
            "resolution": self.resolution.value,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "TerminalResolution":
        if type(raw) is not dict or set(raw) != {"selection_id", "resolution"}:
            raise OutcomeUniverseError(
                "terminal resolution must contain canonical fields"
            )
        try:
            resolution = SettlementResolution(raw["resolution"])
        except (TypeError, ValueError) as exc:
            raise OutcomeUniverseError("unsupported settlement resolution") from exc
        return cls(selection_id=raw["selection_id"], resolution=resolution)


@dataclass(frozen=True, slots=True)
class MarketTerminalState:
    state_id: str
    resolutions: tuple[TerminalResolution, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_id", _text(self.state_id, "terminal state_id"))
        resolutions = tuple(self.resolutions)
        if not resolutions:
            raise OutcomeUniverseError("terminal state requires resolutions")
        if any(not isinstance(item, TerminalResolution) for item in resolutions):
            raise OutcomeUniverseError(
                "terminal state resolutions must be TerminalResolution values"
            )
        resolutions = tuple(sorted(resolutions, key=lambda item: item.selection_id))
        selection_ids = [item.selection_id for item in resolutions]
        if len(selection_ids) != len(set(selection_ids)):
            raise OutcomeUniverseError(
                "terminal state contains duplicate selection resolutions"
            )
        object.__setattr__(self, "resolutions", resolutions)

    def to_dict(self) -> dict[str, object]:
        return {
            "state_id": self.state_id,
            "resolutions": [item.to_dict() for item in self.resolutions],
        }

    @classmethod
    def from_dict(cls, raw: object) -> "MarketTerminalState":
        if type(raw) is not dict or set(raw) != {"state_id", "resolutions"}:
            raise OutcomeUniverseError(
                "terminal state must contain canonical fields"
            )
        if type(raw["resolutions"]) is not list:
            raise OutcomeUniverseError("terminal state resolutions must be an array")
        return cls(
            state_id=raw["state_id"],
            resolutions=tuple(
                TerminalResolution.from_dict(item) for item in raw["resolutions"]
            ),
        )


@dataclass(frozen=True, slots=True)
class MarketOutcomeUniverse:
    """Typed authority for one exact market's applicable terminal state space.

    This contract deliberately separates provider/rule authority from ScenarioGroup.
    A caller-supplied ScenarioGroup can never be promoted into this type. The
    authoritative object must carry provider/source revision, causal timestamps,
    roster completeness and explicit per-terminal-state settlement mappings.
    """

    sport: str
    event_id: str
    market_id: str
    market_status: MarketLifecycle
    authority: MarketAuthorityEvidence
    selections: tuple[MarketOutcomeSelection, ...]
    terminal_states: tuple[MarketTerminalState, ...] = ()

    SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        try:
            sport = _canonical_sport_value(self.sport)
        except ValueError as exc:
            raise OutcomeUniverseError(str(exc)) from exc
        object.__setattr__(self, "sport", sport)
        object.__setattr__(self, "event_id", _text(self.event_id, "event_id"))
        object.__setattr__(self, "market_id", _text(self.market_id, "market_id"))
        if not isinstance(self.market_status, MarketLifecycle):
            raise OutcomeUniverseError("market_status must be a MarketLifecycle")
        if not isinstance(self.authority, MarketAuthorityEvidence):
            raise OutcomeUniverseError(
                "authority must be MarketAuthorityEvidence"
            )

        selections = tuple(self.selections)
        if not selections:
            raise OutcomeUniverseError("market outcome universe requires selections")
        if any(not isinstance(item, MarketOutcomeSelection) for item in selections):
            raise OutcomeUniverseError(
                "selections must contain MarketOutcomeSelection values"
            )
        selections = tuple(sorted(selections, key=lambda item: item.selection_id))
        selection_ids = [item.selection_id for item in selections]
        if len(selection_ids) != len(set(selection_ids)):
            raise OutcomeUniverseError(
                "market outcome universe contains duplicate selection_id"
            )
        for selection in selections:
            if _as_datetime(selection.lifecycle_ts) > _as_datetime(
                self.authority.observed_ts
            ):
                raise OutcomeUniverseError(
                    "selection lifecycle_ts cannot be later than authority observed_ts"
                )
        object.__setattr__(self, "selections", selections)

        states = tuple(self.terminal_states)
        if any(not isinstance(item, MarketTerminalState) for item in states):
            raise OutcomeUniverseError(
                "terminal_states must contain MarketTerminalState values"
            )
        states = tuple(sorted(states, key=lambda item: item.state_id))
        state_ids = [item.state_id for item in states]
        if len(state_ids) != len(set(state_ids)):
            raise OutcomeUniverseError(
                "market outcome universe contains duplicate terminal state_id"
            )

        if self.authority.settlement_semantics_complete:
            if not states:
                raise OutcomeUniverseError(
                    "complete settlement semantics require terminal states"
                )
            expected = set(selection_ids)
            removed = {
                item.selection_id
                for item in selections
                if item.availability is OutcomeAvailability.REMOVED
            }
            for state in states:
                actual = {item.selection_id for item in state.resolutions}
                if actual != expected:
                    raise OutcomeUniverseError(
                        "each terminal state must resolve the exact authoritative roster"
                    )
                resolution_by_id = {
                    item.selection_id: item.resolution for item in state.resolutions
                }
                if any(
                    resolution_by_id[selection_id] is not SettlementResolution.VOID
                    for selection_id in removed
                ):
                    raise OutcomeUniverseError(
                        "removed selections must resolve void in every terminal state"
                    )
            if self.market_status is MarketLifecycle.CANCELLED:
                if any(
                    item.resolution is not SettlementResolution.VOID
                    for state in states
                    for item in state.resolutions
                ):
                    raise OutcomeUniverseError(
                        "cancelled market terminal semantics must resolve every selection void"
                    )
        elif states:
            raise OutcomeUniverseError(
                "partial settlement semantics must not publish terminal states as authoritative"
            )

        object.__setattr__(self, "terminal_states", states)

    @property
    def market_identity(self) -> tuple[str, str, str]:
        return (self.sport, self.event_id, self.market_id)

    @property
    def source_id(self) -> str:
        return self.authority.provider_source_id

    @property
    def is_complete(self) -> bool:
        return (
            self.authority.roster_complete
            and self.authority.settlement_semantics_complete
        )

    def quote_key(self, selection_id: str) -> str:
        canonical_selection = _text(selection_id, "selection_id")
        if canonical_selection not in {
            item.selection_id for item in self.selections
        }:
            raise OutcomeUniverseError(
                "selection_id is not in the authoritative roster"
            )
        return _quote_identity(
            self.event_id,
            self.market_id,
            canonical_selection,
            self.sport,
        )

    def validate_for_decision(self, decision_ts: str) -> str:
        canonical_decision = _timestamp(decision_ts, "decision_ts")
        decision = _as_datetime(canonical_decision)
        if _as_datetime(self.authority.source_ts) > decision:
            raise OutcomeUniverseError(
                "authority source_ts is from the future relative to decision_ts"
            )
        if _as_datetime(self.authority.observed_ts) > decision:
            raise OutcomeUniverseError(
                "authority observed_ts is from the future relative to decision_ts"
            )
        for selection in self.selections:
            if _as_datetime(selection.lifecycle_ts) > decision:
                raise OutcomeUniverseError(
                    "selection lifecycle evidence is from the future relative to decision_ts"
                )
        return canonical_decision

    def settlement_outcomes(
        self,
        state_id: str,
        *,
        decision_ts: str | None = None,
    ) -> dict[str, str]:
        if decision_ts is not None:
            self.validate_for_decision(decision_ts)
        if not self.authority.settlement_semantics_complete:
            raise OutcomeUniverseError(
                "authoritative settlement semantics are incomplete"
            )
        canonical_state = _text(state_id, "state_id")
        state = next(
            (item for item in self.terminal_states if item.state_id == canonical_state),
            None,
        )
        if state is None:
            raise OutcomeUniverseError(
                "terminal state is not in the authoritative universe"
            )
        return {
            self.quote_key(item.selection_id): item.resolution.value
            for item in state.resolutions
        }

    def scenario_quote_keys(self, *, decision_ts: str) -> tuple[str, ...]:
        """Return the exact ScenarioGroup projection when semantics permit it.

        ScenarioSearch currently represents each market terminal state by exactly
        one winning quote. Markets with cancellation, pushes/dead-heats, multiple
        simultaneous winners, or any applicable void semantics are therefore
        rejected rather than flattened into false exhaustiveness.
        """

        self.validate_for_decision(decision_ts)
        if not self.is_complete:
            raise OutcomeUniverseError(
                "authoritative roster and settlement semantics must both be complete"
            )
        if self.market_status not in {
            MarketLifecycle.OPEN,
            MarketLifecycle.SUSPENDED,
        }:
            raise OutcomeUniverseError(
                "closed/cancelled market authority cannot drive a new decision scenario"
            )

        applicable = {
            item.selection_id
            for item in self.selections
            if item.availability is not OutcomeAvailability.REMOVED
        }
        removed = {
            item.selection_id
            for item in self.selections
            if item.availability is OutcomeAvailability.REMOVED
        }
        if len(applicable) < 2:
            raise OutcomeUniverseError(
                "ScenarioGroup projection requires at least two applicable outcomes"
            )

        winners: list[str] = []
        for state in self.terminal_states:
            resolution_by_id = {
                item.selection_id: item.resolution for item in state.resolutions
            }
            if any(
                resolution_by_id[selection_id] is not SettlementResolution.VOID
                for selection_id in removed
            ):
                raise OutcomeUniverseError(
                    "removed selections are not canonically void"
                )
            state_winners = [
                selection_id
                for selection_id in sorted(applicable)
                if resolution_by_id[selection_id] is SettlementResolution.WIN
            ]
            if len(state_winners) != 1:
                raise OutcomeUniverseError(
                    "terminal state is not representable as one exhaustive ScenarioGroup winner"
                )
            winner = state_winners[0]
            for selection_id in applicable:
                expected = (
                    SettlementResolution.WIN
                    if selection_id == winner
                    else SettlementResolution.LOSS
                )
                if resolution_by_id[selection_id] is not expected:
                    raise OutcomeUniverseError(
                        "terminal state contains applicable void/non-exclusive semantics"
                    )
            winners.append(winner)

        if len(winners) != len(applicable) or set(winners) != applicable:
            raise OutcomeUniverseError(
                "terminal states do not form a one-to-one exhaustive applicable outcome roster"
            )
        return tuple(self.quote_key(selection_id) for selection_id in sorted(applicable))

    def validate_scenario_quote_keys(
        self,
        quote_keys: set[str],
        *,
        decision_ts: str,
    ) -> None:
        if type(quote_keys) is not set or any(type(item) is not str for item in quote_keys):
            raise OutcomeUniverseError("quote_keys must be an exact set[str]")
        authoritative = set(self.scenario_quote_keys(decision_ts=decision_ts))
        if quote_keys != authoritative:
            missing = sorted(authoritative - quote_keys)
            extra = sorted(quote_keys - authoritative)
            raise OutcomeUniverseError(
                f"scenario membership is not authoritative: missing={missing}, extra={extra}"
            )

    @property
    def universe_id(self) -> str:
        return _canonical_hash(self._identity_payload())

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "sport": self.sport,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "market_status": self.market_status.value,
            "authority": self.authority.to_dict(),
            "selections": [item.to_dict() for item in self.selections],
            "terminal_states": [item.to_dict() for item in self.terminal_states],
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "universe_id": self.universe_id,
            **self._identity_payload(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "MarketOutcomeUniverse":
        expected = {
            "universe_id",
            "schema_version",
            "sport",
            "event_id",
            "market_id",
            "market_status",
            "authority",
            "selections",
            "terminal_states",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise OutcomeUniverseError(
                "market outcome universe must contain canonical fields"
            )
        if raw["schema_version"] != cls.SCHEMA_VERSION:
            raise OutcomeUniverseError("unsupported market outcome universe schema")
        if type(raw["selections"]) is not list or type(raw["terminal_states"]) is not list:
            raise OutcomeUniverseError(
                "selections and terminal_states must be JSON arrays"
            )
        try:
            market_status = MarketLifecycle(raw["market_status"])
        except (TypeError, ValueError) as exc:
            raise OutcomeUniverseError("unsupported market_status") from exc
        result = cls(
            sport=raw["sport"],
            event_id=raw["event_id"],
            market_id=raw["market_id"],
            market_status=market_status,
            authority=MarketAuthorityEvidence.from_dict(raw["authority"]),
            selections=tuple(
                MarketOutcomeSelection.from_dict(item) for item in raw["selections"]
            ),
            terminal_states=tuple(
                MarketTerminalState.from_dict(item)
                for item in raw["terminal_states"]
            ),
        )
        expected_id = _sha256(raw["universe_id"], "universe_id")
        if result.universe_id != expected_id:
            raise OutcomeUniverseError(
                "universe_id does not match canonical authoritative contents"
            )
        return result
