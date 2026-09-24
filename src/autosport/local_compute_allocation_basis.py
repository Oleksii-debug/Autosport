"""Owner-reviewed, durable allocation basis for LOCAL model-compute money.

This authority deliberately separates evidence acquisition/review from tariff publication.
A basis contains the exact measurement document bytes the owner reviewed, product-clock
availability, current EconomicGoal identity, and a mechanically derived per-request
amount.  No caller-supplied evidence digest or availability timestamp is accepted.

This module does not itself prove that a real invoice/energy measurement has been
provided.  Positive economic use requires a real reviewed document plus downstream
re-resolution of the durable basis.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Final, Mapping

from .economic_goal_store import EconomicGoalStore, economic_goal_to_payload
from .integrity import atomic_write_json, sha256_file
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import AuthorityPhase, MonotonicWorkspaceAuthority
from .workspace_lock import WorkspaceEconomicLock


SCHEMA: Final = "autosport.local_compute_allocation_basis"
SCHEMA_VERSION: Final = 1
FILE_NAME: Final = "local-compute-allocation-bases.json"
AUTHORITY_DOMAIN: Final = "autosport.local-compute-allocation-basis.v1"
AUTHORITY_KEY: Final = "owner-reviewed-local-compute-allocation-bases"
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE: Final = re.compile(r"^[A-Z]{3}$")
_MAX_INTEGER_DIGITS: Final = 24
_MAX_FRACTIONAL_DIGITS: Final = 18
_MAX_DENOMINATOR: Final = 1_000_000_000
_MAX_MEASUREMENT_DOCUMENT_BYTES: Final = 262_144

# Owner-goal authority must remain anchored to the import-time durable store
# contract. Module globals are writable and therefore cannot decide which goal
# store or serializer grants positive monetary basis authority.
_CANONICAL_ECONOMIC_GOAL_STORE_CLASS: Final = EconomicGoalStore
_CANONICAL_ECONOMIC_GOAL_STORE_LOAD: Final = EconomicGoalStore.load
_CANONICAL_ECONOMIC_GOAL_TO_PAYLOAD: Final = economic_goal_to_payload


class LocalComputeAllocationBasisError(ValueError):
    """The reviewed allocation basis or durable state is malformed."""


def _build_local_compute_monotonic_authority_root():
    """Freeze the shared LOCAL-compute machine-state root at import time."""

    os_name = os.name
    path_type = Path
    error_type = LocalComputeAllocationBasisError

    if os_name == "nt":
        try:
            import ctypes

            create_unicode_buffer = ctypes.create_unicode_buffer
            get_folder_path = (
                ctypes.windll.shell32.SHGetFolderPathW  # type: ignore[attr-defined]
            )
        except (AttributeError, ImportError) as exc:
            raise error_type(
                "cannot resolve product-owned Windows authority root"
            ) from exc

        def resolve() -> Path:
            try:
                buffer = create_unicode_buffer(32768)
                result = get_folder_path(
                    None,
                    0x001C,  # CSIDL_LOCAL_APPDATA
                    None,
                    0,
                    buffer,
                )
            except (AttributeError, OSError, ValueError) as exc:
                raise error_type(
                    "cannot resolve product-owned Windows authority root"
                ) from exc
            if result != 0 or not buffer.value:
                raise error_type(
                    "cannot resolve product-owned Windows authority root"
                )
            base = path_type(buffer.value)
            relative = (
                path_type("Autosport")
                / "application-state"
                / "monotonic-authority-v1"
            )
            if not base.is_absolute():
                raise error_type(
                    "product-owned monotonic authority root must be absolute"
                )
            return base / relative

        return resolve

    try:
        import pwd

        getuid = os.getuid
        getpwuid = pwd.getpwuid
    except (AttributeError, ImportError) as exc:
        raise error_type(
            "cannot resolve product-owned POSIX authority root"
        ) from exc

    def resolve() -> Path:
        try:
            home = getpwuid(getuid()).pw_dir
        except (KeyError, OSError) as exc:
            raise error_type(
                "cannot resolve product-owned POSIX authority root"
            ) from exc
        base = path_type(home) / ".local" / "state"
        relative = path_type("autosport") / "monotonic-authority-v1"
        if not base.is_absolute():
            raise error_type(
                "product-owned monotonic authority root must be absolute"
            )
        return base / relative

    return resolve


local_compute_monotonic_authority_root = (
    _build_local_compute_monotonic_authority_root()
)


def _text(value: object, field: str, *, limit: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > limit
    ):
        raise LocalComputeAllocationBasisError(
            f"{field} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise LocalComputeAllocationBasisError(
            f"{field} must be valid UTF-8"
        ) from exc
    return value


def _sha(value: object, field: str) -> str:
    value = _text(value, field, limit=64)
    if _SHA256_RE.fullmatch(value) is None:
        raise LocalComputeAllocationBasisError(
            f"{field} must be lowercase SHA-256"
        )
    return value


def _currency(value: object) -> str:
    value = _text(value, "currency", limit=3)
    if _CURRENCY_RE.fullmatch(value) is None:
        raise LocalComputeAllocationBasisError(
            "currency must be uppercase three-letter code"
        )
    return value


def _instant(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LocalComputeAllocationBasisError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LocalComputeAllocationBasisError(
            f"{field} must include timezone"
        )
    return parsed.astimezone(timezone.utc)


def _time(value: object, field: str) -> str:
    return _instant(value, field).isoformat().replace("+00:00", "Z")


def _money(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise LocalComputeAllocationBasisError(
            f"{field} must be a finite non-negative exact Decimal"
        )
    _sign, digits, exponent = value.as_tuple()
    integer_digits = max(1, len(digits) + exponent)
    fractional_digits = max(0, -exponent)
    if (
        integer_digits > _MAX_INTEGER_DIGITS
        or fractional_digits > _MAX_FRACTIONAL_DIGITS
    ):
        raise LocalComputeAllocationBasisError(
            f"{field} exceeds supported exact monetary precision"
        )
    return value


def _money_text(value: Decimal, field: str) -> str:
    value = _money(value, field)
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _money_from_text(value: object, field: str) -> Decimal:
    raw = _text(value, field, limit=80)
    if "e" in raw.lower():
        raise LocalComputeAllocationBasisError(
            f"{field} must use canonical fixed-point Decimal text"
        )
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise LocalComputeAllocationBasisError(
            f"{field} is invalid Decimal text"
        ) from exc
    if _money_text(parsed, field) != raw:
        raise LocalComputeAllocationBasisError(
            f"{field} must use canonical fixed-point Decimal text"
        )
    return parsed


def _positive_denominator(value: object) -> int:
    if (
        type(value) is not int
        or value < 1
        or value > _MAX_DENOMINATOR
    ):
        raise LocalComputeAllocationBasisError(
            f"request_denominator must be integer 1..{_MAX_DENOMINATOR}"
        )
    return value


def _derive_per_request(total: Decimal, denominator: int) -> Decimal:
    total = _money(total, "total_allocable_cost")
    denominator = _positive_denominator(denominator)
    if total.is_zero():
        return Decimal("0")

    sign, digits, exponent = total.as_tuple()
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    if sign:
        coefficient = -coefficient
    if exponent >= 0:
        numerator = coefficient * (10 ** exponent)
        rational_denominator = denominator
    else:
        numerator = coefficient
        rational_denominator = denominator * (10 ** (-exponent))
    divisor = math.gcd(abs(numerator), rational_denominator)
    remaining = rational_denominator // divisor
    while remaining % 2 == 0:
        remaining //= 2
    while remaining % 5 == 0:
        remaining //= 5
    if remaining != 1:
        raise LocalComputeAllocationBasisError(
            "total_allocable_cost/request_denominator has no exact finite Decimal representation"
        )

    with localcontext() as context:
        context.prec = 96
        result = total / Decimal(denominator)
        if result * Decimal(denominator) != total:
            raise LocalComputeAllocationBasisError(
                "per-request allocation is not exact"
            )
    return _money(result, "amount_per_request")


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
        raise LocalComputeAllocationBasisError(
            "allocation basis is outside canonical JSON domain"
        ) from exc


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
        raise LocalComputeAllocationBasisError(
            "allocation basis is outside canonical JSON domain"
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def _document_bytes(value: object) -> bytes:
    if type(value) is not bytes:
        raise LocalComputeAllocationBasisError(
            "measurement_document must be exact bytes"
        )
    if not value or len(value) > _MAX_MEASUREMENT_DOCUMENT_BYTES:
        raise LocalComputeAllocationBasisError(
            "measurement_document size is outside supported bounds"
        )
    return value


def _document_b64(value: bytes) -> str:
    return base64.b64encode(
        _document_bytes(value)
    ).decode("ascii")


def _decode_document(value: object) -> bytes:
    raw = _text(
        value,
        "measurement_document_b64",
        limit=((_MAX_MEASUREMENT_DOCUMENT_BYTES + 2) // 3) * 4,
    )
    try:
        decoded = base64.b64decode(raw.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise LocalComputeAllocationBasisError(
            "measurement_document_b64 must be canonical base64"
        ) from exc
    decoded = _document_bytes(decoded)
    if _document_b64(decoded) != raw:
        raise LocalComputeAllocationBasisError(
            "measurement_document_b64 must be canonical base64"
        )
    return decoded


def _goal_sha256(goal: object) -> str:
    return _digest(
        _CANONICAL_ECONOMIC_GOAL_TO_PAYLOAD(goal)  # type: ignore[arg-type]
    )


def _authoritative_utc_now() -> str:
    """Read product-owned UTC wall time for causal publication authority."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_CANONICAL_AUTHORITY_NOW: Final = _authoritative_utc_now
