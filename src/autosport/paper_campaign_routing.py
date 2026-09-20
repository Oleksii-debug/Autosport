"""Durable multi-ticket routing for PAPER campaign learning handoffs.

The continuous product session can settle several tickets in one tick while one
``PaperCampaignLearningHandoff`` is intentionally bound to one serial AgentLoop
campaign.  This module adds only the missing routing index: it derives immutable
route identity from already-canonical campaign handoffs, persists that identity,
and dispatches settlement preparation/recovery to the exact bound handoff.

It owns no ticket, settlement, reward, campaign, AgentLoop, or economic truth.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .paper_campaign_runtime import PaperCampaignLearningHandoff
from .paper_settlement_learning import PaperSettlementLearningBridgeError
from .continuous_session import SettlementResolution
from .workspace_lock import WorkspaceEconomicLock


class PaperCampaignRouteError(RuntimeError):
    """Durable ticket-to-learning routing is missing or conflicts with truth."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value or "\x00" in value:
        raise PaperCampaignRouteError(f"{field} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PaperCampaignRouteError(f"{field} must be valid UTF-8") from exc
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PaperCampaignRouteError("route evidence is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PaperCampaignRouteError(f"{field} must be lowercase SHA-256 hex")
    return text


@dataclass(frozen=True, slots=True)
class PaperCampaignRoute:
    route_id: str
    ticket_id: str
    environment_id: str
    episode_id: str
    action_id: str
    status: str


class PaperCampaignRouteStore:
    """Append-only identity index with one monotone ACTIVE -> FINALIZED state."""

    _SCHEMA = "autosport.paper_campaign_route_index"
    _VERSION = 1
    _ACTIVE = "ACTIVE"
    _FINALIZED = "FINALIZED"
    _FIELDS = {"schema", "schema_version", "routes", "state_sha256"}
    _ROUTE_FIELDS = {
        "route_id",
        "ticket_id",
        "environment_id",
        "episode_id",
        "action_id",
        "status",
    }

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(self.path.parent):
            if self.path.exists():
                self._read()
            else:
                self._write({})

    @staticmethod
    def _route_identity(
        *,
        ticket_id: str,
        environment_id: str,
        episode_id: str,
        action_id: str,
    ) -> dict[str, str]:
        return {
            "ticket_id": _text(ticket_id, "ticket_id"),
            "environment_id": _text(environment_id, "environment_id"),
            "episode_id": _text(episode_id, "episode_id"),
            "action_id": _text(action_id, "action_id"),
        }

    @classmethod
    def _record(
        cls,
        *,
        ticket_id: str,
        environment_id: str,
        episode_id: str,
        action_id: str,
        status: str,
    ) -> dict[str, str]:
        identity = cls._route_identity(
            ticket_id=ticket_id,
            environment_id=environment_id,
            episode_id=episode_id,
            action_id=action_id,
        )
        if status not in {cls._ACTIVE, cls._FINALIZED}:
            raise PaperCampaignRouteError("unsupported campaign route status")
        return {"route_id": _digest(identity), **identity, "status": status}

    def _write(self, routes: dict[str, object]) -> None:
        bare = {
            "schema": self._SCHEMA,
            "schema_version": self._VERSION,
            "routes": routes,
        }
        atomic_write_json(self.path, {**bare, "state_sha256": _digest(bare)})

    def _read(self) -> dict[str, object]:
        try:
            state = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise PaperCampaignRouteError("campaign route index is unreadable") from exc
        if (
            type(state) is not dict
            or set(state) != self._FIELDS
            or state.get("schema") != self._SCHEMA
            or state.get("schema_version") != self._VERSION
            or type(state.get("routes")) is not dict
        ):
            raise PaperCampaignRouteError("campaign route index schema mismatch")
        bare = {
            "schema": state["schema"],
            "schema_version": state["schema_version"],
            "routes": state["routes"],
        }
        if _sha(state["state_sha256"], "state_sha256") != _digest(bare):
            raise PaperCampaignRouteError("campaign route index digest mismatch")
        action_routes: dict[tuple[str, str, str], str] = {}
        for ticket_id, raw in state["routes"].items():
            _text(ticket_id, "route ticket key")
            if type(raw) is not dict or set(raw) != self._ROUTE_FIELDS:
                raise PaperCampaignRouteError("campaign route fields mismatch")
            expected = self._record(
                ticket_id=raw["ticket_id"],
                environment_id=raw["environment_id"],
                episode_id=raw["episode_id"],
                action_id=raw["action_id"],
                status=raw["status"],
            )
            if raw != expected or raw["ticket_id"] != ticket_id:
                raise PaperCampaignRouteError("campaign route identity mismatch")
            action_key = (
                raw["environment_id"],
                raw["episode_id"],
                raw["action_id"],
            )
            previous_ticket = action_routes.get(action_key)
            if previous_ticket is not None and previous_ticket != ticket_id:
                raise PaperCampaignRouteError(
                    "one campaign action cannot route multiple PaperTickets"
                )
            action_routes[action_key] = ticket_id
        return state

    @staticmethod
    def _from_raw(raw: dict[str, object]) -> PaperCampaignRoute:
        return PaperCampaignRoute(
            route_id=raw["route_id"],
            ticket_id=raw["ticket_id"],
            environment_id=raw["environment_id"],
            episode_id=raw["episode_id"],
            action_id=raw["action_id"],
            status=raw["status"],
        )

    @staticmethod
    def _handoff_identity(handoff: PaperCampaignLearningHandoff) -> dict[str, str]:
        if not isinstance(handoff, PaperCampaignLearningHandoff):
            raise TypeError("handoff must be PaperCampaignLearningHandoff")
        snapshot = handoff.runtime.agent_loop.snapshot()
        identity = PaperCampaignRouteStore._route_identity(
            ticket_id=handoff.ticket_id,
            environment_id=getattr(snapshot, "environment_id", None),
            episode_id=getattr(snapshot, "episode_id", None),
            action_id=getattr(snapshot, "action_id", None),
        )

        # Registration is only a projection of the bridge's existing durable
        # ticket/action authority.  A handoff's public ticket string is not
        # sufficient: prove that the bridge already binds this exact ticket to
        # the exact AgentLoop environment/episode/action before persisting a
        # routing row.  `_read()` is the bridge's canonical integrity validator;
        # identity fields are immutable after bind, so an atomic snapshot read is
        # sufficient here and avoids creating a second binding authority.
        bridge = getattr(handoff.runtime, "settlement_bridge", None)
        try:
            state = bridge._read()
            bindings = state["bindings"]
            binding = bindings.get(handoff.ticket_id)
        except (AttributeError, KeyError, TypeError, PaperSettlementLearningBridgeError) as exc:
            raise PaperCampaignRouteError(
                "cannot verify durable settlement-learning ticket binding"
            ) from exc
        if type(binding) is not dict:
            raise PaperCampaignRouteError(
                "handoff ticket lacks durable settlement-learning binding"
            )
        bound_identity = PaperCampaignRouteStore._route_identity(
            ticket_id=binding.get("ticket_id"),
            environment_id=binding.get("environment_id"),
            episode_id=binding.get("episode_id"),
            action_id=binding.get("action_id"),
        )
        if bound_identity != identity:
            raise PaperCampaignRouteError(
                "handoff ticket binding conflicts with durable AgentLoop action"
            )
        return identity

    def register(self, handoff: PaperCampaignLearningHandoff) -> PaperCampaignRoute:
        identity = self._handoff_identity(handoff)
        candidate = self._record(**identity, status=self._ACTIVE)
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            existing = state["routes"].get(identity["ticket_id"])
            if existing is not None:
                if existing["route_id"] != candidate["route_id"]:
                    raise PaperCampaignRouteError(
                        "ticket is already bound to another campaign learning route"
                    )
                return self._from_raw(existing)
            action_key = (
                identity["environment_id"],
                identity["episode_id"],
                identity["action_id"],
            )
            if any(
                (
                    raw["environment_id"],
                    raw["episode_id"],
                    raw["action_id"],
                )
                == action_key
                for raw in state["routes"].values()
            ):
                raise PaperCampaignRouteError(
                    "one campaign action cannot route multiple PaperTickets"
                )
            state["routes"][identity["ticket_id"]] = candidate
            self._write(state["routes"])
        return self._from_raw(candidate)

    def get(self, ticket_id: str) -> PaperCampaignRoute:
        canonical_ticket = _text(ticket_id, "ticket_id")
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            raw = state["routes"].get(canonical_ticket)
            if raw is None:
                raise PaperCampaignRouteError("ticket has no durable campaign learning route")
            return self._from_raw(raw)

    def routes(self) -> tuple[PaperCampaignRoute, ...]:
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            return tuple(
                self._from_raw(state["routes"][ticket_id])
                for ticket_id in sorted(state["routes"])
            )

    def _require_terminal_handoff(
        self,
        route: PaperCampaignRoute,
        handoff: PaperCampaignLearningHandoff,
    ) -> None:
        """Re-resolve canonical terminal evidence before persisting FINALIZED."""

        identity = self._handoff_identity(handoff)
        expected_identity = self._route_identity(
            ticket_id=route.ticket_id,
            environment_id=route.environment_id,
            episode_id=route.episode_id,
            action_id=route.action_id,
        )
        if identity != expected_identity:
            raise PaperCampaignRouteError(
                "terminal campaign handoff conflicts with durable ticket route"
            )
        try:
            witness = handoff.runtime.settlement_bridge.resolution_witness(route.ticket_id)
        except PaperSettlementLearningBridgeError as exc:
            raise PaperCampaignRouteError(
                "campaign route lacks canonical terminal settlement evidence"
            ) from exc

        transition = getattr(witness, "transition", None)
        next_checkpoint = getattr(witness, "next_checkpoint", None)
        transition_id = getattr(transition, "transition_id", None)
        checkpoint_id = getattr(next_checkpoint, "checkpoint_id", None)
        if (
            getattr(transition, "environment_id", None) != route.environment_id
            or getattr(transition, "episode_id", None) != route.episode_id
            or getattr(transition, "action_id", None) != route.action_id
            or not transition_id
            or not checkpoint_id
        ):
            raise PaperCampaignRouteError(
                "campaign terminal witness conflicts with durable ticket route"
            )

        snapshot = handoff.runtime.agent_loop.snapshot()
        if (
            getattr(snapshot, "environment_id", None) != route.environment_id
            or getattr(snapshot, "episode_id", None) != route.episode_id
            or getattr(snapshot, "action_id", None) != route.action_id
            or getattr(snapshot, "transition_id", None) != transition_id
            or getattr(snapshot, "checkpointed_transition_id", None) != transition_id
            or getattr(snapshot, "environment_checkpoint_id", None) != checkpoint_id
            or getattr(snapshot, "attribution_id", None) is None
            or getattr(snapshot, "postmortem_id", None) is None
        ):
            raise PaperCampaignRouteError(
                "campaign handoff has not reached canonical terminal checkpoint"
            )

    def mark_finalized(
        self,
        route: PaperCampaignRoute,
        *,
        handoff: PaperCampaignLearningHandoff,
    ) -> PaperCampaignRoute:
        if not isinstance(route, PaperCampaignRoute):
            raise TypeError("route must be PaperCampaignRoute")
        self._require_terminal_handoff(route, handoff)
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            current = state["routes"].get(route.ticket_id)
            if current is None or current["route_id"] != route.route_id:
                raise PaperCampaignRouteError("campaign route changed before finalization")
            expected = self._record(
                ticket_id=current["ticket_id"],
                environment_id=current["environment_id"],
                episode_id=current["episode_id"],
                action_id=current["action_id"],
                status=self._FINALIZED,
            )
            if current != expected:
                state["routes"][route.ticket_id] = expected
                self._write(state["routes"])
            return self._from_raw(expected)


class PaperCampaignLearningRouter:
    """SettlementLearningHandoff implementation for independently durable episodes."""

    def __init__(
        self,
        store: PaperCampaignRouteStore,
        *,
        resolve_handoff: Callable[[PaperCampaignRoute], PaperCampaignLearningHandoff],
    ) -> None:
        if not isinstance(store, PaperCampaignRouteStore):
            raise TypeError("store must be PaperCampaignRouteStore")
        if not callable(resolve_handoff):
            raise TypeError("resolve_handoff must be callable")
        self.store = store
        self.resolve_handoff = resolve_handoff

    def register(self, handoff: PaperCampaignLearningHandoff) -> PaperCampaignRoute:
        return self.store.register(handoff)

    def _resolve(self, route: PaperCampaignRoute) -> PaperCampaignLearningHandoff:
        handoff = self.resolve_handoff(route)
        if not isinstance(handoff, PaperCampaignLearningHandoff):
            raise PaperCampaignRouteError(
                "route resolver did not return PaperCampaignLearningHandoff"
            )
        identity = self.store._handoff_identity(handoff)
        expected = self.store._route_identity(
            ticket_id=route.ticket_id,
            environment_id=route.environment_id,
            episode_id=route.episode_id,
            action_id=route.action_id,
        )
        if identity != expected:
            raise PaperCampaignRouteError(
                "resolved campaign handoff conflicts with durable ticket route"
            )
        return handoff

    def prepare_settlement(
        self,
        *,
        paper_book_path: Path,
        resolutions: tuple[SettlementResolution, ...],
        at: str,
    ) -> tuple[str, ...]:
        if type(resolutions) is not tuple:
            raise TypeError("settlement handoff resolutions must be a tuple")
        prepared: list[str] = []
        for route in self.store.routes():
            if route.status == PaperCampaignRouteStore._FINALIZED:
                continue
            handoff = self._resolve(route)
            routed = handoff.prepare_settlement(
                paper_book_path=paper_book_path,
                resolutions=resolutions,
                at=at,
            )
            if any(ticket_id != route.ticket_id for ticket_id in routed):
                raise PaperCampaignRouteError(
                    "campaign handoff prepared a ticket outside its durable route"
                )
            if route.ticket_id in routed:
                prepared.append(route.ticket_id)
        return tuple(prepared)

    def reconcile_after_settlement(
        self,
        *,
        paper_book_path: Path,
        resolutions: tuple[SettlementResolution, ...],
        settled_ticket_ids: tuple[str, ...],
        at: str,
    ) -> tuple[str, ...]:
        if type(resolutions) is not tuple or type(settled_ticket_ids) is not tuple:
            raise TypeError("settlement handoff collections must be tuples")
        routes = self.store.routes()
        by_ticket = {route.ticket_id: route for route in routes}
        for ticket_id in settled_ticket_ids:
            canonical_ticket = _text(ticket_id, "settled_ticket_id")
            if canonical_ticket not in by_ticket:
                raise PaperCampaignRouteError(
                    "settled ticket lacks durable campaign learning route"
                )

        transitions: list[str] = []
        seen: set[str] = set()
        for route in routes:
            if route.status == PaperCampaignRouteStore._FINALIZED:
                continue
            handoff = self._resolve(route)
            routed = handoff.reconcile_after_settlement(
                paper_book_path=paper_book_path,
                resolutions=resolutions,
                settled_ticket_ids=settled_ticket_ids,
                at=at,
            )
            for transition_id in routed:
                canonical_transition = _text(transition_id, "transition_id")
                if canonical_transition in seen:
                    raise PaperCampaignRouteError(
                        "campaign routes produced duplicate transition identity"
                    )
                seen.add(canonical_transition)
                transitions.append(canonical_transition)
            try:
                handoff.runtime.settlement_bridge.resolution_witness(route.ticket_id)
            except PaperSettlementLearningBridgeError as exc:
                if str(exc) != "ticket has no durable learner outbox":
                    raise PaperCampaignRouteError(
                        "cannot verify routed learner outbox"
                    ) from exc
            else:
                self.store.mark_finalized(route, handoff=handoff)
        return tuple(transitions)


__all__ = [
    "PaperCampaignLearningRouter",
    "PaperCampaignRoute",
    "PaperCampaignRouteError",
    "PaperCampaignRouteStore",
]
