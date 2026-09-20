"""Autosport paper/replay laboratory."""

__version__ = "0.1.0"

# Install the PAPER execution durability/freshness guards before the public
# facade subclasses or calls the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401,E402
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401,E402

# Bind explicit realized-VOC admissions to the exact canonical ResearchProtocol
# and protocol-derived cohort before the scoring facade is imported by consumers.
from . import _voc_admission_identity_guard as _voc_admission_identity_guard  # noqa: F401,E402