_CANONICAL_AUTHORITY_NOW_CODE: Final = _CANONICAL_AUTHORITY_NOW.__code__
_CANONICAL_AUTHORITY_NOW_DATETIME: Final = datetime
_CANONICAL_AUTHORITY_NOW_TIMEZONE: Final = timezone


@dataclass(frozen=True, slots=True)
class LocalComputeAllocationBasisReview:
    """Exact values and evidence bytes presented for owner confirmation."""

    basis_id: str
    backend_id: str
    model_id: str
    config_sha256: str
    allocation_policy_id: str
    measurement_source_id: str
    measurement_period_start: str
    measurement_period_end: str
    total_allocable_cost: Decimal
    request_denominator: int
    measurement_document_b64: str
    measurement_document_sha256: str
    currency: str
    owner_goal_id: str
    owner_goal_revision: int
    owner_bankroll_id: str
    owner_goal_sha256: str

    def __post_init__(self) -> None:
        _text(self.basis_id, "basis_id")
        _text(self.backend_id, "backend_id")
        _text(self.model_id, "model_id")
        _sha(self.config_sha256, "config_sha256")
        _text(self.allocation_policy_id, "allocation_policy_id")
        _text(self.measurement_source_id, "measurement_source_id")
        start = _instant(
            self.measurement_period_start,
            "measurement_period_start",
        )
        end = _instant(
            self.measurement_period_end,
            "measurement_period_end",
        )
        if end <= start:
            raise LocalComputeAllocationBasisError(
                "measurement_period_end must be after measurement_period_start"
            )
        _money(self.total_allocable_cost, "total_allocable_cost")
        _positive_denominator(self.request_denominator)
        document = _decode_document(self.measurement_document_b64)
        if hashlib.sha256(document).hexdigest() != _sha(
            self.measurement_document_sha256,
            "measurement_document_sha256",
        ):
            raise LocalComputeAllocationBasisError(
                "measurement document digest mismatch"
            )
        _currency(self.currency)
        _text(self.owner_goal_id, "owner_goal_id")
        if (
            type(self.owner_goal_revision) is not int
            or isinstance(self.owner_goal_revision, bool)
            or self.owner_goal_revision < 1
        ):
            raise LocalComputeAllocationBasisError(
                "owner_goal_revision must be a positive integer"
            )
        _text(self.owner_bankroll_id, "owner_bankroll_id")
        _sha(self.owner_goal_sha256, "owner_goal_sha256")
        _derive_per_request(
            self.total_allocable_cost,
            self.request_denominator,
        )

    @property
    def amount_per_request(self) -> Decimal:
        return _derive_per_request(
            self.total_allocable_cost,
            self.request_denominator,
        )

    def payload(self) -> dict[str, object]:
        return {
            "basis_id": self.basis_id,
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "config_sha256": self.config_sha256,
            "allocation_policy_id": self.allocation_policy_id,
            "measurement_source_id": self.measurement_source_id,
            "measurement_period_start": _time(
                self.measurement_period_start,
                "measurement_period_start",
            ),
            "measurement_period_end": _time(
                self.measurement_period_end,
                "measurement_period_end",
            ),
            "total_allocable_cost": _money_text(
                self.total_allocable_cost,
                "total_allocable_cost",
            ),
            "request_denominator": self.request_denominator,
            "amount_per_request": _money_text(
                self.amount_per_request,
                "amount_per_request",
            ),
            "denominator_unit": "request",
            "measurement_document_b64": self.measurement_document_b64,
            "measurement_document_sha256": self.measurement_document_sha256,
            "currency": self.currency,
            "owner_goal_id": self.owner_goal_id,
            "owner_goal_revision": self.owner_goal_revision,
            "owner_bankroll_id": self.owner_bankroll_id,
            "owner_goal_sha256": self.owner_goal_sha256,
        }

    @property
    def review_sha256(self) -> str:
        return _digest(self.payload())


