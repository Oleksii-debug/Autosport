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


def _text(
    value: object,
    label: str,
    *,
    _error_type=ProviderSettlementReceiptError,
    _str_type=str,
    _type=type,
    _ord=ord,
    _any=any,
) -> str:
    if (
        _type(value) is not _str_type
        or not value
        or value != value.strip()
    ):
        raise _error_type(
            f"{label} must be a non-empty canonical string"
        )
    if _any(_ord(char) < 32 or _ord(char) == 127 for char in value):
        raise _error_type(f"{label} contains control characters")
    return value


def _string(
    value: object,
    label: str,
    *,
    _text_impl=_text,
) -> str:
    return _text_impl(value, label)


def _canonical_disposition_value(
    value: object,
    *,
    _bindings=(
        (SettlementDisposition.WIN, "win"),
        (SettlementDisposition.LOSS, "loss"),
        (SettlementDisposition.VOID, "void"),
        (SettlementDisposition.PUSH, "push"),
    ),
    _error_type=ProviderSettlementReceiptError,
) -> str:
    for member, canonical_value in _bindings:
        if member.value != canonical_value:
            raise _error_type(
                "settlement disposition canonical member state changed"
            )
        if value is member:
            return canonical_value
    raise _error_type("disposition must be exact SettlementDisposition")


def _parse_canonical_disposition(
    value: object,
    *,
    _string_impl=_string,
    _members=(
        SettlementDisposition.WIN,
        SettlementDisposition.LOSS,
        SettlementDisposition.VOID,
        SettlementDisposition.PUSH,
    ),
    _value_impl=_canonical_disposition_value,
    _error_type=ProviderSettlementReceiptError,
) -> SettlementDisposition:
    text = _string_impl(value, "disposition")
    for member in _members:
        if _value_impl(member) == text:
            return member
    raise _error_type("unsupported settlement disposition")


def _sha256(
    value: object,
    label: str,
    *,
    _text_impl=_text,
    _sha256_re=_SHA256_RE,
    _error_type=ProviderSettlementReceiptError,
) -> str:
    text = _text_impl(value, label)
    if _sha256_re.fullmatch(text) is None:
        raise _error_type(
            f"{label} must be lowercase SHA-256 hex"
        )
    return text


def _utc(
    value: object,
    label: str,
    *,
    _datetime_type=datetime,
    _timedelta_type=timedelta,
    _error_type=ProviderSettlementReceiptError,
    _type=type,
) -> datetime:
    if (
        _type(value) is not _datetime_type
        or value.tzinfo is None
        or value.utcoffset() != _timedelta_type(0)
    ):
        raise _error_type(f"{label} must be timezone-aware UTC")
    return value


