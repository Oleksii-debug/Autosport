from __future__ import annotations

"""Atomically reserve collector sources at the canonical PIT store boundary.

The collector adapter is the only positive authority for a source once that source
is admitted through durable CollectorDelta -> DesktopApplicationReceipt evidence.
This guard installs the reservation on the public SourceRevisionAuthorityStore
itself, so reopening the same workspace through the parent API cannot create or
resolve an alternate generic authority family.

The canonical collector policy is the durable reservation.  Reservation checks and
publication run under the same WorkspaceEconomicLock, removing the former
check-then-lock race.  No Python token, closure identity, ContextVar, or private
"issuer" is used as authority: generic parent methods always reject collector-kind
records, while the collector methods below construct and verify their exact records
from durable collector/checkpoint evidence before publishing them.
"""

from . import _point_in_time_authority_legacy as _legacy
from . import collector_point_in_time as _collector
from . import point_in_time_authority as _pit
from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock


_KIND = _collector.COLLECTOR_APPLICATION_WITNESS_KIND
_BASE = _pit.SourceRevisionAuthorityStore
_STORE = _collector.CollectorPointInTimeSourceRevisionAuthorityStore


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
    policies, _, _ = _records(state)
    return frozenset(
        policy.source_identity
        for policy in policies
        if policy.witness_kind == _KIND
    )


def _assert_exclusive_state(state) -> None:
    policies, witnesses, revisions = _records(state)
    reserved = _reserved_sources(state)

    # Collector witness/revision records without the reservation policy are not
    # valid positive authority even if their local digests happen to verify.
    for record in (*witnesses, *revisions):
        if record.witness_kind == _KIND and record.source_identity not in reserved:
            raise _legacy.SourceRevisionAuthorityError(
                "collector source family record lacks canonical reservation policy"
            )

    for record in (*policies, *witnesses, *revisions):
        if record.source_identity in reserved and record.witness_kind != _KIND:
            raise _legacy.SourceRevisionAuthorityError(
                "reserved collector source has alternate generic availability authority"
            )


def _state_payload(state, **changes) -> dict[str, object]:
    return {
        "schema": state["schema"],
        "schema_version": state["schema_version"],
        "policies": list(changes.get("policies", state["policies"])),
        "witnesses": list(changes.get("witnesses", state["witnesses"])),
        "feature_memberships": list(
            changes.get("feature_memberships", state["feature_memberships"])
        ),
        "revisions": list(changes.get("revisions", state["revisions"])),
    }


def _generic_write_allowed(state, *, source_identity: str, witness_kind: str) -> None:
    source = _legacy._text(source_identity, "source_identity")
    kind = _legacy._text(witness_kind, "witness_kind")
    if kind == _KIND:
        raise _legacy.SourceRevisionAuthorityError(
            "collector availability authority requires canonical collector/checkpoint path"
        )
    if source in _reserved_sources(state):
        raise _legacy.SourceRevisionAuthorityError(
            "reserved collector source requires canonical collector availability authority"
        )


def _validate_revision_references(store, state, revision) -> None:
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

    # Preserve public provider provenance verification for generic/provider paths.
    if hasattr(store, "_verify_provider_policy"):
        store._verify_provider_policy(policy)
    if hasattr(store, "_verify_provider_witness"):
        store._verify_provider_witness(witness)


def _find_policy(state, revision_policy_id: str, expected_sha256: str | None):
    wanted = _legacy._text(revision_policy_id, "revision_policy_id")
    expected = (
        None
        if expected_sha256 is None
        else _legacy._sha256(expected_sha256, "expected_sha256")
    )
    for raw in state["policies"]:
        policy = _legacy.RevisionPolicyAuthority.from_payload(raw)
        if policy.revision_policy_id != wanted:
            continue
        if expected is not None and policy.authority_sha256 != expected:
            raise _legacy.SourceRevisionAuthorityError(
                "revision policy authority digest mismatch"
            )
        return policy
    raise _legacy.SourceRevisionAuthorityError("unknown revision policy authority")


def _find_witness(state, availability_witness_id: str, expected_sha256: str | None):
    wanted = _legacy._text(availability_witness_id, "availability_witness_id")
    expected = (
        None
        if expected_sha256 is None
        else _legacy._sha256(expected_sha256, "expected_sha256")
    )
    for raw in state["witnesses"]:
        witness = _legacy.AvailabilityWitnessAuthority.from_payload(raw)
        if witness.availability_witness_id != wanted:
            continue
        if expected is not None and witness.authority_sha256 != expected:
            raise _legacy.SourceRevisionAuthorityError(
                "availability witness authority digest mismatch"
            )
        return witness
    raise _legacy.SourceRevisionAuthorityError("unknown availability witness authority")


