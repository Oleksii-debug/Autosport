from __future__ import annotations

import os
from pathlib import Path

import pytest

from autosport.deployment_runtime_authority import (
    DeploymentRuntimeAuthorityError,
    DeploymentRuntimeAuthorityStore,
)
from autosport.learning_environment import EnvironmentIdentity, Episode


def _append(store: DeploymentRuntimeAuthorityStore):
    environment = EnvironmentIdentity(
        source_id="paper-provider",
        config_id="config-post-init-hardlink",
        data_id="dataset-post-init-hardlink",
        protocol_id="protocol-v1",
        cutoff_ts="2026-09-22T13:00:00Z",
        seed=7,
    )
    episode = Episode(
        environment_id=environment.environment_id,
        episode_key="episode-post-init-hardlink",
        policy_id="policy-post-init-hardlink",
        admissible_actions=("WAIT",),
    )
    return store.append(
        environment=environment,
        episode=episode,
        action_semantics_version="paper-actions-v1",
        action_semantics_meanings=(
            ("WAIT", "Observe only; do not create an external effect."),
        ),
    )


def test_hard_link_created_after_store_open_fails_closed_on_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-authority.json"
    alias = tmp_path / "runtime-authority-late-hardlink-read.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(path)

    try:
        os.link(path, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable in this environment: {exc}")

    assert path.stat().st_nlink == 2
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="hard-linked",
    ):
        store.records()

    assert os.path.samefile(path, alias)


def test_hard_link_created_after_store_open_fails_closed_before_append(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-authority.json"
    alias = tmp_path / "runtime-authority-late-hardlink.json"
    store = DeploymentRuntimeAuthorityStore.initialize_pristine(path)

    # The existing constructor-time nlink check has already passed here.  A later
    # hard link must still be rejected before os.replace can detach `path` from the
    # alias and leave two independently acceptable nlink=1 histories.
    try:
        os.link(path, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable in this environment: {exc}")

    assert path.stat().st_nlink == 2
    assert alias.stat().st_nlink == 2

    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="hard-linked",
    ):
        _append(store)

    # A rejected append must leave both names on the same pristine inode/state.
    assert os.path.samefile(path, alias)
    assert DeploymentRuntimeAuthorityStore.__name__
