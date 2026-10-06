from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from autosport.prophetx_session_lifecycle import (
    ProphetXLoginAdmissionAction,
    ProphetXSessionLifecycle,
    ProphetXSessionScope,
)


NOW = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)


def _scope(
    *,
    key: str = "key-a",
    revision: str = "rev-1",
    environment: str = "sandbox",
    role: str = "market-maker-primary",
) -> ProphetXSessionScope:
    return ProphetXSessionScope(
        environment=environment,
        access_key_identity_sha256=sha256(key.encode("ascii")).hexdigest(),
        credential_revision=revision,
        integration_role=role,
    )


def test_constructor_scope_mutation_cannot_rebind_persisted_pool_authority(tmp_path):
    caller_scope = _scope()
    lifecycle = ProphetXSessionLifecycle(tmp_path, scope=caller_scope)
    original_view = lifecycle.scope
    original_state_path = lifecycle.state_path

    object.__setattr__(caller_scope, "environment", "production")
    object.__setattr__(
        caller_scope,
        "access_key_identity_sha256",
        sha256(b"key-b").hexdigest(),
    )
    object.__setattr__(caller_scope, "credential_revision", "rev-poisoned")
    object.__setattr__(caller_scope, "integration_role", "participant")

    admission = lifecycle.begin_login(
        now=NOW,
        access_token_available=False,
    )

    assert admission.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert lifecycle.state_path == original_state_path
    assert lifecycle.scope == original_view
    assert admission.snapshot is not None
    assert admission.snapshot.credential_revision == "rev-1"
    assert admission.snapshot.integration_role == "market-maker-primary"

    payload = json.loads(original_state_path.read_text(encoding="utf-8"))
    assert payload["environment"] == "sandbox"
    assert payload["access_key_identity_sha256"] == sha256(b"key-a").hexdigest()
    assert payload["credential_revision"] == "rev-1"
    assert payload["integration_role"] == "market-maker-primary"

    same_pool_reader = ProphetXSessionLifecycle(tmp_path, scope=_scope())
    assert same_pool_reader.state_path == original_state_path
    assert same_pool_reader.read_snapshot() == admission.snapshot

    poisoned_pool = ProphetXSessionLifecycle(
        tmp_path,
        scope=_scope(
            key="key-b",
            revision="rev-poisoned",
            environment="production",
            role="participant",
        ),
    )
    assert poisoned_pool.state_path != original_state_path
    assert poisoned_pool.read_snapshot() is None


def test_public_scope_view_is_detached_from_authority(tmp_path):
    lifecycle = ProphetXSessionLifecycle(tmp_path, scope=_scope())
    expected = lifecycle.scope
    state_path = lifecycle.state_path

    exposed = lifecycle.scope
    object.__setattr__(exposed, "credential_revision", "rev-poisoned")
    object.__setattr__(exposed, "integration_role", "participant")
    object.__setattr__(exposed, "environment", "production")
    object.__setattr__(
        exposed,
        "access_key_identity_sha256",
        sha256(b"key-b").hexdigest(),
    )

    assert lifecycle.scope == expected
    assert lifecycle.state_path == state_path

    admission = lifecycle.begin_login(
        now=NOW,
        access_token_available=False,
    )
    assert admission.snapshot is not None
    assert admission.snapshot.credential_revision == expected.credential_revision
    assert admission.snapshot.integration_role == expected.integration_role


def test_scope_property_cannot_be_reassigned_even_with_object_setattr(tmp_path):
    lifecycle = ProphetXSessionLifecycle(tmp_path, scope=_scope())
    replacement = _scope(key="key-b", revision="rev-2")
    expected = lifecycle.scope
    state_path = lifecycle.state_path

    with pytest.raises(AttributeError):
        lifecycle.scope = replacement

    with pytest.raises(AttributeError):
        object.__setattr__(lifecycle, "scope", replacement)

    assert lifecycle.scope == expected
    assert lifecycle.state_path == state_path
