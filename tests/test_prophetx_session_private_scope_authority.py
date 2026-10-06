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


def _scope_authority_records() -> dict[int, tuple[object, ...]]:
    guarded_getattribute = ProphetXSessionLifecycle.__getattribute__
    closure = guarded_getattribute.__closure__
    assert closure is not None
    require_current = next(
        cell.cell_contents
        for cell in closure
        if callable(cell.cell_contents)
        and getattr(cell.cell_contents, "__name__", None) == "_require_current"
    )
    inner_closure = require_current.__closure__
    assert inner_closure is not None
    records = [
        cell.cell_contents
        for cell in inner_closure
        if isinstance(cell.cell_contents, dict)
    ]
    assert len(records) == 1
    return records[0]


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


def test_scope_directory_alias_created_after_construction_fails_closed(tmp_path):
    lifecycle = ProphetXSessionLifecycle(tmp_path, scope=_scope())
    state = object.__getattribute__(lifecycle, "__dict__")
    scope_dir = state["_scope_dir"]
    original_state_path = state["_state_path"]
    outside = tmp_path / "outside-session-pool"
    outside.mkdir()
    scope_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        scope_dir.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink is unavailable: {exc}")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="session scope filesystem authority changed",
    ):
        lifecycle.begin_login(
            now=NOW,
            access_token_available=False,
        )

    assert list(outside.iterdir()) == []
    assert original_state_path.resolve(strict=False) != original_state_path


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


def test_deleting_closure_scope_anchor_cannot_reenable_unanchored_scope(tmp_path):
    lifecycle = ProphetXSessionLifecycle(tmp_path, scope=_scope())
    original_state_path = lifecycle.state_path
    redirected_dir = tmp_path / "registry-deletion-poisoned-pool"

    records = _scope_authority_records()
    issued = records.pop(id(lifecycle))
    assert issued[0]() is lifecycle

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


def test_replacing_closure_scope_anchor_cannot_reanchor_poisoned_scope(tmp_path):
    lifecycle = ProphetXSessionLifecycle(tmp_path, scope=_scope())
    original_state_path = lifecycle.state_path
    redirected_dir = tmp_path / "registry-replacement-poisoned-pool"

    records = _scope_authority_records()
    original = records[id(lifecycle)]
    assert original[0]() is lifecycle

    object.__setattr__(lifecycle, "_scope_dir", redirected_dir)
    object.__setattr__(
        lifecycle,
        "_state_path",
        redirected_dir / "prophetx-session-state.json",
    )
    state = object.__getattribute__(lifecycle, "__dict__")
    records[id(lifecycle)] = (
        original[0],
        state["workspace"],
        state["_scope_environment"],
        state["_scope_access_key_identity_sha256"],
        state["_scope_credential_revision"],
        state["_scope_integration_role"],
        state["_scope_dir"],
        state["_state_path"],
    )

    try:
        with pytest.raises(
            ProphetXSessionLifecycleError,
            match="session scope authority changed",
        ):
            lifecycle.begin_login(
                now=NOW,
                access_token_available=False,
            )
    finally:
        records[id(lifecycle)] = original

    assert not redirected_dir.exists()
    assert not original_state_path.exists()


def test_equal_looking_scope_anchor_replacement_is_not_authority(tmp_path):
    lifecycle = ProphetXSessionLifecycle(tmp_path, scope=_scope())
    original_state_path = lifecycle.state_path

    records = _scope_authority_records()
    original = records[id(lifecycle)]
    replacement = tuple(list(original))
    assert replacement == original
    assert replacement is not original
    records[id(lifecycle)] = replacement

    try:
        with pytest.raises(
            ProphetXSessionLifecycleError,
            match="session scope authority changed",
        ):
            lifecycle.begin_login(
                now=NOW,
                access_token_available=False,
            )
    finally:
        records[id(lifecycle)] = original

    assert not original_state_path.exists()
