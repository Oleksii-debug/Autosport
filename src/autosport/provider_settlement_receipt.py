"""Immutable provider settlement evidence with explicit rule lineage.

This module is an integrity boundary for settlement/reconciliation evidence. It
binds one provider settlement result to the exact execution/market/selection,
provider evidence digest, settlement rule identity/version/digest, and UTC time.
It does not acquire provider data and does not prove that a provider accepted a
wager or moved real money.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping
from weakref import ReferenceType, ref


SCHEMA_VERSION = 1
SOURCE_FAMILY = "provider.settlement-receipt.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProviderSettlementReceiptError(ValueError):
    """Raised when settlement evidence is malformed or has ambiguous lineage."""


class SettlementDisposition(str, Enum):
    """Canonical terminal provider settlement outcomes."""

    WIN = "win"
    LOSS = "loss"
    VOID = "void"
    PUSH = "push"


def _receipt_payload_unchecked(receipt: Any) -> dict[str, Any]:
    """Canonical payload projection used only by the sealed integrity authority."""

    return {
        "schema_version": SCHEMA_VERSION,
        "source_family": SOURCE_FAMILY,
        "provider": receipt.provider,
        "provider_receipt_id": receipt.provider_receipt_id,
        "execution_id": receipt.execution_id,
        "market_id": receipt.market_id,
        "selection_id": receipt.selection_id,
        "disposition": receipt.disposition.value,
        "settled_at": _datetime_text(receipt.settled_at),
        "rule_id": receipt.rule_id,
        "rule_version": receipt.rule_version,
        "rule_sha256": receipt.rule_sha256,
        "provider_evidence_sha256": receipt.provider_evidence_sha256,
        "revision": receipt.revision,
        "supersedes_receipt_sha256": receipt.supersedes_receipt_sha256,
    }


def _build_receipt_integrity_seal():
    issued: dict[int, tuple[ReferenceType[Any], str]] = {}

    def seal(receipt: Any) -> None:
        receipt_id = id(receipt)
        payload_sha256 = _digest(_receipt_payload_unchecked(receipt))

        def forget(_weakref: object, *, key: int = receipt_id) -> None:
            issued.pop(key, None)

        issued[receipt_id] = (ref(receipt, forget), payload_sha256)

    def assert_sealed(receipt: Any) -> str:
        record = issued.get(id(receipt))
        if record is None or record[0]() is not receipt:
            raise ProviderSettlementReceiptError(
                "settlement receipt was not sealed by canonical construction"
            )
        try:
            current_sha256 = _digest(_receipt_payload_unchecked(receipt))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProviderSettlementReceiptError(
                "settlement receipt changed after canonical construction"
            ) from exc
        if current_sha256 != record[1]:
            raise ProviderSettlementReceiptError(
                "settlement receipt changed after canonical construction"
            )
        return record[1]

    return seal, assert_sealed


_seal_receipt, _assert_receipt_sealed = _build_receipt_integrity_seal()


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ProviderSettlementReceipt:
    """Canonical immutable provider settlement evidence.

    ``provider_evidence_sha256`` binds this normalized receipt to the provider
    evidence bytes held by the acquisition layer. ``rule_sha256`` separately
    binds the interpretation to the exact settlement rule artifact. Revisions
    form a hash-linked chain so provider corrections cannot silently overwrite
    an earlier settlement.
    """

    provider: str
    provider_receipt_id: str
    execution_id: str
    market_id: str
    selection_id: str
    disposition: SettlementDisposition
    settled_at: datetime
    rule_id: str
    rule_version: str
    rule_sha256: str
    provider_evidence_sha256: str
    revision: int = 0
    supersedes_receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("provider", self.provider),
            ("provider_receipt_id", self.provider_receipt_id),
            ("execution_id", self.execution_id),
            ("market_id", self.market_id),
            ("selection_id", self.selection_id),
            ("rule_id", self.rule_id),
            ("rule_version", self.rule_version),
        ):
            _text(value, label)
        if type(self.disposition) is not SettlementDisposition:
            raise ProviderSettlementReceiptError(
                "disposition must be exact SettlementDisposition"
            )
        _utc(self.settled_at, "settled_at")
        _sha256(self.rule_sha256, "rule_sha256")
        _sha256(self.provider_evidence_sha256, "provider_evidence_sha256")
        if type(self.revision) is not int or self.revision < 0:
            raise ProviderSettlementReceiptError(
                "revision must be a non-negative integer"
            )
        if self.revision == 0:
            if self.supersedes_receipt_sha256 is not None:
                raise ProviderSettlementReceiptError(
                    "initial settlement revision cannot supersede a receipt"
                )
        else:
            if self.supersedes_receipt_sha256 is None:
                raise ProviderSettlementReceiptError(
                    "settlement revision requires predecessor receipt digest"
                )
            _sha256(
                self.supersedes_receipt_sha256,
                "supersedes_receipt_sha256",
            )
        _seal_receipt(self)

    @property
    def receipt_sha256(self) -> str:
        return _assert_receipt_sealed(self)

    @property
    def record_sha256(self) -> str:
        return self.receipt_sha256

    def payload(self) -> dict[str, Any]:
        _assert_receipt_sealed(self)
        return _receipt_payload_unchecked(self)

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["receipt_sha256"] = self.receipt_sha256
        raw["record_sha256"] = self.record_sha256
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProviderSettlementReceipt":
        _keys(
            raw,
            {
                "schema_version",
                "source_family",
                "provider",
                "provider_receipt_id",
                "execution_id",
                "market_id",
                "selection_id",
                "disposition",
                "settled_at",
                "rule_id",
                "rule_version",
                "rule_sha256",
                "provider_evidence_sha256",
                "revision",
                "supersedes_receipt_sha256",
                "receipt_sha256",
                "record_sha256",
            },
        )
        schema_version = raw["schema_version"]
        source_family = _string(raw["source_family"], "source_family")
        if (
            type(schema_version) is not int
            or schema_version != SCHEMA_VERSION
            or source_family != SOURCE_FAMILY
        ):
            raise ProviderSettlementReceiptError("unsupported settlement receipt")
        disposition_raw = _string(raw["disposition"], "disposition")
        try:
            disposition = SettlementDisposition(disposition_raw)
        except ValueError as exc:
            raise ProviderSettlementReceiptError(
                "unsupported settlement disposition"
            ) from exc
        revision = raw["revision"]
        if type(revision) is not int:
            raise ProviderSettlementReceiptError("revision must be an integer")
        predecessor = raw["supersedes_receipt_sha256"]
        if predecessor is not None:
            predecessor = _string(predecessor, "supersedes_receipt_sha256")
        item = cls(
            provider=_string(raw["provider"], "provider"),
            provider_receipt_id=_string(
                raw["provider_receipt_id"], "provider_receipt_id"
            ),
            execution_id=_string(raw["execution_id"], "execution_id"),
            market_id=_string(raw["market_id"], "market_id"),
            selection_id=_string(raw["selection_id"], "selection_id"),
            disposition=disposition,
            settled_at=_parse_datetime(raw["settled_at"]),
            rule_id=_string(raw["rule_id"], "rule_id"),
            rule_version=_string(raw["rule_version"], "rule_version"),
            rule_sha256=_string(raw["rule_sha256"], "rule_sha256"),
            provider_evidence_sha256=_string(
                raw["provider_evidence_sha256"], "provider_evidence_sha256"
            ),
            revision=revision,
            supersedes_receipt_sha256=predecessor,
        )
        if (
            raw["receipt_sha256"] != item.receipt_sha256
            or raw["record_sha256"] != item.record_sha256
        ):
            raise ProviderSettlementReceiptError("settlement receipt digest mismatch")
        return item


def verify_settlement_revision(
    previous: ProviderSettlementReceipt,
    current: ProviderSettlementReceipt,
) -> None:
    """Fail closed unless ``current`` is the immediate correction to ``previous``."""

    if (
        type(previous) is not ProviderSettlementReceipt
        or type(current) is not ProviderSettlementReceipt
    ):
        raise ProviderSettlementReceiptError(
            "revision verification requires canonical settlement receipts"
        )
    previous_sha256 = previous.receipt_sha256
    _ = current.receipt_sha256
    for label in (
        "provider",
        "provider_receipt_id",
        "execution_id",
        "market_id",
        "selection_id",
    ):
        if getattr(previous, label) != getattr(current, label):
            raise ProviderSettlementReceiptError(
                f"settlement revision changed immutable identity: {label}"
            )
    if current.revision != previous.revision + 1:
        raise ProviderSettlementReceiptError(
            "settlement revision must advance by exactly one"
        )
    if current.supersedes_receipt_sha256 != previous_sha256:
        raise ProviderSettlementReceiptError(
            "settlement revision predecessor digest mismatch"
        )
    if current.settled_at < previous.settled_at:
        raise ProviderSettlementReceiptError(
            "settlement revision cannot move settlement time backwards"
        )
    if current.disposition is not previous.disposition:
        provider_basis_changed = (
            current.provider_evidence_sha256 != previous.provider_evidence_sha256
        )
        # Rule labels/version are descriptive identity; only a different
        # rule artifact digest is new causal rule evidence for a changed outcome.
        rule_basis_changed = current.rule_sha256 != previous.rule_sha256
        if not provider_basis_changed and not rule_basis_changed:
            raise ProviderSettlementReceiptError(
                "semantic settlement correction requires changed provider evidence "
                "or settlement rule basis"
            )


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderSettlementReceiptError(
            f"{label} must be a non-empty canonical string"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProviderSettlementReceiptError(f"{label} contains control characters")
    return value


def _string(value: object, label: str) -> str:
    return _text(value, label)


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ProviderSettlementReceiptError(
            f"{label} must be lowercase SHA-256 hex"
        )
    return text


def _utc(value: object, label: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ProviderSettlementReceiptError(f"{label} must be timezone-aware UTC")
    return value


def _datetime_text(value: datetime) -> str:
    _utc(value, "datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_datetime(value: object) -> datetime:
    text = _string(value, "settled_at")
    if not text.endswith("Z"):
        raise ProviderSettlementReceiptError(
            "settled_at must use canonical UTC Z representation"
        )
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ProviderSettlementReceiptError("invalid settled_at timestamp") from exc
    _utc(parsed, "settled_at")
    if _datetime_text(parsed) != text:
        raise ProviderSettlementReceiptError(
            "settled_at must use canonical microsecond UTC representation"
        )
    return parsed


def _keys(raw: Mapping[str, Any], expected: set[str]) -> None:
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ProviderSettlementReceiptError("settlement receipt keys mismatch")


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
