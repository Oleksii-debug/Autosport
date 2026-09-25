"""Restart-safe persistence for paired untouched-forward policy comparisons.

This module is deliberately a consumer-side durability seam. It freezes exact
references to existing scientific authorities and preserves paired comparison
membership/results across restart. It does not issue PolicyEvaluation evidence,
consume/reset holdouts, account trial families, promote policies, or grant any
execution/money authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .workspace_lock import WorkspaceEconomicLock


SCHEMA = "autosport.forward_policy_comparison"
SCHEMA_VERSION = 1
_AUTHORITY_DOMAIN = "learning.forward-policy-comparison.v1"
_HEX = frozenset("0123456789abcdef")


class ForwardPolicyComparisonError(ValueError):
    """A paired forward comparison is malformed or semantically conflicting."""


class ForwardPolicyComparisonIntegrityError(ForwardPolicyComparisonError):
    """Durable paired-forward state is stale, rolled back, or corrupted."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ForwardPolicyComparisonError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ForwardPolicyComparisonError(f"{name} contains invalid Unicode") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ForwardPolicyComparisonError(f"{name} must be lowercase SHA-256")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardPolicyComparisonError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardPolicyComparisonError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_bytes(payload: object) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ForwardPolicyComparisonError(
            "forward comparison escaped canonical JSON domain"
        ) from exc


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


class ComparisonArm(StrEnum):
    CHAMPION = "CHAMPION"
    CHALLENGER = "CHALLENGER"


class MemberState(StrEnum):
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    INCONCLUSIVE = "INCONCLUSIVE"