def _prepare_owner_review(
    *,
    basis_id: str,
    backend_id: str,
    model_id: str,
    config_sha256: str,
    allocation_policy_id: str,
    measurement_source_id: str,
    measurement_period_start: str,
    measurement_period_end: str,
    total_allocable_cost: Decimal,
    request_denominator: int,
    measurement_document: bytes,
    currency: str,
    owner_goal_id: str,
    owner_goal_revision: int,
    owner_bankroll_id: str,
    owner_goal_sha256: str,
) -> LocalComputeAllocationBasisReview:
    """Build a snapshot whose denomination/owner identity came from the store."""

    document = _document_bytes(measurement_document)
    return LocalComputeAllocationBasisReview(
        basis_id=_text(basis_id, "basis_id"),
        backend_id=_text(backend_id, "backend_id"),
        model_id=_text(model_id, "model_id"),
        config_sha256=_sha(config_sha256, "config_sha256"),
        allocation_policy_id=_text(
            allocation_policy_id,
            "allocation_policy_id",
        ),
        measurement_source_id=_text(
            measurement_source_id,
            "measurement_source_id",
        ),
        measurement_period_start=_time(
            measurement_period_start,
            "measurement_period_start",
        ),
        measurement_period_end=_time(
            measurement_period_end,
            "measurement_period_end",
        ),
        total_allocable_cost=_money(
            total_allocable_cost,
            "total_allocable_cost",
        ),
        request_denominator=_positive_denominator(
            request_denominator
        ),
        measurement_document_b64=_document_b64(document),
        measurement_document_sha256=hashlib.sha256(
            document
        ).hexdigest(),
        currency=_currency(currency),
        owner_goal_id=_text(owner_goal_id, "owner_goal_id"),
        owner_goal_revision=owner_goal_revision,
        owner_bankroll_id=_text(owner_bankroll_id, "owner_bankroll_id"),
        owner_goal_sha256=_sha(owner_goal_sha256, "owner_goal_sha256"),
    )


