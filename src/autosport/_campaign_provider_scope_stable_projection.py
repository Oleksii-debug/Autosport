"""Preserve immutable T0 campaign/provider scope across later source re-verification.

The canonical #660 resolver may reacquire authenticated provider evidence after a
restart. That later T1 evidence validates the already-issued T0 applicability
scope; it must never replace or reconstruct T0 authority from caller-owned data.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from weakref import ref

from . import campaign_provider_scope_authority as scope
from . import _campaign_provider_scope_devapp_identity as _devapp  # noqa: F401


_RAW_RESOLVE = scope.resolve_campaign_provider_scope
_RECEIPT_SCHEMA = "autosport.campaign_provider_scope_t0_receipt"
_RECEIPT_SCHEMA_VERSION = 1
_PROJECTION_SCHEMA = "autosport.campaign_provider_scope_projection"

# These fields describe the later provider evidence instance, not the frozen T0
# campaign/action/account/market applicability identity. They may legitimately
# change when the same durable external effect is re-read after restart.
_REVERIFICATION_FIELDS = frozenset(
    {
        "provider_capture_sha256",
        "provider_evidence_id",
        "provider_source_sha256",
        "source_interval_start",
        "source_interval_end",
        "observed_at",
        "available_at",
    }
)

_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "session_id",
        "plan_id",
        "attempt_id",
        "applicability_digest",
        "projection",
    }
)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise scope.CampaignProviderScopeError(
            "provider scope T0 receipt is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise scope.CampaignProviderScopeError(
                "provider scope T0 receipt contains duplicate JSON keys"
            )
        result[key] = value
    return result


def _stable_payload(
    projection: scope.CampaignProviderScopeProjection,
) -> dict[str, object]:
    payload = projection.payload()
    return {
        key: value
        for key, value in payload.items()
        if key not in _REVERIFICATION_FIELDS
    }


def _receipt_path(execution_ledger) -> Path:
    ledger_path = getattr(execution_ledger, "path", None)
    if not isinstance(ledger_path, (str, os.PathLike)):
        raise scope.CampaignProviderScopeError(
            "execution ledger lacks durable provider-scope receipt path"
        )
    path = Path(ledger_path)
    return path.with_name(path.name + ".campaign-provider-scope-t0.jsonl")


def _projection_from_payload(
    payload: object,
) -> scope.CampaignProviderScopeProjection:
    if type(payload) is not dict:
        raise scope.CampaignProviderScopeError(
            "durable T0 provider scope projection payload is invalid"
        )
    values = dict(payload)
    if values.pop("schema", None) != _PROJECTION_SCHEMA:
        raise scope.CampaignProviderScopeError(
            "durable T0 provider scope projection schema is invalid"
        )
    expected_fields = set(scope.CampaignProviderScopeProjection.__dataclass_fields__)
    if set(values) != expected_fields:
        raise scope.CampaignProviderScopeError(
            "durable T0 provider scope projection fields are invalid"
        )
    try:
        projection = scope.CampaignProviderScopeProjection(**values)
    except (TypeError, ValueError) as exc:
        raise scope.CampaignProviderScopeError(
            "durable T0 provider scope projection is invalid"
        ) from exc
    if (
        type(projection.schema_version) is not int
        or projection.schema_version != scope.SCHEMA_VERSION
    ):
        raise scope.CampaignProviderScopeError(
            "durable T0 provider scope projection version is invalid"
        )
    return projection


def _validate_receipt(
    record: object,
) -> tuple[tuple[str, str, str], scope.CampaignProviderScopeProjection]:
    if type(record) is not dict or set(record) != _RECEIPT_FIELDS:
        raise scope.CampaignProviderScopeError(
            "provider scope T0 receipt schema is invalid"
        )
    if (
        record.get("schema") != _RECEIPT_SCHEMA
        or type(record.get("schema_version")) is not int
        or record.get("schema_version") != _RECEIPT_SCHEMA_VERSION
    ):
        raise scope.CampaignProviderScopeError(
            "provider scope T0 receipt version is invalid"
        )
    session_id = scope._text(record.get("session_id"), "receipt session_id")
    plan_id = scope._text(record.get("plan_id"), "receipt plan_id")
    attempt_id = scope._text(record.get("attempt_id"), "receipt attempt_id")
    expected_digest = scope._sha(
        record.get("applicability_digest"),
        "receipt applicability_digest",
    )
    projection = _projection_from_payload(record.get("projection"))
    if projection.session_id != session_id or projection.plan_id != plan_id:
        raise scope.CampaignProviderScopeError(
            "durable T0 provider scope receipt identity drifted"
        )
    if projection.applicability_digest != expected_digest:
        raise scope.CampaignProviderScopeError(
            "durable T0 provider scope receipt digest drifted"
        )
    return (session_id, plan_id, attempt_id), projection


def _load_receipts(
    path: Path,
) -> dict[tuple[str, str, str], scope.CampaignProviderScopeProjection]:
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise scope.CampaignProviderScopeError(
            "durable T0 provider scope receipt cannot be read"
        ) from exc
    if not raw:
        return {}
    if not raw.endswith(b"\n"):
        raise scope.CampaignProviderScopeError(
            "durable T0 provider scope receipt has incomplete final record"
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise scope.CampaignProviderScopeError(
            "durable T0 provider scope receipt is not UTF-8"
        ) from exc

    result: dict[tuple[str, str, str], scope.CampaignProviderScopeProjection] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line:
            raise scope.CampaignProviderScopeError(
                f"durable T0 provider scope receipt has blank record at line {line_number}"
            )
        try:
            envelope = json.loads(line, object_pairs_hook=_pairs)
        except scope.CampaignProviderScopeError:
            raise
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise scope.CampaignProviderScopeError(
                f"durable T0 provider scope receipt JSON is invalid at line {line_number}"
            ) from exc
        if type(envelope) is not dict or set(envelope) != {"sha256", "record"}:
            raise scope.CampaignProviderScopeError(
                f"durable T0 provider scope envelope is invalid at line {line_number}"
            )
        expected_envelope_digest = scope._sha(
            envelope.get("sha256"),
            "receipt envelope sha256",
        )
        record = envelope.get("record")
        if expected_envelope_digest != _digest(record):
            raise scope.CampaignProviderScopeError(
                f"durable T0 provider scope receipt integrity failed at line {line_number}"
            )
        key, projection = _validate_receipt(record)
        if key in result:
            raise scope.CampaignProviderScopeError(
                "durable T0 provider scope receipt contains duplicate identity"
            )
        result[key] = projection
    return result


def _sync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _persist_t0_projection(
    execution_ledger,
    *,
    session_id: str,
    plan_id: str,
    attempt_id: str,
    projection: scope.CampaignProviderScopeProjection,
) -> scope.CampaignProviderScopeProjection:
    path = _receipt_path(execution_ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".writer.lock")
    try:
        lock_fd = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError as exc:
        raise scope.CampaignProviderScopeError(
            "provider scope T0 receipt writer is busy; fail closed"
        ) from exc
    except OSError as exc:
        raise scope.CampaignProviderScopeError(
            "provider scope T0 receipt lock cannot be acquired"
        ) from exc

    try:
        receipts = _load_receipts(path)
        key = (
            scope._text(session_id, "session_id"),
            scope._text(plan_id, "plan_id"),
            scope._text(attempt_id, "attempt_id"),
        )
        existing = receipts.get(key)
        if existing is not None:
            if (
                existing.applicability_digest != projection.applicability_digest
                or existing.payload() != projection.payload()
            ):
                raise scope.CampaignProviderScopeError(
                    "provider scope T0 receipt identity was reused with different authority"
                )
            return existing

        record = {
            "schema": _RECEIPT_SCHEMA,
            "schema_version": _RECEIPT_SCHEMA_VERSION,
            "session_id": key[0],
            "plan_id": key[1],
            "attempt_id": key[2],
            "applicability_digest": projection.applicability_digest,
            "projection": projection.payload(),
        }
        envelope = {
            "sha256": _digest(record),
            "record": record,
        }
        line = _canonical_json(envelope) + b"\n"
        try:
            with path.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            _sync_parent_directory(path)
        except OSError as exc:
            raise scope.CampaignProviderScopeError(
                "provider scope T0 receipt durability barrier failed"
            ) from exc
        return projection
    finally:
        os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _load_t0_projection(
    execution_ledger,
    *,
    session_id: str,
    plan_id: str,
    attempt_id: str,
    expected_applicability_digest: str,
) -> scope.CampaignProviderScopeProjection:
    path = _receipt_path(execution_ledger)
    key = (
        scope._text(session_id, "session_id"),
        scope._text(plan_id, "plan_id"),
        scope._text(attempt_id, "attempt_id"),
    )
    projection = _load_receipts(path).get(key)
    if projection is None:
        raise scope.CampaignProviderScopeError(
            "durable original T0 provider scope receipt is unavailable"
        )
    expected = scope._sha(
        expected_applicability_digest,
        "expected_applicability_digest",
    )
    if projection.applicability_digest != expected:
        raise scope.CampaignProviderScopeError(
            "durable T0 provider scope digest does not match expected digest"
        )
    return projection


def _validate_original_t0_projection(
    original: scope.CampaignProviderScopeProjection,
    current: scope.CampaignProviderScopeProjection,
    *,
    expected_applicability_digest: str,
    execution_ledger,
    attempt_id: str,
) -> None:
    if type(original) is not scope.CampaignProviderScopeProjection:
        raise scope.CampaignProviderScopeError(
            "restart re-verification requires exact original T0 provider scope projection"
        )
    expected = scope._sha(
        expected_applicability_digest,
        "expected_applicability_digest",
    )
    if original.applicability_digest != expected:
        raise scope.CampaignProviderScopeError(
            "original T0 provider scope digest does not match durable expected digest"
        )
    if _stable_payload(original) != _stable_payload(current):
        raise scope.CampaignProviderScopeError(
            "provider re-verification changed immutable T0 applicability scope"
        )

    binding = execution_ledger.provider_evidence_binding(attempt_id)
    if binding is None:
        raise scope.CampaignProviderScopeError(
            "execution attempt lacks durable provider evidence binding"
        )
    if binding.get("evidence_id") != original.provider_evidence_id:
        raise scope.CampaignProviderScopeError(
            "original T0 provider scope evidence is not durable attempt authority"
        )
    if scope._canonical_instant(
        binding.get("observed_at"),
        "provider binding observed_at",
    ) != scope._canonical_instant(
        original.observed_at,
        "original provider scope observed_at",
    ):
        raise scope.CampaignProviderScopeError(
            "original T0 provider scope time is not durable attempt authority"
        )
    if scope._instant(
        current.available_at,
        "current provider scope available_at",
    ) < scope._instant(
        original.available_at,
        "original provider scope available_at",
    ):
        raise scope.CampaignProviderScopeError(
            "provider re-verification predates original T0 applicability evidence"
        )


def _install_stable_projection_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}

    def register(
        projection: scope.CampaignProviderScopeProjection,
    ) -> scope.CampaignProviderScopeProjection:
        key = id(projection)

        def forget(_weakref: object, *, projection_key: int = key) -> None:
            issued.pop(projection_key, None)

        issued[key] = (ref(projection, forget), projection.applicability_digest)
        return projection

    def authoritative_resolve(
        authority,
        *,
        session_id: str,
        execution_ledger,
        plan_id: str,
        attempt_id: str,
        capture,
        expected_applicability_digest: str | None = None,
        expected_projection: scope.CampaignProviderScopeProjection | None = None,
    ) -> scope.CampaignProviderScopeProjection:
        if (
            expected_projection is not None
            and expected_applicability_digest is None
        ):
            raise scope.CampaignProviderScopeError(
                "original T0 projection requires its durable applicability digest"
            )

        current = _RAW_RESOLVE(
            authority,
            session_id=session_id,
            execution_ledger=execution_ledger,
            plan_id=plan_id,
            attempt_id=attempt_id,
            capture=capture,
            expected_applicability_digest=None,
        )

        if expected_applicability_digest is None:
            persisted = _persist_t0_projection(
                execution_ledger,
                session_id=session_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                projection=current,
            )
            return register(persisted)

        original = _load_t0_projection(
            execution_ledger,
            session_id=session_id,
            plan_id=plan_id,
            attempt_id=attempt_id,
            expected_applicability_digest=expected_applicability_digest,
        )
        if expected_projection is not None:
            if (
                type(expected_projection)
                is not scope.CampaignProviderScopeProjection
                or expected_projection.applicability_digest
                != original.applicability_digest
                or expected_projection.payload() != original.payload()
            ):
                raise scope.CampaignProviderScopeError(
                    "caller-supplied original T0 projection is not durable authority"
                )

        _validate_original_t0_projection(
            original,
            current,
            expected_applicability_digest=expected_applicability_digest,
            execution_ledger=execution_ledger,
            attempt_id=attempt_id,
        )
        return register(original)

    def assert_authoritative(
        projection: scope.CampaignProviderScopeProjection,
    ) -> None:
        if type(projection) is not scope.CampaignProviderScopeProjection:
            raise scope.CampaignProviderScopeError(
                "campaign provider scope projection type is not canonical"
            )
        record = issued.get(id(projection))
        if record is None or record[0]() is not projection:
            raise scope.CampaignProviderScopeError(
                "campaign provider scope projection was not issued by canonical resolver"
            )
        if record[1] != projection.applicability_digest:
            raise scope.CampaignProviderScopeError(
                "campaign provider scope projection changed after resolution"
            )

    scope.resolve_campaign_provider_scope = authoritative_resolve
    scope.assert_campaign_provider_scope_authoritative = assert_authoritative


_install_stable_projection_authority()
del _install_stable_projection_authority