class ComparisonState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    INCONCLUSIVE = "INCONCLUSIVE"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class ForwardComparisonIdentity:
    """Frozen references defining one paired prospective comparison.

    Every digest here is an opaque reference to an already-existing authority. This
    object binds those references; it does not create or validate their upstream
    scientific authority.
    """

    trial_family_id: str
    trial_family_snapshot_sha256: str
    research_protocol_id: str
    protocol_sha256: str
    champion_policy_id: str
    challenger_policy_id: str
    campaign_id: str
    campaign_sha256: str
    holdout_access_id: str
    holdout_access_sha256: str
    evaluator_sha256: str
    metric_definition_sha256: str
    guardrail_definition_sha256: str
    cost_definition_sha256: str
    risk_policy_sha256: str
    causal_boundary_sha256: str
    created_at: str
    first_eligible_at: str

    def __post_init__(self) -> None:
        for name in (
            "trial_family_id",
            "research_protocol_id",
            "campaign_id",
            "holdout_access_id",
        ):
            _text(getattr(self, name), name)
        for name in (
            "trial_family_snapshot_sha256",
            "protocol_sha256",
            "champion_policy_id",
            "challenger_policy_id",
            "campaign_sha256",
            "holdout_access_sha256",
            "evaluator_sha256",
            "metric_definition_sha256",
            "guardrail_definition_sha256",
            "cost_definition_sha256",
            "risk_policy_sha256",
            "causal_boundary_sha256",
        ):
            _sha256(getattr(self, name), name)
        if self.champion_policy_id == self.challenger_policy_id:
            raise ForwardPolicyComparisonError(
                "champion and challenger must have distinct exact identities"
            )
        if _instant(self.created_at, "created_at") >= _instant(
            self.first_eligible_at, "first_eligible_at"
        ):
            raise ForwardPolicyComparisonError(
                "comparison identity must exist before the first eligible boundary"
            )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "trial_family_id": self.trial_family_id,
            "trial_family_snapshot_sha256": self.trial_family_snapshot_sha256,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "champion_policy_id": self.champion_policy_id,
            "challenger_policy_id": self.challenger_policy_id,
            "campaign_id": self.campaign_id,
            "campaign_sha256": self.campaign_sha256,
            "holdout_access_id": self.holdout_access_id,
            "holdout_access_sha256": self.holdout_access_sha256,
            "evaluator_sha256": self.evaluator_sha256,
            "metric_definition_sha256": self.metric_definition_sha256,
            "guardrail_definition_sha256": self.guardrail_definition_sha256,
            "cost_definition_sha256": self.cost_definition_sha256,
            "risk_policy_sha256": self.risk_policy_sha256,
            "causal_boundary_sha256": self.causal_boundary_sha256,
            "created_at": self.created_at,
            "first_eligible_at": self.first_eligible_at,
        }

    @property
    def comparison_id(self) -> str:
        return _digest(
            {
                "kind": "autosport-forward-policy-comparison-identity-v1",
                **self.canonical_payload(),
            }
        )

    def to_payload(self) -> dict[str, object]:
        return {"comparison_id": self.comparison_id, **self.canonical_payload()}

    @classmethod
    def from_payload(cls, payload: object) -> "ForwardComparisonIdentity":
        fields = {
            "comparison_id",
            "trial_family_id",
            "trial_family_snapshot_sha256",
            "research_protocol_id",
            "protocol_sha256",
            "champion_policy_id",
            "challenger_policy_id",
            "campaign_id",
            "campaign_sha256",
            "holdout_access_id",
            "holdout_access_sha256",
            "evaluator_sha256",
            "metric_definition_sha256",
            "guardrail_definition_sha256",
            "cost_definition_sha256",
            "risk_policy_sha256",
            "causal_boundary_sha256",
            "created_at",
            "first_eligible_at",
        }
        if type(payload) is not dict or set(payload) != fields:
            raise ForwardPolicyComparisonError("comparison identity fields mismatch")
        result = cls(
            trial_family_id=payload["trial_family_id"],
            trial_family_snapshot_sha256=payload["trial_family_snapshot_sha256"],
            research_protocol_id=payload["research_protocol_id"],
            protocol_sha256=payload["protocol_sha256"],
            champion_policy_id=payload["champion_policy_id"],
            challenger_policy_id=payload["challenger_policy_id"],
            campaign_id=payload["campaign_id"],
            campaign_sha256=payload["campaign_sha256"],
            holdout_access_id=payload["holdout_access_id"],
            holdout_access_sha256=payload["holdout_access_sha256"],
            evaluator_sha256=payload["evaluator_sha256"],
            metric_definition_sha256=payload["metric_definition_sha256"],
            guardrail_definition_sha256=payload["guardrail_definition_sha256"],
            cost_definition_sha256=payload["cost_definition_sha256"],
            risk_policy_sha256=payload["risk_policy_sha256"],
            causal_boundary_sha256=payload["causal_boundary_sha256"],
            created_at=payload["created_at"],
            first_eligible_at=payload["first_eligible_at"],
        )
        if payload["comparison_id"] != result.comparison_id:
            raise ForwardPolicyComparisonError("comparison identity digest mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ForwardComparisonMember:
    """One frozen denominator member and its paired-arm progress."""

    member_id: str
    member_manifest_sha256: str
    observed_at: str
    denominator_class: str
    source_evidence_sha256: str
    champion_evaluation_sha256: str | None = None
    challenger_evaluation_sha256: str | None = None
    reward_available_at: str | None = None
    state: MemberState = MemberState.PENDING
    resolved_at: str | None = None

    def __post_init__(self) -> None:
        _text(self.member_id, "member_id")
        _sha256(self.member_manifest_sha256, "member_manifest_sha256")
        observed = _instant(self.observed_at, "observed_at")
        _text(self.denominator_class, "denominator_class")
        _sha256(self.source_evidence_sha256, "source_evidence_sha256")
        for name in ("champion_evaluation_sha256", "challenger_evaluation_sha256"):
            value = getattr(self, name)
            if value is not None:
                _sha256(value, name)
        if self.reward_available_at is not None:
            if _instant(self.reward_available_at, "reward_available_at") < observed:
                raise ForwardPolicyComparisonError(
                    "reward_available_at must not precede observed_at"
                )
        if not isinstance(self.state, MemberState):
            raise ForwardPolicyComparisonError("member state is invalid")
        if self.state is MemberState.PENDING:
            if self.resolved_at is not None:
                raise ForwardPolicyComparisonError(
                    "pending member cannot have resolved_at"
                )
        else:
            if self.resolved_at is None:
                raise ForwardPolicyComparisonError(
                    "terminal member requires resolved_at"
                )
            resolved = _instant(self.resolved_at, "resolved_at")
            if resolved < observed:
                raise ForwardPolicyComparisonError(
                    "resolved_at must not precede observed_at"
                )
            if self.reward_available_at is not None and resolved < _instant(
                self.reward_available_at, "reward_available_at"
            ):
                raise ForwardPolicyComparisonError(
                    "resolved_at must not precede reward availability"
                )
            if self.state is MemberState.COMPLETE:
                if (
                    self.champion_evaluation_sha256 is None
                    or self.challenger_evaluation_sha256 is None
                ):
                    raise ForwardPolicyComparisonError(
                        "complete paired member requires both arm evaluations"
                    )
                if self.reward_available_at is None:
                    raise ForwardPolicyComparisonError(
                        "complete paired member requires causal reward availability"
                    )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "member_id": self.member_id,
            "member_manifest_sha256": self.member_manifest_sha256,
            "observed_at": self.observed_at,
            "denominator_class": self.denominator_class,
            "source_evidence_sha256": self.source_evidence_sha256,
            "champion_evaluation_sha256": self.champion_evaluation_sha256,
            "challenger_evaluation_sha256": self.challenger_evaluation_sha256,
            "reward_available_at": self.reward_available_at,
            "state": self.state.value,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ForwardComparisonMember":
        fields = {
            "member_id",
            "member_manifest_sha256",
            "observed_at",
            "denominator_class",
            "source_evidence_sha256",
            "champion_evaluation_sha256",
            "challenger_evaluation_sha256",
            "reward_available_at",
            "state",
            "resolved_at",
        }
        if type(payload) is not dict or set(payload) != fields:
            raise ForwardPolicyComparisonError("comparison member fields mismatch")
        try:
            state = MemberState(payload["state"])
        except (TypeError, ValueError) as exc:
            raise ForwardPolicyComparisonError("comparison member state is invalid") from exc
        return cls(
            member_id=payload["member_id"],
            member_manifest_sha256=payload["member_manifest_sha256"],
            observed_at=payload["observed_at"],
            denominator_class=payload["denominator_class"],
            source_evidence_sha256=payload["source_evidence_sha256"],
            champion_evaluation_sha256=payload["champion_evaluation_sha256"],
            challenger_evaluation_sha256=payload["challenger_evaluation_sha256"],
            reward_available_at=payload["reward_available_at"],
            state=state,
            resolved_at=payload["resolved_at"],
        )


@dataclass(frozen=True, slots=True)
class ForwardPolicyComparison:
    """Immutable snapshot of one durable paired forward comparison."""

    identity: ForwardComparisonIdentity
    members: tuple[ForwardComparisonMember, ...] = ()
    state: ComparisonState = ComparisonState.RUNNING
    terminal_at: str | None = None

    def __post_init__(self) -> None:
        if type(self.identity) is not ForwardComparisonIdentity:
            raise ForwardPolicyComparisonError(
                "identity must be exact ForwardComparisonIdentity"
            )
        if type(self.members) is not tuple or any(
            type(member) is not ForwardComparisonMember for member in self.members
        ):
            raise ForwardPolicyComparisonError(
                "members must be exact ForwardComparisonMember tuple"
            )
        member_ids = tuple(member.member_id for member in self.members)
        if member_ids != tuple(sorted(member_ids)) or len(member_ids) != len(
            set(member_ids)
        ):
            raise ForwardPolicyComparisonError(
                "members must be sorted and unique by member_id"
            )
        boundary = _instant(self.identity.first_eligible_at, "first_eligible_at")
        for member in self.members:
            if _instant(member.observed_at, "member observed_at") < boundary:
                raise ForwardPolicyComparisonError(
                    "member predates frozen first eligible boundary"
                )
        if not isinstance(self.state, ComparisonState):
            raise ForwardPolicyComparisonError("comparison state is invalid")
        if self.state is ComparisonState.RUNNING:
            if self.terminal_at is not None:
                raise ForwardPolicyComparisonError(
                    "running comparison cannot have terminal_at"
                )
        else:
            if self.terminal_at is None:
                raise ForwardPolicyComparisonError(
                    "terminal comparison requires terminal_at"
                )
            terminal = _instant(self.terminal_at, "terminal_at")
            if not self.members:
                raise ForwardPolicyComparisonError(
                    "terminal comparison requires denominator members"
                )
            if any(member.state is MemberState.PENDING for member in self.members):
                raise ForwardPolicyComparisonError(
                    "terminal comparison cannot contain pending members"
                )
            if self.state is ComparisonState.COMPLETE and any(
                member.state is not MemberState.COMPLETE for member in self.members
            ):
                raise ForwardPolicyComparisonError(
                    "complete comparison requires every paired member complete"
                )
            for member in self.members:
                assert member.resolved_at is not None
                if terminal < _instant(member.resolved_at, "member resolved_at"):
                    raise ForwardPolicyComparisonError(
                        "terminal_at must not precede member resolution"
                    )

    @property
    def comparison_id(self) -> str:
        return self.identity.comparison_id

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "identity": self.identity.to_payload(),
            "members": [member.canonical_payload() for member in self.members],
            "state": self.state.value,
            "terminal_at": self.terminal_at,
        }

    @property
    def ledger_sha256(self) -> str:
        return _digest(self.to_payload())

    @property
    def terminal_evidence_sha256(self) -> str | None:
        return None if self.state is ComparisonState.RUNNING else self.ledger_sha256

    @classmethod
    def from_payload(cls, payload: object) -> "ForwardPolicyComparison":
        fields = {
            "schema",
            "schema_version",
            "identity",
            "members",
            "state",
            "terminal_at",
        }
        if type(payload) is not dict or set(payload) != fields:
            raise ForwardPolicyComparisonError("forward comparison fields mismatch")
        if payload["schema"] != SCHEMA or payload["schema_version"] != SCHEMA_VERSION:
            raise ForwardPolicyComparisonError(
                "forward comparison schema is unsupported"
            )
        if type(payload["members"]) is not list:
            raise ForwardPolicyComparisonError(
                "forward comparison members must be a list"
            )
        try:
            state = ComparisonState(payload["state"])
        except (TypeError, ValueError) as exc:
            raise ForwardPolicyComparisonError(
                "forward comparison state is invalid"
            ) from exc
        return cls(
            identity=ForwardComparisonIdentity.from_payload(payload["identity"]),
            members=tuple(
                ForwardComparisonMember.from_payload(item)
                for item in payload["members"]
            ),
            state=state,
            terminal_at=payload["terminal_at"],
        )

    def commit_member(
        self, member: ForwardComparisonMember
    ) -> "ForwardPolicyComparison":
        if self.state is not ComparisonState.RUNNING:
            raise ForwardPolicyComparisonError(
                "terminal comparison cannot accept members"
            )
        if type(member) is not ForwardComparisonMember:
            raise ForwardPolicyComparisonError(
                "member must be exact ForwardComparisonMember"
            )
        if member.state is not MemberState.PENDING:
            raise ForwardPolicyComparisonError(
                "new comparison member must enter as PENDING"
            )
        if _instant(member.observed_at, "member observed_at") < _instant(
            self.identity.first_eligible_at, "first_eligible_at"
        ):
            raise ForwardPolicyComparisonError(
                "member predates frozen first eligible boundary"
            )
        existing = next(
            (item for item in self.members if item.member_id == member.member_id),
            None,
        )
        if existing is not None:
            if existing == member:
                return self
            raise ForwardPolicyComparisonError(
                "conflicting duplicate forward comparison member"
            )
        return replace(
            self,
            members=tuple(
                sorted((*self.members, member), key=lambda item: item.member_id)
            ),
        )

    def record_arm_evaluation(
        self,
        member_id: str,
        *,
        arm: ComparisonArm,
        evaluation_sha256: str,
        reward_available_at: str | None = None,
    ) -> "ForwardPolicyComparison":
        if self.state is not ComparisonState.RUNNING:
            raise ForwardPolicyComparisonError(
                "terminal comparison cannot accept evaluation evidence"
            )
        if not isinstance(arm, ComparisonArm):
            raise ForwardPolicyComparisonError("arm must be ComparisonArm")
        member_key = _text(member_id, "member_id")
        evaluation = _sha256(evaluation_sha256, "evaluation_sha256")
        reward_at = None
        if reward_available_at is not None:
            reward_at = _text(reward_available_at, "reward_available_at")
            _instant(reward_at, "reward_available_at")
        updated: list[ForwardComparisonMember] = []
        found = False
        for member in self.members:
            if member.member_id != member_key:
                updated.append(member)
                continue
            found = True
            if member.state is not MemberState.PENDING:
                raise ForwardPolicyComparisonError(
                    "terminal member evaluation cannot be rewritten"
                )
            target_name = (
                "champion_evaluation_sha256"
                if arm is ComparisonArm.CHAMPION
                else "challenger_evaluation_sha256"
            )
            current = getattr(member, target_name)
            if current is not None and current != evaluation:
                raise ForwardPolicyComparisonError(
                    "paired arm evaluation identity cannot be rewritten"
                )
            if (
                member.reward_available_at is not None
                and reward_at is not None
                and member.reward_available_at != reward_at
            ):
                raise ForwardPolicyComparisonError(
                    "reward availability identity cannot be rewritten"
                )
            next_reward_at = member.reward_available_at or reward_at
            updated.append(
                replace(
                    member,
                    **{target_name: evaluation},
                    reward_available_at=next_reward_at,
                )
            )
        if not found:
            raise ForwardPolicyComparisonError("unknown comparison member")
        return replace(self, members=tuple(updated))

    def resolve_member(
        self,
        member_id: str,
        *,
        state: MemberState,
        resolved_at: str,
    ) -> "ForwardPolicyComparison":
        if self.state is not ComparisonState.RUNNING:
            raise ForwardPolicyComparisonError(
                "terminal comparison cannot resolve members"
            )
        if state not in (MemberState.COMPLETE, MemberState.INCONCLUSIVE):
            raise ForwardPolicyComparisonError(
                "member resolution must be COMPLETE or INCONCLUSIVE"
            )
        member_key = _text(member_id, "member_id")
        when = _text(resolved_at, "resolved_at")
        _instant(when, "resolved_at")
        updated: list[ForwardComparisonMember] = []
        found = False
        for member in self.members:
            if member.member_id != member_key:
                updated.append(member)
                continue
            found = True
            if member.state is not MemberState.PENDING:
                if member.state is state and member.resolved_at == when:
                    updated.append(member)
                    continue
                raise ForwardPolicyComparisonError(
                    "terminal member resolution cannot be rewritten"
                )
            updated.append(replace(member, state=state, resolved_at=when))
        if not found:
            raise ForwardPolicyComparisonError("unknown comparison member")
        return replace(self, members=tuple(updated))

    def terminalize(
        self,
        *,
        state: ComparisonState,
        terminal_at: str,
    ) -> "ForwardPolicyComparison":
        if state is ComparisonState.RUNNING:
            raise ForwardPolicyComparisonError(
                "terminal state cannot be RUNNING"
            )
        when = _text(terminal_at, "terminal_at")
        _instant(when, "terminal_at")
        if self.state is not ComparisonState.RUNNING:
            if self.state is state and self.terminal_at == when:
                return self
            raise ForwardPolicyComparisonError(
                "terminal comparison disposition cannot be rewritten"
            )
        return replace(self, state=state, terminal_at=when)


