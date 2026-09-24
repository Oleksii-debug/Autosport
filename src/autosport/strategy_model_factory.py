from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from . import _strategy_model_factory_publish_receipt_impl as _receipt
from ._strategy_model_factory_publish_receipt_impl import *  # noqa: F401,F403
from .integrity import atomic_write_json
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicAuthorityRecoveryRequiredError,
    MonotonicWorkspaceAuthority,
)
from .workspace_lock import WorkspaceEconomicLock


_IMPL = _receipt._impl
_FACTORY_PUBLISH_COMMIT_SCHEMA_VERSION = (
    _receipt._FACTORY_PUBLISH_COMMIT_SCHEMA_VERSION
)
_FACTORY_ZERO_PREDECESSOR_SHA256 = _receipt._FACTORY_ZERO_PREDECESSOR_SHA256
_ORIGINAL_READ_PUBLISH_COMMIT_LEDGER = (
    _receipt.FactoryArtifactStore._read_publish_commit_ledger
)
_ORIGINAL_PUBLICATION_RECEIPT = _receipt.FactoryArtifactStore.publication_receipt


def _canonical_publish_commit_utc_now(
    _now=datetime.now,
    _utc=timezone.utc,
) -> datetime:
    """Return product-owned UTC time without consulting mutable module aliases."""

    return _now(_utc)


def _bind_canonical_publish_commit_clock(
    implementation,
    _utc_now=_canonical_publish_commit_utc_now,
    _datetime_type=datetime,
    _utc=timezone.utc,
    _instant=_IMPL._instant,
):
    """Close publication-time authority over import-composed trusted primitives."""

    def bound(self, transaction):
        return implementation(
            self,
            transaction,
            _utc_now,
            _datetime_type,
            _utc,
            _instant,
        )

    return bound


def _bind_publish_commit_workspace_lock(
    implementation,
    _lock_type=WorkspaceEconomicLock,
):
    """Serialize publish-ledger readers and writers on the canonical store root."""

    def bound(self, *args, **kwargs):
        with _lock_type(self.root.resolve(strict=False)):
            return implementation(self, *args, **kwargs)

    return bound


