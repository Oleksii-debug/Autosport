from __future__ import annotations

"""Rollback-detectable transaction publication receipts for StrategyModelFactory."""

from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from . import strategy_model_factory as _factory
from .integrity import atomic_write_json as _atomic_write_json
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicAuthorityRecoveryRequiredError,
    MonotonicWorkspaceAuthority,
)
from .run_transaction import RunTransactionError
from .workspace_lock import WorkspaceEconomicLock


_FACTORY_PUBLISH_COMMIT_LEDGER_FILENAME = ".factory-publish-commit-ledger-v1.json"
_FACTORY_PUBLISH_COMMIT_SCHEMA_VERSION = 1
_FACTORY_ZERO_PREDECESSOR_SHA256 = "0" * 64

_STORE_TYPE = _factory.FactoryArtifactStore
_RUNNER_TYPE = _factory.ExperimentRunner
_IMPL = _factory._impl
_ORIGINAL_UNLINK_TRANSACTION_MANIFEST = _factory._unlink_transaction_manifest
_ORIGINAL_RECOVER_INTERRUPTED_FACTORY_PUBLISH = (
    _factory._recover_interrupted_factory_publish
)
_ORIGINAL_RUN_POLICY_CANDIDATE = _RUNNER_TYPE.run_policy_candidate
_ORIGINAL_RUN_BASELINE_CANDIDATE = _RUNNER_TYPE.run_baseline_candidate
_STABLE_READER = _factory.RunTransaction._read_canonical_file_snapshot
_READ_PUBLISH_TRANSACTION = _factory._read_publish_transaction
_REGISTRY_STATE_SHA256 = _factory._registry_state_sha256
_PUBLISH_TRANSACTION_PATH = _factory._publish_transaction_path
_PUBLICATION_CONTEXT: ContextVar[tuple[object, object] | None] = ContextVar(
    "autosport_factory_publication_context",
    default=None,
)


def _canonical_publish_commit_utc_now(
    _now=datetime.now,
    _utc=timezone.utc,
) -> datetime:
    return _now(_utc)


def _publish_commit_ledger_path(self) -> Path:
    return self.root / _FACTORY_PUBLISH_COMMIT_LEDGER_FILENAME


