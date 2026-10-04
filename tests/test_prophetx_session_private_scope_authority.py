from datetime import datetime, timezone
from hashlib import sha256

import pytest

from autosport.prophetx_session_lifecycle import (
    ProphetXSessionLifecycle,
    ProphetXSessionLifecycleError,
    ProphetXSessionScope,
)


NOW = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)


def _scope() -> ProphetXSessionScope:
    return ProphetXSessionScope(
        environment="sandbox",
        access_key_identity_sha256=sha256(b"key-a").hexdigest(),
        credential_revision="rev-1",
        integration_role="market-maker-primary",
    )


def test_private_scope_primitive_rebind_cannot_poison_original_pool_state(tmp_path):
    lifecycle = ProphetXSessionLifecycle(tmp_path, scope=_scope())
    original_state_path = lifecycle.state_path
    poisoned_key = sha256(b"key-poisoned").hexdigest()

    object.__setattr__(
        lifecycle,
        "_scope_access_key_identity_sha256",
        poisoned_key,
    )
    object.__setattr__(lifecycle, "_scope_credential_revision", "rev-poisoned")
    object.__setattr__(lifecycle, "_scope_integration_role", "participant")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="session scope authority changed",
    ):
        lifecycle.begin_login(
            now=NOW,
            access_token_available=False,
        )

    assert not original_state_path.exists()


def test_state_path_rebind_cannot_redirect_durable_session_write(tmp_path):
    lifecycle = ProphetXSessionLifecycle(tmp_path, scope=_scope())
    original_state_path = lifecycle.state_path
    redirected = tmp_path / "outside-session-state.json"
    assert redirected != original_state_path

    object.__setattr__(lifecycle, "_state_path", redirected)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="session scope authority changed",
    ):
        lifecycle.begin_login(
            now=NOW,
            access_token_available=False,
        )

    assert not redirected.exists()
    assert not original_state_path.exists()


def test_scope_directory_rebind_cannot_move_pool_lock_or_state_authority(tmp_path):
    lifecycle = ProphetXSessionLifecycle(tmp_path, scope=_scope())
    original_state_path = lifecycle.state_path
    redirected_dir = tmp_path / "foreign-pool"

    object.__setattr__(lifecycle, "_scope_dir", redirected_dir)
    object.__setattr__(
        lifecycle,
        "_state_path",
        redirected_dir / "prophetx-session-state.json",
    )

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="session scope authority changed",
    ):
        lifecycle.begin_login(
            now=NOW,
            access_token_available=False,
        )

    assert not redirected_dir.exists()
    assert not original_state_path.exists()
