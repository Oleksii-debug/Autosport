from __future__ import annotations

"""Narrow deterministic fixtures for the point-in-time authority tests.

Production deliberately fails closed for positive historical-provider authority
until origin can be independently re-resolved.  The point-in-time unit tests still
need a deterministic *already trusted* source witness in order to exercise the
separate persistence, revision, feature-availability and restart contracts without
making a network call or weakening that production boundary.

This collection hook therefore replaces only the test module's private
``_source_store`` helper.  It temporarily stubs the external-origin assertion for
the single fixture publication, then immediately restores the production function.
Adversarial tests that call the public/injected historical capture path continue to
see the real fail-closed behavior.
"""

from types import ModuleType

import autosport.point_in_time_authority as _pit


def _install_point_in_time_source_fixture(module: ModuleType) -> None:
    artifact_capture = module._provider_capture

    def trusted_source_store(
        tmp_path,
        *,
        available_at=module.BASE + module.timedelta(minutes=30),
        source_as_of=module.BASE + module.timedelta(minutes=20),
        feature_available_at=module.BASE + module.timedelta(minutes=15),
    ):
        store = module.SourceRevisionAuthorityStore.initialize_pristine(tmp_path)
        policy = store.register_provider_capture_policy(
            revision_policy_id="provider-publication-time-v1",
            frozen_at=module.BASE - module.timedelta(days=1),
        )
        capture = artifact_capture(
            tmp_path,
            source_as_of=source_as_of,
            available_at=available_at,
        )

        # Model the one fact this unit-test layer cannot establish locally:
        # independent production-origin attestation.  Keep the bypass scoped to
        # this fixture publication and restore production behavior immediately.
        production_assert = _pit.assert_historical_snapshot_capture_authoritative
        try:
            _pit.assert_historical_snapshot_capture_authoritative = lambda capture: None
            witness = store.register_provider_capture_witness(
                availability_witness_id="provider-publication:17",
                capture=capture,
                recorded_at=max(
                    available_at,
                    module.BASE + module.timedelta(minutes=35),
                ),
            )
        finally:
            _pit.assert_historical_snapshot_capture_authoritative = production_assert

        membership = module.FeatureMembershipAuthority.create(
            feature_set_id="features-1",
            feature_set_version="v1",
            feature_definition_sha256=module.FEATURE_MANIFEST_SHA256,
            feature_manifest_json=module.FEATURE_MANIFEST_JSON,
            feature_source_sha256=module.SHA_C,
            feature_name="participant.form.trailing_5",
            available_at=feature_available_at,
        )
        store.register_feature_membership(membership)
        revision = module.SourceRevisionAuthority(
            source_revision_authority_id="source-authority-17",
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
            recorded_at=max(
                available_at,
                module.BASE + module.timedelta(minutes=40),
            ),
        )
        store.register_revision(revision)
        return store, policy, revision

    module._source_store = trusted_source_store


def pytest_collection_modifyitems(items) -> None:
    patched: set[int] = set()
    for item in items:
        path = getattr(item, "path", None)
        if path is None or path.name != "test_point_in_time_authority.py":
            continue
        module = item.module
        key = id(module)
        if key not in patched:
            _install_point_in_time_source_fixture(module)
            patched.add(key)
