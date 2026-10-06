"""Restart-safe handoff from a sealed PAPER campaign checkpoint to the next episode.

This module is deliberately composition-only.  It does not create another scheduler,
policy authority, settlement/reward engine, risk authority, or provider execution
path.  The completed :class:`PaperCampaignRuntime` remains the parent authority and
:class:`ChampionAgentEpisode` remains the child episode authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from .agent_loop import AgentLoopPhase
from .champion_agent_episode import ChampionAgentEpisode, ChampionAgentEpisodeError
from .integrity import atomic_write_json
from .learning_environment import EnvironmentIdentity, LearningEnvironmentError
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .paper_campaign_runtime import PaperCampaignRuntime
from .scientific_registry import ScientificRegistry
from .strategy_model_factory import FactoryArtifactStore
from .workspace_lock import WorkspaceEconomicLock


HANDOFF_SCHEMA: Final = "autosport.paper_campaign_episode_handoff"
HANDOFF_SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")
_PREPARED: Final = "PREPARED"
_COMMITTED: Final = "COMMITTED"
_INTENT_AUTHORITY_DOMAIN: Final = "paper-campaign-episode-handoff-intent-v1"
_CONSUMPTION_AUTHORITY_DOMAIN: Final = "paper-campaign-episode-handoff-consumption-v1"


class PaperCampaignEpisodeHandoffError(RuntimeError):
    """The requested campaign-to-episode handoff conflicts with durable truth."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise PaperCampaignEpisodeHandoffError(
            f"{name} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PaperCampaignEpisodeHandoffError(f"{name} must be valid UTF-8") from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise PaperCampaignEpisodeHandoffError(f"{name} must be canonical SHA-256 hex")
    return text


