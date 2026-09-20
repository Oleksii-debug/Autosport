"""Autosport paper/replay laboratory."""

__version__ = "0.1.0"

# Install durability/freshness authority guards before public facades import the
# compatibility implementations they protect.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401,E402
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401,E402
from . import _historical_capture_authority_guard as _historical_capture_authority_guard  # noqa: F401,E402
