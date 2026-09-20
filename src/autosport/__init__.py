"""Autosport paper/replay laboratory."""

__version__ = "0.1.0"

# Install the PAPER execution durability/freshness guards before the public
# facade subclasses or calls the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401,E402
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401,E402

# Route point-in-time holdout durability through the single shared machine-state
# MonotonicWorkspaceAuthority rather than a workspace-local rollback marker.
from . import _point_in_time_shared_monotonic as _point_in_time_shared_monotonic  # noqa: F401,E402
