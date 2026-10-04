"""Contract-only causal credit-assignment evidence for resolved rewards.

This module deliberately does not compute reward, utility, settlement, policy updates,
or promotion authority. It records which causal reward components have explicit
evidence and preserves UNKNOWN when a component cannot be resolved safely.

Schema v1 is assertion-only: authority references are durable correlation handles, not
proof that their source was re-resolved by the product. Therefore every record remains
source_resolved=False and policy_update_eligible=False.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Any, Final, Mapping


SCHEMA: Final = "autosport.reward_component_attribution"
SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class RewardAttributionError(ValueError):
    """Raised when reward-attribution evidence is malformed or overclaims truth."""


class RewardAttributionComponent(StrEnum):
    FORECAST = "FORECAST"
    SELECTION = "SELECTION"
    SIZING = "SIZING"
    EXECUTION = "EXECUTION"
    DATA_QUALITY = "DATA_QUALITY"


class AttributionTruth(StrEnum):
    UNKNOWN = "UNKNOWN"
    OBSERVED = "OBSERVED"
    SIMULATED = "SIMULATED"


REQUIRED_COMPONENTS: Final = (
    RewardAttributionComponent.FORECAST,
    RewardAttributionComponent.SELECTION,
    RewardAttributionComponent.SIZING,
    RewardAttributionComponent.EXECUTION,
    RewardAttributionComponent.DATA_QUALITY,
)


def _text(
    value: object,
    name: str,
    *,
    _error_type=RewardAttributionError,
    _str_type=str,
    _type=type,
) -> str:
    if (
        _type(value) is not _str_type
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise _error_type(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _error_type(f"{name} must be valid UTF-8") from exc
    return value


def _sha256(
    value: object,
    name: str,
    *,
    _text_impl=_text,
    _hex=_HEX,
    _error_type=RewardAttributionError,
) -> str:
    text = _text_impl(value, name)
    if len(text) != 64 or any(character not in _hex for character in text):
        raise _error_type(f"{name} must be lowercase SHA-256")
    return text


def _exact_keys(
    raw: Mapping[str, Any],
    expected: set[str],
    name: str,
    *,
    _error_type=RewardAttributionError,
    _dict_type=dict,
    _set_type=set,
    _type=type,
) -> None:
    if _type(raw) is not _dict_type or _set_type(raw) != expected:
        raise _error_type(f"{name} keys mismatch")


def _digest(
    payload: Mapping[str, Any],
    *,
    _json_dumps=json.dumps,
    _sha256_constructor=hashlib.sha256,
    _dict_type=dict,
    _error_type=RewardAttributionError,
) -> str:
    try:
        encoded = _json_dumps(
            _dict_type(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error_type("attribution payload is not canonical JSON") from exc
    return _sha256_constructor(encoded).hexdigest()


@dataclass(frozen=True, order=True, slots=True)
class AttributionAuthorityRef:
    """Correlation handle for one external authority; never authority by itself."""

    family: str
    evidence_id: str
    sha256: str

    def __post_init__(
        self,
        _text_impl=_text,
        _sha256_impl=_sha256,
    ) -> None:
        _text_impl(self.family, "authority family")
        _text_impl(self.evidence_id, "authority evidence_id")
        _sha256_impl(self.sha256, "authority sha256")

    def to_dict(self) -> dict[str, str]:
        return {
            "family": self.family,
            "evidence_id": self.evidence_id,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AttributionAuthorityRef":
        _exact_keys(raw, {"family", "evidence_id", "sha256"}, "AttributionAuthorityRef")
        return cls(
            family=_text(raw["family"], "authority family"),
            evidence_id=_text(raw["evidence_id"], "authority evidence_id"),
            sha256=_sha256(raw["sha256"], "authority sha256"),
        )


@dataclass(frozen=True, slots=True)
class RewardComponentAttribution:
    """Evidence status for one causal component of a resolved total reward.

    No monetary contribution is represented because additivity is not generally
    identifiable. UNKNOWN explicitly means the product lacks sufficient component
    evidence; it is never interpreted as a zero contribution.
    """

    component: RewardAttributionComponent
    truth: AttributionTruth
    authority_refs: tuple[AttributionAuthorityRef, ...] = ()
    counterfactual_ref: AttributionAuthorityRef | None = None

    def __post_init__(
        self,
        _component_type=RewardAttributionComponent,
        _truth_type=AttributionTruth,
        _ref_type=AttributionAuthorityRef,
        _unknown_truth=AttributionTruth.UNKNOWN,
        _simulated_truth=AttributionTruth.SIMULATED,
        _observed_truth=AttributionTruth.OBSERVED,
        _error_type=RewardAttributionError,
        _tuple_type=tuple,
        _set_type=set,
        _sorted=sorted,
        _type=type,
        _any=any,
    ) -> None:
        if _type(self.component) is not _component_type:
            raise _error_type(
                "component must be exact RewardAttributionComponent"
            )
        if _type(self.truth) is not _truth_type:
            raise _error_type("truth must be exact AttributionTruth")
        if _type(self.authority_refs) is not _tuple_type or _any(
            _type(item) is not _ref_type for item in self.authority_refs
        ):
            raise _error_type(
                "authority_refs must contain exact AttributionAuthorityRef values"
            )
        if (
            self.counterfactual_ref is not None
            and _type(self.counterfactual_ref) is not _ref_type
        ):
            raise _error_type(
                "counterfactual_ref must be exact AttributionAuthorityRef or None"
            )
        if _tuple_type(_sorted(self.authority_refs)) != self.authority_refs:
            raise _error_type("authority_refs must be sorted")
        if len(_set_type(self.authority_refs)) != len(self.authority_refs):
            raise _error_type("authority_refs must be unique")
        authority_identities = _tuple_type(
            (item.family, item.evidence_id) for item in self.authority_refs
        )
        if len(_set_type(authority_identities)) != len(authority_identities):
            raise _error_type(
                "authority_refs cannot bind one authority identity to multiple digests"
            )

        if self.truth is _unknown_truth:
            if self.authority_refs or self.counterfactual_ref is not None:
                raise _error_type(
                    "UNKNOWN attribution cannot carry positive authority assertions"
                )
        elif not self.authority_refs:
            raise _error_type(
                "OBSERVED/SIMULATED attribution requires explicit authority_refs"
            )

        if self.truth is _simulated_truth and self.counterfactual_ref is None:
            raise _error_type(
                "SIMULATED attribution requires counterfactual authority"
            )
        if self.truth is _observed_truth and self.counterfactual_ref is not None:
            raise _error_type(
                "OBSERVED attribution cannot carry counterfactual_ref"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component.value,
            "truth": self.truth.value,
            "authority_refs": [item.to_dict() for item in self.authority_refs],
            "counterfactual_ref": (
                None
                if self.counterfactual_ref is None
                else self.counterfactual_ref.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RewardComponentAttribution":
        _exact_keys(
            raw,
            {"component", "truth", "authority_refs", "counterfactual_ref"},
            "RewardComponentAttribution",
        )
        refs_raw = raw["authority_refs"]
        if type(refs_raw) is not list:
            raise RewardAttributionError("authority_refs must be a list")
        counterfactual_raw = raw["counterfactual_ref"]
        try:
            component = RewardAttributionComponent(
                _text(raw["component"], "component")
            )
            truth = AttributionTruth(_text(raw["truth"], "truth"))
        except ValueError as exc:
            raise RewardAttributionError("unsupported reward attribution enum") from exc
        return cls(
            component=component,
            truth=truth,
            authority_refs=tuple(
                AttributionAuthorityRef.from_dict(item) for item in refs_raw
            ),
            counterfactual_ref=(
                None
                if counterfactual_raw is None
                else AttributionAuthorityRef.from_dict(counterfactual_raw)
            ),
        )


@dataclass(frozen=True, slots=True)
class RewardAttributionEvidence:
    """Complete five-component assertion bound to one exact causal reward transition."""

    environment_id: str
    episode_id: str
    action_id: str
    outcome_id: str
    reward_id: str
    transition_id: str
    components: tuple[RewardComponentAttribution, ...]
    schema: str = SCHEMA
    schema_version: int = SCHEMA_VERSION

    def __post_init__(
        self,
        _schema=SCHEMA,
        _schema_version=SCHEMA_VERSION,
        _sha256_impl=_sha256,
        _component_type=RewardComponentAttribution,
        _required_components=REQUIRED_COMPONENTS,
        _error_type=RewardAttributionError,
        _str_type=str,
        _int_type=int,
        _tuple_type=tuple,
        _type=type,
        _any=any,
    ) -> None:
        if (
            _type(self.schema) is not _str_type
            or self.schema != _schema
            or _type(self.schema_version) is not _int_type
            or self.schema_version != _schema_version
        ):
            raise _error_type("unsupported reward attribution schema")
        for name in (
            "environment_id",
            "episode_id",
            "action_id",
            "outcome_id",
            "reward_id",
            "transition_id",
        ):
            _sha256_impl(getattr(self, name), name)
        if _type(self.components) is not _tuple_type or _any(
            _type(item) is not _component_type for item in self.components
        ):
            raise _error_type(
                "components must contain exact RewardComponentAttribution values"
            )
        actual = _tuple_type(item.component for item in self.components)
        if actual != _required_components:
            raise _error_type(
                "components must contain exactly FORECAST, SELECTION, SIZING, "
                "EXECUTION, DATA_QUALITY in canonical order"
            )

        authority_digests: dict[tuple[str, str], str] = {}
        for component in self.components:
            refs = component.authority_refs
            if component.counterfactual_ref is not None:
                refs = (*refs, component.counterfactual_ref)
            for ref in refs:
                identity = (ref.family, ref.evidence_id)
                existing_digest = authority_digests.get(identity)
                if existing_digest is None:
                    authority_digests[identity] = ref.sha256
                elif existing_digest != ref.sha256:
                    raise _error_type(
                        "attribution envelope cannot bind one authority identity "
                        "to multiple digests"
                    )

    @property
    def source_resolved(self) -> bool:
        return False

    @property
    def policy_update_eligible(self) -> bool:
        return False

    @property
    def semantic_key(self, _digest_impl=_digest) -> str:
        return _digest_impl(
            {
                "schema": self.schema,
                "schema_version": self.schema_version,
                "environment_id": self.environment_id,
                "episode_id": self.episode_id,
                "action_id": self.action_id,
                "outcome_id": self.outcome_id,
                "reward_id": self.reward_id,
                "transition_id": self.transition_id,
            }
        )

    @property
    def evidence_id(self, _digest_impl=_digest) -> str:
        return _digest_impl(self.payload())

    def require_policy_update_eligible(
        self,
        _error_type=RewardAttributionError,
    ) -> None:
        raise _error_type(
            "schema v1 is contract-only: product-owned attribution resolution is required"
        )

    def component(
        self,
        component: RewardAttributionComponent,
        _component_type=RewardAttributionComponent,
        _required_components=REQUIRED_COMPONENTS,
        _error_type=RewardAttributionError,
        _type=type,
    ) -> RewardComponentAttribution:
        if _type(component) is not _component_type:
            raise _error_type(
                "component lookup requires exact RewardAttributionComponent"
            )
        return self.components[_required_components.index(component)]

    def payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "environment_id": self.environment_id,
            "episode_id": self.episode_id,
            "action_id": self.action_id,
            "outcome_id": self.outcome_id,
            "reward_id": self.reward_id,
            "transition_id": self.transition_id,
            "components": [item.to_dict() for item in self.components],
            "source_resolved": False,
            "policy_update_eligible": False,
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["semantic_key"] = self.semantic_key
        raw["evidence_id"] = self.evidence_id
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RewardAttributionEvidence":
        expected = {
            "schema",
            "schema_version",
            "environment_id",
            "episode_id",
            "action_id",
            "outcome_id",
            "reward_id",
            "transition_id",
            "components",
            "source_resolved",
            "policy_update_eligible",
            "semantic_key",
            "evidence_id",
        }
        _exact_keys(raw, expected, "RewardAttributionEvidence")
        if (
            type(raw["schema"]) is not str
            or raw["schema"] != SCHEMA
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != SCHEMA_VERSION
        ):
            raise RewardAttributionError("unsupported reward attribution schema")
        if raw["source_resolved"] is not False or raw["policy_update_eligible"] is not False:
            raise RewardAttributionError(
                "schema v1 cannot carry positive attribution authority/eligibility"
            )
        components_raw = raw["components"]
        if type(components_raw) is not list:
            raise RewardAttributionError("components must be a list")
        evidence = cls(
            environment_id=_sha256(raw["environment_id"], "environment_id"),
            episode_id=_sha256(raw["episode_id"], "episode_id"),
            action_id=_sha256(raw["action_id"], "action_id"),
            outcome_id=_sha256(raw["outcome_id"], "outcome_id"),
            reward_id=_sha256(raw["reward_id"], "reward_id"),
            transition_id=_sha256(raw["transition_id"], "transition_id"),
            components=tuple(
                RewardComponentAttribution.from_dict(item) for item in components_raw
            ),
        )
        if _sha256(raw["semantic_key"], "semantic_key") != evidence.semantic_key:
            raise RewardAttributionError("reward attribution semantic key mismatch")
        if _sha256(raw["evidence_id"], "evidence_id") != evidence.evidence_id:
            raise RewardAttributionError("reward attribution evidence digest mismatch")
        return evidence


def _build_unknown_reward_attribution(
    *,
    _evidence_type=RewardAttributionEvidence,
    _component_type=RewardComponentAttribution,
    _unknown_truth=AttributionTruth.UNKNOWN,
    _required_components=REQUIRED_COMPONENTS,
    _tuple_type=tuple,
):
    """Compose the public UNKNOWN baseline from canonical implementation roots."""

    def unknown_reward_attribution(
        *,
        environment_id: str,
        episode_id: str,
        action_id: str,
        outcome_id: str,
        reward_id: str,
        transition_id: str,
    ) -> RewardAttributionEvidence:
        """Create the only safe baseline when no component authority has been resolved."""

        return _evidence_type(
            environment_id=environment_id,
            episode_id=episode_id,
            action_id=action_id,
            outcome_id=outcome_id,
            reward_id=reward_id,
            transition_id=transition_id,
            components=_tuple_type(
                _component_type(component, _unknown_truth)
                for component in _required_components
            ),
        )

    return unknown_reward_attribution


unknown_reward_attribution = _build_unknown_reward_attribution()
del _build_unknown_reward_attribution


__all__ = [
    "AttributionAuthorityRef",
    "AttributionTruth",
    "REQUIRED_COMPONENTS",
    "RewardAttributionComponent",
    "RewardAttributionError",
    "RewardAttributionEvidence",
    "RewardComponentAttribution",
    "unknown_reward_attribution",
]
