"""Autosport paper/replay laboratory."""

# Install the PAPER execution durability/freshness guards before the public
# facade subclasses or calls the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401

__version__ = "0.1.0"
