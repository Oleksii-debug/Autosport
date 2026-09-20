"""Autosport paper/replay laboratory."""

__version__ = "0.1.0"

# Install the PAPER execution durability/freshness guards before the public
# facade subclasses or calls the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401,E402
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401,E402

# Preserve immutable schema-v1 DatasetSnapshot ancestry proof identities while
# requiring an authority-owned causal re-observation witness before those proofs
# may authorize a later-session activation.
from . import _dataset_snapshot_lineage_publication as _dataset_snapshot_lineage_publication  # noqa: F401,E402
from . import _dataset_snapshot_lineage_publication_provenance as _dataset_snapshot_lineage_publication_provenance  # noqa: F401,E402