@dataclass(frozen=True, slots=True)
class LocalComputeAllocationBasisRecord:
    basis_id: str
    backend_id: str
    model_id: str
    config_sha256: str
    allocation_policy_id: str
    measurement_source_id: str
    measurement_period_start: str
    measurement_period_end: str
    total_allocable_cost: Decimal
    request_denominator: int
    amount_per_request: Decimal
    currency: str
    measurement_document_b64: str
    measurement_document_sha256: str
    owner_review_sha256: str
    available_at: str
    owner_goal_id: str
    owner_goal_revision: int
    owner_bankroll_id: str
    owner_goal_sha256: str

    def __post_init__(self) -> None:
        review = LocalComputeAllocationBasisReview(
            basis_id=self.basis_id,
            backend_id=self.backend_id,
            model_id=self.model_id,
            config_sha256=self.config_sha256,
            allocation_policy_id=self.allocation_policy_id,
            measurement_source_id=self.measurement_source_id,
            measurement_period_start=self.measurement_period_start,
            measurement_period_end=self.measurement_period_end,
            total_allocable_cost=self.total_allocable_cost,
            request_denominator=self.request_denominator,
            measurement_document_b64=self.measurement_document_b64,
            measurement_document_sha256=self.measurement_document_sha256,
            currency=self.currency,
            owner_goal_id=self.owner_goal_id,
            owner_goal_revision=self.owner_goal_revision,
            owner_bankroll_id=self.owner_bankroll_id,
            owner_goal_sha256=self.owner_goal_sha256,
        )
        if self.amount_per_request != review.amount_per_request:
            raise LocalComputeAllocationBasisError(
                "amount_per_request is not mechanically derived from reviewed basis"
            )
        if self.owner_review_sha256 != review.review_sha256:
            raise LocalComputeAllocationBasisError(
                "owner review digest mismatch"
            )
        available = _instant(self.available_at, "available_at")
        if _instant(
            self.measurement_period_end,
            "measurement_period_end",
        ) > available:
            raise LocalComputeAllocationBasisError(
                "allocation basis cannot be available before measurement period end"
            )

    def payload(self) -> dict[str, object]:
        return {
            **LocalComputeAllocationBasisReview(
                basis_id=self.basis_id,
                backend_id=self.backend_id,
                model_id=self.model_id,
                config_sha256=self.config_sha256,
                allocation_policy_id=self.allocation_policy_id,
                measurement_source_id=self.measurement_source_id,
                measurement_period_start=self.measurement_period_start,
                measurement_period_end=self.measurement_period_end,
                total_allocable_cost=self.total_allocable_cost,
                request_denominator=self.request_denominator,
                measurement_document_b64=self.measurement_document_b64,
                measurement_document_sha256=self.measurement_document_sha256,
                currency=self.currency,
                owner_goal_id=self.owner_goal_id,
                owner_goal_revision=self.owner_goal_revision,
                owner_bankroll_id=self.owner_bankroll_id,
                owner_goal_sha256=self.owner_goal_sha256,
            ).payload(),
            "owner_review_sha256": self.owner_review_sha256,
            "available_at": _time(self.available_at, "available_at"),
        }

    @property
    def basis_sha256(self) -> str:
        return _digest(self.payload())

    def to_dict(self) -> dict[str, object]:
        return {**self.payload(), "basis_sha256": self.basis_sha256}

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, object],
    ) -> "LocalComputeAllocationBasisRecord":
        expected = {
            "basis_id",
            "backend_id",
            "model_id",
            "config_sha256",
            "allocation_policy_id",
            "measurement_source_id",
            "measurement_period_start",
            "measurement_period_end",
            "total_allocable_cost",
            "request_denominator",
            "amount_per_request",
            "denominator_unit",
            "measurement_document_b64",
            "measurement_document_sha256",
            "currency",
            "owner_review_sha256",
            "available_at",
            "owner_goal_id",
            "owner_goal_revision",
            "owner_bankroll_id",
            "owner_goal_sha256",
            "basis_sha256",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise LocalComputeAllocationBasisError(
                "allocation basis record fields do not match schema"
            )
        if raw["denominator_unit"] != "request":
            raise LocalComputeAllocationBasisError(
                "unsupported allocation denominator unit"
            )
        item = cls(
            basis_id=raw["basis_id"],  # type: ignore[arg-type]
            backend_id=raw["backend_id"],  # type: ignore[arg-type]
            model_id=raw["model_id"],  # type: ignore[arg-type]
            config_sha256=raw["config_sha256"],  # type: ignore[arg-type]
            allocation_policy_id=raw["allocation_policy_id"],  # type: ignore[arg-type]
            measurement_source_id=raw["measurement_source_id"],  # type: ignore[arg-type]
            measurement_period_start=raw["measurement_period_start"],  # type: ignore[arg-type]
            measurement_period_end=raw["measurement_period_end"],  # type: ignore[arg-type]
            total_allocable_cost=_money_from_text(
                raw["total_allocable_cost"],
                "total_allocable_cost",
            ),
            request_denominator=_positive_denominator(
                raw["request_denominator"]
            ),
            amount_per_request=_money_from_text(
                raw["amount_per_request"],
                "amount_per_request",
            ),
            currency=raw["currency"],  # type: ignore[arg-type]
            measurement_document_b64=raw["measurement_document_b64"],  # type: ignore[arg-type]
            measurement_document_sha256=raw["measurement_document_sha256"],  # type: ignore[arg-type]
            owner_review_sha256=raw["owner_review_sha256"],  # type: ignore[arg-type]
            available_at=raw["available_at"],  # type: ignore[arg-type]
            owner_goal_id=raw["owner_goal_id"],  # type: ignore[arg-type]
            owner_goal_revision=raw["owner_goal_revision"],  # type: ignore[arg-type]
            owner_bankroll_id=raw["owner_bankroll_id"],  # type: ignore[arg-type]
            owner_goal_sha256=raw["owner_goal_sha256"],  # type: ignore[arg-type]
        )
        if raw["basis_sha256"] != item.basis_sha256:
            raise LocalComputeAllocationBasisError(
                "allocation basis digest mismatch"
            )
        return item


def _state_payload(
    records: tuple[LocalComputeAllocationBasisRecord, ...],
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "records": [record.to_dict() for record in records],
    }


def _validate_publication_available_at(
    records: tuple[LocalComputeAllocationBasisRecord, ...],
    available_at: str,
) -> datetime:
    """Validate causal append order without exposing a writable clock seam."""

    available_instant = _instant(available_at, "available_at")
    if (
        records
        and available_instant
        <= _instant(records[-1].available_at, "available_at")
    ):
        raise LocalComputeAllocationBasisError(
            "product clock did not advance before allocation basis publication"
        )
    return available_instant


def _build_allocation_basis_store_init():
    """Bind one LOCAL-compute namespace to the product machine root."""

    root_resolver = local_compute_monotonic_authority_root
    root_code = getattr(root_resolver, "__code__", None)
    root_closure = getattr(root_resolver, "__closure__", None)
    try:
        root_closure_state = tuple(
            cell.cell_contents for cell in (root_closure or ())
        )
    except ValueError as exc:
        raise LocalComputeAllocationBasisError(
            "canonical product authority root closure is invalid"
        ) from exc

    path_type = Path
    authority_type = MonotonicWorkspaceAuthority
    lock_type = WorkspaceEconomicLock
    file_name = FILE_NAME
    authority_domain = AUTHORITY_DOMAIN
    authority_key = AUTHORITY_KEY
    error_type = LocalComputeAllocationBasisError

    def sealed_init(self, workspace: str | Path) -> None:
        if getattr(root_resolver, "__code__", None) is not root_code:
            raise error_type(
                "canonical product authority root resolver code changed"
            )
        live_closure = getattr(root_resolver, "__closure__", None)
        try:
            live_closure_state = tuple(
                cell.cell_contents for cell in (live_closure or ())
            )
        except ValueError as exc:
            raise error_type(
                "canonical product authority root closure changed"
            ) from exc
        if (
            len(live_closure_state) != len(root_closure_state)
            or any(
                current is not frozen
                for current, frozen in zip(
                    live_closure_state,
                    root_closure_state,
                )
            )
        ):
            raise error_type(
                "canonical product authority root closure changed"
            )

        self.workspace = path_type(workspace).absolute().resolve(strict=False)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path = self.workspace / file_name
        self._authority = authority_type(
            workspace=self.workspace,
            domain=authority_domain,
            key=authority_key,
            authority_root=root_resolver(),
        )
        with lock_type(self.workspace):
            self._recover()
            self._records = self._load()

    return sealed_init


class LocalComputeAllocationBasisAuthorityStore:
    """Creation-only owner-reviewed basis store with rollback fencing."""

    __init__ = _build_allocation_basis_store_init()

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

    def _load(self) -> tuple[LocalComputeAllocationBasisRecord, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = strict_json_loads(
                self.path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise LocalComputeAllocationBasisError(
                "local compute allocation basis state is unreadable"
            ) from exc
        if (
            type(raw) is not dict
            or set(raw) != {"schema", "schema_version", "records"}
            or raw["schema"] != SCHEMA
            or raw["schema_version"] != SCHEMA_VERSION
        ):
            raise LocalComputeAllocationBasisError(
                "local compute allocation basis state schema is invalid"
            )
        values = raw["records"]
        if type(values) is not list:
            raise LocalComputeAllocationBasisError(
                "allocation basis records must be a list"
            )
        records: list[LocalComputeAllocationBasisRecord] = []
        ids: set[str] = set()
        digests: set[str] = set()
        previous_available_at: datetime | None = None
        for value in values:
            record = LocalComputeAllocationBasisRecord.from_dict(value)
            if (
                record.basis_id in ids
                or record.basis_sha256 in digests
            ):
                raise LocalComputeAllocationBasisError(
                    "duplicate allocation basis identity"
                )
            available_at = _instant(record.available_at, "available_at")
            if (
                previous_available_at is not None
                and available_at <= previous_available_at
            ):
                raise LocalComputeAllocationBasisError(
                    "allocation basis availability history did not strictly advance"
                )
            previous_available_at = available_at
            ids.add(record.basis_id)
            digests.add(record.basis_sha256)
            records.append(record)
        return tuple(records)

    def _current_goal(self):
        try:
            goal_store = _CANONICAL_ECONOMIC_GOAL_STORE_CLASS(self.workspace)
            goal = _CANONICAL_ECONOMIC_GOAL_STORE_LOAD(goal_store)
        except Exception as exc:
            raise LocalComputeAllocationBasisError(
                "current durable EconomicGoal is required"
            ) from exc
        return goal, _goal_sha256(goal)

    def prepare_owner_review(
        self,
        *,
        basis_id: str,
        backend_id: str,
        model_id: str,
        config_sha256: str,
        allocation_policy_id: str,
        measurement_source_id: str,
        measurement_period_start: str,
        measurement_period_end: str,
        total_allocable_cost: Decimal,
        request_denominator: int,
        measurement_document: bytes,
    ) -> LocalComputeAllocationBasisReview:
        """Build an exact review snapshot bound to the current durable owner goal."""

        with WorkspaceEconomicLock(self.workspace):
            self._recover()
            self._records = self._load()
            goal, goal_sha256 = self._current_goal()
            return _prepare_owner_review(
                basis_id=basis_id,
                backend_id=backend_id,
                model_id=model_id,
                config_sha256=config_sha256,
                allocation_policy_id=allocation_policy_id,
                measurement_source_id=measurement_source_id,
                measurement_period_start=measurement_period_start,
                measurement_period_end=measurement_period_end,
                total_allocable_cost=total_allocable_cost,
                request_denominator=request_denominator,
                measurement_document=measurement_document,
                currency=goal.currency,
                owner_goal_id=goal.goal_id,
                owner_goal_revision=goal.revision,
                owner_bankroll_id=goal.bankroll_id,
                owner_goal_sha256=goal_sha256,
            )

    def publish_owner_basis(
        self,
        review: LocalComputeAllocationBasisReview,
        *,
        confirmed: bool,
    ) -> LocalComputeAllocationBasisRecord:
        """Publish exactly the reviewed snapshot after explicit confirmation."""

        if type(review) is not LocalComputeAllocationBasisReview:
            raise LocalComputeAllocationBasisError(
                "review must be exact LocalComputeAllocationBasisReview"
            )
        if confirmed is not True:
            raise LocalComputeAllocationBasisError(
                "explicit owner confirmation is required"
            )

        with WorkspaceEconomicLock(self.workspace):
            self._recover()
            self._records = self._load()
            goal, goal_sha256 = self._current_goal()
            if (
                review.currency != goal.currency
                or review.owner_goal_id != goal.goal_id
                or review.owner_goal_revision != goal.revision
                or review.owner_bankroll_id != goal.bankroll_id
                or review.owner_goal_sha256 != goal_sha256
            ):
                raise LocalComputeAllocationBasisError(
                    "reviewed EconomicGoal changed before owner confirmation"
                )

            for existing in self._records:
                if existing.basis_id != review.basis_id:
                    continue
                if (
                    existing.owner_review_sha256 == review.review_sha256
                    and existing.owner_goal_id == goal.goal_id
                    and existing.owner_goal_revision == goal.revision
                    and existing.owner_bankroll_id == goal.bankroll_id
                    and existing.owner_goal_sha256 == goal_sha256
                    and existing.currency == goal.currency
                ):
                    return existing
                raise LocalComputeAllocationBasisError(
                    "basis_id is immutable"
                )

            clock_globals = getattr(
                _CANONICAL_AUTHORITY_NOW,
                "__globals__",
                {},
            )
            if (
                _authoritative_utc_now is not _CANONICAL_AUTHORITY_NOW
                or getattr(
                    _CANONICAL_AUTHORITY_NOW,
                    "__code__",
                    None,
                )
                is not _CANONICAL_AUTHORITY_NOW_CODE
                or clock_globals.get("datetime")
                is not _CANONICAL_AUTHORITY_NOW_DATETIME
                or clock_globals.get("timezone")
                is not _CANONICAL_AUTHORITY_NOW_TIMEZONE
            ):
                raise LocalComputeAllocationBasisError(
                    "product clock authority changed"
                )
            available_at = _time(
                _CANONICAL_AUTHORITY_NOW(),
                "available_at",
            )
            available_instant = _validate_publication_available_at(
                self._records,
                available_at,
            )
            if _instant(
                review.measurement_period_end,
                "measurement_period_end",
            ) > available_instant:
                raise LocalComputeAllocationBasisError(
                    "measurement period has not completed at owner confirmation"
                )

            record = LocalComputeAllocationBasisRecord(
                basis_id=review.basis_id,
                backend_id=review.backend_id,
                model_id=review.model_id,
                config_sha256=review.config_sha256,
                allocation_policy_id=review.allocation_policy_id,
                measurement_source_id=review.measurement_source_id,
                measurement_period_start=review.measurement_period_start,
                measurement_period_end=review.measurement_period_end,
                total_allocable_cost=review.total_allocable_cost,
                request_denominator=review.request_denominator,
                amount_per_request=review.amount_per_request,
                currency=review.currency,
                measurement_document_b64=review.measurement_document_b64,
                measurement_document_sha256=review.measurement_document_sha256,
                owner_review_sha256=review.review_sha256,
                available_at=available_at,
                owner_goal_id=review.owner_goal_id,
                owner_goal_revision=review.owner_goal_revision,
                owner_bankroll_id=review.owner_bankroll_id,
                owner_goal_sha256=review.owner_goal_sha256,
            )
            staged = (*self._records, record)
            payload = _state_payload(staged)
            intended = hashlib.sha256(
                _canonical_bytes(payload)
            ).hexdigest()
            observed = self._observed_sha256()
            binding = _digest(
                {
                    "kind": "OWNER_LOCAL_COMPUTE_ALLOCATION_BASIS_PUBLISH",
                    "basis_sha256": record.basis_sha256,
                    "owner_review_sha256": record.owner_review_sha256,
                    "owner_goal_sha256": record.owner_goal_sha256,
                    "observed_state_sha256": observed,
                    "intended_state_sha256": intended,
                }
            )
            tx_id = f"local-compute-allocation-{record.basis_sha256}"
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
                raise LocalComputeAllocationBasisError(
                    "published basis bytes do not match prepared monotonic state"
                )
            self._authority.commit(
                tx_id=tx_id,
                observed_state_sha256=published,
                semantic_binding_sha256=binding,
            )
            self._records = staged
            return record

    def resolve_current(
        self,
        *,
        basis_id: str,
        backend_id: str,
        model_id: str,
        config_sha256: str,
        allocation_policy_id: str,
        bankroll_id: str,
        currency: str,
    ) -> LocalComputeAllocationBasisRecord | None:
        """Resolve current basis state for a new decision after this lookup.

        This is deliberately not a historical-availability oracle.  A successful
        lookup proves only that the exact owner-reviewed basis exists now under
        the current durable EconomicGoal.  Downstream decision authority must
        bind the returned basis identity before issuing the new decision.
        """

        canonical_basis_id = _text(basis_id, "basis_id")
        canonical_backend = _text(backend_id, "backend_id")
        canonical_model = _text(model_id, "model_id")
        canonical_config = _sha(config_sha256, "config_sha256")
        canonical_policy = _text(
            allocation_policy_id,
            "allocation_policy_id",
        )
        canonical_bankroll = _text(bankroll_id, "bankroll_id")
        canonical_currency = _currency(currency)

        with WorkspaceEconomicLock(self.workspace):
            self._recover()
            records = self._load()
            goal, goal_sha256 = self._current_goal()
            if (
                goal.bankroll_id != canonical_bankroll
                or goal.currency != canonical_currency
            ):
                raise LocalComputeAllocationBasisError(
                    "decision bankroll/currency does not match current owner EconomicGoal"
                )

            matches = [
                record
                for record in records
                if record.basis_id == canonical_basis_id
                and record.backend_id == canonical_backend
                and record.model_id == canonical_model
                and record.config_sha256 == canonical_config
                and record.allocation_policy_id == canonical_policy
                and record.owner_goal_id == goal.goal_id
                and record.owner_goal_revision == goal.revision
                and record.owner_bankroll_id == goal.bankroll_id
                and record.owner_goal_sha256 == goal_sha256
                and record.currency == goal.currency
            ]
            if len(matches) > 1:
                raise LocalComputeAllocationBasisError(
                    "ambiguous allocation basis authority"
                )
            return None if not matches else matches[0]

    def resolve(
        self,
        *,
        basis_id: str,
        backend_id: str,
        model_id: str,
        config_sha256: str,
        allocation_policy_id: str,
        decision_at: str,
        bankroll_id: str,
        currency: str,
    ) -> LocalComputeAllocationBasisRecord | None:
        """Fail closed for timestamp-only historical decision-time resolution.

        Local OS wall time cannot prove that the first durable basis existed at a
        past decision instant.  Historical positive authority therefore requires
        a separate durable causal observation/decision witness; this store does
        not mint one.
        """

        _instant(decision_at, "decision_at")
        raise LocalComputeAllocationBasisError(
            "timestamp-only historical allocation basis resolution requires "
            "durable causal observation authority"
        )

__all__ = [
    "LocalComputeAllocationBasisAuthorityStore",
    "LocalComputeAllocationBasisError",
    "LocalComputeAllocationBasisRecord",
    "LocalComputeAllocationBasisReview",
]
