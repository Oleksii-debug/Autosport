"""Autosport paper/replay laboratory."""

__version__ = "0.1.0"

# Install the PAPER execution durability/freshness guards before the public
# facade subclasses or calls the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401,E402
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401,E402

# Install the fail-closed predictive runtime authority bridge before callers import
# decision modules.  The import is intentionally private; public APIs remain in the
# owning opportunity/predictive modules.
from . import predictive_authority as _predictive_authority  # noqa: E402,F401