def _publish_commit_digest(
    self,
    record: dict[str, object],
    *,
    _sha256=hashlib.sha256,
    _dumps=json.dumps,
) -> str:
    payload = {
        key: record[key]
        for key in (
            "committed_at",
            "original_registry_sha256",
            "final_registry_sha256",
            "artifacts",
            "predecessor_record_sha256",
        )
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


def _validated_publish_artifacts(self, raw_artifacts: object) -> list[dict[str, str]]:
    if type(raw_artifacts) is not list:
        raise ValueError("factory publish commit artifacts must be a list")
    validated: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_artifacts:
        if type(raw) is not dict or set(raw) != {"kind", "identity", "sha256"}:
            raise ValueError("factory publish commit artifact entry is invalid")
        kind = _IMPL._text(raw.get("kind"), "factory publish commit artifact kind")
        identity = _IMPL._text(
            raw.get("identity"),
            "factory publish commit artifact identity",
        )
        sha256 = _IMPL._sha256(
            raw.get("sha256"),
            "factory publish commit artifact sha256",
        )
        key = (kind, identity)
        if key in seen:
            raise ValueError("factory publish commit artifact identities must be unique")
        seen.add(key)
        validated.append({"kind": kind, "identity": identity, "sha256": sha256})
    canonical = sorted(validated, key=lambda item: (item["kind"], item["identity"]))
    if validated != canonical:
        raise ValueError("factory publish commit artifacts must be canonically sorted")
    return canonical


def _read_publish_commit_ledger_raw(
    self,
    *,
    _utc=timezone.utc,
    _instant=_IMPL._instant,
) -> list[dict[str, object]]:
    path = self._publish_commit_ledger_path()
    if not path.exists():
        return []
    try:
        snapshot = _STABLE_READER(path, "factory publish commit ledger")
    except RunTransactionError as exc:
        raise ValueError(
            "factory publish commit ledger is not a stable regular object"
        ) from exc
    try:
        payload = json.loads(snapshot.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("factory publish commit ledger is invalid JSON") from exc
    if type(payload) is not dict or set(payload) != {"schema_version", "records"}:
        raise ValueError("factory publish commit ledger fields mismatch")
    if payload.get("schema_version") != _FACTORY_PUBLISH_COMMIT_SCHEMA_VERSION:
        raise ValueError("factory publish commit ledger schema version mismatch")
    records = payload.get("records")
    if type(records) is not list:
        raise ValueError("factory publish commit ledger records must be a list")

    previous = _FACTORY_ZERO_PREDECESSOR_SHA256
    previous_committed_at: datetime | None = None
    artifact_owners: set[tuple[str, str]] = set()
    validated: list[dict[str, object]] = []
    for raw in records:
        if type(raw) is not dict or set(raw) != {
            "committed_at",
            "original_registry_sha256",
            "final_registry_sha256",
            "artifacts",
            "predecessor_record_sha256",
            "record_sha256",
        }:
            raise ValueError("factory publish commit record fields mismatch")
        committed = _instant(
            raw.get("committed_at"),
            "factory publish committed_at",
        ).astimezone(_utc)
        canonical_committed = committed.isoformat().replace("+00:00", "Z")
        if raw.get("committed_at") != canonical_committed:
            raise ValueError("factory publish committed_at must be canonical UTC Z")
        if previous_committed_at is not None and committed < previous_committed_at:
            raise ValueError("factory publish committed_at moved backwards")
        previous_committed_at = committed
        original_sha256 = _IMPL._sha256(
            raw.get("original_registry_sha256"),
            "factory publish original_registry_sha256",
        )
        final_sha256 = _IMPL._sha256(
            raw.get("final_registry_sha256"),
            "factory publish final_registry_sha256",
        )
        if original_sha256 == final_sha256:
            raise ValueError("factory publish commit must change ScientificRegistry state")
        artifacts = self._validated_publish_artifacts(raw.get("artifacts"))
        for artifact in artifacts:
            key = (artifact["kind"], artifact["identity"])
            if key in artifact_owners:
                raise ValueError("factory artifact appears in multiple publish commits")
            artifact_owners.add(key)
        predecessor = _IMPL._sha256(
            raw.get("predecessor_record_sha256"),
            "factory publish predecessor_record_sha256",
        )
        record_sha256 = _IMPL._sha256(
            raw.get("record_sha256"),
            "factory publish record_sha256",
        )
        if predecessor != previous:
            raise ValueError("factory publish commit ledger predecessor mismatch")
        if record_sha256 != self._publish_commit_digest(raw):
            raise ValueError("factory publish commit ledger record digest mismatch")
        checked = dict(raw)
        checked["artifacts"] = artifacts
        validated.append(checked)
        previous = record_sha256
    return validated


def _publish_commit_state_sha256(
    self,
    records: list[dict[str, object]],
    *,
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
        return authority.recover(observed_state_sha256=observed_state_sha256)
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


def _read_publish_commit_ledger(
    self,
    *,
    _utc=timezone.utc,
    _instant=_IMPL._instant,
) -> list[dict[str, object]]:
    with WorkspaceEconomicLock(self.root.resolve(strict=False)):
        records = _read_publish_commit_ledger_raw(
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


def _publication_receipt(
    self,
    kind: str,
    identity: str,
    *,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    with WorkspaceEconomicLock(self.root.resolve(strict=False)):
        canonical_kind = _IMPL._text(kind, "kind")
        canonical_identity = _IMPL._text(identity, "identity")
        actual_sha256 = self.sha256(canonical_kind, canonical_identity)
        if expected_sha256 is not None and actual_sha256 != _IMPL._sha256(
            expected_sha256,
            "expected_sha256",
        ):
            raise ValueError("factory publication receipt artifact hash mismatch")
        records = _read_publish_commit_ledger_raw(self)
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


def _bind_canonical_publish_commit_clock(
    implementation,
    _utc_now=_canonical_publish_commit_utc_now,
    _datetime_type=datetime,
    _utc=timezone.utc,
    _instant=_IMPL._instant,
):
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


@_bind_canonical_publish_commit_clock
def _append_publish_commit_record(
    self,
    transaction: dict[str, object],
    _utc_now,
    _datetime_type,
    _utc,
    _instant,
) -> dict[str, object]:
    with WorkspaceEconomicLock(self.root.resolve(strict=False)):
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
        records = _read_publish_commit_ledger_raw(self)
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
                raise ValueError("factory publish committed_at moved backwards")
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
        _atomic_write_json(
            self._publish_commit_ledger_path(),
            {
                "schema_version": _FACTORY_PUBLISH_COMMIT_SCHEMA_VERSION,
                "records": next_records,
            },
        )
        published_records = _read_publish_commit_ledger_raw(self)
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


def _record_committed_factory_publish(registry, store) -> dict[str, object]:
    path = _PUBLISH_TRANSACTION_PATH(registry)
    transaction = _READ_PUBLISH_TRANSACTION(path)
    current_sha256 = _REGISTRY_STATE_SHA256(registry._read())
    if current_sha256 != transaction["final_registry_sha256"]:
        raise ValueError(
            "factory publish commit requires the durable final registry state"
        )
    return store._append_publish_commit_record(transaction)


def _guarded_unlink_transaction_manifest(
    path: Path,
    primary: BaseException | None = None,
) -> None:
    context = _PUBLICATION_CONTEXT.get()
    if primary is None and context is not None and Path(path).exists():
        registry, store = context
        expected_path = _PUBLISH_TRANSACTION_PATH(registry)
        if Path(path) == expected_path:
            transaction = _READ_PUBLISH_TRANSACTION(Path(path))
            current_sha256 = _REGISTRY_STATE_SHA256(registry._read())
            if current_sha256 == transaction["final_registry_sha256"]:
                _record_committed_factory_publish(registry, store)
            elif current_sha256 != transaction["original_registry_sha256"]:
                raise ValueError(
                    "factory publish transaction registry state is neither "
                    "precommit nor committed"
                )
    _ORIGINAL_UNLINK_TRANSACTION_MANIFEST(path, primary)


def _recover_interrupted_factory_publish(registry, store) -> None:
    token = _PUBLICATION_CONTEXT.set((registry, store))
    try:
        _ORIGINAL_RECOVER_INTERRUPTED_FACTORY_PUBLISH(registry, store)
    finally:
        _PUBLICATION_CONTEXT.reset(token)


def _run_policy_candidate(self, *args, **kwargs):
    token = _PUBLICATION_CONTEXT.set((self.registry, self.artifact_store))
    try:
        return _ORIGINAL_RUN_POLICY_CANDIDATE(self, *args, **kwargs)
    finally:
        _PUBLICATION_CONTEXT.reset(token)


def _run_baseline_candidate(self, *args, **kwargs):
    token = _PUBLICATION_CONTEXT.set((self.registry, self.artifact_store))
    try:
        return _ORIGINAL_RUN_BASELINE_CANDIDATE(self, *args, **kwargs)
    finally:
        _PUBLICATION_CONTEXT.reset(token)


_STORE_TYPE._publish_commit_ledger_path = _publish_commit_ledger_path
_STORE_TYPE._publish_commit_digest = _publish_commit_digest
_STORE_TYPE._validated_publish_artifacts = _validated_publish_artifacts
_STORE_TYPE._publish_commit_state_sha256 = _publish_commit_state_sha256
_STORE_TYPE._publish_commit_authority = _publish_commit_authority
_STORE_TYPE._recover_publish_commit_authority = _recover_publish_commit_authority
_STORE_TYPE._read_publish_commit_ledger = _read_publish_commit_ledger
_STORE_TYPE._append_publish_commit_record = _append_publish_commit_record
_STORE_TYPE.publication_receipt = _publication_receipt

_factory._FACTORY_PUBLISH_COMMIT_LEDGER_FILENAME = (
    _FACTORY_PUBLISH_COMMIT_LEDGER_FILENAME
)
_factory._FACTORY_PUBLISH_COMMIT_SCHEMA_VERSION = (
    _FACTORY_PUBLISH_COMMIT_SCHEMA_VERSION
)
_factory._canonical_publish_commit_utc_now = _canonical_publish_commit_utc_now
_factory._record_committed_factory_publish = _record_committed_factory_publish
_factory._unlink_transaction_manifest = _guarded_unlink_transaction_manifest
_factory._recover_interrupted_factory_publish = _recover_interrupted_factory_publish
_RUNNER_TYPE.run_policy_candidate = _run_policy_candidate
_RUNNER_TYPE.run_baseline_candidate = _run_baseline_candidate
