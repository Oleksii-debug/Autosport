"""Explicit first-run product workspace identity initialization.

This module composes Autosport's existing durable workspace binding primitive into a
small product-level first-run contract. It does not create a second workspace
identity system: the canonical WorkspaceIdentityBinding remains the only persisted
identity authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .monotonic_workspace_authority import (
    MonotonicAuthorityConfigurationError,
    resolve_monotonic_authority_root,
)
from .monotonic_workspace_binding import (
    BINDING_SCHEMA_VERSION,
    WorkspaceBindingConflictError,
    WorkspaceBindingError,
    WorkspaceIdentityBinding,
)


PRODUCT_WORKSPACE_BINDING_SCHEMA_VERSION: Final = BINDING_SCHEMA_VERSION


class ProductWorkspaceInitializationError(RuntimeError):
    """The canonical product workspace identity cannot be initialized safely."""


@dataclass(frozen=True, slots=True)
class ProductWorkspaceInitialization:
    """Verified durable identity established for one product workspace."""

    workspace: Path
    workspace_instance_id: str
    workspace_marker_path: Path
    path_binding_path: Path
    binding_schema_version: int = PRODUCT_WORKSPACE_BINDING_SCHEMA_VERSION


def _absolute_workspace(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError, RuntimeError) as exc:
        raise ProductWorkspaceInitializationError(
            "product workspace path cannot be resolved"
        ) from exc
    if not path.is_absolute():
        raise ProductWorkspaceInitializationError(
            "product workspace must be an absolute path"
        )
    return path


def _resolve_binding(
    *,
    workspace: Path,
    authority_root: Path,
) -> WorkspaceIdentityBinding:
    return WorkspaceIdentityBinding.resolve(
        workspace=workspace,
        authority_root=authority_root,
        requested_workspace_instance_id=None,
    )


def initialize_product_workspace(
    workspace: str | Path,
    *,
    authority_root: str | Path | None = None,
) -> ProductWorkspaceInitialization:
    """Establish and verify one durable identity for a first-run workspace.

    Concurrent pristine initializers can provision different in-memory UUID
    candidates before either one publishes. The canonical binding's exclusive
    create then chooses exactly one durable identity. A loser retries resolution
    once from the now-durable evidence and converges to that identity.

    The same retry also repairs the safe crash prefix where the machine path
    binding was published but the workspace-local marker was not yet published.
    Corrupt or contradictory durable evidence still fails closed.
    """

    workspace_path = _absolute_workspace(workspace)
    try:
        resolved_authority_root = resolve_monotonic_authority_root(
            workspace_path,
            authority_root,
        )
    except MonotonicAuthorityConfigurationError as exc:
        raise ProductWorkspaceInitializationError(
            "product workspace and machine identity authority cannot be configured safely"
        ) from exc

    last_conflict: WorkspaceBindingConflictError | None = None
    for attempt in range(2):
        try:
            binding = _resolve_binding(
                workspace=workspace_path,
                authority_root=resolved_authority_root,
            )
            binding.ensure_bound()
            workspace_bound, path_bound = binding.validate_existing(
                register_moved_or_copied_path=False
            )
            if not workspace_bound or not path_bound:
                raise ProductWorkspaceInitializationError(
                    "product workspace identity publication is incomplete"
                )
            return ProductWorkspaceInitialization(
                workspace=workspace_path,
                workspace_instance_id=binding.workspace_instance_id,
                workspace_marker_path=binding.workspace_marker_path,
                path_binding_path=binding.path_binding_path,
            )
        except WorkspaceBindingConflictError as exc:
            last_conflict = exc
            if attempt == 0:
                continue
            break
        except WorkspaceBindingError as exc:
            raise ProductWorkspaceInitializationError(
                "cannot verify durable product workspace identity"
            ) from exc
        except OSError as exc:
            raise ProductWorkspaceInitializationError(
                "cannot persist durable product workspace identity"
            ) from exc

    raise ProductWorkspaceInitializationError(
        "concurrent product workspace initialization did not converge"
    ) from last_conflict
