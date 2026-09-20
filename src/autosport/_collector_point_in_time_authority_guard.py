from __future__ import annotations

"""Reserve collector source identities to the canonical collector PIT authority.

Once a source is admitted through the durable CollectorDelta -> completed desktop
application resolver, that source must not retain a second generic caller-authored
policy/witness/revision path. The underlying generic source authority remains
available for unrelated sources; this guard only makes the collector source-family
binding exclusive and restart-verifiable.
"""

from . import _point_in_time_authority_legacy as _legacy
from . import collector_point_in_time as _collector


_KIND = _collector.COLLECTOR_APPLICATION_WITNESS_KIND
_STORE = _collector.CollectorPointInTimeSourceRevisionAuthorityStore


def _install_guard() -> None:
    if getattr(_STORE.register_policy, "_collector_source_family_guard", False):
        return

    original_init = _STORE.__init__
    original_register_policy = _STORE.register_policy
    original_register_collector_policy = _STORE.register_collector_policy
    original_register_witness = _STORE.register_witness
    original_register_revision = _STORE.register_revision
    original_register_collector_revision = _STORE.register_collector_revision
    original_resolve_revision = _STORE.resolve_revision

    def records(store):
        state = store._read()
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

    def reserved_sources(store) -> frozenset[str]:
        policies, _, _ = records(store)
        return frozenset(
            policy.source_identity
            for policy in policies
            if policy.witness_kind == _KIND
        )

    def assert_source_can_be_reserved(store, source_identity: str) -> None:
        source = _legacy._text(source_identity, "source_identity")
        policies, witnesses, revisions = records(store)
        for record in (*policies, *witnesses, *revisions):
            if record.source_identity == source and record.witness_kind != _KIND:
                raise _legacy.SourceRevisionAuthorityError(
                    "collector source already has non-canonical generic availability authority"
                )

    def assert_persisted_family_exclusive(store) -> None:
        policies, witnesses, revisions = records(store)
        reserved = frozenset(
            policy.source_identity
            for policy in policies
            if policy.witness_kind == _KIND
        )
        if not reserved:
            return
        for policy in policies:
            if policy.source_identity in reserved and policy.witness_kind != _KIND:
                raise _legacy.SourceRevisionAuthorityError(
                    "reserved collector source has alternate generic revision policy"
                )
        for witness in witnesses:
            if witness.source_identity in reserved and witness.witness_kind != _KIND:
                raise _legacy.SourceRevisionAuthorityError(
                    "reserved collector source has alternate generic availability witness"
                )
        for revision in revisions:
            if revision.source_identity in reserved and revision.witness_kind != _KIND:
                raise _legacy.SourceRevisionAuthorityError(
                    "reserved collector source has alternate generic source revision authority"
                )

    def guarded_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        assert_persisted_family_exclusive(self)

    def guarded_register_policy(self, policy):
        if isinstance(policy, _legacy.RevisionPolicyAuthority):
            if (
                policy.witness_kind != _KIND
                and policy.source_identity in reserved_sources(self)
            ):
                raise _legacy.SourceRevisionAuthorityError(
                    "reserved collector source requires canonical collector revision policy"
                )
        return original_register_policy(self, policy)

    def guarded_register_collector_policy(
        self,
        *,
        source_identity: str,
        revision_policy_id: str,
        frozen_at,
    ):
        assert_source_can_be_reserved(self, source_identity)
        policy = original_register_collector_policy(
            self,
            source_identity=source_identity,
            revision_policy_id=revision_policy_id,
            frozen_at=frozen_at,
        )
        assert_persisted_family_exclusive(self)
        return policy

    def guarded_register_witness(self, witness):
        if isinstance(witness, _legacy.AvailabilityWitnessAuthority):
            if witness.source_identity in reserved_sources(self):
                raise _legacy.SourceRevisionAuthorityError(
                    "reserved collector source witness requires canonical collector/checkpoint evidence"
                )
        return original_register_witness(self, witness)

    def guarded_register_revision(self, revision):
        if isinstance(revision, _legacy.SourceRevisionAuthority):
            if revision.source_identity in reserved_sources(self):
                raise _legacy.SourceRevisionAuthorityError(
                    "reserved collector source revision requires canonical collector/checkpoint evidence"
                )
        return original_register_revision(self, revision)

    def guarded_register_collector_revision(self, *args, **kwargs):
        assert_persisted_family_exclusive(self)
        revision = original_register_collector_revision(self, *args, **kwargs)
        assert_persisted_family_exclusive(self)
        return revision

    def guarded_resolve_revision(self, source_revision_authority_id: str, **kwargs):
        revision = original_resolve_revision(
            self,
            source_revision_authority_id,
            **kwargs,
        )
        if (
            revision.source_identity in reserved_sources(self)
            and revision.witness_kind != _KIND
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "reserved collector source revision bypassed canonical collector authority"
            )
        return revision

    setattr(guarded_register_policy, "_collector_source_family_guard", True)
    _STORE.__init__ = guarded_init
    _STORE.register_policy = guarded_register_policy
    _STORE.register_collector_policy = guarded_register_collector_policy
    _STORE.register_witness = guarded_register_witness
    _STORE.register_revision = guarded_register_revision
    _STORE.register_collector_revision = guarded_register_collector_revision
    _STORE.resolve_revision = guarded_resolve_revision


_install_guard()
del _install_guard

__all__: list[str] = []
