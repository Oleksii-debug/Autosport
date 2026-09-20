from __future__ import annotations

"""Reserve the canonical provider PIT source identity against generic fallback.

The public point-in-time store supports generic source authorities, while the
ParlayAPI historical source has a separate canonical provider provenance path.
The source identity itself therefore has to be reserved: changing only the
``witness_kind`` spelling must never turn the provider into a caller-authored
generic availability source.

This module does not create another authority store.  It fences the existing
``SourceRevisionAuthorityStore`` after the collector-family guard is installed.
Canonical provider policy/witness publication continues to use the dedicated
provider methods, which publish through the legacy ``super()`` path and are
still subject to the historical-capture provenance verifier.
"""

from . import _point_in_time_authority_legacy as _legacy
from . import point_in_time_authority as _pit
from .historical_snapshot import HISTORICAL_CAPTURE_WITNESS_KIND
from .workspace_lock import WorkspaceEconomicLock


_BASE = _pit.SourceRevisionAuthorityStore
_PROVIDER_SOURCE_ID = _BASE.PROVIDER_SOURCE_ID
_PROVIDER_KIND = HISTORICAL_CAPTURE_WITNESS_KIND


def _provider_source(record: object) -> bool:
    return getattr(record, "source_identity", None) == _PROVIDER_SOURCE_ID


def _assert_provider_source_state(state: dict[str, object]) -> None:
    records = (
        *(
            _legacy.RevisionPolicyAuthority.from_payload(raw)
            for raw in state["policies"]
        ),
        *(
            _legacy.AvailabilityWitnessAuthority.from_payload(raw)
            for raw in state["witnesses"]
        ),
        *(
            _legacy.SourceRevisionAuthority.from_payload(raw)
            for raw in state["revisions"]
        ),
    )
    for record in records:
        if _provider_source(record) and record.witness_kind != _PROVIDER_KIND:
            raise _legacy.SourceRevisionAuthorityError(
                "canonical provider source has alternate generic availability authority"
            )


def _reject_generic_provider_record(record: object, *, label: str) -> None:
    if _provider_source(record):
        raise _legacy.SourceRevisionAuthorityError(
            f"canonical provider source requires canonical provider {label} authority"
        )


def _install_provider_source_guard() -> None:
    if getattr(_BASE.register_policy, "_provider_source_identity_guard_v1", False):
        return

    original_init = _BASE.__init__
    original_register_policy = _BASE.register_policy
    original_register_witness = _BASE.register_witness
    original_register_revision = _BASE.register_revision
    original_resolve_policy = _BASE.resolve_policy
    original_resolve_witness = _BASE.resolve_witness
    original_resolve_revision = _BASE.resolve_revision

    def guarded_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        with WorkspaceEconomicLock(self.workspace):
            _assert_provider_source_state(self._read())

    def guarded_register_policy(self, policy):
        if isinstance(policy, _legacy.RevisionPolicyAuthority):
            _reject_generic_provider_record(policy, label="policy")
        return original_register_policy(self, policy)

    def guarded_register_witness(self, witness):
        if isinstance(witness, _legacy.AvailabilityWitnessAuthority):
            _reject_generic_provider_record(witness, label="witness")
        return original_register_witness(self, witness)

    def guarded_register_revision(self, revision):
        if isinstance(revision, _legacy.SourceRevisionAuthority):
            if _provider_source(revision) and revision.witness_kind != _PROVIDER_KIND:
                raise _legacy.SourceRevisionAuthorityError(
                    "canonical provider source requires canonical provider revision authority"
                )
        return original_register_revision(self, revision)

    def guarded_resolve_policy(
        self,
        revision_policy_id: str,
        *,
        expected_sha256: str | None = None,
    ):
        policy = original_resolve_policy(
            self,
            revision_policy_id,
            expected_sha256=expected_sha256,
        )
        if _provider_source(policy):
            if policy.witness_kind != _PROVIDER_KIND:
                raise _legacy.SourceRevisionAuthorityError(
                    "canonical provider source has alternate generic availability authority"
                )
            self._verify_provider_policy(policy)
        return policy

    def guarded_resolve_witness(
        self,
        availability_witness_id: str,
        *,
        expected_sha256: str | None = None,
    ):
        witness = original_resolve_witness(
            self,
            availability_witness_id,
            expected_sha256=expected_sha256,
        )
        if _provider_source(witness):
            if witness.witness_kind != _PROVIDER_KIND:
                raise _legacy.SourceRevisionAuthorityError(
                    "canonical provider source has alternate generic availability authority"
                )
            self._verify_provider_witness(witness)
        return witness

    def guarded_resolve_revision(
        self,
        source_revision_authority_id: str,
        *,
        expected_sha256: str | None = None,
    ):
        revision = original_resolve_revision(
            self,
            source_revision_authority_id,
            expected_sha256=expected_sha256,
        )
        if _provider_source(revision) and revision.witness_kind != _PROVIDER_KIND:
            raise _legacy.SourceRevisionAuthorityError(
                "canonical provider source has alternate generic availability authority"
            )
        return revision

    setattr(
        guarded_register_policy,
        "_provider_source_identity_guard_v1",
        True,
    )
    _BASE.__init__ = guarded_init
    _BASE.register_policy = guarded_register_policy
    _BASE.register_witness = guarded_register_witness
    _BASE.register_revision = guarded_register_revision
    _BASE.resolve_policy = guarded_resolve_policy
    _BASE.resolve_witness = guarded_resolve_witness
    _BASE.resolve_revision = guarded_resolve_revision


_install_provider_source_guard()
del _install_provider_source_guard

__all__: list[str] = []
