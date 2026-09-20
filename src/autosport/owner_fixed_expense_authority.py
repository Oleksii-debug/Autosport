from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

from .campaign_cost_evidence import (
    CostBasis,
    CostClass,
    CostEvidence,
    CostEvidenceError,
    CostSourceRef,
    CostTreatment,
    CostTruth,
    CostUnit,
)
from .campaign_economic_authority import CanonicalMembershipRef, FinalizedCampaignAuthority
from .monotonic_workspace_authority import AuthorityPhase, MonotonicWorkspaceAuthority


SCHEMA_VERSION = 1
SOURCE_FAMILY = "economics.owner-fixed-expense.v1"
AUTHORITY_ID = "autosport.owner-admin.fixed-expense.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class OwnerFixedExpenseAuthorityError(RuntimeError):
    """Raised when owner/admin fixed-expense authority cannot be proven exactly."""


@dataclass(frozen=True, slots=True)
class OwnerFixedExpenseRecord:
    campaign_sha256: str
    memberships: tuple[CanonicalMembershipRef, ...]
    amount: Decimal
    currency: str
    incurred_at: datetime
    recorded_at: datetime
    supersedes_expense_id: str | None = None

    def __post_init__(self) -> None:
        _sha256(self.campaign_sha256, "campaign_sha256")
        if tuple(sorted(self.memberships)) != self.memberships or len(set(self.memberships)) != len(self.memberships):
            raise OwnerFixedExpenseAuthorityError("memberships must be sorted and unique")
        if not self.memberships:
            raise OwnerFixedExpenseAuthorityError("fixed expense requires canonical campaign memberships")
        for membership in self.memberships:
            if type(membership) is not CanonicalMembershipRef:
                raise OwnerFixedExpenseAuthorityError("membership must be canonical")
            _text(membership.kind, "membership kind")
            _text(membership.evidence_id, "membership evidence_id")
            _sha256(membership.sha256, "membership sha256")
        _money(self.amount, self.currency)
        _utc(self.incurred_at, "incurred_at")
        _utc(self.recorded_at, "recorded_at")
        if self.incurred_at > self.recorded_at:
            raise OwnerFixedExpenseAuthorityError("incurred_at cannot be later than recorded_at")
        if self.supersedes_expense_id is not None:
            _sha256(self.supersedes_expense_id, "supersedes_expense_id")

    @property
    def expense_id(self) -> str:
        return _digest(self.payload())

    @property
    def record_sha256(self) -> str:
        return self.expense_id

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "authority_id": AUTHORITY_ID,
            "campaign_sha256": self.campaign_sha256,
            "memberships": [
                {
                    "kind": value.kind,
                    "evidence_id": value.evidence_id,
                    "sha256": value.sha256,
                }
                for value in self.memberships
            ],
            "amount": _decimal_text(self.amount),
            "currency": self.currency,
            "incurred_at": _datetime_text(self.incurred_at),
            "recorded_at": _datetime_text(self.recorded_at),
            "supersedes_expense_id": self.supersedes_expense_id,
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["expense_id"] = self.expense_id
        raw["record_sha256"] = self.record_sha256
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OwnerFixedExpenseRecord":
        _keys(
            raw,
            {
                "schema_version", "authority_id", "campaign_sha256", "memberships",
                "amount", "currency", "incurred_at", "recorded_at",
                "supersedes_expense_id", "expense_id", "record_sha256",
            },
            "owner fixed expense",
        )
        if raw["schema_version"] != SCHEMA_VERSION or raw["authority_id"] != AUTHORITY_ID:
            raise OwnerFixedExpenseAuthorityError("unsupported owner fixed-expense authority")
        memberships: list[CanonicalMembershipRef] = []
        for value in _list(raw["memberships"], "memberships"):
            item = _mapping(value, "membership")
            _keys(item, {"kind", "evidence_id", "sha256"}, "membership")
            memberships.append(
                CanonicalMembershipRef(
                    kind=_string(item["kind"], "kind"),
                    evidence_id=_string(item["evidence_id"], "evidence_id"),
                    sha256=_string(item["sha256"], "sha256"),
                )
            )
        record = cls(
            campaign_sha256=_string(raw["campaign_sha256"], "campaign_sha256"),
            memberships=tuple(memberships),
            amount=_parse_decimal(raw["amount"], "amount"),
            currency=_string(raw["currency"], "currency"),
            incurred_at=_parse_datetime(raw["incurred_at"], "incurred_at"),
            recorded_at=_parse_datetime(raw["recorded_at"], "recorded_at"),
            supersedes_expense_id=_optional_string(raw["supersedes_expense_id"], "supersedes_expense_id"),
        )
        if raw["expense_id"] != record.expense_id or raw["record_sha256"] != record.record_sha256:
            raise OwnerFixedExpenseAuthorityError("owner fixed-expense digest mismatch")
        return record


