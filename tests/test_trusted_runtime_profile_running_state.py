from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import autosport.trusted_runtime_code_profile as profile_module
from autosport.continuous_session import SessionState


_FACTORY_SPEC = "autosport.product_source:create_parlay_product_source"
_PROVIDER_SOURCE_ID = "parlayapi:table_tennis"


class _StatefulRuntime:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.manifest = SimpleNamespace(source_id=_PROVIDER_SOURCE_ID)
        self.state = SessionState.RUNNING


def test_stopped_runtime_invalidates_profile_before_origin_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _StatefulRuntime(tmp_path)
    entry = SimpleNamespace(
        source_id="parlayapi-table-tennis",
        factory_spec=_FACTORY_SPEC,
        expected_provider_source_id=_PROVIDER_SOURCE_ID,
    )

    monkeypatch.setattr(profile_module, "AutonomousProductRuntime", _StatefulRuntime)
    monkeypatch.setattr(profile_module, "_CANONICAL_RUNTIME_TYPE", _StatefulRuntime)
    monkeypatch.setattr(
        profile_module,
        "_CANONICAL_RUNTIME_STATUS",
        lambda value: SimpleNamespace(state=value.state),
    )
    monkeypatch.setattr(
        profile_module,
        "require_product_owned_source_factory_identity",
        lambda **_kwargs: entry,
    )

    profile_module._register_started_product_runtime_origin(
        runtime,
        source_factory=_FACTORY_SPEC,
        expected_provider_source_id=_PROVIDER_SOURCE_ID,
    )
    profile = profile_module.issue_trusted_runtime_code_profile(runtime)
    try:
        assert profile_module.is_authoritative_trusted_runtime_code_profile(
            profile,
            workspace=tmp_path,
        )

        # Reproduce the worker's normal terminal ordering: runtime.stop() makes the
        # durable session STOPPED before finally revokes the profile/origin record.
        runtime.state = SessionState.STOPPED

        assert not profile_module.is_authoritative_trusted_runtime_code_profile(
            profile,
            workspace=tmp_path,
        )
    finally:
        profile_module.revoke_trusted_runtime_code_profile(profile)
        profile_module._clear_started_product_runtime_origin(runtime)