def _find_revision(
    state,
    source_revision_authority_id: str,
    expected_sha256: str | None,
):
    wanted = _legacy._text(
        source_revision_authority_id,
        "source_revision_authority_id",
    )
    expected = (
        None
        if expected_sha256 is None
        else _legacy._sha256(expected_sha256, "expected_sha256")
    )
    for raw in state["revisions"]:
        revision = _legacy.SourceRevisionAuthority.from_payload(raw)
        if revision.source_revision_authority_id != wanted:
            continue
        if expected is not None and revision.authority_sha256 != expected:
            raise _legacy.SourceRevisionAuthorityError(
                "source revision authority digest mismatch"
            )
        return revision
    raise _legacy.SourceRevisionAuthorityError("unknown source revision authority")


def _install_guard() -> None:
    if getattr(_BASE.register_policy, "_collector_source_family_guard_v3", False):
        return

    original_base_init = _BASE.__init__

    def guarded_base_init(self, *args, **kwargs):
        original_base_init(self, *args, **kwargs)
        with WorkspaceEconomicLock(self.workspace):
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
            _generic_write_allowed(
                state,
                source_identity=policy.source_identity,
                witness_kind=policy.witness_kind,
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
            atomic_write_json(self.path, _state_payload(state, policies=policies))
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
            _generic_write_allowed(
                state,
                source_identity=witness.source_identity,
                witness_kind=witness.witness_kind,
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
            atomic_write_json(self.path, _state_payload(state, witnesses=witnesses))
        return witness.authority_sha256

    def guarded_base_register_revision(self, revision):
        if not isinstance(revision, _legacy.SourceRevisionAuthority):
            raise _legacy.SourceRevisionAuthorityError(
                "revision must be a SourceRevisionAuthority"
            )
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            _assert_exclusive_state(state)
            _generic_write_allowed(
                state,
                source_identity=revision.source_identity,
                witness_kind=revision.witness_kind,
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
            atomic_write_json(self.path, _state_payload(state, revisions=revisions))
        return revision.authority_sha256

    def guarded_base_resolve_policy(
        self,
        revision_policy_id: str,
        *,
        expected_sha256: str | None = None,
    ):
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            _assert_exclusive_state(state)
            policy = _find_policy(state, revision_policy_id, expected_sha256)
            if policy.witness_kind == _KIND:
                raise _legacy.SourceRevisionAuthorityError(
                    "collector availability authority requires canonical collector resolver"
                )
            return policy

    def guarded_base_resolve_witness(
        self,
        availability_witness_id: str,
        *,
        expected_sha256: str | None = None,
    ):
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            _assert_exclusive_state(state)
            witness = _find_witness(state, availability_witness_id, expected_sha256)
            if witness.witness_kind == _KIND:
                raise _legacy.SourceRevisionAuthorityError(
                    "collector availability authority requires canonical collector resolver"
                )
            if hasattr(self, "_verify_provider_witness"):
                self._verify_provider_witness(witness)
            return witness

    def guarded_base_resolve_revision(
        self,
        source_revision_authority_id: str,
        *,
        expected_sha256: str | None = None,
    ):
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            _assert_exclusive_state(state)
            revision = _find_revision(
                state,
                source_revision_authority_id,
                expected_sha256,
            )
            if revision.witness_kind == _KIND:
                raise _legacy.SourceRevisionAuthorityError(
                    "collector availability authority requires canonical collector resolver"
                )
        # Re-run the provider/generic linked authority checks without holding the
        # writer lock through external provider bundle reads.
        self.resolve_witness(
            revision.availability_witness_id,
            expected_sha256=revision.availability_witness_record_sha256,
        )
        self.resolve_policy(
            revision.revision_policy_id,
            expected_sha256=revision.revision_policy_record_sha256,
        )
        return revision

    def guarded_collector_register_policy(self, policy):
        if (
            isinstance(policy, _legacy.RevisionPolicyAuthority)
            and policy.witness_kind == _KIND
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "collector revision policy must be registered from canonical collector authority"
            )
        return _BASE.register_policy(self, policy)

    def guarded_collector_register_witness(self, witness):
        if (
            isinstance(witness, _legacy.AvailabilityWitnessAuthority)
            and witness.witness_kind == _KIND
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "collector availability witness requires canonical collector/checkpoint evidence"
            )
        return _BASE.register_witness(self, witness)

    def guarded_collector_register_revision(self, revision):
        if (
            isinstance(revision, _legacy.SourceRevisionAuthority)
            and revision.witness_kind == _KIND
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "collector source revision requires canonical collector/checkpoint evidence"
            )
        return _BASE.register_revision(self, revision)

    def guarded_register_collector_policy(
        self,
        *,
        source_identity: str,
        revision_policy_id: str,
        frozen_at,
    ):
        policy = self._collector_policy(
            source_identity=source_identity,
            revision_policy_id=revision_policy_id,
            frozen_at=frozen_at,
        )
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            _assert_exclusive_state(state)
            source = policy.source_identity
            reserved = _reserved_sources(state)
            if source not in reserved:
                policies, witnesses, revisions = _records(state)
                if any(
                    record.source_identity == source
                    for record in (*policies, *witnesses, *revisions)
                ):
                    raise _legacy.SourceRevisionAuthorityError(
                        "collector source already has non-canonical generic availability authority"
                    )
            for raw in state["policies"]:
                current = _legacy.RevisionPolicyAuthority.from_payload(raw)
                if current.revision_policy_id != policy.revision_policy_id:
                    continue
                if current.authority_sha256 != policy.authority_sha256:
                    raise _legacy.SourceRevisionAuthorityError(
                        "conflicting immutable revision policy identity"
                    )
                return policy
            policies = list(state["policies"])
            policies.append(self._stored_policy(policy))
            next_state = _state_payload(state, policies=policies)
            _assert_exclusive_state(next_state)
            atomic_write_json(self.path, next_state)
        return policy

    def guarded_register_collector_revision(
        self,
        *,
        delta_id: str,
        revision_policy_id: str,
        recorded_at,
    ):
        delta, receipt = self._resolve_collector_application(delta_id)
        policy = guarded_collector_resolve_policy(
            self,
            revision_policy_id,
        )
        if (
            policy.source_identity != delta.source_id
            or policy.witness_kind != _KIND
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "collector revision policy does not authorize this source/delta"
            )
        witness_id = self._collector_witness_id(delta, receipt)
        witness = self._expected_collector_witness(
            availability_witness_id=witness_id,
            delta=delta,
            receipt=receipt,
            recorded_at=recorded_at,
        )
        self._verify_collector_witness(witness)
        revision = _legacy.SourceRevisionAuthority(
            source_revision_authority_id=self._source_revision_authority_id(
                delta=delta,
                policy=policy,
                witness=witness,
            ),
            source_identity=witness.source_identity,
            source_revision=witness.source_revision,
            source_revision_sha256=witness.source_revision_sha256,
            revision_policy_id=policy.revision_policy_id,
            revision_policy_record_sha256=policy.authority_sha256,
            availability_witness_id=witness.availability_witness_id,
            availability_witness_sha256=witness.witness_content_sha256,
            availability_witness_record_sha256=witness.authority_sha256,
            witness_kind=witness.witness_kind,
            source_as_of=witness.source_as_of,
            available_at=witness.available_at,
            recorded_at=recorded_at,
        )

        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            _assert_exclusive_state(state)
            if revision.source_identity not in _reserved_sources(state):
                raise _legacy.SourceRevisionAuthorityError(
                    "collector source must be reserved before positive availability evidence"
                )
            stored_policy = _find_policy(
                state,
                revision.revision_policy_id,
                revision.revision_policy_record_sha256,
            )
            if (
                stored_policy.source_identity != revision.source_identity
                or stored_policy.witness_kind != _KIND
            ):
                raise _legacy.SourceRevisionAuthorityError(
                    "collector revision policy is not the exact reserved authority"
                )
            if stored_policy.frozen_at > revision.recorded_at:
                raise _legacy.SourceRevisionAuthorityError(
                    "revision policy was not frozen before authority recording"
                )

            witnesses = list(state["witnesses"])
            existing_witness = next(
                (
                    _legacy.AvailabilityWitnessAuthority.from_payload(raw)
                    for raw in witnesses
                    if raw.get("availability_witness_id")
                    == witness.availability_witness_id
                ),
                None,
            )
            if existing_witness is None:
                witnesses.append(self._stored_witness(witness))
            elif existing_witness.authority_sha256 != witness.authority_sha256:
                raise _legacy.SourceRevisionAuthorityError(
                    "conflicting immutable availability witness identity"
                )

            revisions = list(state["revisions"])
            existing_revision = next(
                (
                    _legacy.SourceRevisionAuthority.from_payload(raw)
                    for raw in revisions
                    if raw.get("source_revision_authority_id")
                    == revision.source_revision_authority_id
                ),
                None,
            )
            if existing_revision is None:
                revisions.append(self._stored_revision(revision))
            elif existing_revision.authority_sha256 != revision.authority_sha256:
                raise _legacy.SourceRevisionAuthorityError(
                    "conflicting immutable source revision authority identity"
                )

            if existing_witness is None or existing_revision is None:
                next_state = _state_payload(
                    state,
                    witnesses=witnesses,
                    revisions=revisions,
                )
                _assert_exclusive_state(next_state)
                atomic_write_json(self.path, next_state)

        return guarded_collector_resolve_revision(
            self,
            revision.source_revision_authority_id,
            expected_sha256=revision.authority_sha256,
        )

    def guarded_collector_resolve_policy(
        self,
        revision_policy_id: str,
        *,
        expected_sha256: str | None = None,
    ):
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            _assert_exclusive_state(state)
            policy = _find_policy(state, revision_policy_id, expected_sha256)
        if policy.witness_kind == _KIND:
            self._verify_collector_policy(policy)
            return policy
        return _BASE.resolve_policy(
            self,
            revision_policy_id,
            expected_sha256=expected_sha256,
        )

    def guarded_collector_resolve_witness(
        self,
        availability_witness_id: str,
        *,
        expected_sha256: str | None = None,
    ):
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            _assert_exclusive_state(state)
            witness = _find_witness(state, availability_witness_id, expected_sha256)
        if witness.witness_kind == _KIND:
            self._verify_collector_witness(witness)
            return witness
        return _BASE.resolve_witness(
            self,
            availability_witness_id,
            expected_sha256=expected_sha256,
        )

    def guarded_collector_resolve_revision(
        self,
        source_revision_authority_id: str,
        *,
        expected_sha256: str | None = None,
    ):
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            _assert_exclusive_state(state)
            revision = _find_revision(
                state,
                source_revision_authority_id,
                expected_sha256,
            )
        if revision.witness_kind != _KIND:
            return _BASE.resolve_revision(
                self,
                source_revision_authority_id,
                expected_sha256=expected_sha256,
            )
        if revision.source_identity not in _reserved_sources(state):
            raise _legacy.SourceRevisionAuthorityError(
                "collector source family record lacks canonical reservation policy"
            )
        witness = guarded_collector_resolve_witness(
            self,
            revision.availability_witness_id,
            expected_sha256=revision.availability_witness_record_sha256,
        )
        policy = guarded_collector_resolve_policy(
            self,
            revision.revision_policy_id,
            expected_sha256=revision.revision_policy_record_sha256,
        )
        if (
            witness.source_identity != revision.source_identity
            or witness.source_revision != revision.source_revision
            or witness.source_revision_sha256 != revision.source_revision_sha256
            or witness.source_as_of != revision.source_as_of
            or witness.available_at != revision.available_at
            or policy.source_identity != revision.source_identity
            or policy.witness_kind != _KIND
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "collector source revision no longer matches durable application evidence"
            )
        return revision

    setattr(
        guarded_base_register_policy,
        "_collector_source_family_guard_v3",
        True,
    )
    _BASE.__init__ = guarded_base_init
    _BASE.register_policy = guarded_base_register_policy
    _BASE.register_witness = guarded_base_register_witness
    _BASE.register_revision = guarded_base_register_revision
    _BASE.resolve_policy = guarded_base_resolve_policy
    _BASE.resolve_witness = guarded_base_resolve_witness
    _BASE.resolve_revision = guarded_base_resolve_revision

    _STORE.register_policy = guarded_collector_register_policy
    _STORE.register_witness = guarded_collector_register_witness
    _STORE.register_revision = guarded_collector_register_revision
    _STORE.register_collector_policy = guarded_register_collector_policy
    _STORE.register_collector_revision = guarded_register_collector_revision
    _STORE.resolve_policy = guarded_collector_resolve_policy
    _STORE.resolve_witness = guarded_collector_resolve_witness
    _STORE.resolve_revision = guarded_collector_resolve_revision


_install_guard()
del _install_guard

__all__: list[str] = []
