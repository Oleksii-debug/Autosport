"""Owner-approved, rollback-fenced local model-compute monetary tariff authority.

This module is a concrete product-owned source for the #732 prospective
MODEL_COMPUTE_AI cost seam. It deliberately does not price provider/cloud
compute and does not treat router estimated_cost as money.

A tariff is an owner-created, immutable per-request costing contract for one
exact LOCAL backend/model/config identity. It is bound to the current durable
EconomicGoal currency and revision, a causally available allocation basis, and
an effective interval. The whole tariff state is protected by the existing
MonotonicWorkspaceAuthority so restoring/deleting an older workspace copy fails
closed while the independent machine-state authority survives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
from typing import Final, Mapping

from .economic_goal_store import EconomicGoalStore, economic_goal_to_payload
from .integrity import atomic_write_json, sha256_file
from .json_integrity import strict_json_loads
from .local_compute_allocation_basis import (
    LocalComputeAllocationBasisAuthorityStore,
    LocalComputeAllocationBasisRecord,
)
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
)
from .workspace_lock import WorkspaceEconomicLock


SCHEMA: Final = "autosport.local_compute_tariff_authority"
SCHEMA_VERSION: Final = 1
FILE_NAME: Final = "local-compute-tariffs.json"
AUTHORITY_DOMAIN: Final = "autosport.local-compute-tariff.v1"
AUTHORITY_KEY: Final = "owner-approved-local-compute-tariffs"
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE: Final = re.compile(r"^[A-Z]{3}$")
_MAX_INTEGER_DIGITS: Final = 24
_MAX_FRACTIONAL_DIGITS: Final = 18


class LocalComputeTariffError(ValueError):
    """The owner tariff input/state is malformed, ambiguous, or non-causal."""


class LocalComputeCostTreatment(StrEnum):
    """What the exact per-request amount represents."""

    FULLY_ALLOCATED_PER_REQUEST = "FULLY_ALLOCATED_PER_REQUEST"


def _text(value: object, field: str, *, limit: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > limit
    ):
        raise LocalComputeTariffError(f"{field} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise LocalComputeTariffError(f"{field} must be valid UTF-8") from exc
    return value


def _sha(value: object, field: str) -> str:
    value = _text(value, field, limit=64)
    if _SHA256_RE.fullmatch(value) is None:
        raise LocalComputeTariffError(f"{field} must be lowercase SHA-256")
    return value


def _currency(value: object) -> str:
    value = _text(value, "currency", limit=3)
    if _CURRENCY_RE.fullmatch(value) is None:
        raise LocalComputeTariffError("currency must be uppercase three-letter code")
    return value


def _instant(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LocalComputeTariffError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LocalComputeTariffError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _time(value: object, field: str) -> str:
    return _instant(value, field).isoformat().replace("+00:00", "Z")


def _money(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise LocalComputeTariffError(f"{field} must be a finite non-negative Decimal")
    _sign, digits, exponent = value.as_tuple()
    integer_digits = max(1, len(digits) + exponent)
    fractional_digits = max(0, -exponent)
    if integer_digits > _MAX_INTEGER_DIGITS or fractional_digits > _MAX_FRACTIONAL_DIGITS:
        raise LocalComputeTariffError(
            f"{field} exceeds supported exact monetary precision"
        )
    return value


def _money_text(value: Decimal) -> str:
    return format(_money(value, "amount_per_request"), "f")


def _canonical_bytes(payload: object) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise LocalComputeTariffError("tariff state is outside canonical JSON domain") from exc


def _digest(payload: object) -> str:
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise LocalComputeTariffError("tariff payload is outside canonical JSON domain") from exc
    return hashlib.sha256(raw).hexdigest()


def _goal_sha256(goal: object) -> str:
    return _digest(economic_goal_to_payload(goal))  # type: ignore[arg-type]


def _authority_now() -> str:
    """Product clock seam. Tests may patch this private function."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class LocalComputeTariffRecord:
    tariff_id: str
    backend_id: str
    model_id: str
    config_sha256: str
    amount_per_request: Decimal
    currency: str
    effective_from: str
    effective_until: str | None
    allocation_treatment: LocalComputeCostTreatment
    allocation_policy_id: str
    allocation_basis_id: str
    allocation_basis_sha256: str
    basis_available_at: str
    recorded_at: str
    owner_goal_id: str
    owner_goal_revision: int
    owner_bankroll_id: str
    owner_goal_sha256: str

    def __post_init__(self) -> None:
        _text(self.tariff_id, "tariff_id")
        _text(self.backend_id, "backend_id")
        _text(self.model_id, "model_id")
        _sha(self.config_sha256, "config_sha256")
        _money(self.amount_per_request, "amount_per_request")
        _currency(self.currency)
        start = _instant(self.effective_from, "effective_from")
        end = None if self.effective_until is None else _instant(
            self.effective_until, "effective_until"
        )
        if end is not None and end <= start:
            raise LocalComputeTariffError("effective_until must be after effective_from")
        if type(self.allocation_treatment) is not LocalComputeCostTreatment:
            raise LocalComputeTariffError(
                "allocation_treatment must be LocalComputeCostTreatment"
            )
        _text(self.allocation_policy_id, "allocation_policy_id")
        _text(self.allocation_basis_id, "allocation_basis_id")
        _sha(self.allocation_basis_sha256, "allocation_basis_sha256")
        basis_at = _instant(self.basis_available_at, "basis_available_at")
        recorded = _instant(self.recorded_at, "recorded_at")
        if basis_at > recorded:
            raise LocalComputeTariffError(
                "allocation basis must be available before owner tariff publication"
            )
        _text(self.owner_goal_id, "owner_goal_id")
        if (
            type(self.owner_goal_revision) is not int
            or isinstance(self.owner_goal_revision, bool)
            or self.owner_goal_revision < 1
        ):
            raise LocalComputeTariffError("owner_goal_revision must be positive integer")
        _text(self.owner_bankroll_id, "owner_bankroll_id")
        _sha(self.owner_goal_sha256, "owner_goal_sha256")

    def payload(self) -> dict[str, object]:
        return {
            "tariff_id": self.tariff_id,
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "config_sha256": self.config_sha256,
            "amount_per_request": _money_text(self.amount_per_request),
            "currency": self.currency,
            "effective_from": _time(self.effective_from, "effective_from"),
            "effective_until": (
                None
                if self.effective_until is None
                else _time(self.effective_until, "effective_until")
            ),
            "allocation_treatment": self.allocation_treatment.value,
            "allocation_policy_id": self.allocation_policy_id,
            "allocation_basis_id": self.allocation_basis_id,
            "allocation_basis_sha256": self.allocation_basis_sha256,
            "basis_available_at": _time(self.basis_available_at, "basis_available_at"),
            "recorded_at": _time(self.recorded_at, "recorded_at"),
            "owner_goal_id": self.owner_goal_id,
            "owner_goal_revision": self.owner_goal_revision,
            "owner_bankroll_id": self.owner_bankroll_id,
            "owner_goal_sha256": self.owner_goal_sha256,
        }

    @property
    def tariff_sha256(self) -> str:
        return _digest(self.payload())

    def to_dict(self) -> dict[str, object]:
        return {**self.payload(), "tariff_sha256": self.tariff_sha256}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "LocalComputeTariffRecord":
        expected = {
            "tariff_id",
            "backend_id",
            "model_id",
            "config_sha256",
            "amount_per_request",
            "currency",
            "effective_from",
            "effective_until",
            "allocation_treatment",
            "allocation_policy_id",
            "allocation_basis_id",
            "allocation_basis_sha256",
            "basis_available_at",
            "recorded_at",
            "owner_goal_id",
            "owner_goal_revision",
            "owner_bankroll_id",
            "owner_goal_sha256",
            "tariff_sha256",
        }
        if set(raw) != expected:
            raise LocalComputeTariffError("tariff record fields do not match schema")
        amount_raw = raw["amount_per_request"]
        if type(amount_raw) is not str:
            raise LocalComputeTariffError("amount_per_request must be Decimal text")
        try:
            amount = Decimal(amount_raw)
        except (InvalidOperation, ValueError) as exc:
            raise LocalComputeTariffError("amount_per_request is invalid Decimal") from exc
        try:
            treatment = LocalComputeCostTreatment(raw["allocation_treatment"])  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise LocalComputeTariffError("unknown allocation_treatment") from exc
        item = cls(
            tariff_id=raw["tariff_id"],  # type: ignore[arg-type]
            backend_id=raw["backend_id"],  # type: ignore[arg-type]
            model_id=raw["model_id"],  # type: ignore[arg-type]
            config_sha256=raw["config_sha256"],  # type: ignore[arg-type]
            amount_per_request=amount,
            currency=raw["currency"],  # type: ignore[arg-type]
            effective_from=raw["effective_from"],  # type: ignore[arg-type]
            effective_until=raw["effective_until"],  # type: ignore[arg-type]
            allocation_treatment=treatment,
            allocation_policy_id=raw["allocation_policy_id"],  # type: ignore[arg-type]
            allocation_basis_id=raw["allocation_basis_id"],  # type: ignore[arg-type]
            allocation_basis_sha256=raw["allocation_basis_sha256"],  # type: ignore[arg-type]
            basis_available_at=raw["basis_available_at"],  # type: ignore[arg-type]
            recorded_at=raw["recorded_at"],  # type: ignore[arg-type]
            owner_goal_id=raw["owner_goal_id"],  # type: ignore[arg-type]
            owner_goal_revision=raw["owner_goal_revision"],  # type: ignore[arg-type]
            owner_bankroll_id=raw["owner_bankroll_id"],  # type: ignore[arg-type]
            owner_goal_sha256=raw["owner_goal_sha256"],  # type: ignore[arg-type]
        )
        if raw["tariff_sha256"] != item.tariff_sha256:
            raise LocalComputeTariffError("tariff record digest mismatch")
        return item