class ForwardPolicyComparisonStore:
    """Durable restart/rollback-safe store for exactly one comparison identity."""

    DIRECTORY = ".forward-policy-comparisons"

    def __init__(
        self,
        workspace: str | Path,
        *,
        comparison_id: str,
        authority_root: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=False)
        self.comparison_id = _sha256(comparison_id, "comparison_id")
        self.path = (
            self.workspace / self.DIRECTORY / f"{self.comparison_id}.json"
        )
        self.monotonic_authority = MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            domain=_AUTHORITY_DOMAIN,
            key=self.comparison_id,
            authority_root=authority_root,
        )

    @classmethod
    def initialize(
        cls,
        workspace: str | Path,
        identity: ForwardComparisonIdentity,
        *,
        authority_root: str | Path | None = None,
    ) -> tuple["ForwardPolicyComparisonStore", ForwardPolicyComparison]:
        if type(identity) is not ForwardComparisonIdentity:
            raise ForwardPolicyComparisonError(
                "identity must be exact ForwardComparisonIdentity"
            )
        store = cls(
            workspace,
            comparison_id=identity.comparison_id,
            authority_root=authority_root,
        )
        existing = store.load()
        if existing is not None:
            if existing.identity != identity:
                raise ForwardPolicyComparisonIntegrityError(
                    "existing comparison identity conflicts with requested identity"
                )
            return store, existing
        ledger = ForwardPolicyComparison(identity)
        store.save(ledger)
        return store, ledger

    def _read_unlocked(self) -> ForwardPolicyComparison | None:
        if not self.path.exists():
            return None
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
            ledger = ForwardPolicyComparison.from_payload(raw)
        except (OSError, TypeError, ValueError) as exc:
            raise ForwardPolicyComparisonIntegrityError(
                "cannot read durable forward comparison"
            ) from exc
        if ledger.comparison_id != self.comparison_id:
            raise ForwardPolicyComparisonIntegrityError(
                "durable comparison identity mismatches store key"
            )
        return ledger

    def _semantic_binding(self, ledger: ForwardPolicyComparison) -> str:
        return _digest(
            {
                "kind": "autosport-forward-policy-comparison-store-binding-v1",
                "comparison_id": ledger.comparison_id,
                "identity": ledger.identity.to_payload(),
            }
        )

    def _recover_unlocked(
        self, ledger: ForwardPolicyComparison | None
    ) -> None:
        observed = None if ledger is None else ledger.ledger_sha256
        try:
            history = self.monotonic_authority.read_history()
            pending = (
                history[-1]
                if history and history[-1].phase is AuthorityPhase.PREPARE
                else None
            )
            if ledger is None:
                self.monotonic_authority.recover(
                    observed_state_sha256=None
                )
                return
            binding = self._semantic_binding(ledger)
            if (
                pending is not None
                and pending.intended_state_sha256 == observed
            ):
                if pending.semantic_binding_sha256 != binding:
                    raise ForwardPolicyComparisonIntegrityError(
                        "prepared comparison semantic binding mismatches published state"
                    )
                self.monotonic_authority.recover(
                    observed_state_sha256=observed,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=binding,
                )
            else:
                self.monotonic_authority.recover(
                    observed_state_sha256=observed
                )
        except MonotonicWorkspaceAuthorityError as exc:
            raise ForwardPolicyComparisonIntegrityError(
                "forward comparison is stale, deleted, rolled back, or unproven"
            ) from exc

    def _next_tx_id(self, intended_state_sha256: str) -> str:
        history = self.monotonic_authority.read_history()
        return (
            f"forward-policy-comparison:{len(history) + 1}:"
            f"{intended_state_sha256[:32]}"
        )

    @staticmethod
    def _assert_member_successor(
        previous: ForwardComparisonMember,
        current: ForwardComparisonMember,
    ) -> None:
        for name in (
            "member_id",
            "member_manifest_sha256",
            "observed_at",
            "denominator_class",
            "source_evidence_sha256",
        ):
            if getattr(previous, name) != getattr(current, name):
                raise ForwardPolicyComparisonIntegrityError(
                    f"durable member {name} cannot change"
                )
        for name in (
            "champion_evaluation_sha256",
            "challenger_evaluation_sha256",
            "reward_available_at",
        ):
            old = getattr(previous, name)
            new = getattr(current, name)
            if old is not None and new != old:
                raise ForwardPolicyComparisonIntegrityError(
                    f"durable member {name} cannot be removed or rewritten"
                )
        if previous.state is not MemberState.PENDING:
            if (
                current.state is not previous.state
                or current.resolved_at != previous.resolved_at
            ):
                raise ForwardPolicyComparisonIntegrityError(
                    "terminal member state cannot be rewritten"
                )
        elif (
            current.state is MemberState.PENDING
            and current.resolved_at is not None
        ):
            raise ForwardPolicyComparisonIntegrityError(
                "pending member cannot gain resolved_at"
            )

    @classmethod
    def _assert_successor(
        cls,
        previous: ForwardPolicyComparison,
        current: ForwardPolicyComparison,
    ) -> None:
        if previous.identity != current.identity:
            raise ForwardPolicyComparisonIntegrityError(
                "frozen comparison identity cannot change"
            )
        previous_map = {
            member.member_id: member for member in previous.members
        }
        current_map = {
            member.member_id: member for member in current.members
        }
        if not previous_map.keys() <= current_map.keys():
            raise ForwardPolicyComparisonIntegrityError(
                "durable comparison denominator cannot shrink"
            )
        for member_id, prior_member in previous_map.items():
            cls._assert_member_successor(
                prior_member, current_map[member_id]
            )
        if previous.state is not ComparisonState.RUNNING:
            if (
                current.state is not previous.state
                or current.terminal_at != previous.terminal_at
            ):
                raise ForwardPolicyComparisonIntegrityError(
                    "terminal comparison cannot be reopened or rewritten"
                )
            if current.ledger_sha256 != previous.ledger_sha256:
                raise ForwardPolicyComparisonIntegrityError(
                    "terminal comparison evidence is immutable"
                )

    def load(self) -> ForwardPolicyComparison | None:
        with WorkspaceEconomicLock(self.workspace):
            ledger = self._read_unlocked()
            self._recover_unlocked(ledger)
            return ledger

    def save(self, ledger: ForwardPolicyComparison) -> None:
        if type(ledger) is not ForwardPolicyComparison:
            raise ForwardPolicyComparisonError(
                "ledger must be exact ForwardPolicyComparison"
            )
        if ledger.comparison_id != self.comparison_id:
            raise ForwardPolicyComparisonIntegrityError(
                "comparison cannot be written to another comparison store"
            )
        with WorkspaceEconomicLock(self.workspace):
            existing = self._read_unlocked()
            self._recover_unlocked(existing)
            if existing is not None:
                self._assert_successor(existing, ledger)
                if existing.ledger_sha256 == ledger.ledger_sha256:
                    return
            observed = (
                None if existing is None else existing.ledger_sha256
            )
            intended = ledger.ledger_sha256
            binding = self._semantic_binding(ledger)
            tx_id = self._next_tx_id(intended)
            try:
                self.monotonic_authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
                atomic_write_json(self.path, ledger.to_payload())
                published = self._read_unlocked()
                if (
                    published is None
                    or published.ledger_sha256 != intended
                ):
                    raise ForwardPolicyComparisonIntegrityError(
                        "published forward comparison does not match intended digest"
                    )
                self.monotonic_authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise ForwardPolicyComparisonIntegrityError(
                    "monotonic forward comparison publication failed closed"
                ) from exc


__all__ = [
    "ComparisonArm",
    "ComparisonState",
    "ForwardComparisonIdentity",
    "ForwardComparisonMember",
    "ForwardPolicyComparison",
    "ForwardPolicyComparisonError",
    "ForwardPolicyComparisonIntegrityError",
    "ForwardPolicyComparisonStore",
    "MemberState",
]
