"""Durable exact reconstruction authority for the canonical PaperRiskPolicy.

This module persists only the existing executable paper-risk configuration. It
does not make risk decisions, widen EconomicGoal authority, or create a second
risk model. A persisted policy can be reconstructed only against the exact
EconomicGoalContract revision whose canonical digest was stored with it. The
workspace file is additionally fenced by the shared independent monotonic
workspace authority so deletion or a coherent older/different replacement
cannot become a new creation-only truth after restart.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from .economic_goal import EconomicGoalContract
from .economic_goal_provenance import provenance_for
from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .risk import PaperRiskPolicy
from .workspace_lock import WorkspaceEconomicLock


PAPER_RISK_POLICY_SCHEMA: Final = "autosport.paper_risk_policy"
PAPER_RISK_POLICY_SCHEMA_VERSION: Final = 1

_AUTHORITY_DOMAIN: Final = "autosport.paper-risk-policy.v1"
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
    if type(goal) is not EconomicGoalContract:
        raise PaperRiskPolicyStoreError(
            "economic_goal must be a canonical EconomicGoalContract or None"
        )
    return provenance_for(goal).contract_sha256


def _durable_bytes(payload: dict[str, object]) -> bytes:
    """Return the exact bytes emitted by integrity.atomic_write_json()."""

    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PaperRiskPolicyStoreError(
            "paper risk policy payload is not durably serializable"
        ) from exc
    return (text + "\n").encode("utf-8")


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
    outside paper_risk_policy.json. This prevents a coherent rewrite of both
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
    """Creation-only durable executable risk-policy configuration authority.

    The workspace file is an exact reconstruction payload. The independent
    MonotonicWorkspaceAuthority is the chronology/existence witness: once a
    policy is committed, missing or different workspace bytes fail closed until
    a future explicit policy-revision authority is implemented.
    """

    FILE_NAME: Final = "paper_risk_policy.json"

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).absolute().resolve(strict=False)
        self.path = self.workspace / self.FILE_NAME
        try:
            self._authority = MonotonicWorkspaceAuthority(
                workspace=self.workspace,
                domain=_AUTHORITY_DOMAIN,
                key=self.FILE_NAME,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise PaperRiskPolicyStoreError(
                "cannot bind paper risk policy anti-rollback authority"
            ) from exc

    def _read_bytes(self) -> bytes:
        if self.path.is_symlink() or not self.path.is_file():
            raise PaperRiskPolicyStoreError(
                "persisted paper risk policy must be a regular file"
            )
        try:
            return self.path.read_bytes()
        except OSError as exc:
            raise PaperRiskPolicyStoreError(
                f"cannot read persisted paper risk policy: {exc}"
            ) from exc

    def _observed_state_sha256(self) -> str | None:
        if self.path.is_symlink():
            raise PaperRiskPolicyStoreError(
                "persisted paper risk policy must be a regular file"
            )
        if not self.path.exists():
            return None
        return hashlib.sha256(self._read_bytes()).hexdigest()

    def _recover_authority(self, observed: str | None) -> None:
        try:
            history = self._authority.read_history()
            pending = (
                history[-1]
                if history and history[-1].phase is AuthorityPhase.PREPARE
                else None
            )
            if pending is None:
                self._authority.recover(observed_state_sha256=observed)
            else:
                self._authority.recover(
                    observed_state_sha256=observed,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=pending.semantic_binding_sha256,
                )
        except MonotonicWorkspaceAuthorityError as exc:
            raise PaperRiskPolicyStoreError(
                "paper risk policy anti-rollback authority rejected workspace state"
            ) from exc

    def load(
        self,
        *,
        economic_goal: EconomicGoalContract | None,
        expected_policy_provenance_sha256: str,
    ) -> PaperRiskPolicy:
        with WorkspaceEconomicLock(self.workspace):
            with durable_path_lock(self.path):
                raw = self._read_bytes()
                try:
                    text = raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise PaperRiskPolicyStoreError(
                        "invalid paper risk policy JSON"
                    ) from exc
                reconstructed = paper_risk_policy_from_json(
                    text,
                    economic_goal=economic_goal,
                    expected_policy_provenance_sha256=expected_policy_provenance_sha256,
                )
                observed = hashlib.sha256(raw).hexdigest()
                self._recover_authority(observed)
                if self._observed_state_sha256() != observed:
                    raise PaperRiskPolicyStoreError(
                        "paper risk policy changed during authority verification"
                    )
                return reconstructed

    def initialize_owner(self, policy: PaperRiskPolicy) -> None:
        """Publish the first owner policy with PREPARE -> publish -> COMMIT fencing."""

        if type(policy) is not PaperRiskPolicy:
            raise PaperRiskPolicyStoreError(
                "paper risk policy persistence requires exact PaperRiskPolicy"
            )
        with WorkspaceEconomicLock(self.workspace):
            with durable_path_lock(self.path):
                observed = self._observed_state_sha256()
                self._recover_authority(observed)
                if observed is not None:
                    raise PaperRiskPolicyStoreError(
                        "persisted paper risk policy already exists; replacement requires "
                        "a separate owner authority boundary"
                    )

                payload = paper_risk_policy_to_payload(policy)
                intended = hashlib.sha256(_durable_bytes(payload)).hexdigest()
                semantic_material = "\0".join(
                    (
                        _AUTHORITY_DOMAIN,
                        self.FILE_NAME,
                        intended,
                        policy.provenance_sha256,
                        _economic_goal_sha256(policy.economic_goal) or "<UNBOUND>",
                    )
                ).encode("utf-8")
                semantic_binding = hashlib.sha256(semantic_material).hexdigest()
                tx_id = f"paper-risk-policy-{uuid.uuid4().hex}"

                try:
                    self._authority.prepare(
                        tx_id=tx_id,
                        observed_state_sha256=None,
                        intended_state_sha256=intended,
                        semantic_binding_sha256=semantic_binding,
                    )
                except MonotonicWorkspaceAuthorityError as exc:
                    raise PaperRiskPolicyStoreError(
                        "cannot prepare paper risk policy anti-rollback authority"
                    ) from exc

                atomic_write_json(self.path, payload)
                published = self._observed_state_sha256()
                if published != intended:
                    raise PaperRiskPolicyStoreError(
                        "published paper risk policy differs from prepared authority bytes"
                    )
                try:
                    self._authority.commit(
                        tx_id=tx_id,
                        observed_state_sha256=published,
                        semantic_binding_sha256=semantic_binding,
                    )
                except MonotonicWorkspaceAuthorityError as exc:
                    raise PaperRiskPolicyStoreError(
                        "cannot commit paper risk policy anti-rollback authority"
                    ) from exc
                if self._observed_state_sha256() != intended:
                    raise PaperRiskPolicyStoreError(
                        "paper risk policy changed during authority commit"
                    )
