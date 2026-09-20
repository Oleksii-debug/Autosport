"""Autosport paper/replay laboratory."""

__version__ = "0.1.0"

# Install the PAPER execution durability/freshness guards before the public
# facade subclasses or calls the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401,E402
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401,E402

# Complete-board freshness is fenced by the generic machine-state journal, while
# provider acquisition origin is a separate production-owned authenticated fact.
# Install this package guard before callers import the public provider module.
from . import _provider_complete_board_provenance as _provider_complete_board_provenance  # noqa: F401,E402
