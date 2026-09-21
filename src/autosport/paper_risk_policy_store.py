"""Durable exact reconstruction authority for the canonical PaperRiskPolicy.

This module persists only the existing executable paper-risk configuration.  It
does not make risk decisions, widen EconomicGoal authority, or create a second
risk model.  A persisted policy can be reconstructed only against the exact
EconomicGoalContract revision whose canonical digest was stored with it.  Load
also requires the exact PaperRiskPolicy provenance identity from a separately
verified durable authority; the mutable store payload is never allowed to
self-authorize a coherent rewrite.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from .economic_goal import EconomicGoalContract
from .economic_goal_provenance import provenance_for
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .risk import PaperRiskPolicy
from .workspace_lock import WorkspaceEconomicLock


PAPER_RISK_POLICY_SCHEMA: Final = "autosport.paper_risk_policy"
PAPER_RISK_POLICY_SCHEMA_VERSION: Final = 1

_POLICY_KEYS: Final = frozenset(
    {
        "max_ticket_fraction",
        "max_committed_fraction",
        "minimum_cash_reserve_fraction",
        "economic_goal_contract_sha256",
    }
)
_ROOT_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "policy",
        "policy_provenance_sha256",
    }
)


class PaperRiskPolicyStoreError(ValueError):
    """Raised when durable paper-risk authority is malformed or mismatched."""


def _require_exact_keys(
    name: str,
    value: dict[str, object],
    expected: frozenset[str],
) -> None:
    keys = frozenset(value)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise PaperRiskPolicyStoreError(
            f"{name} keys must match schema exactly; missing={missing!r} extra={extra!r}"
        )


def _decimal_text(name: str, value: object) -> Decimal:
    if type(value) is not str or not value or value != value.strip():
        raise PaperRiskPolicyStoreError(f"{name} must be a canonical Decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise PaperRiskPolicyStoreError(
            f"{name} must be a canonical Decimal string"
        ) from exc
    if not parsed.is_finite() or str(parsed) != value:
        raise PaperRiskPolicyStoreError(f"{name} must be a canonical finite Decimal")
    return parsed


def _sha256_text(name: str, value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PaperRiskPolicyStoreError(
            f"{name} must be lowercase 64-character SHA-256 hex"
        )
    return value


def _economic_goal_sha256(goal: EconomicGoalContract | None) -> str | None:
    if goal is None:
        return None
    if not isinstance(goal, EconomicGoalContract):
        raise PaperRiskPolicyStoreError(
            "economic_goal must be a canonical EconomicGoalContract or None"
        )
    return provenance_for(goal).contract_sha256


def paper_risk_policy_to_payload(policy: PaperRiskPolicy) -> dict[str, object]:
    """Return the canonical durable schema-v1 payload for ``policy``."""

    if type(policy) is not PaperRiskPolicy:
        raise PaperRiskPolicyStoreError(
            "paper risk policy persistence requires exact PaperRiskPolicy"
        )
    goal_sha256 = _economic_goal_sha256(policy.economic_goal)
    return {
        "schema": PAPER_RISK_POLICY_SCHEMA,
        "schema_version": PAPER_RISK_POLICY_SCHEMA_VERSION,
        "policy": {
            "max_ticket_fraction": str(policy.max_ticket_fraction),
            "max_committed_fraction": str(policy.max_committed_fraction),
            "minimum_cash_reserve_fraction": str(
                policy.minimum_cash_reserve_fraction
            ),
            "economic_goal_contract_sha256": goal_sha256,
        },
        "policy_provenance_sha256": policy.provenance_sha256,
    }


def paper_risk_policy_from_payload(
    payload: object,
    *,
    economic_goal: EconomicGoalContract | None,
    expected_policy_provenance_sha256: str,
) -> PaperRiskPolicy:
    """Reconstruct only when an external immutable policy identity agrees.

    The expected provenance must come from a separately verified authority
    outside paper_risk_policy.json.  This prevents a coherent rewrite of both
    executable fractions and the self-derived digest stored in this payload.
    """

    if type(payload) is not dict or not all(type(key) is str for key in payload):
        raise PaperRiskPolicyStoreError(
            "paper risk policy payload must be a JSON object"
        )
    root: dict[str, object] = payload
    _require_exact_keys("paper risk policy payload", root, _ROOT_KEYS)

    if root["schema"] != PAPER_RISK_POLICY_SCHEMA:
        raise PaperRiskPolicyStoreError("unsupported paper risk policy schema")
    version = root["schema_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != PAPER_RISK_POLICY_SCHEMA_VERSION
    ):
        raise PaperRiskPolicyStoreError(
            "unsupported paper risk policy schema_version"
        )

    raw_policy = root["policy"]
    if type(raw_policy) is not dict or not all(
        type(key) is str for key in raw_policy
    ):
        raise PaperRiskPolicyStoreError("policy must be a JSON object")
    body: dict[str, object] = raw_policy
    _require_exact_keys("policy", body, _POLICY_KEYS)

    stored_goal_sha256 = body["economic_goal_contract_sha256"]
    if stored_goal_sha256 is not None:
        stored_goal_sha256 = _sha256_text(
            "economic_goal_contract_sha256",
            stored_goal_sha256,
        )
    expected_goal_sha256 = _economic_goal_sha256(economic_goal)
    if stored_goal_sha256 != expected_goal_sha256:
        raise PaperRiskPolicyStoreError(
            "economic goal contract does not match durable paper risk policy"
        )

    persisted_provenance = _sha256_text(
        "policy_provenance_sha256",
        root["policy_provenance_sha256"],
    )
    expected_provenance = _sha256_text(
        "expected_policy_provenance_sha256",
        expected_policy_provenance_sha256,
    )
    if persisted_provenance != expected_provenance:
        raise PaperRiskPolicyStoreError(
            "durable paper risk policy provenance does not match external authority"
        )
    try:
        reconstructed = PaperRiskPolicy(
            max_ticket_fraction=_decimal_text(
                "max_ticket_fraction",
                body["max_ticket_fraction"],
            ),
            max_committed_fraction=_decimal_text(
                "max_committed_fraction",
                body["max_committed_fraction"],
            ),
            minimum_cash_reserve_fraction=_decimal_text(
                "minimum_cash_reserve_fraction",
                body["minimum_cash_reserve_fraction"],
            ),
            economic_goal=economic_goal,
        )
    except (TypeError, ValueError) as exc:
        raise PaperRiskPolicyStoreError(
            "durable paper risk policy is invalid"
        ) from exc

    if reconstructed.provenance_sha256 != persisted_provenance:
        raise PaperRiskPolicyStoreError(
            "durable paper risk policy provenance mismatch"
        )
    if reconstructed.provenance_sha256 != expected_provenance:
        raise PaperRiskPolicyStoreError(
            "reconstructed paper risk policy does not match external authority"
        )
    return reconstructed


def paper_risk_policy_from_json(
    text: str,
    *,
    economic_goal: EconomicGoalContract | None,
    expected_policy_provenance_sha256: str,
) -> PaperRiskPolicy:
    """Decode one strict JSON document and verify exact policy reconstruction."""

    if type(text) is not str:
        raise PaperRiskPolicyStoreError("paper risk policy JSON must be text")
    try:
        payload = strict_json_loads(text)
    except (TypeError, ValueError) as exc:
        raise PaperRiskPolicyStoreError("invalid paper risk policy JSON") from exc
    return paper_risk_policy_from_payload(
        payload,
        economic_goal=economic_goal,
        expected_policy_provenance_sha256=expected_policy_provenance_sha256,
    )


class PaperRiskPolicyStore:
    """Workspace-local, creation-only durable paper-risk configuration authority.

    Owner initialization is deliberately one-way.  This store does not provide an
    automatic update API, because changing any executable risk fraction is an
    authority transition that belongs at a separate explicit owner boundary.
    """

    FILE_NAME: Final = "paper_risk_policy.json"

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.path = self.workspace / self.FILE_NAME

    def load(
        self,
        *,
        economic_goal: EconomicGoalContract | None,
        expected_policy_provenance_sha256: str,
    ) -> PaperRiskPolicy:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PaperRiskPolicyStoreError(
                f"cannot read persisted paper risk policy: {exc}"
            ) from exc
        return paper_risk_policy_from_json(
            text,
            economic_goal=economic_goal,
            expected_policy_provenance_sha256=expected_policy_provenance_sha256,
        )

    def initialize_owner(self, policy: PaperRiskPolicy) -> None:
        """Create the exact initial policy under the canonical economic writer lock."""

        if type(policy) is not PaperRiskPolicy:
            raise PaperRiskPolicyStoreError(
                "paper risk policy persistence requires exact PaperRiskPolicy"
            )
        with WorkspaceEconomicLock(self.workspace):
            if self.path.exists():
                raise PaperRiskPolicyStoreError(
                    "persisted paper risk policy already exists; replacement requires "
                    "a separate owner authority boundary"
                )
            atomic_write_json(self.path, paper_risk_policy_to_payload(policy))
