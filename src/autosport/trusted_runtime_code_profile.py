"""Process-local evidence for the closed packaged product runtime code profile.

This authority proves one narrow prerequisite only: an exact running
AutonomousProductRuntime was started by the packaged ProductGuiWorker path from a
product-shipped closed source-registry entry whose expected provider source identity
matches the durable runtime composition.

It is deliberately not a sandbox. Arbitrary hostile code already executing with full
Python interpreter privileges is outside this authority's enforceable boundary. The
profile never authorizes a provider write or REAL-money execution by itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from secrets import token_hex
from threading import RLock

from .collector_service import _load_source_factory
from .operator_source_registry import (
    OperatorSourceRegistryError,
    resolve_product_source_runtime_binding,
)
from .product_runtime import AutonomousProductRuntime


PROFILE_SCHEMA = "autosport.trusted_product_runtime_code_profile"
PROFILE_SCHEMA_VERSION = 1
TRUST_BOUNDARY = "TRUSTED_PRODUCT_INTERPRETER"


class TrustedRuntimeCodeProfileError(RuntimeError):
    """The supported product runtime code-origin prerequisite is not proven."""


@dataclass(frozen=True, slots=True)
class TrustedRuntimeCodeProfile:
    """Public evidence bytes; authority remains process-local and origin-bound."""

    profile_id: str
    operator_source_id: str
    factory_spec: str
    provider_source_id: str
    workspace: str
    trust_boundary: str = TRUST_BOUNDARY

    def __post_init__(self) -> None:
        if (
            type(self.profile_id) is not str
            or not self.profile_id.startswith("trusted-runtime:")
            or len(self.profile_id) != len("trusted-runtime:") + 64
            or any(
                character not in "0123456789abcdef"
                for character in self.profile_id.removeprefix("trusted-runtime:")
            )
        ):
            raise TrustedRuntimeCodeProfileError("profile_id is not canonical")
        for field, value in (
            ("operator_source_id", self.operator_source_id),
            ("factory_spec", self.factory_spec),
            ("provider_source_id", self.provider_source_id),
            ("workspace", self.workspace),
        ):
            if type(value) is not str or not value or value.strip() != value:
                raise TrustedRuntimeCodeProfileError(
                    f"{field} must be a non-empty trimmed string"
                )
        if self.trust_boundary != TRUST_BOUNDARY:
            raise TrustedRuntimeCodeProfileError("trust_boundary is product-owned")

    @property
    def provider_write_authorized(self) -> bool:
        """A code-origin prerequisite is never provider-write authority by itself."""

        return False

    @property
    def real_money_execution_authorized(self) -> bool:
        return False

    @property
    def evidence_sha256(self) -> str:
        payload = {
            "schema": PROFILE_SCHEMA,
            "schema_version": PROFILE_SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "operator_source_id": self.operator_source_id,
            "factory_spec": self.factory_spec,
            "provider_source_id": self.provider_source_id,
            "workspace": self.workspace,
            "trust_boundary": self.trust_boundary,
            "provider_write_authorized": False,
            "real_money_execution_authorized": False,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class _IssuedRuntimeProfile:
    profile: TrustedRuntimeCodeProfile
    evidence_sha256: str
    runtime: AutonomousProductRuntime


_LOCK = RLock()
_ISSUED: dict[int, _IssuedRuntimeProfile] = {}
_ACTIVE_BY_RUNTIME: dict[int, int] = {}
_ACTIVE_BY_WORKSPACE: dict[str, int] = {}


def _workspace_text(runtime: AutonomousProductRuntime) -> str:
    try:
        resolved = runtime.workspace.resolve()
    except (AttributeError, OSError, RuntimeError) as exc:
        raise TrustedRuntimeCodeProfileError(
            "runtime workspace cannot be resolved canonically"
        ) from exc
    text = str(resolved)
    if not text or text.strip() != text:
        raise TrustedRuntimeCodeProfileError("runtime workspace is not canonical")
    return text


def require_product_owned_source_factory_identity(
    *,
    source_factory: str,
    expected_provider_source_id: str,
):
    """Require the dynamic loader to resolve the exact product-imported callable."""

    try:
        entry, canonical_factory = resolve_product_source_runtime_binding(
            source_factory,
            expected_provider_source_id,
        )
        observed_factory = _load_source_factory(source_factory)
    except (OperatorSourceRegistryError, ImportError, AttributeError, TypeError, ValueError) as exc:
        raise TrustedRuntimeCodeProfileError(
            "runtime code profile requires one exact product-shipped source binding"
        ) from exc
    if observed_factory is not canonical_factory:
        raise TrustedRuntimeCodeProfileError(
            "registered product source factory callable identity has drifted"
        )
    return entry


def issue_trusted_runtime_code_profile(
    runtime: AutonomousProductRuntime,
    *,
    source_factory: str,
    expected_provider_source_id: str,
) -> TrustedRuntimeCodeProfile:
    """Issue one active profile for the exact running closed-registry runtime.

    The caller must invoke this only after runtime.start() succeeds. Direct/headless
    module:function construction intentionally has no issuance path.
    """

    if type(runtime) is not AutonomousProductRuntime:
        raise TrustedRuntimeCodeProfileError(
            "trusted runtime profile requires exact AutonomousProductRuntime"
        )
    entry = require_product_owned_source_factory_identity(
        source_factory=source_factory,
        expected_provider_source_id=expected_provider_source_id,
    )
    if runtime.manifest.source_id != expected_provider_source_id:
        raise TrustedRuntimeCodeProfileError(
            "runtime manifest source_id does not match the closed registry"
        )
    workspace = _workspace_text(runtime)
    runtime_identity = id(runtime)

    with _LOCK:
        existing_profile_identity = _ACTIVE_BY_RUNTIME.get(runtime_identity)
        if existing_profile_identity is not None:
            existing = _ISSUED.get(existing_profile_identity)
            if existing is not None and existing.runtime is runtime:
                if is_authoritative_trusted_runtime_code_profile(
                    existing.profile,
                    workspace=workspace,
                ):
                    return existing.profile
            raise TrustedRuntimeCodeProfileError(
                "runtime has an inconsistent active code profile"
            )
        if workspace in _ACTIVE_BY_WORKSPACE:
            raise TrustedRuntimeCodeProfileError(
                "workspace already has an active trusted runtime profile"
            )

        profile = TrustedRuntimeCodeProfile(
            profile_id=f"trusted-runtime:{token_hex(32)}",
            operator_source_id=entry.source_id,
            factory_spec=entry.factory_spec,
            provider_source_id=entry.expected_provider_source_id,
            workspace=workspace,
        )
        profile_identity = id(profile)
        _ISSUED[profile_identity] = _IssuedRuntimeProfile(
            profile=profile,
            evidence_sha256=profile.evidence_sha256,
            runtime=runtime,
        )
        _ACTIVE_BY_RUNTIME[runtime_identity] = profile_identity
        _ACTIVE_BY_WORKSPACE[workspace] = profile_identity
        return profile


def is_authoritative_trusted_runtime_code_profile(
    value: object,
    *,
    workspace: str | Path | None = None,
) -> bool:
    """Return whether value is the exact unchanged active process issuance."""

    if type(value) is not TrustedRuntimeCodeProfile:
        return False
    with _LOCK:
        record = _ISSUED.get(id(value))
        if record is None or record.profile is not value:
            return False
        runtime = record.runtime
        if type(runtime) is not AutonomousProductRuntime:
            return False
        if _ACTIVE_BY_RUNTIME.get(id(runtime)) != id(value):
            return False
        if _ACTIVE_BY_WORKSPACE.get(value.workspace) != id(value):
            return False
        if record.evidence_sha256 != value.evidence_sha256:
            return False
        try:
            runtime_workspace = _workspace_text(runtime)
            entry = require_product_owned_source_factory_identity(
                source_factory=value.factory_spec,
                expected_provider_source_id=value.provider_source_id,
            )
        except TrustedRuntimeCodeProfileError:
            return False
        if (
            runtime_workspace != value.workspace
            or runtime.manifest.source_id != value.provider_source_id
            or entry.source_id != value.operator_source_id
        ):
            return False
        if workspace is not None:
            try:
                requested_workspace = str(Path(workspace).resolve())
            except (OSError, RuntimeError, TypeError, ValueError):
                return False
            if requested_workspace != value.workspace:
                return False
        return True


def require_authoritative_trusted_runtime_code_profile(
    value: object,
    *,
    workspace: str | Path | None = None,
) -> TrustedRuntimeCodeProfile:
    if not is_authoritative_trusted_runtime_code_profile(value, workspace=workspace):
        raise TrustedRuntimeCodeProfileError(
            "trusted product runtime code profile is not current and authoritative"
        )
    assert type(value) is TrustedRuntimeCodeProfile
    return value


def revoke_trusted_runtime_code_profile(value: object) -> bool:
    """Revoke one exact issuance; copied/forged values cannot revoke authority."""

    if type(value) is not TrustedRuntimeCodeProfile:
        return False
    with _LOCK:
        record = _ISSUED.get(id(value))
        if record is None or record.profile is not value:
            return False
        _ISSUED.pop(id(value), None)
        runtime_identity = id(record.runtime)
        if _ACTIVE_BY_RUNTIME.get(runtime_identity) == id(value):
            _ACTIVE_BY_RUNTIME.pop(runtime_identity, None)
        if _ACTIVE_BY_WORKSPACE.get(value.workspace) == id(value):
            _ACTIVE_BY_WORKSPACE.pop(value.workspace, None)
        return True
