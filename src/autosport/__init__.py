"""Autosport paper/replay laboratory."""

__version__ = "0.1.0"

# Install the PAPER execution durability/freshness guards before the public
# facade subclasses or calls the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401,E402
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401,E402

# Provider-origin credentials must never inherit a caller-selected generic
# monotonic trust root; keep that origin proof on the production machine root.
from . import _provider_receipt_trust_root as _provider_receipt_trust_root  # noqa: F401,E402