def _state_payload(records: tuple[LocalComputeTariffRecord, ...]) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "records": [record.to_dict() for record in records],
    }


def _intervals_overlap(
    left_start: datetime,
    left_end: datetime | None,
    right_start: datetime,
    right_end: datetime | None,
) -> bool:
    left_before_right_end = right_end is None or left_start < right_end
    right_before_left_end = left_end is None or right_start < left_end
    return left_before_right_end and right_before_left_end


class LocalComputeTariffAuthorityStore:
    """Creation-only owner tariff store with independent rollback fencing."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        authority_root: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).absolute()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path = self.workspace / FILE_NAME
        self._basis_store = LocalComputeAllocationBasisAuthorityStore(
            self.workspace,
            authority_root=authority_root,
        )
        self._authority = MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            domain=AUTHORITY_DOMAIN,
            key=AUTHORITY_KEY,
            authority_root=authority_root,
        )
        with WorkspaceEconomicLock(self.workspace):
            self._recover()
            self._records = self._load()

    def _observed_sha256(self) -> str | None:
        return sha256_file(self.path) if self.path.exists() else None

    def _recover(self) -> None:
        observed = self._observed_sha256()
        history = self._authority.read_history()
        if history and history[-1].phase is AuthorityPhase.PREPARE:
            pending = history[-1]
            self._authority.recover(
                observed_state_sha256=observed,
                tx_id=pending.tx_id,
                semantic_binding_sha256=pending.semantic_binding_sha256,
            )
            return
        self._authority.recover(observed_state_sha256=observed)

    def _load(self) -> tuple[LocalComputeTariffRecord, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise LocalComputeTariffError("local compute tariff state is unreadable") from exc
        if not isinstance(raw, dict) or set(raw) != {"schema", "schema_version", "records"}:
            raise LocalComputeTariffError("local compute tariff state fields are invalid")
        if raw["schema"] != SCHEMA or raw["schema_version"] != SCHEMA_VERSION:
            raise LocalComputeTariffError("unsupported local compute tariff state schema")
        values = raw["records"]
        if type(values) is not list:
            raise LocalComputeTariffError("local compute tariff records must be a list")
        records: list[LocalComputeTariffRecord] = []
        ids: set[str] = set()
        digests: set[str] = set()
        for raw_record in values:
            if not isinstance(raw_record, dict):
                raise LocalComputeTariffError("tariff record must be an object")
            record = LocalComputeTariffRecord.from_dict(raw_record)
            if record.tariff_id in ids or record.tariff_sha256 in digests:
                raise LocalComputeTariffError("duplicate tariff identity")
            ids.add(record.tariff_id)
            digests.add(record.tariff_sha256)
            records.append(record)
        return tuple(records)

    def _current_goal(self):
        try:
            goal = EconomicGoalStore(self.workspace).load()
        except Exception as exc:
            raise LocalComputeTariffError(
                "current durable EconomicGoal is required for local compute tariff authority"
            ) from exc
        return goal, _goal_sha256(goal)

    def _basis_authority(self) -> LocalComputeAllocationBasisAuthorityStore:
        authority = self._basis_store
        if type(authority) is not LocalComputeAllocationBasisAuthorityStore:
            raise LocalComputeTariffError(
                "local compute allocation basis authority instance is not canonical"
            )
        return authority

    def publish_owner_tariff(
        self,
        *,
        tariff_id: str,
        backend_id: str,
        model_id: str,
        config_sha256: str,
        effective_from: str,
        effective_until: str | None,
        allocation_policy_id: str,
        allocation_basis_id: str,
    ) -> LocalComputeTariffRecord:
        """Persist one tariff derived from an exact product-owned allocation basis."""

        canonical_tariff_id = _text(tariff_id, "tariff_id")
        canonical_backend = _text(backend_id, "backend_id")
        canonical_model = _text(model_id, "model_id")
        canonical_config = _sha(config_sha256, "config_sha256")
        canonical_start = _time(effective_from, "effective_from")
        canonical_end = (
            None if effective_until is None else _time(effective_until, "effective_until")
        )
        canonical_policy = _text(allocation_policy_id, "allocation_policy_id")
        canonical_basis_id = _text(allocation_basis_id, "allocation_basis_id")
        recorded_at = _time(_authority_now(), "recorded_at")
        goal_for_basis, _ = self._current_goal()
        basis = LocalComputeAllocationBasisAuthorityStore.resolve(
            self._basis_authority(),
            basis_id=canonical_basis_id,
            backend_id=canonical_backend,
            model_id=canonical_model,
            config_sha256=canonical_config,
            allocation_policy_id=canonical_policy,
            decision_at=recorded_at,
            bankroll_id=goal_for_basis.bankroll_id,
            currency=goal_for_basis.currency,
        )
        if basis is None:
            raise LocalComputeTariffError(
                "product-owned allocation basis is missing or not causally available"
            )
        canonical_amount = basis.amount_per_request
        canonical_basis = basis.basis_sha256
        canonical_basis_at = basis.available_at

        with WorkspaceEconomicLock(self.workspace):
            self._recover()
            self._records = self._load()
            goal, goal_sha256 = self._current_goal()
            if (
                basis.owner_goal_id != goal.goal_id
                or basis.owner_goal_revision != goal.revision
                or basis.owner_bankroll_id != goal.bankroll_id
                or basis.owner_goal_sha256 != goal_sha256
                or basis.currency != goal.currency
            ):
                raise LocalComputeTariffError(
                    "allocation basis no longer matches the current EconomicGoal"
                )

            for existing in self._records:
                if existing.tariff_id != canonical_tariff_id:
                    continue
                same_request = (
                    existing.backend_id == canonical_backend
                    and existing.model_id == canonical_model
                    and existing.config_sha256 == canonical_config
                    and existing.amount_per_request == canonical_amount
                    and existing.currency == goal.currency
                    and existing.effective_from == canonical_start
                    and existing.effective_until == canonical_end
                    and existing.allocation_policy_id == canonical_policy
                    and existing.allocation_basis_id == canonical_basis_id
                    and existing.allocation_basis_sha256 == canonical_basis
                    and existing.basis_available_at == canonical_basis_at
                    and existing.owner_goal_id == goal.goal_id
                    and existing.owner_goal_revision == goal.revision
                    and existing.owner_bankroll_id == goal.bankroll_id
                    and existing.owner_goal_sha256 == goal_sha256
                )
                if same_request:
                    return existing
                raise LocalComputeTariffError("tariff_id is immutable")

            record = LocalComputeTariffRecord(
                tariff_id=canonical_tariff_id,
                backend_id=canonical_backend,
                model_id=canonical_model,
                config_sha256=canonical_config,
                amount_per_request=canonical_amount,
                currency=goal.currency,
                effective_from=canonical_start,
                effective_until=canonical_end,
                allocation_treatment=LocalComputeCostTreatment.FULLY_ALLOCATED_PER_REQUEST,
                allocation_policy_id=canonical_policy,
                allocation_basis_id=canonical_basis_id,
                allocation_basis_sha256=canonical_basis,
                basis_available_at=canonical_basis_at,
                recorded_at=recorded_at,
                owner_goal_id=goal.goal_id,
                owner_goal_revision=goal.revision,
                owner_bankroll_id=goal.bankroll_id,
                owner_goal_sha256=goal_sha256,
            )

            new_start = _instant(record.effective_from, "effective_from")
            new_end = None if record.effective_until is None else _instant(
                record.effective_until, "effective_until"
            )
            for existing in self._records:
                if (
                    existing.backend_id,
                    existing.model_id,
                    existing.config_sha256,
                    existing.owner_goal_sha256,
                ) != (
                    record.backend_id,
                    record.model_id,
                    record.config_sha256,
                    record.owner_goal_sha256,
                ):
                    continue
                old_start = _instant(existing.effective_from, "effective_from")
                old_end = None if existing.effective_until is None else _instant(
                    existing.effective_until, "effective_until"
                )
                if _intervals_overlap(new_start, new_end, old_start, old_end):
                    raise LocalComputeTariffError(
                        "same compute identity cannot have overlapping owner tariffs"
                    )

            staged = (*self._records, record)
            payload = _state_payload(staged)
            intended = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
            observed = self._observed_sha256()
            binding = _digest(
                {
                    "kind": "OWNER_LOCAL_COMPUTE_TARIFF_PUBLISH",
                    "tariff_sha256": record.tariff_sha256,
                    "owner_goal_sha256": record.owner_goal_sha256,
                    "observed_state_sha256": observed,
                    "intended_state_sha256": intended,
                }
            )
            tx_id = f"local-compute-tariff-{record.tariff_sha256}"
            self._authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            try:
                atomic_write_json(self.path, payload)
            except Exception:
                self._authority.abort(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
                raise
            published = self._observed_sha256()
            if published != intended:
                raise LocalComputeTariffError(
                    "published tariff bytes do not match prepared monotonic state"
                )
            self._authority.commit(
                tx_id=tx_id,
                observed_state_sha256=published,
                semantic_binding_sha256=binding,
            )
            self._records = staged
            return record

    def resolve(
        self,
        *,
        backend_id: str,
        model_id: str,
        config_sha256: str,
        decision_at: str,
        bankroll_id: str,
        currency: str,
    ) -> LocalComputeTariffRecord | None:
        """Resolve the unique causally available exact tariff for a LOCAL decision."""

        canonical_backend = _text(backend_id, "backend_id")
        canonical_model = _text(model_id, "model_id")
        canonical_config = _sha(config_sha256, "config_sha256")
        cutoff = _instant(decision_at, "decision_at")
        canonical_bankroll = _text(bankroll_id, "bankroll_id")
        canonical_currency = _currency(currency)

        with WorkspaceEconomicLock(self.workspace):
            self._recover()
            records = self._load()
            goal, goal_sha256 = self._current_goal()
            if goal.bankroll_id != canonical_bankroll or goal.currency != canonical_currency:
                raise LocalComputeTariffError(
                    "intent bankroll/currency does not match current owner EconomicGoal"
                )

            matches: list[LocalComputeTariffRecord] = []
            for record in records:
                if (
                    record.backend_id,
                    record.model_id,
                    record.config_sha256,
                ) != (canonical_backend, canonical_model, canonical_config):
                    continue
                if (
                    record.owner_goal_id != goal.goal_id
                    or record.owner_goal_revision != goal.revision
                    or record.owner_bankroll_id != goal.bankroll_id
                    or record.owner_goal_sha256 != goal_sha256
                    or record.currency != goal.currency
                ):
                    continue
                if _instant(record.recorded_at, "recorded_at") > cutoff:
                    continue
                if _instant(record.basis_available_at, "basis_available_at") > cutoff:
                    continue
                if cutoff < _instant(record.effective_from, "effective_from"):
                    continue
                if (
                    record.effective_until is not None
                    and cutoff >= _instant(record.effective_until, "effective_until")
                ):
                    continue
                matches.append(record)

            if len(matches) > 1:
                raise LocalComputeTariffError(
                    "ambiguous overlapping local compute tariff authority"
                )
            resolved = None if not matches else matches[0]

        if resolved is None:
            return None
        basis = LocalComputeAllocationBasisAuthorityStore.resolve(
            self._basis_authority(),
            basis_id=resolved.allocation_basis_id,
            backend_id=resolved.backend_id,
            model_id=resolved.model_id,
            config_sha256=resolved.config_sha256,
            allocation_policy_id=resolved.allocation_policy_id,
            decision_at=_time(cutoff.isoformat(), "decision_at"),
            bankroll_id=resolved.owner_bankroll_id,
            currency=resolved.currency,
        )
        if basis is None:
            return None
        if (
            basis.basis_sha256 != resolved.allocation_basis_sha256
            or basis.available_at != resolved.basis_available_at
            or basis.amount_per_request != resolved.amount_per_request
            or basis.currency != resolved.currency
        ):
            raise LocalComputeTariffError(
                "resolved allocation basis no longer matches tariff authority"
            )
        return resolved


__all__ = [
    "LocalComputeAllocationBasisAuthorityStore",
    "LocalComputeAllocationBasisRecord",
    "LocalComputeCostTreatment",
    "LocalComputeTariffAuthorityStore",
    "LocalComputeTariffError",
    "LocalComputeTariffRecord",
]
