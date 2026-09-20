from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .integrity import atomic_write_json
from .monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
    RecoveryDisposition,
)
from .workspace_lock import WorkspaceEconomicLock


_STATE_SCHEMA = "autosport.campaign_monetary_authority_state"
_STATE_VERSION = 1
_STATE_KEYS = {
    "schema",
    "schema_version",
    "generation",
    "receipt_admissions",
    "currency_admissions",
    "source_bindings",
    "allocation_bindings",
    "correction_bindings",
    "campaign_currency_bindings",
    "last_tx_id",
    "last_semantic_binding_sha256",
}


class MonetaryAuthorityStateError(RuntimeError):
    """Raised when canonical monetary authority state is conflicting or rolled back."""


class MonetaryAuthorityStateIntegrityError(MonetaryAuthorityStateError):
    """Canonical state is missing, malformed, rolled back, or inconsistent."""


class MonetaryAuthorityStateConflictError(MonetaryAuthorityStateError):
    """A new authority transition conflicts with already committed state."""


class MonetaryAuthorityStateStore:
    """Rollback-resistant canonical binding state for campaign monetary evidence.

    Content-addressed receipt/currency/allocation files are audit artifacts. This
    state is the positive admission authority. Every transition is additionally
    committed to the project-wide machine monotonic authority, so rolling back or
    deleting this local state while the separate machine authority survives fails
    closed instead of reviving stale monetary truth.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        authority_root: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=False)
        self.state_path = self.workspace / "authority-state.json"
        resolved_authority_root = (
            None
            if authority_root is None
            else Path(authority_root).expanduser().resolve(strict=False)
        )
        self.monotonic = MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            domain="campaign-monetary-cost-authority",
            key="canonical-admission-state-v1",
            authority_root=resolved_authority_root,
        )

    def admit_receipt(
        self,
        *,
        record_id: str,
        namespace: str,
        source_authority: str,
        source_evidence_id: str,
        resolver_family: str,
        resolver_evidence_id: str,
        resolver_sha256: str,
        canonical_treatment: str,
        supersedes_receipt_ids: Sequence[str],
    ) -> Mapping[str, Any]:
        details = {
            "kind": "ADMIT_RECEIPT",
            "record_id": record_id,
            "namespace": namespace,
            "source_authority": source_authority,
            "source_evidence_id": source_evidence_id,
            "resolver_family": resolver_family,
            "resolver_evidence_id": resolver_evidence_id,
            "resolver_sha256": resolver_sha256,
            "canonical_treatment": canonical_treatment,
            "supersedes_receipt_ids": list(supersedes_receipt_ids),
        }
        with WorkspaceEconomicLock(self.workspace):
            state, observed = self._read_current_locked()
            existing = _find(state["receipt_admissions"], "record_id", record_id)
            admission = {
                "record_id": record_id,
                "resolver_family": resolver_family,
                "resolver_evidence_id": resolver_evidence_id,
                "resolver_sha256": resolver_sha256,
                "canonical_treatment": canonical_treatment,
            }
            if existing is not None:
                if existing != admission:
                    raise MonetaryAuthorityStateConflictError(
                        "receipt already has a different canonical admission"
                    )
                self._assert_source_binding_in_state(
                    state,
                    namespace=namespace,
                    source_authority=source_authority,
                    source_evidence_id=source_evidence_id,
                    record_id=record_id,
                )
                return state

            source_key = _source_key(namespace, source_authority, source_evidence_id)
            source = _find(state["source_bindings"], "source_key", source_key)
            if source is not None and source["record_id"] != record_id:
                raise MonetaryAuthorityStateConflictError(
                    "same external source identity already authorizes different immutable evidence"
                )
            receipt_ids = {item["record_id"] for item in state["receipt_admissions"]}
            for prior_id in supersedes_receipt_ids:
                if prior_id not in receipt_ids:
                    raise MonetaryAuthorityStateConflictError(
                        "receipt correction predecessor is not canonically admitted"
                    )
                correction = _find(
                    state["correction_bindings"], "prior_receipt_id", prior_id
                )
                if correction is not None and correction["replacement_receipt_id"] != record_id:
                    raise MonetaryAuthorityStateConflictError(
                        "receipt already has a different append-only correction"
                    )

            next_state = copy.deepcopy(state)
            if source is None:
                next_state["source_bindings"].append(
                    {
                        "source_key": source_key,
                        "namespace": namespace,
                        "source_authority": source_authority,
                        "source_evidence_id": source_evidence_id,
                        "record_id": record_id,
                    }
                )
            next_state["receipt_admissions"].append(admission)
            for prior_id in supersedes_receipt_ids:
                next_state["correction_bindings"].append(
                    {
                        "prior_receipt_id": prior_id,
                        "replacement_receipt_id": record_id,
                    }
                )
            self._sort_collections(next_state)
            return self._commit_locked(state, observed, next_state, details)

    def admit_currency(
        self,
        *,
        record_id: str,
        campaign_sha256: str,
        currency: str,
        source_authority: str,
        source_evidence_id: str,
        resolver_family: str,
        resolver_evidence_id: str,
        resolver_sha256: str,
    ) -> Mapping[str, Any]:
        details = {
            "kind": "ADMIT_CURRENCY",
            "record_id": record_id,
            "campaign_sha256": campaign_sha256,
            "currency": currency,
            "source_authority": source_authority,
            "source_evidence_id": source_evidence_id,
            "resolver_family": resolver_family,
            "resolver_evidence_id": resolver_evidence_id,
            "resolver_sha256": resolver_sha256,
        }
        with WorkspaceEconomicLock(self.workspace):
            state, observed = self._read_current_locked()
            admission = {
                "record_id": record_id,
                "resolver_family": resolver_family,
                "resolver_evidence_id": resolver_evidence_id,
                "resolver_sha256": resolver_sha256,
            }
            existing = _find(state["currency_admissions"], "record_id", record_id)
            if existing is not None:
                if existing != admission:
                    raise MonetaryAuthorityStateConflictError(
                        "currency evidence already has a different canonical admission"
                    )
                return state

            source_key = _source_key("currency", source_authority, source_evidence_id)
            source = _find(state["source_bindings"], "source_key", source_key)
            if source is not None and source["record_id"] != record_id:
                raise MonetaryAuthorityStateConflictError(
                    "same external currency source already authorizes different evidence"
                )
            campaign_binding = _find(
                state["campaign_currency_bindings"], "campaign_sha256", campaign_sha256
            )
            intended_binding = {
                "campaign_sha256": campaign_sha256,
                "currency": currency,
                "evidence_id": record_id,
            }
            if campaign_binding is not None and campaign_binding != intended_binding:
                raise MonetaryAuthorityStateConflictError(
                    "campaign already has a different canonical currency authority"
                )

            next_state = copy.deepcopy(state)
            if source is None:
                next_state["source_bindings"].append(
                    {
                        "source_key": source_key,
                        "namespace": "currency",
                        "source_authority": source_authority,
                        "source_evidence_id": source_evidence_id,
                        "record_id": record_id,
                    }
                )
            next_state["currency_admissions"].append(admission)
            if campaign_binding is None:
                next_state["campaign_currency_bindings"].append(intended_binding)
            self._sort_collections(next_state)
            return self._commit_locked(state, observed, next_state, details)

    def bind_allocation(self, *, receipt_id: str, allocation_id: str) -> Mapping[str, Any]:
        details = {
            "kind": "BIND_ALLOCATION",
            "receipt_id": receipt_id,
            "allocation_id": allocation_id,
        }
        with WorkspaceEconomicLock(self.workspace):
            state, observed = self._read_current_locked()
            if _find(state["receipt_admissions"], "record_id", receipt_id) is None:
                raise MonetaryAuthorityStateConflictError(
                    "allocation source receipt is not canonically admitted"
                )
            existing = _find(state["allocation_bindings"], "receipt_id", receipt_id)
            binding = {"receipt_id": receipt_id, "allocation_id": allocation_id}
            if existing is not None:
                if existing != binding:
                    raise MonetaryAuthorityStateConflictError(
                        "source receipt already has a different canonical allocation"
                    )
                return state
            next_state = copy.deepcopy(state)
            next_state["allocation_bindings"].append(binding)
            self._sort_collections(next_state)
            return self._commit_locked(state, observed, next_state, details)

    def receipt_admission(self, record_id: str) -> Mapping[str, Any]:
        state = self.read_current()
        item = _find(state["receipt_admissions"], "record_id", record_id)
        if item is None:
            raise MonetaryAuthorityStateIntegrityError(
                "missing receipt resolver admission in canonical state"
            )
        return item

    def currency_admission(self, record_id: str) -> Mapping[str, Any]:
        state = self.read_current()
        item = _find(state["currency_admissions"], "record_id", record_id)
        if item is None:
            raise MonetaryAuthorityStateIntegrityError(
                "missing currency resolver admission in canonical state"
            )
        return item

    def allocation_id(self, receipt_id: str) -> str:
        state = self.read_current()
        item = _find(state["allocation_bindings"], "receipt_id", receipt_id)
        if item is None:
            raise MonetaryAuthorityStateIntegrityError(
                "missing canonical allocation binding"
            )
        return _text(item["allocation_id"], "allocation_id")

    def correction_successor(self, receipt_id: str) -> str | None:
        state = self.read_current()
        item = _find(state["correction_bindings"], "prior_receipt_id", receipt_id)
        return None if item is None else _text(item["replacement_receipt_id"], "replacement_receipt_id")

    def campaign_currency(self, campaign_sha256: str) -> Mapping[str, Any]:
        state = self.read_current()
        item = _find(
            state["campaign_currency_bindings"], "campaign_sha256", campaign_sha256
        )
        if item is None:
            raise MonetaryAuthorityStateIntegrityError(
                "missing canonical campaign currency binding"
            )
        return item

    def assert_source_binding(
        self,
        *,
        namespace: str,
        source_authority: str,
        source_evidence_id: str,
        record_id: str,
    ) -> None:
        state = self.read_current()
        self._assert_source_binding_in_state(
            state,
            namespace=namespace,
            source_authority=source_authority,
            source_evidence_id=source_evidence_id,
            record_id=record_id,
        )

    def read_current(self) -> Mapping[str, Any]:
        with WorkspaceEconomicLock(self.workspace):
            state, _observed = self._read_current_locked()
            return copy.deepcopy(state)

    def _read_current_locked(self) -> tuple[dict[str, Any], str | None]:
        if not self.state_path.exists():
            state = _empty_state()
            observed = None
            tx_id = None
            binding = None
        else:
            state = self._load_state_file()
            observed = _state_digest(state)
            tx_id = state["last_tx_id"]
            binding = state["last_semantic_binding_sha256"]
        try:
            recovery = self.monotonic.recover(
                observed_state_sha256=observed,
                tx_id=tx_id,
                semantic_binding_sha256=binding,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise MonetaryAuthorityStateIntegrityError(
                "monotonic monetary authority rejected workspace state"
            ) from exc

        if observed is None:
            if recovery.disposition is not RecoveryDisposition.PRISTINE:
                raise MonetaryAuthorityStateIntegrityError(
                    "monotonic authority has history but canonical state is missing"
                )
            return state, None

        if recovery.committed_state_sha256 != observed:
            raise MonetaryAuthorityStateIntegrityError(
                "canonical state digest does not match monotonic authority"
            )
        if recovery.committed_generation != state["generation"]:
            raise MonetaryAuthorityStateIntegrityError(
                "canonical state generation does not match monotonic authority"
            )
        return state, observed

    def _commit_locked(
        self,
        prior_state: Mapping[str, Any],
        observed: str | None,
        next_state: dict[str, Any],
        details: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        generation = int(prior_state["generation"]) + 1
        semantic_binding = _digest({"transition": dict(details)})
        tx_id = f"monetary-{generation:020d}-{semantic_binding[:32]}"
        next_state["schema"] = _STATE_SCHEMA
        next_state["schema_version"] = _STATE_VERSION
        next_state["generation"] = generation
        next_state["last_tx_id"] = tx_id
        next_state["last_semantic_binding_sha256"] = semantic_binding
        self._validate_state(next_state)
        intended = _state_digest(next_state)
        try:
            self.monotonic.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=semantic_binding,
            )
            atomic_write_json(self.state_path, next_state)
            _fsync_directory(self.workspace)
            reread = self._load_state_file()
            if _state_digest(reread) != intended:
                raise MonetaryAuthorityStateIntegrityError(
                    "canonical state readback digest mismatch"
                )
            self.monotonic.commit(
                tx_id=tx_id,
                observed_state_sha256=intended,
                semantic_binding_sha256=semantic_binding,
            )
        except MonetaryAuthorityStateError:
            raise
        except MonotonicWorkspaceAuthorityError as exc:
            raise MonetaryAuthorityStateIntegrityError(
                "monotonic monetary authority transition failed"
            ) from exc
        return copy.deepcopy(next_state)

    def _load_state_file(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            raise MonetaryAuthorityStateIntegrityError(
                "canonical monetary authority state is unreadable"
            ) from exc
        if type(raw) is not dict:
            raise MonetaryAuthorityStateIntegrityError(
                "canonical monetary authority state must be a JSON object"
            )
        self._validate_state(raw)
        return raw

    def _validate_state(self, state: Mapping[str, Any]) -> None:
        if set(state) != _STATE_KEYS:
            raise MonetaryAuthorityStateIntegrityError(
                "canonical monetary authority state keys are invalid"
            )
        if state["schema"] != _STATE_SCHEMA or state["schema_version"] != _STATE_VERSION:
            raise MonetaryAuthorityStateIntegrityError(
                "canonical monetary authority state schema is unsupported"
            )
        generation = state["generation"]
        if type(generation) is not int or generation < 0:
            raise MonetaryAuthorityStateIntegrityError("state generation is invalid")
        if generation == 0:
            if state["last_tx_id"] is not None or state["last_semantic_binding_sha256"] is not None:
                raise MonetaryAuthorityStateIntegrityError("pristine state cannot name a transaction")
        else:
            _text(state["last_tx_id"], "last_tx_id")
            _sha256(state["last_semantic_binding_sha256"], "last_semantic_binding_sha256")
        for key in (
            "receipt_admissions",
            "currency_admissions",
            "source_bindings",
            "allocation_bindings",
            "correction_bindings",
            "campaign_currency_bindings",
        ):
            if type(state[key]) is not list:
                raise MonetaryAuthorityStateIntegrityError(f"{key} must be a list")
        self._require_sorted_unique(state["receipt_admissions"], "record_id")
        self._require_sorted_unique(state["currency_admissions"], "record_id")
        self._require_sorted_unique(state["source_bindings"], "source_key")
        self._require_sorted_unique(state["allocation_bindings"], "receipt_id")
        self._require_sorted_unique(state["correction_bindings"], "prior_receipt_id")
        self._require_sorted_unique(state["campaign_currency_bindings"], "campaign_sha256")

    @staticmethod
    def _require_sorted_unique(items: list[Any], key: str) -> None:
        if any(type(item) is not dict or key not in item for item in items):
            raise MonetaryAuthorityStateIntegrityError(
                f"canonical state collection lacks key {key}"
            )
        values = [item[key] for item in items]
        if values != sorted(values) or len(values) != len(set(values)):
            raise MonetaryAuthorityStateIntegrityError(
                f"canonical state collection {key} is not sorted unique"
            )

    @staticmethod
    def _sort_collections(state: dict[str, Any]) -> None:
        state["receipt_admissions"].sort(key=lambda item: item["record_id"])
        state["currency_admissions"].sort(key=lambda item: item["record_id"])
        state["source_bindings"].sort(key=lambda item: item["source_key"])
        state["allocation_bindings"].sort(key=lambda item: item["receipt_id"])
        state["correction_bindings"].sort(key=lambda item: item["prior_receipt_id"])
        state["campaign_currency_bindings"].sort(key=lambda item: item["campaign_sha256"])

    @staticmethod
    def _assert_source_binding_in_state(
        state: Mapping[str, Any],
        *,
        namespace: str,
        source_authority: str,
        source_evidence_id: str,
        record_id: str,
    ) -> None:
        key = _source_key(namespace, source_authority, source_evidence_id)
        item = _find(state["source_bindings"], "source_key", key)
        expected = {
            "source_key": key,
            "namespace": namespace,
            "source_authority": source_authority,
            "source_evidence_id": source_evidence_id,
            "record_id": record_id,
        }
        if item != expected:
            raise MonetaryAuthorityStateIntegrityError(
                "canonical source identity binding does not authorize this record"
            )


def _empty_state() -> dict[str, Any]:
    return {
        "schema": _STATE_SCHEMA,
        "schema_version": _STATE_VERSION,
        "generation": 0,
        "receipt_admissions": [],
        "currency_admissions": [],
        "source_bindings": [],
        "allocation_bindings": [],
        "correction_bindings": [],
        "campaign_currency_bindings": [],
        "last_tx_id": None,
        "last_semantic_binding_sha256": None,
    }


def _find(items: Sequence[Mapping[str, Any]], key: str, value: str) -> Mapping[str, Any] | None:
    return next((item for item in items if item.get(key) == value), None)


def _source_key(namespace: str, source_authority: str, source_evidence_id: str) -> str:
    return hashlib.sha256(
        "\0".join((namespace, source_authority, source_evidence_id)).encode("utf-8")
    ).hexdigest()


def _state_digest(state: Mapping[str, Any]) -> str:
    return _digest(dict(state))


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise MonetaryAuthorityStateIntegrityError(f"{label} must be a canonical string")
    return value


def _sha256(value: Any, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise MonetaryAuthorityStateIntegrityError(f"{label} must be SHA-256 hex")
    return text


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