class OwnerFixedExpenseAuthority:
    """Durable explicit owner/admin truth for already-incurred campaign expenses.

    This is intentionally narrow. It cannot ingest provider invoices, model-compute
    charges, exchange commission, public prices, estimates, or arbitrary generic
    `IncurredMonetaryReceipt` objects. The only positive path is an explicit admin
    record bound to a live `FinalizedCampaignAuthority`; campaign membership and
    economic treatment are derived by this authority rather than caller-selected.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        authority_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self._authority = MonotonicWorkspaceAuthority(
            workspace=self.root,
            domain="autosport.owner_fixed_expense.v1",
            key="global",
            authority_root=authority_root,
        )

    def record_expense(
        self,
        *,
        campaign: FinalizedCampaignAuthority,
        amount: Decimal,
        currency: str,
        incurred_at: datetime,
        supersedes_expense_id: str | None = None,
    ) -> OwnerFixedExpenseRecord:
        if type(campaign) is not FinalizedCampaignAuthority:
            raise OwnerFixedExpenseAuthorityError("campaign must be FinalizedCampaignAuthority")
        projection = campaign.projection()
        recorded_at = _now_utc()
        candidate = OwnerFixedExpenseRecord(
            campaign_sha256=projection.campaign_sha256,
            memberships=projection.membership_refs,
            amount=amount,
            currency=currency,
            incurred_at=incurred_at,
            recorded_at=recorded_at,
            supersedes_expense_id=supersedes_expense_id,
        )
        records = self._load_records()
        by_id = {value.expense_id: value for value in records}
        if candidate.expense_id in by_id:
            return by_id[candidate.expense_id]
        if supersedes_expense_id is not None:
            prior = by_id.get(supersedes_expense_id)
            if prior is None:
                raise OwnerFixedExpenseAuthorityError("correction must supersede committed expense")
            if prior.campaign_sha256 != candidate.campaign_sha256:
                raise OwnerFixedExpenseAuthorityError("correction cannot move expense to another campaign")
            if prior.memberships != candidate.memberships:
                raise OwnerFixedExpenseAuthorityError("correction cannot rewrite campaign membership")
            if prior.currency != candidate.currency:
                raise OwnerFixedExpenseAuthorityError("correction cannot rewrite currency")
            if any(value.supersedes_expense_id == prior.expense_id for value in records):
                raise OwnerFixedExpenseAuthorityError("expense already has a correction successor")
        updated = tuple(sorted((*records, candidate), key=lambda value: value.expense_id))
        self._publish(updated)
        return candidate

    def resolve_cost_evidence(
        self,
        *,
        campaign: FinalizedCampaignAuthority,
        expense_id: str,
        record_sha256: str,
        as_of: datetime,
    ) -> CostEvidence:
        if type(campaign) is not FinalizedCampaignAuthority:
            raise CostEvidenceError("campaign must be FinalizedCampaignAuthority")
        _sha256(expense_id, "expense_id")
        _sha256(record_sha256, "record_sha256")
        _utc(as_of, "as_of")
        projection = campaign.projection()
        records = self._load_records()
        matches = [value for value in records if value.expense_id == expense_id]
        if len(matches) != 1:
            raise CostEvidenceError("owner fixed expense is not uniquely committed")
        record = matches[0]
        if record.record_sha256 != record_sha256:
            raise CostEvidenceError("owner fixed-expense digest mismatch")
        if record.campaign_sha256 != projection.campaign_sha256:
            raise CostEvidenceError("owner fixed expense belongs to another campaign")
        if record.memberships != projection.membership_refs:
            raise CostEvidenceError("campaign membership changed since expense was recorded")
        if record.recorded_at > as_of:
            raise CostEvidenceError("future owner fixed expense cannot be backdated")
        for successor in records:
            if successor.supersedes_expense_id == record.expense_id and successor.recorded_at <= as_of:
                raise CostEvidenceError("owner fixed expense was superseded before as_of")
        return CostEvidence(
            cost_class=CostClass.FIXED_CAMPAIGN,
            truth=CostTruth.KNOWN_ZERO if record.amount == 0 else CostTruth.KNOWN_AMOUNT,
            basis=CostBasis.OBSERVED_INCURRED,
            treatment=CostTreatment.SUBTRACT_FROM_GROSS,
            source=CostSourceRef(
                family=SOURCE_FAMILY,
                evidence_id=record.expense_id,
                sha256=record.record_sha256,
            ),
            campaign_sha256=record.campaign_sha256,
            memberships=record.memberships,
            unit=CostUnit.MONEY,
            currency=record.currency,
            amount=record.amount,
            observed_at=record.recorded_at,
            available_at=record.recorded_at,
            incurred_at=record.incurred_at,
            shared_source=False,
            allocation_source=None,
        )

    def verify(self) -> tuple[OwnerFixedExpenseRecord, ...]:
        return self._load_records()

    def _load_records(self) -> tuple[OwnerFixedExpenseRecord, ...]:
        path = self._state_path()
        if not path.exists():
            self._authority.recover(observed_state_sha256=None)
            return ()
        raw = _strict_json(path.read_bytes(), "owner fixed-expense state")
        _keys(raw, {"schema_version", "records", "state_sha256"}, "owner fixed-expense state")
        if raw["schema_version"] != SCHEMA_VERSION:
            raise OwnerFixedExpenseAuthorityError("unsupported owner fixed-expense state schema")
        records = tuple(
            OwnerFixedExpenseRecord.from_dict(_mapping(value, "record"))
            for value in _list(raw["records"], "records")
        )
        if tuple(sorted(records, key=lambda value: value.expense_id)) != records:
            raise OwnerFixedExpenseAuthorityError("owner fixed expenses are not canonically sorted")
        _validate_lineage(records)
        state_sha256 = _state_digest(records)
        if raw["state_sha256"] != state_sha256:
            raise OwnerFixedExpenseAuthorityError("owner fixed-expense state digest mismatch")
        self._authority.recover(
            observed_state_sha256=state_sha256,
            tx_id=state_sha256,
            semantic_binding_sha256=_binding(state_sha256),
        )
        return records

    def _publish(self, records: tuple[OwnerFixedExpenseRecord, ...]) -> None:
        _validate_lineage(records)
        previous_records = self._load_records()
        previous = None if not self._state_path().exists() else _state_digest(previous_records)
        intended = _state_digest(records)
        if previous == intended:
            return
        binding = _binding(intended)
        prepared = self._authority.prepare(
            tx_id=intended,
            observed_state_sha256=previous,
            intended_state_sha256=intended,
            semantic_binding_sha256=binding,
        )
        if prepared.phase is not AuthorityPhase.PREPARE or prepared.intended_state_sha256 != intended:
            raise OwnerFixedExpenseAuthorityError("owner fixed expense did not acquire exact PREPARE")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "records": [value.to_dict() for value in records],
            "state_sha256": intended,
        }
        _atomic_json(self._state_path(), payload)
        observed = _strict_json(self._state_path().read_bytes(), "owner fixed-expense state")
        if observed.get("state_sha256") != intended:
            raise OwnerFixedExpenseAuthorityError("owner fixed-expense durable re-read failed")
        self._authority.commit(
            tx_id=intended,
            observed_state_sha256=intended,
            semantic_binding_sha256=binding,
        )

    def _state_path(self) -> Path:
        return self.root / "owner_fixed_expense" / "state.json"


def _validate_lineage(records: Sequence[OwnerFixedExpenseRecord]) -> None:
    by_id = {value.expense_id: value for value in records}
    if len(by_id) != len(records):
        raise OwnerFixedExpenseAuthorityError("duplicate owner fixed-expense identity")
    successors: dict[str, str] = {}
    for value in records:
        predecessor = value.supersedes_expense_id
        if predecessor is None:
            continue
        prior = by_id.get(predecessor)
        if prior is None:
            raise OwnerFixedExpenseAuthorityError("expense correction predecessor is missing")
        if predecessor in successors:
            raise OwnerFixedExpenseAuthorityError("expense has multiple correction successors")
        if prior.campaign_sha256 != value.campaign_sha256 or prior.memberships != value.memberships:
            raise OwnerFixedExpenseAuthorityError("expense correction rewrites campaign applicability")
        if prior.currency != value.currency:
            raise OwnerFixedExpenseAuthorityError("expense correction rewrites currency")
        if value.recorded_at < prior.recorded_at:
            raise OwnerFixedExpenseAuthorityError("expense correction predates predecessor")
        successors[predecessor] = value.expense_id
    for start in by_id:
        seen: set[str] = set()
        cursor: str | None = start
        while cursor is not None:
            if cursor in seen:
                raise OwnerFixedExpenseAuthorityError("expense correction lineage contains a cycle")
            seen.add(cursor)
            cursor = successors.get(cursor)


def _state_digest(records: Sequence[OwnerFixedExpenseRecord]) -> str:
    return _digest({
        "schema_version": SCHEMA_VERSION,
        "records": [value.to_dict() for value in records],
    })


def _binding(state_sha256: str) -> str:
    return _digest({"domain": "autosport.owner_fixed_expense.v1", "state_sha256": state_sha256})


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OwnerFixedExpenseAuthorityError(f"{label} must be non-empty canonical text")


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise OwnerFixedExpenseAuthorityError(f"{label} must be lowercase SHA-256 hex")


def _utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OwnerFixedExpenseAuthorityError(f"{label} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise OwnerFixedExpenseAuthorityError(f"{label} must be UTC")


def _money(amount: Decimal, currency: str) -> None:
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
        raise OwnerFixedExpenseAuthorityError("amount must be a finite non-negative Decimal")
    if not isinstance(currency, str) or _CURRENCY_RE.fullmatch(currency) is None:
        raise OwnerFixedExpenseAuthorityError("currency must be uppercase three-letter ISO-style code")


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise OwnerFixedExpenseAuthorityError("decimal must be finite")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _parse_decimal(value: Any, label: str) -> Decimal:
    text = _string(value, label)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise OwnerFixedExpenseAuthorityError(f"{label} is not Decimal-compatible") from exc
    if _decimal_text(parsed) != text:
        raise OwnerFixedExpenseAuthorityError(f"{label} is not canonical Decimal text")
    return parsed


def _datetime_text(value: datetime) -> str:
    _utc(value, "datetime")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: Any, label: str) -> datetime:
    text = _string(value, label)
    if not text.endswith("Z"):
        raise OwnerFixedExpenseAuthorityError(f"{label} must use UTC Z notation")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise OwnerFixedExpenseAuthorityError(f"{label} is invalid") from exc
    if _datetime_text(parsed) != text:
        raise OwnerFixedExpenseAuthorityError(f"{label} is not canonical")
    return parsed


def _membership_payload(value: CanonicalMembershipRef) -> dict[str, str]:
    return {"kind": value.kind, "evidence_id": value.evidence_id, "sha256": value.sha256}


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(raw) != expected:
        raise OwnerFixedExpenseAuthorityError(f"{label} keys mismatch")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise OwnerFixedExpenseAuthorityError(f"{label} must be a string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise OwnerFixedExpenseAuthorityError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise OwnerFixedExpenseAuthorityError(f"{label} must be an array")
    return value


def _strict_json(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OwnerFixedExpenseAuthorityError(f"{label} is not UTF-8") from exc

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise OwnerFixedExpenseAuthorityError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise OwnerFixedExpenseAuthorityError(f"{label} contains non-standard number {value}")

    try:
        parsed = json.loads(text, object_pairs_hook=no_duplicates, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise OwnerFixedExpenseAuthorityError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise OwnerFixedExpenseAuthorityError(f"{label} must contain an object")
    return parsed


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write((_canonical_json(payload) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
