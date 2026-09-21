"""First-run bootstrap for the canonical durable Autosport workspace identity.

This module exposes the existing monotonic workspace binding as a narrow product
bootstrap contract. It deliberately does not create a second workspace identity,
configuration store, lock protocol, or durability authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .monotonic_workspace_authority import (
    MonotonicAuthorityConfigurationError,
    MonotonicAuthorityIntegrityError,
    resolve_monotonic_authority_root,
)
from .monotonic_workspace_binding import (
    WorkspaceBindingConflictError,
    WorkspaceBindingIntegrityError,
    WorkspaceIdentityBinding,
)


class WorkspaceBootstrapError(RuntimeError):
    """The canonical product workspace identity could not be established safely."""


@dataclass(frozen=True, slots=True)
class WorkspaceBootstrap:
    """Verified product-facing view of the canonical workspace identity binding."""

    workspace: Path
    workspace_instance_id: str
    workspace_marker_path: Path
    path_binding_path: Path

    def __post_init__(self) -> None:
        if not self.workspace.is_absolute():
            raise WorkspaceBootstrapError("workspace must be absolute")
        if (
            type(self.workspace_instance_id) is not str
            or not self.workspace_instance_id
            or self.workspace_instance_id != self.workspace_instance_id.strip()
        ):
            raise WorkspaceBootstrapError(
                "workspace_instance_id must be a non-empty canonical string"
            )
        if not self.workspace_marker_path.is_absolute():
            raise WorkspaceBootstrapError("workspace_marker_path must be absolute")
        if not self.path_binding_path.is_absolute():
            raise WorkspaceBootstrapError("path_binding_path must be absolute")


def _absolute_workspace(workspace: str | Path) -> Path:
    try:
        path = Path(workspace).expanduser()
    except (RuntimeError, TypeError) as exc:
        raise WorkspaceBootstrapError(
            "workspace path could not be resolved"
        ) from exc
    if not path.is_absolute():
        raise WorkspaceBootstrapError(
            "workspace must be an absolute path so first-run identity is CWD-independent"
        )
    return path


def bootstrap_workspace(
    workspace: str | Path,
    *,
    authority_root: str | Path | None = None,
) -> WorkspaceBootstrap:
    """Establish or reopen exactly one canonical durable workspace identity.

    The underlying WorkspaceIdentityBinding owns identity generation and two-root
    durability. Its path receipt is published first and the workspace-local marker
    second. Therefore a crash between the two publications can be retried without
    minting a new identity, and concurrent first-run initializers either converge on
    one already-durable identity or one fails explicitly.

    This call is safe to repeat across process restarts. It never accepts a caller-
    supplied workspace instance id.
    """

    workspace_path = _absolute_workspace(workspace)
    try:
        resolved_authority_root = resolve_monotonic_authority_root(
            workspace_path, authority_root
        )
        binding = WorkspaceIdentityBinding.resolve(
            workspace=workspace_path,
            authority_root=resolved_authority_root,
            requested_workspace_instance_id=None,
        )
        binding.ensure_bound()
        has_local_marker, has_machine_path_binding = binding.validate_existing()
    except (
        WorkspaceBindingConflictError,
        WorkspaceBindingIntegrityError,
        MonotonicAuthorityConfigurationError,
        MonotonicAuthorityIntegrityError,
        OSError,
    ) as exc:
        raise WorkspaceBootstrapError(
            "cannot establish canonical durable workspace identity"
        ) from exc

    if not has_local_marker or not has_machine_path_binding:
        raise WorkspaceBootstrapError(
            "workspace identity bootstrap did not publish both canonical bindings"
        )

    return WorkspaceBootstrap(
        workspace=workspace_path,
        workspace_instance_id=binding.workspace_instance_id,
        workspace_marker_path=binding.workspace_marker_path,
        path_binding_path=binding.path_binding_path,
    )