def _publish_commit_state_sha256(
    self,
    records: list[dict[str, object]],
    _sha256=hashlib.sha256,
    _dumps=json.dumps,
) -> str:
    payload = {
        "schema_version": _FACTORY_PUBLISH_COMMIT_SCHEMA_VERSION,
        "records": records,
    }
    return _sha256(
        _dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _publish_commit_authority(
    self,
    _authority_type=MonotonicWorkspaceAuthority,
):
    return _authority_type(
        workspace=self.root.resolve(strict=False),
        workspace_instance_id=None,
        domain="strategy-model-factory",
        key="publish-commit-ledger-v1",
    )


def _recover_publish_commit_authority(
    self,
    observed_state_sha256: str | None,
    *,
    authority=None,
):
    authority = authority or self._publish_commit_authority()
    try:
        return authority.recover(
            observed_state_sha256=observed_state_sha256,
        )
    except MonotonicAuthorityRecoveryRequiredError:
        history = authority.read_history()
        pending = history[-1] if history else None
        if (
            pending is None
            or pending.phase is not AuthorityPhase.PREPARE
            or pending.intended_state_sha256 != observed_state_sha256
        ):
            raise
        return authority.recover(
            observed_state_sha256=observed_state_sha256,
            tx_id=pending.tx_id,
            semantic_binding_sha256=pending.semantic_binding_sha256,
        )


@_bind_publish_commit_workspace_lock
def _read_publish_commit_ledger(
    self,
    *,
    _utc=timezone.utc,
    _instant=_IMPL._instant,
) -> list[dict[str, object]]:
    records = _ORIGINAL_READ_PUBLISH_COMMIT_LEDGER(
        self,
        _utc=_utc,
        _instant=_instant,
    )
    observed_state_sha256 = (
        self._publish_commit_state_sha256(records)
        if self._publish_commit_ledger_path().exists()
        else None
    )
    self._recover_publish_commit_authority(observed_state_sha256)
    return records


@_bind_publish_commit_workspace_lock
def _publication_receipt(
    self,
    kind: str,
    identity: str,
    *,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    """Resolve one publication receipt from one stable artifact/ledger snapshot."""

    canonical_kind = _IMPL._text(kind, "kind")
    canonical_identity = _IMPL._text(identity, "identity")
    actual_sha256 = self.sha256(canonical_kind, canonical_identity)
    if expected_sha256 is not None and actual_sha256 != _IMPL._sha256(
        expected_sha256,
        "expected_sha256",
    ):
        raise ValueError("factory publication receipt artifact hash mismatch")

    records = _ORIGINAL_READ_PUBLISH_COMMIT_LEDGER(self)
    observed_state_sha256 = (
        self._publish_commit_state_sha256(records)
        if self._publish_commit_ledger_path().exists()
        else None
    )
    self._recover_publish_commit_authority(observed_state_sha256)

    matches: list[dict[str, object]] = []
    for record in records:
        for artifact in record["artifacts"]:
            if (
                artifact["kind"] == canonical_kind
                and artifact["identity"] == canonical_identity
            ):
                if artifact["sha256"] != actual_sha256:
                    raise ValueError(
                        "factory publication receipt artifact hash mismatch"
                    )
                matches.append(record)
    if len(matches) != 1:
        raise ValueError(
            "factory publication receipt is missing or ambiguous: "
            f"{kind}:{identity}"
        )

    if self.sha256(canonical_kind, canonical_identity) != actual_sha256:
        raise ValueError(
            "factory artifact changed during publication receipt verification"
        )

    record = dict(matches[0])
    record["artifacts"] = [
        dict(artifact) for artifact in matches[0]["artifacts"]
    ]
    return record


@_bind_publish_commit_workspace_lock
@_bind_canonical_publish_commit_clock
def _append_publish_commit_record(
    self,
    transaction: dict[str, object],
    _utc_now,
    _datetime_type,
    _utc,
    _instant,
) -> dict[str, object]:
    """Append one rollback-detectable transaction-bound publication receipt."""

    if type(transaction) is not dict or set(transaction) != {
        "schema_version",
        "phase",
        "original_registry_sha256",
        "final_registry_sha256",
        "artifacts",
    }:
        raise ValueError("factory publish commit transaction fields mismatch")
    if (
        transaction.get("schema_version") != 1
        or transaction.get("phase") != "prepared"
    ):
        raise ValueError("factory publish commit transaction identity mismatch")
    original_sha256 = _IMPL._sha256(
        transaction.get("original_registry_sha256"),
        "factory publish original_registry_sha256",
    )
    final_sha256 = _IMPL._sha256(
        transaction.get("final_registry_sha256"),
        "factory publish final_registry_sha256",
    )
    if original_sha256 == final_sha256:
        raise ValueError(
            "factory publish commit must change ScientificRegistry state"
        )
    artifacts = self._validated_publish_artifacts(transaction.get("artifacts"))
    for artifact in artifacts:
        kind = artifact["kind"]
        identity = artifact["identity"]
        if not self.path_for_testing(kind, identity).exists():
            raise ValueError(
                "factory publish commit is missing transaction artifact: "
                f"{kind}:{identity}"
            )
        if self.sha256(kind, identity) != artifact["sha256"]:
            raise ValueError(
                "factory publish commit artifact hash mismatch: "
                f"{kind}:{identity}"
            )

    # Parse without dispatching through the patched reader so one authority object
    # owns recovery, PREPARE and COMMIT for this complete transition.
    records = _ORIGINAL_READ_PUBLISH_COMMIT_LEDGER(self)
    current_state_sha256 = (
        self._publish_commit_state_sha256(records)
        if self._publish_commit_ledger_path().exists()
        else None
    )
    authority = self._publish_commit_authority()
    self._recover_publish_commit_authority(
        current_state_sha256,
        authority=authority,
    )

    for existing in records:
        if (
            existing["original_registry_sha256"] == original_sha256
            and existing["final_registry_sha256"] == final_sha256
            and existing["artifacts"] == artifacts
        ):
            return existing

    prior_artifacts = {
        (artifact["kind"], artifact["identity"])
        for existing in records
        for artifact in existing["artifacts"]
    }
    for artifact in artifacts:
        if (artifact["kind"], artifact["identity"]) in prior_artifacts:
            raise ValueError(
                "factory artifact is already bound to another publish commit"
            )

    now = _utc_now()
    if (
        type(now) is not _datetime_type
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise ValueError(
            "factory publish commit clock must return an aware datetime"
        )
    committed = now.astimezone(_utc)
    if records:
        previous_committed = _instant(
            records[-1]["committed_at"],
            "factory publish committed_at",
        ).astimezone(_utc)
        if committed < previous_committed:
            raise ValueError(
                "factory publish committed_at moved backwards"
            )
    committed_at = committed.isoformat().replace("+00:00", "Z")
    record: dict[str, object] = {
        "committed_at": committed_at,
        "original_registry_sha256": original_sha256,
        "final_registry_sha256": final_sha256,
        "artifacts": artifacts,
        "predecessor_record_sha256": (
            records[-1]["record_sha256"]
            if records
            else _FACTORY_ZERO_PREDECESSOR_SHA256
        ),
    }
    record["record_sha256"] = self._publish_commit_digest(record)
    next_records = [*records, record]
    intended_state_sha256 = self._publish_commit_state_sha256(next_records)
    semantic_binding_sha256 = record["record_sha256"]
    assert isinstance(semantic_binding_sha256, str)

    tx_id = (
        "factory-publish-"
        f"{semantic_binding_sha256}-"
        f"{len(authority.read_history()) + 1}"
    )
    prepared = authority.prepare(
        tx_id=tx_id,
        observed_state_sha256=current_state_sha256,
        intended_state_sha256=intended_state_sha256,
        semantic_binding_sha256=semantic_binding_sha256,
    )
    if prepared.phase is not AuthorityPhase.PREPARE:
        raise RuntimeError("factory publish authority did not enter PREPARE")

    atomic_write_json(
        self._publish_commit_ledger_path(),
        {
            "schema_version": _FACTORY_PUBLISH_COMMIT_SCHEMA_VERSION,
            "records": next_records,
        },
    )
    published_records = _ORIGINAL_READ_PUBLISH_COMMIT_LEDGER(self)
    published_state_sha256 = self._publish_commit_state_sha256(
        published_records
    )
    if published_state_sha256 != intended_state_sha256:
        raise ValueError(
            "factory publish commit ledger changed before authority COMMIT"
        )
    authority.commit(
        tx_id=tx_id,
        observed_state_sha256=published_state_sha256,
        semantic_binding_sha256=semantic_binding_sha256,
    )
    return record


# Patch the existing exact store type rather than introducing a subclass.  Current
# product PolicyEvaluation issuance seals both its exact MRO and instance shape
# ({root, _clock}); preserving that type is required for composition safety.
FactoryArtifactStore = _receipt.FactoryArtifactStore
FactoryArtifactStore._publish_commit_state_sha256 = _publish_commit_state_sha256
FactoryArtifactStore._publish_commit_authority = _publish_commit_authority
FactoryArtifactStore._recover_publish_commit_authority = _recover_publish_commit_authority
FactoryArtifactStore._read_publish_commit_ledger = _read_publish_commit_ledger
FactoryArtifactStore.publication_receipt = _publication_receipt
FactoryArtifactStore._append_publish_commit_record = _append_publish_commit_record
ExperimentRunner = _receipt.ExperimentRunner

for _compat_name, _compat_value in vars(_receipt).items():
    if _compat_name.startswith("__"):
        continue
    if _compat_name not in globals():
        globals()[_compat_name] = _compat_value

for _public_name, _public_value in tuple(globals().items()):
    if (
        not _public_name.startswith("_")
        and getattr(_public_value, "__module__", None) == _receipt.__name__
    ):
        try:
            _public_value.__module__ = __name__
        except (AttributeError, TypeError):
            pass

del _compat_name, _compat_value, _public_name, _public_value