def _datetime_text(
    value: datetime,
    *,
    _utc_impl=_utc,
    _timezone_utc=timezone.utc,
) -> str:
    _utc_impl(value, "datetime")
    return value.astimezone(_timezone_utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _parse_datetime(
    value: object,
    *,
    _string_impl=_string,
    _datetime_type=datetime,
    _utc_impl=_utc,
    _datetime_text_impl=_datetime_text,
    _error_type=ProviderSettlementReceiptError,
) -> datetime:
    text = _string_impl(value, "settled_at")
    if not text.endswith("Z"):
        raise _error_type(
            "settled_at must use canonical UTC Z representation"
        )
    try:
        parsed = _datetime_type.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise _error_type("invalid settled_at timestamp") from exc
    _utc_impl(parsed, "settled_at")
    if _datetime_text_impl(parsed) != text:
        raise _error_type(
            "settled_at must use canonical microsecond UTC representation"
        )
    return parsed


def _keys(
    raw: Mapping[str, Any],
    expected: set[str],
    *,
    _mapping_type=Mapping,
    _set_type=set,
    _isinstance=isinstance,
    _error_type=ProviderSettlementReceiptError,
) -> None:
    if (
        not _isinstance(raw, _mapping_type)
        or _set_type(raw) != expected
    ):
        raise _error_type("settlement receipt keys mismatch")


def _digest(
    payload: Mapping[str, Any],
    *,
    _json_dumps=json.dumps,
    _sha256_constructor=hashlib.sha256,
) -> str:
    encoded = _json_dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _sha256_constructor(encoded).hexdigest()


def _receipt_payload_unchecked(
    receipt: Any,
    *,
    _schema_version=SCHEMA_VERSION,
    _source_family=SOURCE_FAMILY,
    _datetime_text_impl=_datetime_text,
    _disposition_value_impl=_canonical_disposition_value,
) -> dict[str, Any]:
    """Canonical payload projection used only by the sealed integrity authority."""

    return {
        "schema_version": _schema_version,
        "source_family": _source_family,
        "provider": receipt.provider,
        "provider_receipt_id": receipt.provider_receipt_id,
        "execution_id": receipt.execution_id,
        "market_id": receipt.market_id,
        "selection_id": receipt.selection_id,
        "disposition": _disposition_value_impl(receipt.disposition),
        "settled_at": _datetime_text_impl(receipt.settled_at),
        "rule_id": receipt.rule_id,
        "rule_version": receipt.rule_version,
        "rule_sha256": receipt.rule_sha256,
        "provider_evidence_sha256": receipt.provider_evidence_sha256,
        "revision": receipt.revision,
        "supersedes_receipt_sha256": receipt.supersedes_receipt_sha256,
    }


def _build_receipt_integrity_seal(
    *,
    _digest_impl=_digest,
    _payload_impl=_receipt_payload_unchecked,
    _ref_impl=ref,
    _error_type=ProviderSettlementReceiptError,
    _id=id,
):
    issued: dict[int, tuple[ReferenceType[Any], str]] = {}

    def seal(receipt: Any) -> None:
        receipt_id = _id(receipt)
        payload_sha256 = _digest_impl(_payload_impl(receipt))

        def forget(_weakref: object, *, key: int = receipt_id) -> None:
            issued.pop(key, None)

        issued[receipt_id] = (
            _ref_impl(receipt, forget),
            payload_sha256,
        )

    def assert_sealed(receipt: Any) -> str:
        record = issued.get(_id(receipt))
        if record is None or record[0]() is not receipt:
            raise _error_type(
                "settlement receipt was not sealed by canonical construction"
            )
        try:
            current_sha256 = _digest_impl(_payload_impl(receipt))
        except (AttributeError, TypeError, ValueError) as exc:
            raise _error_type(
                "settlement receipt changed after canonical construction"
            ) from exc
        if current_sha256 != record[1]:
            raise _error_type(
                "settlement receipt changed after canonical construction"
            )
        return record[1]

    return seal, assert_sealed


_seal_receipt, _assert_receipt_sealed = _build_receipt_integrity_seal()
del _build_receipt_integrity_seal


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ProviderSettlementReceipt:
    """Canonical immutable provider settlement evidence.

    provider_evidence_sha256 binds this normalized receipt to provider evidence
    bytes held by the acquisition layer. rule_sha256 separately binds the
    interpretation to the exact settlement rule artifact. Revisions form a
    hash-linked chain so provider corrections cannot silently overwrite an
    earlier settlement.
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

    def __post_init__(
        self,
        _text_impl=_text,
        _disposition_type=SettlementDisposition,
        _disposition_value_impl=_canonical_disposition_value,
        _utc_impl=_utc,
        _sha256_impl=_sha256,
        _seal_impl=_seal_receipt,
        _error_type=ProviderSettlementReceiptError,
        _int_type=int,
        _type=type,
    ) -> None:
        for label, value in (
            ("provider", self.provider),
            ("provider_receipt_id", self.provider_receipt_id),
            ("execution_id", self.execution_id),
            ("market_id", self.market_id),
            ("selection_id", self.selection_id),
            ("rule_id", self.rule_id),
            ("rule_version", self.rule_version),
        ):
            _text_impl(value, label)
        if _type(self.disposition) is not _disposition_type:
            raise _error_type(
                "disposition must be exact SettlementDisposition"
            )
        _disposition_value_impl(self.disposition)
        _utc_impl(self.settled_at, "settled_at")
        _sha256_impl(self.rule_sha256, "rule_sha256")
        _sha256_impl(
            self.provider_evidence_sha256,
            "provider_evidence_sha256",
        )
        if (
            _type(self.revision) is not _int_type
            or self.revision < 0
        ):
            raise _error_type(
                "revision must be a non-negative integer"
            )
        if self.revision == 0:
            if self.supersedes_receipt_sha256 is not None:
                raise _error_type(
                    "initial settlement revision cannot supersede a receipt"
                )
        else:
            if self.supersedes_receipt_sha256 is None:
                raise _error_type(
                    "settlement revision requires predecessor receipt digest"
                )
            _sha256_impl(
                self.supersedes_receipt_sha256,
                "supersedes_receipt_sha256",
            )
        _seal_impl(self)

    @property
    def receipt_sha256(
        self,
        _assert_impl=_assert_receipt_sealed,
    ) -> str:
        return _assert_impl(self)

    @property
    def record_sha256(
        self,
        _assert_impl=_assert_receipt_sealed,
    ) -> str:
        return _assert_impl(self)

    def payload(
        self,
        _assert_impl=_assert_receipt_sealed,
        _payload_impl=_receipt_payload_unchecked,
    ) -> dict[str, Any]:
        _assert_impl(self)
        return _payload_impl(self)

    def to_dict(
        self,
        _assert_impl=_assert_receipt_sealed,
        _payload_impl=_receipt_payload_unchecked,
    ) -> dict[str, Any]:
        receipt_sha256 = _assert_impl(self)
        raw = _payload_impl(self)
        raw["receipt_sha256"] = receipt_sha256
        raw["record_sha256"] = receipt_sha256
        return raw


def _build_receipt_from_dict(
    *,
    _receipt_type=ProviderSettlementReceipt,
    _keys_impl=_keys,
    _string_impl=_string,
    _parse_datetime_impl=_parse_datetime,
    _parse_disposition_impl=_parse_canonical_disposition,
    _schema_version=SCHEMA_VERSION,
    _source_family=SOURCE_FAMILY,
    _error_type=ProviderSettlementReceiptError,
    _int_type=int,
    _type=type,
):
    expected_keys = {
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
    }

    def from_dict(
        cls,
        raw: Mapping[str, Any],
    ) -> ProviderSettlementReceipt:
        if cls is not _receipt_type:
            raise _error_type(
                "settlement receipt parser requires canonical receipt type"
            )
        _keys_impl(raw, expected_keys)
        schema_version = raw["schema_version"]
        source_family = _string_impl(
            raw["source_family"],
            "source_family",
        )
        if (
            _type(schema_version) is not _int_type
            or schema_version != _schema_version
            or source_family != _source_family
        ):
            raise _error_type("unsupported settlement receipt")
        disposition_raw = _string_impl(
            raw["disposition"],
            "disposition",
        )
        disposition = _parse_disposition_impl(disposition_raw)
        revision = raw["revision"]
        if _type(revision) is not _int_type:
            raise _error_type("revision must be an integer")
        predecessor = raw["supersedes_receipt_sha256"]
        if predecessor is not None:
            predecessor = _string_impl(
                predecessor,
                "supersedes_receipt_sha256",
            )
        item = _receipt_type(
            provider=_string_impl(raw["provider"], "provider"),
            provider_receipt_id=_string_impl(
                raw["provider_receipt_id"],
                "provider_receipt_id",
            ),
            execution_id=_string_impl(
                raw["execution_id"],
                "execution_id",
            ),
            market_id=_string_impl(raw["market_id"], "market_id"),
            selection_id=_string_impl(
                raw["selection_id"],
                "selection_id",
            ),
            disposition=disposition,
            settled_at=_parse_datetime_impl(raw["settled_at"]),
            rule_id=_string_impl(raw["rule_id"], "rule_id"),
            rule_version=_string_impl(
                raw["rule_version"],
                "rule_version",
            ),
            rule_sha256=_string_impl(
                raw["rule_sha256"],
                "rule_sha256",
            ),
            provider_evidence_sha256=_string_impl(
                raw["provider_evidence_sha256"],
                "provider_evidence_sha256",
            ),
            revision=revision,
            supersedes_receipt_sha256=predecessor,
        )
        receipt_sha256 = item.receipt_sha256
        if (
            raw["receipt_sha256"] != receipt_sha256
            or raw["record_sha256"] != receipt_sha256
        ):
            raise _error_type("settlement receipt digest mismatch")
        return item

    return classmethod(from_dict)


ProviderSettlementReceipt.from_dict = _build_receipt_from_dict()
del _build_receipt_from_dict


def verify_settlement_revision(
    previous: ProviderSettlementReceipt,
    current: ProviderSettlementReceipt,
    *,
    _receipt_type=ProviderSettlementReceipt,
    _error_type=ProviderSettlementReceiptError,
    _type=type,
    _getattr=getattr,
) -> None:
    """Fail closed unless current is the immediate correction to previous."""

    if (
        _type(previous) is not _receipt_type
        or _type(current) is not _receipt_type
    ):
        raise _error_type(
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
        if _getattr(previous, label) != _getattr(current, label):
            raise _error_type(
                f"settlement revision changed immutable identity: {label}"
            )
    if current.revision != previous.revision + 1:
        raise _error_type(
            "settlement revision must advance by exactly one"
        )
    if current.supersedes_receipt_sha256 != previous_sha256:
        raise _error_type(
            "settlement revision predecessor digest mismatch"
        )
    if current.settled_at < previous.settled_at:
        raise _error_type(
            "settlement revision cannot move settlement time backwards"
        )
    if current.disposition is not previous.disposition:
        provider_basis_changed = (
            current.provider_evidence_sha256
            != previous.provider_evidence_sha256
        )
        rule_basis_changed = (
            current.rule_sha256 != previous.rule_sha256
        )
        if not provider_basis_changed and not rule_basis_changed:
            raise _error_type(
                "semantic settlement correction requires changed provider evidence "
                "or settlement rule basis"
            )

