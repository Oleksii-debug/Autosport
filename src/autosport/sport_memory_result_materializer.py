"""Causal projection from canonical settlement evidence into sport memory.

This module owns no outcome, identity, opponent-model, market, or economic truth.
It resolves a result binding against durable pre-reveal market history, revalidates
canonical event membership and participant identity at reveal, and appends or retires
performance through the existing opponent-intelligence authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from .continuous_session import ContinuousSessionCoordinator, SettlementResolution
from .event_lifecycle import (
    CatalogLifecycleError,
    ContinuousEventLifecycle,
    EventPhase,
)
from .domain import MarketEvent
from .learning_environment import EvidenceTruth
from .opponent_intelligence import (
    ObservedPerformance,
    OpponentIntelligenceError,
    OpponentIntelligenceStore,
    PerformanceRecord,
)
from .participant_identity import (
    EntityKind,
    IdentityView,
    ParticipantIdentityError,
    ParticipantIdentityRegistry,
)
from .product_runtime import (
    AutonomousProductRuntime,
    ProductCompositionError,
    _settlement_authority_identity,
)
from .storage import SQLiteMarketStore


SCHEMA: Final = "autosport.sport_memory_result_binding"
SCHEMA_VERSION: Final = 6
_SOURCE_EVIDENCE_KIND: Final = "sport-memory-result-source-pair-v5"


class SportMemoryResultMaterializationError(ValueError):
    """Canonical result evidence cannot be projected safely into sport memory."""


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise SportMemoryResultMaterializationError(
            f"{name} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SportMemoryResultMaterializationError(
            f"{name} must be valid UTF-8"
        ) from exc
    return value


def _sha256(name: str, value: object) -> str:
    text = _text(name, value)
    if len(text) != 64 or text != text.lower() or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise SportMemoryResultMaterializationError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SportMemoryResultMaterializationError(
            f"{name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SportMemoryResultMaterializationError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _time_text(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SportMemoryResultMaterializationError(
            "result binding must be canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SportMemoryResultBinding:
    """A source binding re-resolved from market and canonical identity authorities.

    Display aliases never double as provider selection or competition identifiers.
    Complete bindings minted by :meth:`freeze_binding` persist provider identifiers,
    canonical participant/league roots, exact quote identities, the canonical
    event-membership fingerprint, and the causal provider-binding cutoff that
    mechanically fixes those identifiers from product-owned pre-reveal evidence.
    """

    event_identity: str
    source_id: str
    subject_alias: str
    opponent_alias: str
    sport_id: str
    league_alias: str
    market_context_id: str
    subject_quote_key: str
    frozen_at: str
    evidence_sha256: str
    subject_selection_id: str | None = None
    opponent_selection_id: str | None = None
    competition_id: str | None = None
    opponent_quote_key: str | None = None
    subject_entity_id: str | None = None
    opponent_entity_id: str | None = None
    league_entity_id: str | None = None
    provider_binding_as_of: str | None = None
    provider_inference_as_of: str | None = None
    identity_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "event_identity",
            "source_id",
            "subject_alias",
            "opponent_alias",
            "sport_id",
            "league_alias",
            "market_context_id",
            "subject_quote_key",
        ):
            _text(name, getattr(self, name))
        object.__setattr__(self, "frozen_at", _time_text("frozen_at", self.frozen_at))
        object.__setattr__(
            self,
            "evidence_sha256",
            _sha256("evidence_sha256", self.evidence_sha256),
        )
        for name in (
            "subject_selection_id",
            "opponent_selection_id",
            "competition_id",
            "opponent_quote_key",
            "subject_entity_id",
            "opponent_entity_id",
            "league_entity_id",
        ):
            value = getattr(self, name)
            if value is not None:
                _text(name, value)
        if self.provider_binding_as_of is not None:
            object.__setattr__(
                self,
                "provider_binding_as_of",
                _time_text("provider_binding_as_of", self.provider_binding_as_of),
            )
        if self.provider_inference_as_of is not None:
            object.__setattr__(
                self,
                "provider_inference_as_of",
                _time_text("provider_inference_as_of", self.provider_inference_as_of),
            )
        if self.identity_sha256 is not None:
            _sha256("identity_sha256", self.identity_sha256)
        if self.subject_alias == self.opponent_alias:
            raise SportMemoryResultMaterializationError(
                "subject and opponent aliases must be distinct"
            )
        if (
            self.subject_selection_id is not None
            and self.opponent_selection_id is not None
            and self.subject_selection_id == self.opponent_selection_id
        ):
            raise SportMemoryResultMaterializationError(
                "subject and opponent provider selection ids must be distinct"
            )
        if (
            self.subject_entity_id is not None
            and self.opponent_entity_id is not None
            and self.subject_entity_id == self.opponent_entity_id
        ):
            raise SportMemoryResultMaterializationError(
                "subject and opponent canonical entity ids must be distinct"
            )

    def payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "event_identity": self.event_identity,
            "source_id": self.source_id,
            "subject_alias": self.subject_alias,
            "opponent_alias": self.opponent_alias,
            "sport_id": self.sport_id,
            "league_alias": self.league_alias,
            "market_context_id": self.market_context_id,
            "subject_quote_key": self.subject_quote_key,
            "frozen_at": self.frozen_at,
            "evidence_sha256": self.evidence_sha256,
            "subject_selection_id": self.subject_selection_id,
            "opponent_selection_id": self.opponent_selection_id,
            "competition_id": self.competition_id,
            "opponent_quote_key": self.opponent_quote_key,
            "subject_entity_id": self.subject_entity_id,
            "opponent_entity_id": self.opponent_entity_id,
            "league_entity_id": self.league_entity_id,
            "provider_binding_as_of": self.provider_binding_as_of,
            "provider_inference_as_of": self.provider_inference_as_of,
            "identity_sha256": self.identity_sha256,
        }

    @property
    def binding_sha256(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class SportMemoryResultMaterialization:
    """Read-only receipt for one projection attempt."""

    binding_sha256: str
    settlement_evidence_id: str
    settlement_evidence_sha256: str
    settlement_ref: str
    outcome: str
    performance_id: str | None

    def __post_init__(self) -> None:
        _sha256("binding_sha256", self.binding_sha256)
        _text("settlement_evidence_id", self.settlement_evidence_id)
        _sha256("settlement_evidence_sha256", self.settlement_evidence_sha256)
        _text("settlement_ref", self.settlement_ref)
        if self.outcome not in {"win", "loss", "void"}:
            raise SportMemoryResultMaterializationError("unsupported result outcome")
        if self.performance_id is not None:
            _sha256("performance_id", self.performance_id)
        if self.outcome == "void" and self.performance_id is not None:
            raise SportMemoryResultMaterializationError(
                "void settlement cannot fabricate participant performance"
            )
        if self.outcome != "void" and self.performance_id is None:
            raise SportMemoryResultMaterializationError(
                "observed win/loss requires a performance id"
            )


@dataclass(frozen=True, slots=True)
class _IdentitySnapshot:
    subject_entity_id: str
    opponent_entity_id: str
    league_entity_id: str
    record_ids: tuple[str, ...]
    roster_entity_ids: tuple[str, ...]

    @property
    def sha256(self) -> str:
        return _digest(
            {
                "subject_entity_id": self.subject_entity_id,
                "opponent_entity_id": self.opponent_entity_id,
                "league_entity_id": self.league_entity_id,
                "record_ids": self.record_ids,
                "roster_entity_ids": self.roster_entity_ids,
            }
        )


def _event_order(event: MarketEvent) -> tuple[datetime, int, str]:
    return (_instant("market ingest_ts", event.ingest_ts), event.sequence, event.dedupe_key)


def _matches_context(
    event: MarketEvent,
    *,
    event_identity: str,
    source_id: str,
    sport_id: str,
    competition_id: str,
    market_context_id: str,
    selection_id: str,
) -> bool:
    return (
        type(event) is MarketEvent
        and event.event_id == event_identity
        and event.source_id == source_id
        and event.sport == sport_id
        and event.competition_id == competition_id
        and event.market_id == market_context_id
        and event.selection_id == selection_id
    )


def _source_pair_digest(
    subject: MarketEvent,
    opponent: MarketEvent,
    frozen_at: str,
    identity_sha256: str,
    provider_binding_as_of: str,
    provider_inference_as_of: str | None,
) -> str:
    return _digest(
        {
            "kind": _SOURCE_EVIDENCE_KIND,
            "frozen_at": frozen_at,
            "identity_sha256": identity_sha256,
            "provider_binding_as_of": provider_binding_as_of,
            "provider_inference_as_of": provider_inference_as_of,
            "subject_selection_id": subject.selection_id,
            "subject_dedupe_key": subject.dedupe_key,
            "subject_quote_key": subject.quote_key,
            "subject_payload_sha256": _digest(subject.to_dict()),
            "opponent_selection_id": opponent.selection_id,
            "opponent_dedupe_key": opponent.dedupe_key,
            "opponent_quote_key": opponent.quote_key,
            "opponent_payload_sha256": _digest(opponent.to_dict()),
        }
    )


class SportMemoryResultMaterializer:
    """Project canonical result evidence into existing sport-memory authorities."""

    def __init__(
        self,
        opponent_store: OpponentIntelligenceStore,
        runtime: AutonomousProductRuntime,
    ) -> None:
        if type(opponent_store) is not OpponentIntelligenceStore:
            raise TypeError("opponent_store must be exact OpponentIntelligenceStore")
        if type(opponent_store.identity_registry) is not ParticipantIdentityRegistry:
            raise TypeError(
                "opponent_store identity_registry must be exact ParticipantIdentityRegistry"
            )
        if type(runtime) is not AutonomousProductRuntime:
            raise TypeError(
                "runtime must be exact product-owned AutonomousProductRuntime"
            )
        coordinator = runtime.coordinator
        if type(coordinator) is not ContinuousSessionCoordinator:
            raise TypeError(
                "runtime coordinator must be exact ContinuousSessionCoordinator"
            )
        if type(runtime.market_store) is not SQLiteMarketStore:
            raise TypeError("runtime market_store must be exact SQLiteMarketStore")
        if type(runtime.lifecycle) is not ContinuousEventLifecycle:
            raise TypeError("runtime lifecycle must be exact ContinuousEventLifecycle")
        if (
            coordinator.market_store is not runtime.market_store
            or coordinator.lifecycle is not runtime.lifecycle
        ):
            raise TypeError(
                "runtime settlement holder is not bound to canonical market/lifecycle"
            )
        outcome_authority = coordinator.outcome_authority
        if outcome_authority is None or not callable(
            getattr(outcome_authority, "resolve", None)
        ):
            raise TypeError(
                "product runtime has no configured settlement outcome authority"
            )
        if outcome_authority is not runtime.collector.source:
            raise TypeError(
                "settlement outcome authority is not owned by the product source"
            )
        authority_identity = runtime.manifest.settlement_authority_identity
        if (
            type(authority_identity) is not str
            or len(authority_identity) != 64
            or authority_identity != authority_identity.lower()
            or any(
                character not in "0123456789abcdef"
                for character in authority_identity
            )
        ):
            raise TypeError(
                "product runtime lacks durable settlement authority identity"
            )
        try:
            computed_identity = _settlement_authority_identity(
                source=runtime.collector.source,
                source_id=runtime.manifest.source_id,
                outcome_authority=outcome_authority,
            )
        except ProductCompositionError as exc:
            raise TypeError(
                "product runtime settlement authority identity cannot be verified"
            ) from exc
        if computed_identity != authority_identity:
            raise TypeError(
                "product runtime settlement authority identity does not match manifest"
            )
        self.opponent_store = opponent_store
        self._runtime = runtime
        self._coordinator = coordinator
        self.market_store = runtime.market_store
        self._lifecycle = runtime.lifecycle
        self._outcome_authority = outcome_authority
        self._settlement_authority_identity = authority_identity

    @property
    def runtime(self) -> AutonomousProductRuntime:
        return self._runtime

    def _require_canonical_stores(self) -> ParticipantIdentityRegistry:
        if type(self.opponent_store) is not OpponentIntelligenceStore:
            raise TypeError("opponent_store capability changed after construction")
        if type(self._runtime) is not AutonomousProductRuntime:
            raise TypeError("product runtime capability changed after construction")
        if type(self._coordinator) is not ContinuousSessionCoordinator:
            raise TypeError("continuous coordinator capability changed after construction")
        if self._runtime.coordinator is not self._coordinator:
            raise TypeError("product runtime coordinator changed after construction")
        if type(self.market_store) is not SQLiteMarketStore:
            raise TypeError("market_store capability changed after construction")
        if self._runtime.market_store is not self.market_store:
            raise TypeError("product runtime market_store changed after construction")
        if type(self._lifecycle) is not ContinuousEventLifecycle:
            raise TypeError("lifecycle capability changed after construction")
        if self._runtime.lifecycle is not self._lifecycle:
            raise TypeError("product runtime lifecycle changed after construction")
        if (
            self._coordinator.market_store is not self.market_store
            or self._coordinator.lifecycle is not self._lifecycle
            or self._coordinator.outcome_authority is not self._outcome_authority
            or self._outcome_authority is not self._runtime.collector.source
            or self._runtime.manifest.settlement_authority_identity
            != self._settlement_authority_identity
        ):
            raise TypeError(
                "product-owned settlement authority binding changed after construction"
            )
        if not callable(getattr(self._outcome_authority, "resolve", None)):
            raise TypeError("outcome_authority capability changed after construction")
        try:
            current_identity = _settlement_authority_identity(
                source=self._runtime.collector.source,
                source_id=self._runtime.manifest.source_id,
                outcome_authority=self._outcome_authority,
            )
        except ProductCompositionError as exc:
            raise TypeError(
                "product-owned settlement authority identity changed after construction"
            ) from exc
        if current_identity != self._settlement_authority_identity:
            raise TypeError(
                "product-owned settlement authority identity changed after construction"
            )
        registry = self.opponent_store.identity_registry
        if type(registry) is not ParticipantIdentityRegistry:
            raise TypeError("identity_registry capability changed after construction")
        return registry

    def _market_events(self, event_identity: str) -> tuple[MarketEvent, ...]:
        self._require_canonical_stores()
        try:
            return tuple(SQLiteMarketStore.events(self.market_store, event_identity))
        except (OSError, TypeError, ValueError) as exc:
            raise SportMemoryResultMaterializationError(
                "canonical market history could not resolve result binding"
            ) from exc

    def _resolve_identity_snapshot(
        self,
        *,
        event_identity: str,
        source_id: str,
        subject_alias: str,
        opponent_alias: str,
        league_alias: str,
        subject_selection_id: str,
        opponent_selection_id: str,
        competition_id: str,
        as_of: str,
    ) -> _IdentitySnapshot:
        registry = self._require_canonical_stores()
        identifiers = (
            subject_alias,
            subject_selection_id,
            opponent_alias,
            opponent_selection_id,
            league_alias,
            competition_id,
        )
        try:
            records = tuple(
                ParticipantIdentityRegistry.resolve_alias_record(
                    registry,
                    source_id,
                    identifier,
                    as_of=as_of,
                    view=IdentityView.AS_KNOWN_AT_DECISION,
                )
                for identifier in identifiers
            )
            entities = tuple(
                ParticipantIdentityRegistry.resolve_alias(
                    registry,
                    source_id,
                    identifier,
                    as_of=as_of,
                    view=IdentityView.AS_KNOWN_AT_DECISION,
                )
                for identifier in identifiers
            )
            roster = ParticipantIdentityRegistry.roster_at(
                registry,
                event_identity,
                source_id,
                as_of=as_of,
                view=IdentityView.AS_KNOWN_AT_DECISION,
            )
        except ParticipantIdentityError as exc:
            raise SportMemoryResultMaterializationError(
                "result binding lacks exact canonical pre-reveal identity evidence"
            ) from exc

        subject_alias_entity, subject_selection_entity = entities[0], entities[1]
        opponent_alias_entity, opponent_selection_entity = entities[2], entities[3]
        league_alias_entity, competition_entity = entities[4], entities[5]
        if subject_alias_entity.entity_id != subject_selection_entity.entity_id:
            raise SportMemoryResultMaterializationError(
                "provider subject selection identity does not match canonical participant alias"
            )
        if opponent_alias_entity.entity_id != opponent_selection_entity.entity_id:
            raise SportMemoryResultMaterializationError(
                "provider opponent selection identity does not match canonical participant alias"
            )
        if league_alias_entity.entity_id != competition_entity.entity_id:
            raise SportMemoryResultMaterializationError(
                "provider competition identity does not match canonical league alias"
            )
        if subject_alias_entity.entity_id == opponent_alias_entity.entity_id:
            raise SportMemoryResultMaterializationError(
                "subject and opponent resolve to the same canonical entity"
            )
        if subject_alias_entity.kind is EntityKind.LEAGUE or opponent_alias_entity.kind is EntityKind.LEAGUE:
            raise SportMemoryResultMaterializationError(
                "event participants cannot resolve to league identities"
            )
        if league_alias_entity.kind is not EntityKind.LEAGUE:
            raise SportMemoryResultMaterializationError(
                "competition must resolve to a canonical league identity"
            )
        roster_ids = tuple(sorted(entity.entity_id for entity in roster))
        if (
            subject_alias_entity.entity_id not in roster_ids
            or opponent_alias_entity.entity_id not in roster_ids
        ):
            raise SportMemoryResultMaterializationError(
                "provider selections are not canonical members of the bound event roster"
            )
        return _IdentitySnapshot(
            subject_entity_id=subject_alias_entity.entity_id,
            opponent_entity_id=opponent_alias_entity.entity_id,
            league_entity_id=league_alias_entity.entity_id,
            record_ids=tuple(record.record_id for record in records),
            roster_entity_ids=roster_ids,
        )

    def _resolve_source_pair(
        self,
        *,
        event_identity: str,
        source_id: str,
        subject_selection_id: str,
        opponent_selection_id: str,
        sport_id: str,
        competition_id: str,
        market_context_id: str,
        subject_quote_key: str,
        opponent_quote_key: str | None,
        as_of: str,
    ) -> tuple[MarketEvent, MarketEvent, str]:
        cutoff = _instant("source binding as_of", as_of)
        events = self._market_events(event_identity)

        def latest(selection_id: str) -> MarketEvent:
            candidates = [
                item
                for item in events
                if _matches_context(
                    item,
                    event_identity=event_identity,
                    source_id=source_id,
                    sport_id=sport_id,
                    competition_id=competition_id,
                    market_context_id=market_context_id,
                    selection_id=selection_id,
                )
                and _instant("market ingest_ts", item.ingest_ts) <= cutoff
            ]
            if not candidates:
                raise SportMemoryResultMaterializationError(
                    "result binding lacks canonical pre-reveal participant quote evidence"
                )
            return max(candidates, key=_event_order)

        subject = latest(subject_selection_id)
        opponent = latest(opponent_selection_id)
        if subject.dedupe_key == opponent.dedupe_key:
            raise SportMemoryResultMaterializationError(
                "subject and opponent quote evidence must be distinct"
            )
        if subject.quote_key != subject_quote_key:
            raise SportMemoryResultMaterializationError(
                "subject quote key does not equal canonical source quote identity"
            )
        if opponent_quote_key is not None and opponent.quote_key != opponent_quote_key:
            raise SportMemoryResultMaterializationError(
                "opponent quote key does not equal canonical source quote identity"
            )
        frozen_at = max(
            _time_text("subject ingest_ts", subject.ingest_ts),
            _time_text("opponent ingest_ts", opponent.ingest_ts),
            key=lambda value: _instant("source pair frozen_at", value),
        )
        return subject, opponent, frozen_at

    def _infer_provider_identifiers(
        self,
        *,
        event_identity: str,
        source_id: str,
        subject_alias: str,
        opponent_alias: str,
        sport_id: str,
        market_context_id: str,
        subject_quote_key: str,
        as_of: str,
        subject_selection_id: str | None,
        opponent_selection_id: str | None,
        competition_id: str | None,
    ) -> tuple[str, str, str]:
        registry = self._require_canonical_stores()
        cutoff = _instant("source binding as_of", as_of)
        events = self._market_events(event_identity)
        subject_candidates = [
            event
            for event in events
            if type(event) is MarketEvent
            and event.event_id == event_identity
            and event.source_id == source_id
            and event.sport == sport_id
            and event.market_id == market_context_id
            and event.quote_key == subject_quote_key
            and _instant("market ingest_ts", event.ingest_ts) <= cutoff
            and (subject_selection_id is None or event.selection_id == subject_selection_id)
            and (competition_id is None or event.competition_id == competition_id)
        ]
        if not subject_candidates:
            raise SportMemoryResultMaterializationError(
                "subject quote key lacks exact canonical pre-reveal market evidence"
            )
        subject = max(subject_candidates, key=_event_order)
        subject_selection = subject.selection_id
        competition = subject.competition_id
        if subject_selection_id is not None and subject_selection != subject_selection_id:
            raise SportMemoryResultMaterializationError(
                "subject provider selection id does not match canonical quote"
            )
        if competition_id is not None and competition != competition_id:
            raise SportMemoryResultMaterializationError(
                "provider competition id does not match canonical quote"
            )

        try:
            opponent_alias_record = ParticipantIdentityRegistry.resolve_alias_record(
                registry,
                source_id,
                opponent_alias,
                as_of=as_of,
                view=IdentityView.AS_KNOWN_AT_DECISION,
            )
        except ParticipantIdentityError as exc:
            raise SportMemoryResultMaterializationError(
                "opponent alias lacks canonical identity at source cutoff"
            ) from exc
        candidate_selections = sorted(
            {
                event.selection_id
                for event in events
                if type(event) is MarketEvent
                and event.event_id == event_identity
                and event.source_id == source_id
                and event.sport == sport_id
                and event.competition_id == competition
                and event.market_id == market_context_id
                and event.selection_id != subject_selection
                and _instant("market ingest_ts", event.ingest_ts) <= cutoff
            }
        )
        matches: list[str] = []
        for selection in candidate_selections:
            try:
                record = ParticipantIdentityRegistry.resolve_alias_record(
                    registry,
                    source_id,
                    selection,
                    as_of=as_of,
                    view=IdentityView.AS_KNOWN_AT_DECISION,
                )
            except ParticipantIdentityError:
                continue
            if record.entity_id == opponent_alias_record.entity_id:
                matches.append(selection)
        if len(matches) != 1:
            raise SportMemoryResultMaterializationError(
                "opponent provider selection cannot be resolved uniquely through canonical identity"
            )
        if opponent_selection_id is not None and matches[0] != opponent_selection_id:
            raise SportMemoryResultMaterializationError(
                "opponent provider selection id does not match canonical pre-reveal identity"
            )
        return subject_selection, matches[0], competition

    def freeze_binding(
        self,
        *,
        event_identity: str,
        source_id: str,
        subject_alias: str,
        opponent_alias: str,
        sport_id: str,
        league_alias: str,
        market_context_id: str,
        subject_quote_key: str,
        as_of: str,
        subject_selection_id: str | None = None,
        opponent_selection_id: str | None = None,
        competition_id: str | None = None,
    ) -> SportMemoryResultBinding:
        """Mint a binding only from durable market, identity, and roster evidence."""

        self._require_canonical_stores()
        for name, value in (
            ("event_identity", event_identity),
            ("source_id", source_id),
            ("subject_alias", subject_alias),
            ("opponent_alias", opponent_alias),
            ("sport_id", sport_id),
            ("league_alias", league_alias),
            ("market_context_id", market_context_id),
            ("subject_quote_key", subject_quote_key),
        ):
            _text(name, value)
        binding_as_of = _time_text("as_of", as_of)
        for name, value in (
            ("subject_selection_id", subject_selection_id),
            ("opponent_selection_id", opponent_selection_id),
            ("competition_id", competition_id),
        ):
            if value is not None:
                _text(name, value)
        if subject_alias == opponent_alias:
            raise SportMemoryResultMaterializationError(
                "subject and opponent aliases must be distinct"
            )
        opponent_was_inferred = opponent_selection_id is None
        subject_selection, opponent_selection, competition = self._infer_provider_identifiers(
            event_identity=event_identity,
            source_id=source_id,
            subject_alias=subject_alias,
            opponent_alias=opponent_alias,
            sport_id=sport_id,
            market_context_id=market_context_id,
            subject_quote_key=subject_quote_key,
            as_of=binding_as_of,
            subject_selection_id=subject_selection_id,
            opponent_selection_id=opponent_selection_id,
            competition_id=competition_id,
        )
        subject, opponent, frozen_at = self._resolve_source_pair(
            event_identity=event_identity,
            source_id=source_id,
            subject_selection_id=subject_selection,
            opponent_selection_id=opponent_selection,
            sport_id=sport_id,
            competition_id=competition,
            market_context_id=market_context_id,
            subject_quote_key=subject_quote_key,
            opponent_quote_key=None,
            as_of=binding_as_of,
        )
        provider_binding_as_of = binding_as_of
        provider_inference_as_of = binding_as_of if opponent_was_inferred else None
        if _instant("provider_binding_as_of", provider_binding_as_of) < _instant(
            "frozen_at", frozen_at
        ):
            raise SportMemoryResultMaterializationError(
                "provider-binding cutoff cannot precede frozen quote evidence"
            )
        snapshot = self._resolve_identity_snapshot(
            event_identity=event_identity,
            source_id=source_id,
            subject_alias=subject_alias,
            opponent_alias=opponent_alias,
            league_alias=league_alias,
            subject_selection_id=subject_selection,
            opponent_selection_id=opponent_selection,
            competition_id=competition,
            as_of=frozen_at,
        )
        identity_sha256 = snapshot.sha256
        evidence_sha256 = _source_pair_digest(
            subject,
            opponent,
            frozen_at,
            identity_sha256,
            provider_binding_as_of,
            provider_inference_as_of,
        )
        return SportMemoryResultBinding(
            event_identity=event_identity,
            source_id=source_id,
            subject_alias=subject_alias,
            opponent_alias=opponent_alias,
            sport_id=sport_id,
            league_alias=league_alias,
            market_context_id=market_context_id,
            subject_quote_key=subject_quote_key,
            frozen_at=frozen_at,
            evidence_sha256=evidence_sha256,
            subject_selection_id=subject_selection,
            opponent_selection_id=opponent_selection,
            competition_id=competition,
            opponent_quote_key=opponent.quote_key,
            subject_entity_id=snapshot.subject_entity_id,
            opponent_entity_id=snapshot.opponent_entity_id,
            league_entity_id=snapshot.league_entity_id,
            provider_binding_as_of=provider_binding_as_of,
            provider_inference_as_of=provider_inference_as_of,
            identity_sha256=identity_sha256,
        )

    @staticmethod
    def _complete_provider_binding(
        binding: SportMemoryResultBinding,
    ) -> tuple[str, str, str, str, str, str, str, str]:
        values = (
            binding.subject_selection_id,
            binding.opponent_selection_id,
            binding.competition_id,
            binding.opponent_quote_key,
            binding.subject_entity_id,
            binding.opponent_entity_id,
            binding.league_entity_id,
            binding.identity_sha256,
        )
        if any(value is None for value in values):
            raise SportMemoryResultMaterializationError(
                "result binding is not exact canonical pre-reveal provider identity evidence"
            )
        (
            subject_selection,
            opponent_selection,
            competition,
            opponent_quote,
            subject_entity,
            opponent_entity,
            league_entity,
            identity_sha,
        ) = values
        assert subject_selection is not None
        assert opponent_selection is not None
        assert competition is not None
        assert opponent_quote is not None
        assert subject_entity is not None
        assert opponent_entity is not None
        assert league_entity is not None
        assert identity_sha is not None
        return (
            _text("subject_selection_id", subject_selection),
            _text("opponent_selection_id", opponent_selection),
            _text("competition_id", competition),
            _text("opponent_quote_key", opponent_quote),
            _text("subject_entity_id", subject_entity),
            _text("opponent_entity_id", opponent_entity),
            _text("league_entity_id", league_entity),
            _sha256("identity_sha256", identity_sha),
        )

    def _assert_source_binding(
        self,
        binding: SportMemoryResultBinding,
        *,
        settlement_available_at: str,
    ) -> _IdentitySnapshot:
        available = _instant("settlement.available_at", settlement_available_at)
        frozen = _instant("binding.frozen_at", binding.frozen_at)
        if frozen >= available:
            raise SportMemoryResultMaterializationError(
                "result binding must be frozen before settlement reveal"
            )
        if binding.provider_binding_as_of is None:
            raise SportMemoryResultMaterializationError(
                "result binding lacks product-owned pre-reveal provider-selection authority"
            )
        provider_binding_as_of = _instant(
            "binding.provider_binding_as_of",
            binding.provider_binding_as_of,
        )
        if provider_binding_as_of < frozen:
            raise SportMemoryResultMaterializationError(
                "provider-binding cutoff cannot precede frozen quote evidence"
            )
        if provider_binding_as_of >= available:
            if binding.provider_inference_as_of is not None:
                raise SportMemoryResultMaterializationError(
                    "provider-selection inference must predate settlement reveal"
                )
            raise SportMemoryResultMaterializationError(
                "result binding is not canonical pre-reveal authority: provider-selection binding must predate settlement reveal"
            )
        if binding.provider_inference_as_of is not None:
            inference = _instant(
                "binding.provider_inference_as_of",
                binding.provider_inference_as_of,
            )
            if inference != provider_binding_as_of:
                raise SportMemoryResultMaterializationError(
                    "provider-selection inference cutoff must equal canonical binding cutoff"
                )
        (
            subject_selection,
            opponent_selection,
            competition,
            opponent_quote,
            subject_entity,
            opponent_entity,
            league_entity,
            identity_sha,
        ) = self._complete_provider_binding(binding)

        # Caller-authored fields are assertions only. Re-derive the exact provider
        # identities from product-owned pre-reveal market + identity history at the
        # persisted causal cutoff even when the caller labels them "explicit". This
        # prevents a post-result caller from choosing among a historically ambiguous
        # candidate set, clearing inference provenance, and recomputing a valid digest.
        derived_subject, derived_opponent, derived_competition = self._infer_provider_identifiers(
            event_identity=binding.event_identity,
            source_id=binding.source_id,
            subject_alias=binding.subject_alias,
            opponent_alias=binding.opponent_alias,
            sport_id=binding.sport_id,
            market_context_id=binding.market_context_id,
            subject_quote_key=binding.subject_quote_key,
            as_of=binding.provider_binding_as_of,
            subject_selection_id=subject_selection,
            opponent_selection_id=opponent_selection,
            competition_id=competition,
        )
        if (
            derived_subject != subject_selection
            or derived_opponent != opponent_selection
            or derived_competition != competition
        ):
            raise SportMemoryResultMaterializationError(
                "provider identities are not uniquely fixed by canonical pre-reveal evidence"
            )

        subject, opponent, expected_frozen = self._resolve_source_pair(
            event_identity=binding.event_identity,
            source_id=binding.source_id,
            subject_selection_id=subject_selection,
            opponent_selection_id=opponent_selection,
            sport_id=binding.sport_id,
            competition_id=competition,
            market_context_id=binding.market_context_id,
            subject_quote_key=binding.subject_quote_key,
            opponent_quote_key=opponent_quote,
            as_of=binding.frozen_at,
        )
        frozen_snapshot = self._resolve_identity_snapshot(
            event_identity=binding.event_identity,
            source_id=binding.source_id,
            subject_alias=binding.subject_alias,
            opponent_alias=binding.opponent_alias,
            league_alias=binding.league_alias,
            subject_selection_id=subject_selection,
            opponent_selection_id=opponent_selection,
            competition_id=competition,
            as_of=binding.frozen_at,
        )
        expected_digest = _source_pair_digest(
            subject,
            opponent,
            expected_frozen,
            frozen_snapshot.sha256,
            binding.provider_binding_as_of,
            binding.provider_inference_as_of,
        )
        if (
            binding.frozen_at != expected_frozen
            or frozen_snapshot.subject_entity_id != subject_entity
            or frozen_snapshot.opponent_entity_id != opponent_entity
            or frozen_snapshot.league_entity_id != league_entity
            or binding.identity_sha256 != identity_sha
            or frozen_snapshot.sha256 != identity_sha
            or binding.evidence_sha256 != expected_digest
            or _instant("subject ingest_ts", subject.ingest_ts) >= available
            or _instant("opponent ingest_ts", opponent.ingest_ts) >= available
        ):
            raise SportMemoryResultMaterializationError(
                "result binding is not exact canonical pre-reveal market and identity evidence"
            )
        return frozen_snapshot

    def _resolve_canonical_settlement(
        self,
        binding: SportMemoryResultBinding,
        asserted: SettlementResolution,
        *,
        as_of: str,
    ) -> SettlementResolution:
        """Re-resolve product-owned outcome truth; caller DTO is assertion-only."""

        self._require_canonical_stores()
        if asserted.event_identity != binding.event_identity:
            raise SportMemoryResultMaterializationError(
                "settlement event does not match frozen result binding"
            )
        try:
            SettlementResolution.validate(asserted, as_of=as_of)
        except (TypeError, ValueError) as exc:
            raise SportMemoryResultMaterializationError(
                "settlement assertion is malformed or not causally available"
            ) from exc
        try:
            record = ContinuousEventLifecycle.get(
                self._lifecycle,
                binding.event_identity,
            )
        except (CatalogLifecycleError, OSError, TypeError, ValueError) as exc:
            raise SportMemoryResultMaterializationError(
                "canonical lifecycle could not resolve settlement authority"
            ) from exc
        if (
            record is None
            or record.phase is not EventPhase.COMPLETED
            or record.settlement_ref is None
        ):
            raise SportMemoryResultMaterializationError(
                "result binding lacks completed canonical lifecycle settlement evidence"
            )
        if (
            record.source_id != binding.source_id
            or record.sport != binding.sport_id
        ):
            raise SportMemoryResultMaterializationError(
                "canonical lifecycle does not match frozen result binding"
            )

        try:
            canonical = self._outcome_authority.resolve(record, as_of=as_of)
        except Exception as exc:
            raise SportMemoryResultMaterializationError(
                "product-owned outcome authority could not resolve settlement"
            ) from exc
        if type(canonical) is not SettlementResolution:
            raise SportMemoryResultMaterializationError(
                "product-owned outcome authority returned no exact settlement"
            )
        try:
            SettlementResolution.validate(canonical, as_of=as_of)
        except (TypeError, ValueError) as exc:
            raise SportMemoryResultMaterializationError(
                "product-owned settlement is malformed or not causally available"
            ) from exc
        if (
            canonical.event_identity != record.identity
            or canonical.settlement_ref != record.settlement_ref
        ):
            raise SportMemoryResultMaterializationError(
                "product-owned settlement does not match canonical lifecycle evidence"
            )
        asserted_payload = (
            asserted.event_identity,
            asserted.settlement_ref,
            asserted.quote_outcomes.copy(),
            asserted.evidence_id,
            asserted.evidence_sha256,
            asserted.available_at,
        )
        canonical_payload = (
            canonical.event_identity,
            canonical.settlement_ref,
            canonical.quote_outcomes.copy(),
            canonical.evidence_id,
            canonical.evidence_sha256,
            canonical.available_at,
        )
        if asserted_payload != canonical_payload:
            raise SportMemoryResultMaterializationError(
                "settlement assertion differs from product-owned outcome authority"
            )

        # Snapshot the exact externally resolved public payload before using it.
        # Restart intentionally repeats the external resolution; no persisted or
        # caller-mintable token becomes settlement truth authority.
        return SettlementResolution(
            event_identity=canonical.event_identity,
            settlement_ref=canonical.settlement_ref,
            quote_outcomes=canonical.quote_outcomes.copy(),
            evidence_id=canonical.evidence_id,
            evidence_sha256=canonical.evidence_sha256,
            available_at=canonical.available_at,
        )

    def _retire_void_predecessor(
        self,
        binding: SportMemoryResultBinding,
        settlement: SettlementResolution,
        performance_id: str,
    ) -> None:
        self._require_canonical_stores()
        performance_id = _sha256("supersedes_performance_id", performance_id)
        predecessor = self.opponent_store._performances.get(performance_id)
        if predecessor is None:
            raise SportMemoryResultMaterializationError(
                "void correction references unknown performance predecessor"
            )
        before = predecessor.observation
        expected_context = (
            binding.event_identity,
            binding.source_id,
            binding.subject_alias,
            binding.opponent_alias,
            binding.sport_id,
            binding.league_alias,
            binding.market_context_id,
        )
        actual_context = (
            before.event_id,
            before.source_id,
            before.subject_alias,
            before.opponent_alias,
            before.sport_id,
            before.league_alias,
            before.market_context_id,
        )
        if actual_context != expected_context:
            raise SportMemoryResultMaterializationError(
                "void correction must preserve exact performance context"
            )
        if any(
            item.observation.supersedes_performance_id == performance_id
            for item in self.opponent_store._performances.values()
        ):
            raise SportMemoryResultMaterializationError(
                "void correction must retire the active performance tip"
            )
        if _instant("void available_at", settlement.available_at) <= _instant(
            "predecessor recorded_at", before.recorded_at
        ):
            raise SportMemoryResultMaterializationError(
                "void correction must become available after predecessor performance"
            )
        try:
            OpponentIntelligenceStore.retire_performance_outcome(
                self.opponent_store,
                performance_id,
                evidence_sha256=settlement.evidence_sha256,
                detected_at=settlement.available_at,
            )
        except OpponentIntelligenceError as exc:
            raise SportMemoryResultMaterializationError(
                "canonical opponent store rejected void correction"
            ) from exc

    def materialize(
        self,
        binding: SportMemoryResultBinding,
        settlement: SettlementResolution,
        *,
        as_of: str,
        supersedes_performance_id: str | None = None,
    ) -> SportMemoryResultMaterialization:
        self._require_canonical_stores()
        if type(binding) is not SportMemoryResultBinding:
            raise TypeError("binding must be exact SportMemoryResultBinding")
        if type(settlement) is not SettlementResolution:
            raise TypeError("settlement must be exact SettlementResolution")
        cutoff = _instant("as_of", as_of)
        settlement = self._resolve_canonical_settlement(
            binding,
            settlement,
            as_of=as_of,
        )
        available = _instant("settlement.available_at", settlement.available_at)
        if cutoff < available:
            raise SportMemoryResultMaterializationError(
                "settlement evidence is not causally available at requested cutoff"
            )
        if settlement.event_identity != binding.event_identity:
            raise SportMemoryResultMaterializationError(
                "settlement event does not match frozen result binding"
            )
        frozen_snapshot = self._assert_source_binding(
            binding,
            settlement_available_at=settlement.available_at,
        )
        if binding.subject_quote_key not in settlement.quote_outcomes:
            raise SportMemoryResultMaterializationError(
                "settlement does not contain the canonical frozen subject quote"
            )
        outcome = settlement.quote_outcomes[binding.subject_quote_key]
        if outcome not in {"win", "loss", "void"}:
            raise SportMemoryResultMaterializationError(
                "settlement contains unsupported subject outcome"
            )

        (
            subject_selection,
            opponent_selection,
            competition,
            _,
            _,
            _,
            _,
            _,
        ) = self._complete_provider_binding(binding)
        result_snapshot = self._resolve_identity_snapshot(
            event_identity=binding.event_identity,
            source_id=binding.source_id,
            subject_alias=binding.subject_alias,
            opponent_alias=binding.opponent_alias,
            league_alias=binding.league_alias,
            subject_selection_id=subject_selection,
            opponent_selection_id=opponent_selection,
            competition_id=competition,
            as_of=settlement.available_at,
        )
        if result_snapshot != frozen_snapshot:
            raise SportMemoryResultMaterializationError(
                "identity or event roster changed between result freeze and reveal; explicit restatement required"
            )

        if supersedes_performance_id is not None:
            _sha256("supersedes_performance_id", supersedes_performance_id)

        if outcome == "void":
            if supersedes_performance_id is not None:
                self._retire_void_predecessor(
                    binding,
                    settlement,
                    supersedes_performance_id,
                )
            return SportMemoryResultMaterialization(
                binding_sha256=binding.binding_sha256,
                settlement_evidence_id=settlement.evidence_id,
                settlement_evidence_sha256=settlement.evidence_sha256,
                settlement_ref=settlement.settlement_ref,
                outcome=outcome,
                performance_id=None,
            )

        observation = ObservedPerformance(
            event_id=binding.event_identity,
            source_id=binding.source_id,
            subject_alias=binding.subject_alias,
            opponent_alias=binding.opponent_alias,
            sport_id=binding.sport_id,
            league_alias=binding.league_alias,
            market_context_id=binding.market_context_id,
            score="1" if outcome == "win" else "0",
            observed_at=settlement.available_at,
            available_at=settlement.available_at,
            recorded_at=settlement.available_at,
            evidence_sha256=settlement.evidence_sha256,
            truth=EvidenceTruth.OBSERVED,
            supersedes_performance_id=supersedes_performance_id,
        )
        try:
            record: PerformanceRecord = OpponentIntelligenceStore.record_performance(
                self.opponent_store,
                observation,
                identity_view=IdentityView.AS_KNOWN_AT_DECISION,
            )
        except (OpponentIntelligenceError, ParticipantIdentityError) as exc:
            raise SportMemoryResultMaterializationError(
                "canonical opponent store rejected result projection"
            ) from exc

        return SportMemoryResultMaterialization(
            binding_sha256=binding.binding_sha256,
            settlement_evidence_id=settlement.evidence_id,
            settlement_evidence_sha256=settlement.evidence_sha256,
            settlement_ref=settlement.settlement_ref,
            outcome=outcome,
            performance_id=record.performance_id,
        )
