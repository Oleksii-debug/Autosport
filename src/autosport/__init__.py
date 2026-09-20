"""Autosport paper/replay laboratory."""

__version__ = "0.1.0"

# Install the PAPER execution durability/freshness guards before the public
# facade subclasses or calls the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401,E402
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401,E402
from . import _paper_execution_append_recovery as _paper_execution_append_recovery  # noqa: F401,E402
from . import _paper_value_execution_authority as _paper_value_execution_authority  # noqa: F401,E402
