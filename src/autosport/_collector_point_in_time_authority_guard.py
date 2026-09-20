from __future__ import annotations

"""Atomically reserve collector sources at the canonical PIT store boundary.

The collector adapter is the only positive authority for a source once that source
is admitted through durable CollectorDelta -> DesktopApplicationReceipt evidence.
This guard installs the reservation on the public base SourceRevisionAuthorityStore
itself, so reopening the same workspace through the parent API cannot create or
resolve an alternate generic authority family.

The reservation is represented by the canonical collector policy already stored in
the point-in-time authority file.  Registration checks and the associated mutation
run under the same WorkspaceEconomicLock; this removes the former check-then-lock
race without introducing a second registry or persistence format.
"""

from . import _point_in_time_authority_legacy as _legacy
from . import collector_point_in_time as _collector
from . import point_in_time_authority as _pit
from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock


_KIND = _collector.COLLECTOR_APPLICATION_WITNESS_KIND
_BASE = _pit.SourceRevisionAuthorityStore
_STORE = _collector.CollectorPointInTimeSourceRevisionAuthorityStore


def _install_guard() -> None:
    if getattr(_BASE.register_policy, "_collector_source_family_guard_v2", False):
        return

    from contextvars import ContextVar

    canonical_collector_call: ContextVar[bool] = ContextVar(
        "autosport_collector_point_in_time_canonical_call",
        default=False,
    )

    original_base_init = _BASE.__init__
    original_base_resolve_policy = _BASE.resolve_policy
    original_base_resolve_witness = _BASE.resolve_witness
    original_base_resolve_revision = _BASE.resolve_revision

    original_register_collector_policy = _STORE.register_collector_policy
    original_register_collector_revision = _STORE.register_collector_revision
    original_collector_resolve_policy = _STORE.resolve_policy
    original_collector_resolve_witness = _STORE.resolve_witness
    original_collector_resolve_revision = _STORE.resolve_revision

    def _records(state):
        policies = tuple(
            _legacy.RevisionPolicyAuthority.from_payload(raw)
            for raw in state["policies"]
        )
        witnesses = tuple(
            _legacy.AvailabilityWitnessAuthority.from_payload(raw)
            for raw in state["witnesses"]
        )
        revisions = tuple(
            _legacy.SourceRevisionAuthority.from_payload(raw)
            for raw in state["revisions"]
        )
        return policies, witnesses, revisions

    def _reserved_sources(state) -> frozenset[str]:
        policies, witnesses, revisions = _records(state)
        return frozenset(
            record.source_identity
            for record in (*policies, *witnesses, *revisions)
            if record.witness_kind == _KIND
        )

    def _assert_exclusive_state(state) -> None:
        policies, witnesses, revisions = _records(state)
        reserved = _reserved_sources(state)
        if not reserved:
            return
        policy_reserved = frozenset(
            policy.source_identity
            for policy in policies
            if policy.witness_kind == _KIND
        )
        missing_policy = sorted(reserved - policy_reserved)
        if missing_policy:
            raise _legacy.SourceRevisionAuthorityError(
                "collector source family record lacks canonical reservation policy"
            )
        for record in (*policies, *witnesses, *revisions):
            if record.source_identity in reserved and record.witness_kind != _KIND:
                raise _legacy.SourceRevisionAuthorityError(
                    "reserved collector source has alternate generic availability authority"
                )

    def _assert_registration_allowed(
        state,
        *,
        source_identity: str,
        witness_kind: str,
        require_reservation: bool,
    ) -> None:
        source = _legacy._text(source_identity, "source_identity")
        kind = _legacy._text(witness_kind, "witness_kind")
        reserved = _reserved_sources(state)
        if kind == _KIND:
            if not canonical_collector_call.get():
                raise _legacy.SourceRevisionAuthorityError(
                    "collector availability authority requires canonical collector/checkpoint path"
                )
            if require_reservation and source not in reserved:
                raise _legacy.SourceRevisionAuthorityError(
                    "collector source must be reserved before positive availability evidence"
                )
            policies, witnesses, revisions = _records(state)
            for record in (*policies, *witnesses, *revisions):
                if record.source_identity == source and record.witness_kind != _KIND:
                    raise _legacy.SourceRevisionAuthorityError(
                        "collector source already has non-canonical generic availability authority"
                    )
            return
        if source in reserved:
            raise _legacy.SourceRevisionAuthorityError(
                "reserved collector source requires canonical collector availability authority"
            )

    def _publish_state(self, state, **changes) -> None:
        next_state = {
            "schema": state["schema"],
            "schema_version": state["schema_version"],
            "policies": list(changes.get("policies", state["policies"])),
            "witnesses": list(changes.get("witnesses", state["witnesses"])),
            "feature_memberships": list(
                changes.get("feature_memberships", state["feature_memberships"])
            ),
            "revisions": list(changes.get("revisions", state["revisions"])),
        }
        _assert_exclusive_state(next_state)
        atomic_write_json(self.path, next_state)

    def guarded_base_init(self, *args, **kwargs):
        original_base_init(self, *args, **kwargs)
        _assert_exclusive_state(self._read())

    def guarded_base_register_policy(self, policy):
        if not isinstance(policy, _legacy.RevisionPolicyAuthority):
            raise _legacy.SourceRevisionAuthorityError(
                "policy must be a RevisionPolicyAuthority"
            )
        if policy.witness_kind.startswith("provider-"):
            raise _legacy.SourceRevisionAuthorityError(
                "provider revision policy must be registered from canonical source policy"
            )
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            _assert_exclusive_state(state)
            _assert_registration_allowed(
                state,
                source_identity=policy.source_identity,
                witness_kind=policy.witness_kind,
                require_reservation=False,
            )
            for raw in state["policies"]:
                current = _legacy.RevisionPolicyAuthority.from_payload(raw)
                if current.revision_policy_id != policy.revision_policy_id:
                    continue
                if current.authority_sha256 != policy.authority_sha256:
                    raise _legacy.SourceRevisionAuthorityError(
                        "conflicting immutable revision policy identity"
                    )
                return current.authority_sha256
            policies = list(state["policies"])
            policies.append(self._stored_policy(policy))
            _publish_state(self, state, policies=policies)
        return policy.authority_sha256

    def guarded_base_register_witness(self, witness):
        if not isinstance(witness, _legacy.AvailabilityWitnessAuthority):
            raise _legacy.SourceRevisionAuthorityError(
                "witness must be an AvailabilityWitnessAuthority"
            )
        if witness.witness_kind.startswith("provider-"):
            raise _legacy.SourceRevisionAuthorityError(
                "provider availability witness requires canonical persisted capture evidence"
            )
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            _assert_exclusive_state(state)
            _assert_registration_allowed(
                state,
                source_identity=witness.source_identity,
                witness_kind=witness.witness_kind,
                require_reservation=True,
            )
            for raw in state["witnesses"]:
                current = _legacy.AvailabilityWitnessAuthority.from_payload(raw)
                if current.availability_witness_id != witness.availability_witness_id:
                    continue
                if current.authority_sha256 != witness.authority_sha256:
                    raise _legacy.SourceRevisionAuthorityError(
                        "conflicting immutable availability witness identity"
                    )
                return current.authority_sha256
            witnesses = list(state["witnesses"])
            witnesses.append(self._stored_witness(witness))
            _publish_state(self, state, witnesses=witnesses)
        return witness.authority_sha256

    def _validate_revision_references(self, state, revision):
        policies = tuple(
            _legacy.RevisionPolicyAuthority.from_payload(item)
            for item in state["policies"]
        )
        policy = next(
            (
                item
                for item in policies
                if item.revision_policy_id == revision.revision_policy_id
            ),
            None,
        )
        if policy is None:
            raise _legacy.SourceRevisionAuthorityError(
                "source revision references unknown revision policy"
            )
        if revision.revision_policy_record_sha256 != policy.authority_sha256:
            raise _legacy.SourceRevisionAuthorityError(
                "source revision policy digest mismatch"
            )
        if revision.source_identity != policy.source_identity:
            raise _legacy.SourceRevisionAuthorityError(
                "source revision policy source identity mismatch"
            )
        if revision.witness_kind != policy.witness_kind:
            raise _legacy.SourceRevisionAuthorityError(
                "source revision witness kind is not authorized by policy"
            )
        if policy.frozen_at > revision.recorded_at:
            raise _legacy.SourceRevisionAuthorityError(
                "revision policy was not frozen before authority recording"
            )

        witnesses = tuple(
            _legacy.AvailabilityWitnessAuthority.from_payload(item)
            for item in state["witnesses"]
        )
        witness = next(
            (
                item
                for item in witnesses
                if item.availability_witness_id == revision.availability_witness_id
            ),
            None,
        )
        if witness is None:
            raise _legacy.SourceRevisionAuthorityError(
                "source revision references unknown availability witness"
            )
        if revision.availability_witness_record_sha256 != witness.authority_sha256:
            raise _legacy.SourceRevisionAuthorityError(
                "source revision availability witness record digest mismatch"
            )
        if revision.availability_witness_sha256 != witness.witness_content_sha256:
            raise _legacy.SourceRevisionAuthorityError(
                "source revision availability witness content digest mismatch"
            )
        if (
            revision.source_identity != witness.source_identity
            or revision.source_revision != witness.source_revision
            or revision.source_revision_sha256 != witness.source_revision_sha256
            or revision.witness_kind != witness.witness_kind
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "source revision availability witness identity mismatch"
            )
        if (
            revision.source_as_of != witness.source_as_of
            or revision.available_at != witness.available_at
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "source revision availability witness time mismatch"
            )
        if witness.recorded_at > revision.recorded_at:
            raise _legacy.SourceRevisionAuthorityError(
                "source revision predates its availability witness record"
            )
        if hasattr(self, "_verify_provider_policy"):
            self._verify_provider_policy(policy)
        if hasattr(self, "_verify_provider_witness"):
            self._verify_provider_witness(witness)

    def guarded_base_register_revision(self, revision):
        if not isinstance(revision, _legacy.SourceRevisionAuthority):
            raise _legacy.SourceRevisionAuthorityError(
                "revision must be a SourceRevisionAuthority"
            )
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            _assert_exclusive_state(state)
            _assert_registration_allowed(
                state,
                source_identity=revision.source_identity,
                witness_kind=revision.witness_kind,
                require_reservation=True,
            )
            _validate_revision_references(self, state, revision)
            for raw in state["revisions"]:
                current = _legacy.SourceRevisionAuthority.from_payload(raw)
                if (
                    current.source_revision_authority_id
                    != revision.source_revision_authority_id
                ):
                    continue
                if current.authority_sha256 != revision.authority_sha256:
                    raise _legacy.SourceRevisionAuthorityError(
                        "conflicting immutable source revision authority identity"
                    )
                return current.authority_sha256
            revisions = list(state["revisions"])
            revisions.append(self._stored_revision(revision))
            _publish_state(self, state, revisions=revisions)
        return revision.authority_sha256

    def _guard_resolution(self, record) -> None:
        state = self._read()
        _assert_exclusive_state(state)
        if record.witness_kind == _KIND and not canonical_collector_call.get():
            raise _legacy.SourceRevisionAuthorityError(
                "collector availability authority requires canonical collector resolver"
            )
        if (
            record.source_identity in _reserved_sources(state)
            and record.witness_kind != _KIND
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "reserved collector source bypassed canonical collector availability authority"
            )

    def guarded_base_resolve_policy(self, *args, **kwargs):
        policy = original_base_resolve_policy(self, *args, **kwargs)
        _guard_resolution(self, policy)
        return policy

    def guarded_base_resolve_witness(self, *args, **kwargs):
        witness = original_base_resolve_witness(self, *args, **kwargs)
        _guard_resolution(self, witness)
        return witness

    def guarded_base_resolve_revision(self, *args, **kwargs):
        revision = original_base_resolve_revision(self, *args, **kwargs)
        _guard_resolution(self, revision)
        return revision

    def _canonical_call(original):
        def wrapped(self, *args, **kwargs):
            token = canonical_collector_call.set(True)
            try:
                return original(self, *args, **kwargs)
            finally:
                canonical_collector_call.reset(token)

        return wrapped

    setattr(guarded_base_register_policy, "_collector_source_family_guard_v2", True)
    _BASE.__init__ = guarded_base_init
    _BASE.register_policy = guarded_base_register_policy
    _BASE.register_witness = guarded_base_register_witness
    _BASE.register_revision = guarded_base_register_revision
    _BASE.resolve_policy = guarded_base_resolve_policy
    _BASE.resolve_witness = guarded_base_resolve_witness
    _BASE.resolve_revision = guarded_base_resolve_revision

    _STORE.register_collector_policy = _canonical_call(
        original_register_collector_policy
    )
    _STORE.register_collector_revision = _canonical_call(
        original_register_collector_revision
    )
    _STORE.resolve_policy = _canonical_call(original_collector_resolve_policy)
    _STORE.resolve_witness = _canonical_call(original_collector_resolve_witness)
    _STORE.resolve_revision = _canonical_call(original_collector_resolve_revision)


_install_guard()
del _install_guard

__all__: list[str] = []
