"""Autosport paper/replay laboratory."""

# Install the PAPER execution anti-rollback witness before the public facade
# subclasses the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401

__version__ = "0.1.0"