def _timestamp(value: object, name: str) -> str:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperCampaignEpisodeHandoffError(
            f"{name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperCampaignEpisodeHandoffError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_datetime(value: object, name: str) -> datetime:
    return datetime.fromisoformat(_timestamp(value, name).replace("Z", "+00:00"))


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
        raise PaperCampaignEpisodeHandoffError(
            "handoff evidence is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PaperCampaignEpisodeHandoffError(
                f"handoff JSON duplicate key: {key}"
            )
        result[key] = value
    return result


def _canonical_path(value: str | Path) -> str:
    path = Path(value)
    try:
        return path.resolve(strict=False).as_posix()
    except OSError as exc:
        raise PaperCampaignEpisodeHandoffError(
            "child AgentLoop path cannot be resolved"
        ) from exc


@dataclass(frozen=True, slots=True)
class PaperCampaignEpisodeHandoffReceipt:
    """Durable acknowledgement of one parent-checkpoint -> child-episode handoff."""

    handoff_id: str
    parent_checkpoint_id: str
    parent_transition_id: str
    child_loop_id: str
    child_episode_id: str
    child_policy_id: str
    child_initial_checkpoint_id: str

    def __post_init__(self) -> None:
        for name in (
            "handoff_id",
            "parent_checkpoint_id",
            "parent_transition_id",
            "child_episode_id",
            "child_initial_checkpoint_id",
        ):
            _sha(getattr(self, name), name)
        _text(self.child_loop_id, "child_loop_id")
        _text(self.child_policy_id, "child_policy_id")


@dataclass(frozen=True, slots=True)
class PaperCampaignEpisodeHandoffResult:
    """Fresh child authority plus its durable parent/child linkage witness."""

    episode: ChampionAgentEpisode
    receipt: PaperCampaignEpisodeHandoffReceipt


@dataclass(frozen=True, slots=True)
class PaperCampaignEpisodeHandoffRecord:
    """Immutable restart locator projected from one committed durable handoff."""

    prepare_id: str
    handoff_id: str
    parent_checkpoint_id: str
    parent_transition_id: str
    parent_episode_id: str
    parent_policy_id: str
    parent_agent_loop_state_sha256: str
    environment_id: str
    child_agent_loop_path: str
    child_loop_id: str
    child_episode_key: str
    canonical_strategy_id: str
    champion_as_of: str
    config_sha256: str
    economic_goal_fingerprint: str
    risk_fingerprint: str
    source_sha256: str
    admissible_actions: tuple[str, ...]
    prepared_at: str
    child_policy_id: str
    child_episode_id: str
    child_initial_checkpoint_id: str


class PaperCampaignEpisodeHandoff:
    """Persist the smallest exactly-once witness between two existing authorities."""

    def __init__(
        self,
        campaign: PaperCampaignRuntime,
        *,
        state_path: str | Path | None = None,
    ) -> None:
        if type(campaign) is not PaperCampaignRuntime:
            raise TypeError("campaign must be exact PaperCampaignRuntime")
        self.campaign = campaign
        self.state_path = (
            Path(state_path)
            if state_path is not None
            else campaign.state_path.with_name(
                f"{campaign.state_path.name}.episode-handoff.json"
            )
        )
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(self.state_path.parent):
            if self.state_path.exists():
                self._read_state()
            else:
                self._write_state({})

    @classmethod
    def open_existing(
        cls,
        campaign: PaperCampaignRuntime,
        *,
        state_path: str | Path | None = None,
    ) -> "PaperCampaignEpisodeHandoff":
        """Open the canonical durable handoff for restart without creating state.

        Writer construction intentionally remains able to initialize a virgin handoff.
        Restart readback is different authority: it must bind to the campaign's
        canonical handoff path and fail closed when those durable bytes are absent.
        """

        if type(campaign) is not PaperCampaignRuntime:
            raise TypeError("campaign must be exact PaperCampaignRuntime")
        canonical_path = campaign.state_path.with_name(
            f"{campaign.state_path.name}.episode-handoff.json"
        )
        selected_path = Path(state_path) if state_path is not None else canonical_path
        try:
            selected_resolved = selected_path.resolve(strict=False)
            canonical_resolved = canonical_path.resolve(strict=False)
        except OSError as exc:
            raise PaperCampaignEpisodeHandoffError(
                "restart handoff state path cannot be resolved"
            ) from exc
        if selected_resolved != canonical_resolved:
            raise PaperCampaignEpisodeHandoffError(
                "restart handoff state path must match canonical campaign handoff path"
            )

        instance = cls.__new__(cls)
        instance.campaign = campaign
        instance.state_path = selected_path
        with WorkspaceEconomicLock(selected_path.parent):
            if not selected_path.exists():
                raise PaperCampaignEpisodeHandoffError(
                    "existing handoff state is missing for restart readback"
                )
            instance._read_state()
        return instance

    def _write_state(self, handoffs: dict[str, object]) -> None:
        bare = {
            "schema": HANDOFF_SCHEMA,
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "handoffs": handoffs,
        }
        atomic_write_json(
            self.state_path,
            {**bare, "state_sha256": _digest(bare)},
        )

    def _read_state(self) -> dict[str, object]:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            state = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    PaperCampaignEpisodeHandoffError(
                        f"handoff JSON contains non-finite value {value}"
                    )
                ),
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PaperCampaignEpisodeHandoffError("handoff state is unreadable") from exc
        if type(state) is not dict or set(state) != {
            "schema",
            "schema_version",
            "handoffs",
            "state_sha256",
        }:
            raise PaperCampaignEpisodeHandoffError("handoff state schema mismatch")
        if (
            state["schema"] != HANDOFF_SCHEMA
            or state["schema_version"] != HANDOFF_SCHEMA_VERSION
            or type(state["handoffs"]) is not dict
        ):
            raise PaperCampaignEpisodeHandoffError("unsupported handoff state")
        bare = {
            "schema": state["schema"],
            "schema_version": state["schema_version"],
            "handoffs": state["handoffs"],
        }
        if _sha(state["state_sha256"], "state_sha256") != _digest(bare):
            raise PaperCampaignEpisodeHandoffError("handoff state digest mismatch")
        for checkpoint_id, record in state["handoffs"].items():
            _sha(checkpoint_id, "parent_checkpoint_id")
            self._validate_record(checkpoint_id, record)
        return state

    @staticmethod
    def _committed_projection(
        record: dict[str, object],
    ) -> PaperCampaignEpisodeHandoffRecord:
        """Project one already-validated COMMITTED record without new authority."""

        if record["status"] != _COMMITTED:
            raise PaperCampaignEpisodeHandoffError(
                "only COMMITTED handoffs can be projected for restart"
            )
        actions = record["admissible_actions"]
        assert isinstance(actions, list)
        return PaperCampaignEpisodeHandoffRecord(
            prepare_id=_sha(record["prepare_id"], "prepare_id"),
            handoff_id=_sha(record["handoff_id"], "handoff_id"),
            parent_checkpoint_id=_sha(
                record["parent_checkpoint_id"], "parent_checkpoint_id"
            ),
            parent_transition_id=_sha(
                record["parent_transition_id"], "parent_transition_id"
            ),
            parent_episode_id=_sha(record["parent_episode_id"], "parent_episode_id"),
            parent_policy_id=_text(record["parent_policy_id"], "parent_policy_id"),
            parent_agent_loop_state_sha256=_sha(
                record["parent_agent_loop_state_sha256"],
                "parent_agent_loop_state_sha256",
            ),
            environment_id=_sha(record["environment_id"], "environment_id"),
            child_agent_loop_path=_text(
                record["child_agent_loop_path"], "child_agent_loop_path"
            ),
            child_loop_id=_text(record["child_loop_id"], "child_loop_id"),
            child_episode_key=_text(
                record["child_episode_key"], "child_episode_key"
            ),
            canonical_strategy_id=_text(
                record["canonical_strategy_id"], "canonical_strategy_id"
            ),
            champion_as_of=_timestamp(record["champion_as_of"], "champion_as_of"),
            config_sha256=_sha(record["config_sha256"], "config_sha256"),
            economic_goal_fingerprint=_sha(
                record["economic_goal_fingerprint"], "economic_goal_fingerprint"
            ),
            risk_fingerprint=_sha(record["risk_fingerprint"], "risk_fingerprint"),
            source_sha256=_sha(record["source_sha256"], "source_sha256"),
            admissible_actions=tuple(
                _text(action, "admissible action") for action in actions
            ),
            prepared_at=_timestamp(record["prepared_at"], "prepared_at"),
            child_policy_id=_text(record["child_policy_id"], "child_policy_id"),
            child_episode_id=_sha(record["child_episode_id"], "child_episode_id"),
            child_initial_checkpoint_id=_sha(
                record["child_initial_checkpoint_id"],
                "child_initial_checkpoint_id",
            ),
        )

    def _verify_committed_authorities(
        self,
        record: dict[str, object],
    ) -> None:
        """Require both independent monotonic commits for a projected child."""

        parent_checkpoint_id = _sha(
            record["parent_checkpoint_id"], "parent_checkpoint_id"
        )
        prepare_id = _sha(record["prepare_id"], "prepare_id")
        handoff_id = _sha(record["handoff_id"], "handoff_id")
        try:
            intent_history = self._intent_authority(
                parent_checkpoint_id
            ).read_history()
            if (
                not intent_history
                or intent_history[-1].phase is not AuthorityPhase.COMMIT
                or intent_history[-1].intended_state_sha256 != prepare_id
                or intent_history[-1].semantic_binding_sha256
                != self._intent_binding(parent_checkpoint_id, prepare_id)
            ):
                raise PaperCampaignEpisodeHandoffError(
                    "committed child lacks exact independent intent authority"
                )

            consumption_binding = _digest(
                {
                    "kind": "paper-campaign-parent-consumption-v1",
                    "parent_checkpoint_id": parent_checkpoint_id,
                    "prepare_id": prepare_id,
                    "handoff_id": handoff_id,
                    "child_episode_id": _sha(
                        record["child_episode_id"], "child_episode_id"
                    ),
                    "child_initial_checkpoint_id": _sha(
                        record["child_initial_checkpoint_id"],
                        "child_initial_checkpoint_id",
                    ),
                }
            )
            consumption_history = self._consumption_authority(
                parent_checkpoint_id
            ).read_history()
            if (
                not consumption_history
                or consumption_history[-1].phase is not AuthorityPhase.COMMIT
                or consumption_history[-1].intended_state_sha256 != handoff_id
                or consumption_history[-1].semantic_binding_sha256
                != consumption_binding
            ):
                raise PaperCampaignEpisodeHandoffError(
                    "committed child lacks exact independent consumption authority"
                )
        except MonotonicWorkspaceAuthorityError as exc:
            raise PaperCampaignEpisodeHandoffError(
                "cannot verify independent committed handoff authority"
            ) from exc

    def _verify_prepared_not_independently_committed(
        self,
        record: dict[str, object],
    ) -> None:
        """Reject local PREPARED state when stronger consumption truth is COMMIT."""

        parent_checkpoint_id = _sha(
            record["parent_checkpoint_id"], "parent_checkpoint_id"
        )
        try:
            consumption_history = self._consumption_authority(
                parent_checkpoint_id
            ).read_history()
        except MonotonicWorkspaceAuthorityError as exc:
            raise PaperCampaignEpisodeHandoffError(
                "cannot verify independent prepared handoff authority"
            ) from exc

        if any(
            entry.phase is AuthorityPhase.COMMIT
            for entry in consumption_history
        ):
            raise PaperCampaignEpisodeHandoffError(
                "local PREPARED handoff conflicts with independent committed "
                "consumption authority"
            )

    def _reject_omitted_committed_parent(
        self,
        state: dict[str, object],
        parent_checkpoint_id: str,
    ) -> None:
        """Reject a locally rolled-back snapshot that omits a committed child."""

        parent_checkpoint_id = _sha(
            parent_checkpoint_id,
            "parent_checkpoint_id",
        )
        handoffs = state["handoffs"]
        assert isinstance(handoffs, dict)
        if parent_checkpoint_id in handoffs:
            return
        try:
            history = self._consumption_authority(
                parent_checkpoint_id
            ).read_history()
        except MonotonicWorkspaceAuthorityError as exc:
            raise PaperCampaignEpisodeHandoffError(
                "cannot verify omitted parent-checkpoint consumption authority"
            ) from exc
        if any(entry.phase is AuthorityPhase.COMMIT for entry in history):
            raise PaperCampaignEpisodeHandoffError(
                "local handoff state omits independently committed child"
            )

    def committed_children(
        self,
    ) -> tuple[PaperCampaignEpisodeHandoffRecord, ...]:
        """Return validated immutable restart locators for committed child episodes.

        PREPARED crash-prefix records are intentionally invisible only while the
        independent consumption authority has never crossed COMMIT. This is a
        projection only: it neither creates a child nor advances either monotonic
        handoff authority.
        """

        _parent_snapshot, parent_checkpoint = self._parent_witness()
        parent_checkpoint_id = _sha(
            parent_checkpoint.checkpoint_id,
            "parent_checkpoint_id",
        )
        with WorkspaceEconomicLock(self.state_path.parent):
            state = self._read_state()
            self._reject_omitted_committed_parent(
                state,
                parent_checkpoint_id,
            )
            projected: list[PaperCampaignEpisodeHandoffRecord] = []
            for parent_checkpoint_id in sorted(state["handoffs"]):
                raw = state["handoffs"][parent_checkpoint_id]
                assert isinstance(raw, dict)
                if raw["status"] == _PREPARED:
                    self._verify_prepared_not_independently_committed(raw)
                    continue
                self._verify_committed_authorities(raw)
                projected.append(self._committed_projection(raw))
            return tuple(projected)

    def _consumption_authority(
        self, parent_checkpoint_id: str
    ) -> MonotonicWorkspaceAuthority:
        try:
            return MonotonicWorkspaceAuthority(
                workspace=self.state_path.parent.resolve(strict=False),
                domain=_CONSUMPTION_AUTHORITY_DOMAIN,
                key=_sha(parent_checkpoint_id, "parent_checkpoint_id"),
            )
        except (OSError, MonotonicWorkspaceAuthorityError) as exc:
            raise PaperCampaignEpisodeHandoffError(
                "cannot establish independent parent-checkpoint consumption authority"
            ) from exc

    def _intent_authority(
        self, parent_checkpoint_id: str
    ) -> MonotonicWorkspaceAuthority:
        try:
            return MonotonicWorkspaceAuthority(
                workspace=self.state_path.parent.resolve(strict=False),
                domain=_INTENT_AUTHORITY_DOMAIN,
                key=_sha(parent_checkpoint_id, "parent_checkpoint_id"),
            )
        except (OSError, MonotonicWorkspaceAuthorityError) as exc:
            raise PaperCampaignEpisodeHandoffError(
                "cannot establish independent parent-checkpoint intent authority"
            ) from exc

    @staticmethod
    def _intent_binding(parent_checkpoint_id: str, prepare_id: str) -> str:
        return _digest(
            {
                "kind": "paper-campaign-parent-intent-v1",
                "parent_checkpoint_id": _sha(
                    parent_checkpoint_id, "parent_checkpoint_id"
                ),
                "prepare_id": _sha(prepare_id, "prepare_id"),
            }
        )

    def _reserve_prepared_intent(
        self,
        parent_checkpoint_id: str,
        prepared_record: dict[str, object],
        existing: object | None,
        handoffs: dict[str, object],
    ) -> None:
        """Seal exact PREPARED intent before any durable child can be created."""

        self._validate_record(parent_checkpoint_id, prepared_record)
        prepare_id = _sha(prepared_record["prepare_id"], "prepare_id")
        binding = self._intent_binding(parent_checkpoint_id, prepare_id)
        authority = self._intent_authority(parent_checkpoint_id)
        try:
            history = authority.read_history()
            pending = (
                history[-1]
                if history and history[-1].phase is AuthorityPhase.PREPARE
                else None
            )
            committed = next(
                (
                    record
                    for record in reversed(history)
                    if record.phase is AuthorityPhase.COMMIT
                ),
                None,
            )

            if pending is not None:
                if (
                    pending.intended_state_sha256 != prepare_id
                    or pending.semantic_binding_sha256 != binding
                ):
                    raise PaperCampaignEpisodeHandoffError(
                        "parent checkpoint intent conflicts with durable reservation"
                    )
                tx_id: str | None = pending.tx_id
            elif committed is not None:
                if (
                    committed.intended_state_sha256 != prepare_id
                    or committed.semantic_binding_sha256 != binding
                ):
                    raise PaperCampaignEpisodeHandoffError(
                        "parent checkpoint intent conflicts with durable reservation"
                    )
                tx_id = None
            else:
                if existing is not None:
                    raise PaperCampaignEpisodeHandoffError(
                        "existing handoff has no independent intent reservation"
                    )
                tx_id = (
                    f"paper-campaign-intent:{len(history) + 1}:"
                    f"{prepare_id[:24]}"
                )
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=prepare_id,
                    semantic_binding_sha256=binding,
                )

            if existing is None:
                handoffs[parent_checkpoint_id] = prepared_record
                self._write_state(handoffs)
            else:
                self._validate_record(parent_checkpoint_id, existing)
                assert isinstance(existing, dict)
                if (
                    existing["prepare_id"] != prepare_id
                    or self._prepared_semantic(existing)
                    != self._prepared_semantic(prepared_record)
                ):
                    raise PaperCampaignEpisodeHandoffError(
                        "parent checkpoint is already bound to a different next episode"
                    )

            if tx_id is not None:
                authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=prepare_id,
                    semantic_binding_sha256=binding,
                )
        except MonotonicWorkspaceAuthorityError as exc:
            raise PaperCampaignEpisodeHandoffError(
                "parent checkpoint intent reservation is missing, rolled back, or unproven"
            ) from exc

    def _recover_consumption_authority(
        self,
        parent_checkpoint_id: str,
        record: object | None,
    ) -> MonotonicWorkspaceAuthority:
        observed: str | None = None
        if record is not None:
            self._validate_record(parent_checkpoint_id, record)
            assert isinstance(record, dict)
            if record["status"] == _COMMITTED:
                observed = _sha(record["handoff_id"], "handoff_id")
        authority = self._consumption_authority(parent_checkpoint_id)
        try:
            history = authority.read_history()
            pending = (
                history[-1]
                if history and history[-1].phase is AuthorityPhase.PREPARE
                else None
            )
            if pending is not None and observed == pending.intended_state_sha256:
                authority.recover(
                    observed_state_sha256=observed,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=pending.semantic_binding_sha256,
                )
            else:
                authority.recover(observed_state_sha256=observed)
        except MonotonicWorkspaceAuthorityError as exc:
            raise PaperCampaignEpisodeHandoffError(
                "parent checkpoint consumption state is missing, rolled back, or unproven"
            ) from exc
        return authority

    @staticmethod
    def _prepared_semantic(record: dict[str, object]) -> dict[str, object]:
        return {
            key: value
            for key, value in record.items()
            if key
            not in {
                "status",
                "prepare_id",
                "child_policy_id",
                "child_episode_id",
                "child_initial_checkpoint_id",
                "handoff_id",
            }
        }

    @classmethod
    def _validate_record(cls, checkpoint_id: str, record: object) -> None:
        expected = {
            "status",
            "prepare_id",
            "parent_checkpoint_id",
            "parent_transition_id",
            "parent_episode_id",
            "parent_policy_id",
            "parent_agent_loop_state_sha256",
            "environment_id",
            "child_agent_loop_path",
            "child_loop_id",
            "child_episode_key",
            "canonical_strategy_id",
            "champion_as_of",
            "config_sha256",
            "economic_goal_fingerprint",
            "risk_fingerprint",
            "source_sha256",
            "admissible_actions",
            "prepared_at",
            "child_policy_id",
            "child_episode_id",
            "child_initial_checkpoint_id",
            "handoff_id",
        }
        if type(record) is not dict or set(record) != expected:
            raise PaperCampaignEpisodeHandoffError("handoff record fields mismatch")
        status = record["status"]
        if status not in {_PREPARED, _COMMITTED}:
            raise PaperCampaignEpisodeHandoffError("handoff status is invalid")
        if record["parent_checkpoint_id"] != checkpoint_id:
            raise PaperCampaignEpisodeHandoffError(
                "handoff record parent checkpoint key mismatch"
            )
        for name in (
            "parent_checkpoint_id",
            "parent_transition_id",
            "parent_episode_id",
            "parent_agent_loop_state_sha256",
            "environment_id",
            "config_sha256",
            "economic_goal_fingerprint",
            "risk_fingerprint",
            "source_sha256",
        ):
            _sha(record[name], name)
        _text(record["parent_policy_id"], "parent_policy_id")
        for name in (
            "child_agent_loop_path",
            "child_loop_id",
            "child_episode_key",
            "canonical_strategy_id",
        ):
            _text(record[name], name)
        _timestamp(record["champion_as_of"], "champion_as_of")
        _timestamp(record["prepared_at"], "prepared_at")
        actions = record["admissible_actions"]
        if (
            type(actions) is not list
            or not actions
            or any(type(item) is not str or not item for item in actions)
            or actions != sorted(actions)
            or len(actions) != len(set(actions))
        ):
            raise PaperCampaignEpisodeHandoffError(
                "handoff admissible_actions must be a sorted unique non-empty list"
            )
        semantic = cls._prepared_semantic(record)
        if _sha(record["prepare_id"], "prepare_id") != _digest(semantic):
            raise PaperCampaignEpisodeHandoffError("handoff prepare digest mismatch")
        child_fields = (
            "child_policy_id",
            "child_episode_id",
            "child_initial_checkpoint_id",
            "handoff_id",
        )
        if status == _PREPARED:
            if any(record[name] is not None for name in child_fields):
                raise PaperCampaignEpisodeHandoffError(
                    "PREPARED handoff cannot contain committed child identity"
                )
            return
        _text(record["child_policy_id"], "child_policy_id")
        _sha(record["child_episode_id"], "child_episode_id")
        _sha(record["child_initial_checkpoint_id"], "child_initial_checkpoint_id")
        committed_semantic = {
            "prepare_id": record["prepare_id"],
            "child_policy_id": record["child_policy_id"],
            "child_episode_id": record["child_episode_id"],
            "child_initial_checkpoint_id": record["child_initial_checkpoint_id"],
        }
        if _sha(record["handoff_id"], "handoff_id") != _digest(committed_semantic):
            raise PaperCampaignEpisodeHandoffError("handoff commit digest mismatch")

    def _parent_witness(self) -> tuple[object, object]:
        snapshot = self.campaign.agent_loop.snapshot()
        if snapshot.phase is not AgentLoopPhase.CHECKPOINT:
            raise PaperCampaignEpisodeHandoffError(
                "next episode requires a fully committed parent CHECKPOINT"
            )
        if snapshot.checkpointed_transition_id is None:
            raise PaperCampaignEpisodeHandoffError(
                "next episode requires a completed parent transition"
            )
        try:
            checkpoint = self.campaign.environment.checkpoint()
        except LearningEnvironmentError as exc:
            raise PaperCampaignEpisodeHandoffError(
                "parent environment is not at a resolved checkpoint"
            ) from exc
        if (
            checkpoint.checkpoint_id != snapshot.environment_checkpoint_id
            or checkpoint.last_transition_id != snapshot.checkpointed_transition_id
            or checkpoint.environment_id != snapshot.environment_id
            or checkpoint.episode_id != snapshot.episode_id
            or checkpoint.policy_id != snapshot.policy_id
        ):
            raise PaperCampaignEpisodeHandoffError(
                "parent campaign checkpoint conflicts with durable AgentLoop"
            )
        if checkpoint.step_index <= 0:
            raise PaperCampaignEpisodeHandoffError(
                "parent checkpoint must contain at least one completed transition"
            )
        return snapshot, checkpoint

    def start_next_episode(
        self,
        child_agent_loop_path: str | Path,
        registry: ScientificRegistry,
        artifact_store: FactoryArtifactStore,
        *,
        identity: EnvironmentIdentity,
        as_of: str,
        canonical_strategy_id: str,
        config_sha256: str,
        episode_key: str,
        admissible_actions: frozenset[str],
        loop_id: str,
        economic_goal_fingerprint: str,
        risk_fingerprint: str,
        source_sha256: str,
        at: str,
    ) -> PaperCampaignEpisodeHandoffResult:
        """Create exactly one fresh same-environment child episode from CHECKPOINT.

        The PREPARED witness is published before the child AgentLoop is initialized.
        If the process dies after either write, the exact same call converges.  Any
        changed identity fails closed instead of minting a second child episode.
        """

        if type(registry) is not ScientificRegistry:
            raise TypeError("registry must be exact ScientificRegistry")
        if type(artifact_store) is not FactoryArtifactStore:
            raise TypeError("artifact_store must be exact FactoryArtifactStore")
        if type(identity) is not EnvironmentIdentity:
            raise TypeError("identity must be exact EnvironmentIdentity")
        if type(admissible_actions) is not frozenset or not admissible_actions:
            raise PaperCampaignEpisodeHandoffError(
                "admissible_actions must be a non-empty exact frozenset"
            )
        for action in admissible_actions:
            _text(action, "admissible action")

        parent_snapshot, parent_checkpoint = self._parent_witness()
        canonical_at = _timestamp(at, "prepared_at")
        if _as_datetime(canonical_at, "prepared_at") < _as_datetime(
            parent_snapshot.updated_at, "parent_updated_at"
        ):
            raise PaperCampaignEpisodeHandoffError(
                "next episode cannot begin before the sealed parent CHECKPOINT"
            )
        if identity.environment_id != parent_checkpoint.environment_id:
            raise PaperCampaignEpisodeHandoffError(
                "same-environment handoff cannot change environment identity"
            )
        if episode_key == self.campaign.environment.episode.episode_key:
            raise PaperCampaignEpisodeHandoffError(
                "next episode must use a new episode_key"
            )
        parent_actions = frozenset(self.campaign.environment.episode.admissible_actions)
        if not admissible_actions.issubset(parent_actions):
            raise PaperCampaignEpisodeHandoffError(
                "next episode cannot widen the parent external action authority"
            )

        canonical_config = _sha(config_sha256, "config_sha256")
        canonical_goal = _sha(
            economic_goal_fingerprint, "economic_goal_fingerprint"
        )
        canonical_risk = _sha(risk_fingerprint, "risk_fingerprint")
        canonical_source = _sha(source_sha256, "source_sha256")
        if canonical_config != parent_snapshot.config_sha256:
            raise PaperCampaignEpisodeHandoffError(
                "next episode config fingerprint differs from parent AgentLoop"
            )
        if canonical_goal != parent_snapshot.economic_goal_fingerprint:
            raise PaperCampaignEpisodeHandoffError(
                "next episode EconomicGoal fingerprint differs from parent AgentLoop"
            )
        if canonical_risk != parent_snapshot.risk_fingerprint:
            raise PaperCampaignEpisodeHandoffError(
                "next episode risk fingerprint differs from parent AgentLoop"
            )
        if canonical_source != parent_snapshot.source_sha256:
            raise PaperCampaignEpisodeHandoffError(
                "next episode source fingerprint differs from parent AgentLoop"
            )

        prepared_semantic: dict[str, object] = {
            "parent_checkpoint_id": parent_checkpoint.checkpoint_id,
            "parent_transition_id": parent_snapshot.checkpointed_transition_id,
            "parent_episode_id": parent_snapshot.episode_id,
            "parent_policy_id": parent_snapshot.policy_id,
            "parent_agent_loop_state_sha256": parent_snapshot.state_sha256,
            "environment_id": identity.environment_id,
            "child_agent_loop_path": _canonical_path(child_agent_loop_path),
            "child_loop_id": _text(loop_id, "child_loop_id"),
            "child_episode_key": _text(episode_key, "child_episode_key"),
            "canonical_strategy_id": _text(
                canonical_strategy_id, "canonical_strategy_id"
            ),
            "champion_as_of": _timestamp(as_of, "champion_as_of"),
            "config_sha256": canonical_config,
            "economic_goal_fingerprint": canonical_goal,
            "risk_fingerprint": canonical_risk,
            "source_sha256": canonical_source,
            "admissible_actions": sorted(admissible_actions),
            "prepared_at": canonical_at,
        }
        prepare_id = _digest(prepared_semantic)
        prepared_record: dict[str, object] = {
            "status": _PREPARED,
            "prepare_id": prepare_id,
            **prepared_semantic,
            "child_policy_id": None,
            "child_episode_id": None,
            "child_initial_checkpoint_id": None,
            "handoff_id": None,
        }

        with WorkspaceEconomicLock(self.state_path.parent):
            state = self._read_state()
            existing = state["handoffs"].get(parent_checkpoint.checkpoint_id)
            self._recover_consumption_authority(
                parent_checkpoint.checkpoint_id, existing
            )
            self._reserve_prepared_intent(
                parent_checkpoint.checkpoint_id,
                prepared_record,
                existing,
                state["handoffs"],
            )

        try:
            child = ChampionAgentEpisode.initialize_pristine(
                child_agent_loop_path,
                registry,
                artifact_store,
                identity=identity,
                as_of=prepared_semantic["champion_as_of"],
                canonical_strategy_id=canonical_strategy_id,
                config_sha256=canonical_config,
                episode_key=episode_key,
                admissible_actions=admissible_actions,
                loop_id=loop_id,
                economic_goal_fingerprint=canonical_goal,
                risk_fingerprint=canonical_risk,
                source_sha256=canonical_source,
                at=prepared_semantic["prepared_at"],
            )
        except ChampionAgentEpisodeError as exc:
            raise PaperCampaignEpisodeHandoffError(
                "canonical champion child episode rejected the handoff"
            ) from exc

        child_snapshot = child.agent_loop.snapshot()
        child_checkpoint = child.environment.checkpoint()
        if (
            child_snapshot.phase is not AgentLoopPhase.BOOTSTRAP
            or child_checkpoint.step_index != 0
            or child_checkpoint.last_transition_id is not None
            or child_snapshot.environment_checkpoint_id != child_checkpoint.checkpoint_id
            or child_snapshot.environment_id != parent_snapshot.environment_id
            or child_snapshot.episode_id == parent_snapshot.episode_id
            or child_snapshot.episode_id != child.environment.episode.episode_id
            or child_snapshot.policy_id != child.policy.policy_id
            or child_snapshot.config_sha256 != canonical_config
            or child_snapshot.economic_goal_fingerprint != canonical_goal
            or child_snapshot.risk_fingerprint != canonical_risk
            or child_snapshot.source_sha256 != canonical_source
        ):
            raise PaperCampaignEpisodeHandoffError(
                "canonical child episode does not satisfy the frozen handoff witness"
            )

        current_parent, current_checkpoint = self._parent_witness()
        if (
            current_parent.state_sha256 != parent_snapshot.state_sha256
            or current_checkpoint.checkpoint_id != parent_checkpoint.checkpoint_id
        ):
            raise PaperCampaignEpisodeHandoffError(
                "parent campaign changed while the next episode was being prepared"
            )

        committed_semantic = {
            "prepare_id": prepare_id,
            "child_policy_id": child.policy.policy_id,
            "child_episode_id": child.environment.episode.episode_id,
            "child_initial_checkpoint_id": child_checkpoint.checkpoint_id,
        }
        handoff_id = _digest(committed_semantic)
        committed_record = {
            **prepared_record,
            "status": _COMMITTED,
            "child_policy_id": child.policy.policy_id,
            "child_episode_id": child.environment.episode.episode_id,
            "child_initial_checkpoint_id": child_checkpoint.checkpoint_id,
            "handoff_id": handoff_id,
        }
        with WorkspaceEconomicLock(self.state_path.parent):
            state = self._read_state()
            current = state["handoffs"].get(parent_checkpoint.checkpoint_id)
            if current is None:
                raise PaperCampaignEpisodeHandoffError(
                    "prepared handoff witness disappeared before commit"
                )
            authority = self._recover_consumption_authority(
                parent_checkpoint.checkpoint_id, current
            )
            self._validate_record(parent_checkpoint.checkpoint_id, current)
            if current["prepare_id"] != prepare_id:
                raise PaperCampaignEpisodeHandoffError(
                    "prepared handoff changed before child commit"
                )
            if current["status"] == _COMMITTED:
                if current != committed_record:
                    raise PaperCampaignEpisodeHandoffError(
                        "committed child identity conflicts with exact retry"
                    )
            else:
                binding = _digest(
                    {
                        "kind": "paper-campaign-parent-consumption-v1",
                        "parent_checkpoint_id": parent_checkpoint.checkpoint_id,
                        "prepare_id": prepare_id,
                        "handoff_id": handoff_id,
                        "child_episode_id": child.environment.episode.episode_id,
                        "child_initial_checkpoint_id": child_checkpoint.checkpoint_id,
                    }
                )
                try:
                    history = authority.read_history()
                    tx_id = (
                        f"paper-campaign-handoff:{len(history) + 1}:"
                        f"{prepare_id[:24]}"
                    )
                    authority.prepare(
                        tx_id=tx_id,
                        observed_state_sha256=None,
                        intended_state_sha256=handoff_id,
                        semantic_binding_sha256=binding,
                    )
                    state["handoffs"][parent_checkpoint.checkpoint_id] = committed_record
                    self._write_state(state["handoffs"])
                    authority.commit(
                        tx_id=tx_id,
                        observed_state_sha256=handoff_id,
                        semantic_binding_sha256=binding,
                    )
                except MonotonicWorkspaceAuthorityError as exc:
                    raise PaperCampaignEpisodeHandoffError(
                        "cannot durably consume parent checkpoint for child episode"
                    ) from exc

        receipt = PaperCampaignEpisodeHandoffReceipt(
            handoff_id=handoff_id,
            parent_checkpoint_id=parent_checkpoint.checkpoint_id,
            parent_transition_id=parent_snapshot.checkpointed_transition_id,
            child_loop_id=child_snapshot.loop_id,
            child_episode_id=child.environment.episode.episode_id,
            child_policy_id=child.policy.policy_id,
            child_initial_checkpoint_id=child_checkpoint.checkpoint_id,
        )
        return PaperCampaignEpisodeHandoffResult(child, receipt)


__all__ = [
    "HANDOFF_SCHEMA",
    "HANDOFF_SCHEMA_VERSION",
    "PaperCampaignEpisodeHandoff",
    "PaperCampaignEpisodeHandoffError",
    "PaperCampaignEpisodeHandoffReceipt",
    "PaperCampaignEpisodeHandoffRecord",
    "PaperCampaignEpisodeHandoffResult",
]
