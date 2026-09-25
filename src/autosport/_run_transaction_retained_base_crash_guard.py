"""Close the RunTransaction BASE-sidecar crash window without a second store.

#1531 retains exact BASE/terminal PaperBook bytes beside the owning transaction.
A hard process exit can occur after the staging manifest is durable but before the
new BASE sidecar write.  The manifest already declares the retained-evidence
contract in that state, so silently reconstructing the sidecar on restart would
turn an incomplete new-format transaction into apparently complete historical
evidence.

This guard composes with the owning RunTransaction implementation:

* a new-format staging manifest that declares retained evidence must already have
  the exact BASE sidecar before precommit can advance;
* a genuinely legacy staging manifest with no retained contract may be upgraded
  while the canonical workspace is still exactly BASE, by retaining those already
  verified bytes before the owning precommit writes the new contract;
* every retained precommit/commit validation rechecks the exact BASE sidecar hash,
  so deleting/tampering it after precommit cannot cross the economic commit.

No independent run, PaperBook, registry, or recovery authority is introduced.
"""

from __future__ import annotations

from typing import Any

from . import run_transaction as _run_transaction


_RunTransaction = _run_transaction.RunTransaction
_RunTransactionError = _run_transaction.RunTransactionError
_RETAINED_CONTRACT = {
    "base_paper_book": "paper_book.base.json",
    "terminal_paper_book": "paper_book.terminal.json",
}

_ORIGINAL_PRECOMMIT = getattr(
    _RunTransaction,
    "_autosport_retained_base_crash_original_precommit",
    _RunTransaction.precommit,
)
_ORIGINAL_VALIDATE_PRECOMMIT = getattr(
    _RunTransaction,
    "_autosport_retained_base_crash_original_validate_precommit",
    _RunTransaction._validate_precommit_evidence,
)


def _retained_base_snapshot(self, manifest: dict[str, Any]):
    expected_hash = self._hash_field(manifest, "base", "paper_book_sha256")
    snapshot = self._verified_canonical_paper_book_snapshot(
        self.base_book_snapshot_path,
        "retained base PaperBook",
    )
    if snapshot.sha256 != expected_hash:
        raise _RunTransactionError(
            "retained base PaperBook SHA-256 does not match transaction BASE"
        )
    return snapshot


def _ensure_base_before_precommit(self, manifest: dict[str, Any]) -> None:
    retained = manifest.get("retained")
    if retained is not None:
        if retained != _RETAINED_CONTRACT:
            raise _RunTransactionError("transaction retained-evidence paths are invalid")
        # New-format start publishes this contract before the sidecar.  Therefore
        # absence/tamper is evidence of an interrupted start and must fail closed;
        # restart must not manufacture historical evidence that was never durable.
        _retained_base_snapshot(self, manifest)
        return

    # Legacy staging transactions predate the retained contract.  They are still
    # before precommit, so the canonical PaperBook is required to be exact BASE by
    # the owning protocol and can safely become the retained historical sidecar.
    expected_hash = self._hash_field(manifest, "base", "paper_book_sha256")
    canonical = self._verified_canonical_paper_book_snapshot(
        self.workspace / "paper_book.json",
        "base PaperBook",
    )
    if canonical.sha256 != expected_hash:
        raise _RunTransactionError(
            "base PaperBook SHA-256 canonical hash is not the expected transaction state"
        )
    self._atomic_write_bytes(self.base_book_snapshot_path, canonical.payload)
    retained_base = _retained_base_snapshot(self, manifest)
    if retained_base.payload != canonical.payload:
        raise _RunTransactionError("retained base PaperBook exact snapshot mismatch")


def _precommit_with_retained_base(self, summary_payload: dict[str, Any]) -> dict[str, Any]:
    # Preserve the owning method's immutable-start requirement before touching any
    # evidence path.  The owning precommit repeats all normal staging validation.
    self._require_complete_identity_anchor()
    manifest = self._read_manifest()
    if manifest.get("phase") == "staging":
        _ensure_base_before_precommit(self, manifest)
    return _ORIGINAL_PRECOMMIT(self, summary_payload)


def _validate_precommit_with_retained_base(self, manifest: dict[str, Any]) -> None:
    _ORIGINAL_VALIDATE_PRECOMMIT(self, manifest)
    retained = manifest.get("retained")
    if retained is None:
        # Historical precommitted transactions without the new contract preserve
        # recovery compatibility but still cannot mint retained snapshot evidence.
        return
    if retained != _RETAINED_CONTRACT:
        raise _RunTransactionError("transaction retained-evidence paths are invalid")
    _retained_base_snapshot(self, manifest)


def _install() -> None:
    if getattr(
        _RunTransaction,
        "_autosport_retained_base_crash_guard_v1",
        False,
    ):
        return
    _RunTransaction.precommit = _precommit_with_retained_base
    _RunTransaction._validate_precommit_evidence = _validate_precommit_with_retained_base
    _RunTransaction._autosport_retained_base_crash_guard_v1 = True


_install()

__all__: list[str] = []
