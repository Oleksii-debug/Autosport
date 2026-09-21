from __future__ import annotations

"""Seal terminal DECISION/REPLAY owner authority for collector retention.

``CollectorRetentionManager.release_pin`` physically removes durable retention
protection.  The caller-supplied pin tuple is therefore assertion data only: the
owner lifecycle must be re-resolved from the canonical RunRegistry and Decision
Ledger before a pin may be deleted.

The base implementation intentionally remains readable in ``collector_retention``
but imported class names there are normal writable Python module globals.  This
installer captures the exact canonical classes and the authority-bearing entry
methods once, then installs one closure-held resolver on the manager.  Rebinding
``collector_retention.RunRegistry`` / ``JsonlDecisionLedger`` (or the corresponding
public entry methods after installation) cannot redirect a release decision.
"""

import hashlib
import json
from pathlib import Path

from . import collector_retention as _retention
from . import decision_ledger as _decision_ledger
from . import run_registry as _run_registry


def _install_owner_lifecycle_authority():
    retention_pin_kind = _retention.RetentionPinKind
    retention_error = _retention.CollectorRetentionError

    run_registry_type = _run_registry.RunRegistry
    run_registry_init = run_registry_type.__init__
    completed_summary_for_run = run_registry_type.verified_completed_summary_for_run

    decision_ledger_type = _decision_ledger.JsonlDecisionLedger
    decision_ledger_init = decision_ledger_type.__init__
    verified_snapshot = decision_ledger_type.verified_snapshot

    path_type = Path
    object_new = object.__new__
    json_loads = json.loads
    sha256 = hashlib.sha256

    def owner_subject(kind, owner_id: str) -> str:
        if type(kind) is not retention_pin_kind:
            raise retention_error("retention pin kind is not canonical")
        prefix = f"{kind.value.lower()}:"
        if not owner_id.startswith(prefix):
            raise retention_error(
                f"{kind.value} retention pin owner_id must use {prefix!r} namespace"
            )
        subject = owner_id[len(prefix) :]
        if not subject or subject.strip() != subject:
            raise retention_error(
                "retention pin owner_id has an invalid lifecycle identity"
            )
        return subject

    def canonical_digest(value: object, field: str) -> str:
        if type(value) is not str or len(value) != 64:
            raise retention_error(f"{field} must be a sha256 hex digest")
        try:
            int(value, 16)
        except ValueError as exc:
            raise retention_error(f"{field} must be a sha256 hex digest") from exc
        if value.lower() != value:
            raise retention_error(f"{field} must be a canonical sha256 hex digest")
        return value

    def terminal_ledger_prefix_count(payload: bytes, expected_sha256: str) -> int:
        if type(payload) is not bytes:
            raise retention_error("canonical Decision Ledger snapshot payload is invalid")
        expected = canonical_digest(
            expected_sha256,
            "terminal_decision_ledger_sha256",
        )
        digest = sha256()
        if digest.hexdigest() == expected:
            return 0
        for line_number, raw_line in enumerate(
            payload.splitlines(keepends=True),
            start=1,
        ):
            digest.update(raw_line)
            if digest.hexdigest() == expected:
                return line_number
        raise retention_error(
            "completed owner Decision Ledger is not an append-prefix of current canonical truth"
        )

    def new_exact(cls, initializer, path):
        instance = object_new(cls)
        initializer(instance, path)
        if type(instance) is not cls:
            raise retention_error("canonical owner authority type was substituted")
        return instance

    def require_releasable_owner(self, kind, owner_id: str) -> None:
        """Re-resolve terminal owner truth without writable retention-module dispatch."""

        subject = owner_subject(kind, owner_id)
        workspace = path_type(self.collector.path).parent
        try:
            registry = new_exact(
                run_registry_type,
                run_registry_init,
                workspace / "run_registry.json",
            )
            if kind == retention_pin_kind.REPLAY:
                completed_summary_for_run(registry, subject)
                return

            ledger = new_exact(
                decision_ledger_type,
                decision_ledger_init,
                workspace / "decisions.jsonl",
            )
            snapshot = verified_snapshot(ledger)
            payload = snapshot.payload
            if type(payload) is not bytes:
                raise retention_error(
                    "canonical Decision Ledger snapshot payload is invalid"
                )

            owner_line_number: int | None = None
            replay_run_id: str | None = None
            for line_number, raw_line in enumerate(
                payload.splitlines(keepends=True),
                start=1,
            ):
                envelope = json_loads(raw_line)
                if type(envelope) is not dict or type(envelope.get("record")) is not dict:
                    raise retention_error("canonical Decision Ledger envelope is invalid")
                record = envelope["record"]
                if record.get("decision_id") == subject:
                    owner_line_number = line_number
                    replay_run_id = record.get("replay_run_id")
                    break
            if (
                owner_line_number is None
                or type(replay_run_id) is not str
                or not replay_run_id
            ):
                raise retention_error(
                    "decision retention pin owner is absent from canonical Decision Ledger"
                )

            summary, _summary_sha256 = completed_summary_for_run(
                registry,
                replay_run_id,
            )
            if type(summary) is not dict:
                raise retention_error("completed replay summary authority is invalid")
            terminal_ledger_sha256 = summary.get("decision_ledger_sha256")
            if type(terminal_ledger_sha256) is not str:
                raise retention_error(
                    "completed replay lacks Decision Ledger terminal authority"
                )
            terminal_line_count = terminal_ledger_prefix_count(
                payload,
                terminal_ledger_sha256,
            )
            if owner_line_number > terminal_line_count:
                raise retention_error(
                    "decision retention pin owner was not durable before replay completion"
                )
        except retention_error:
            raise
        except Exception as exc:
            raise retention_error(
                "retention pin owner lacks verified terminal lifecycle authority"
            ) from exc

    return require_releasable_owner


# Install one closure whose authority dependencies are no longer looked up through
# writable ``collector_retention`` globals at pin-release time.
_retention.CollectorRetentionManager._require_releasable_owner = (
    _install_owner_lifecycle_authority()
)
