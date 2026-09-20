"""Autosport paper/replay laboratory."""

__version__ = "0.1.0"

# Install the PAPER execution durability/freshness guards before the public
# facade subclasses or calls the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401,E402
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401,E402

# Bind explicit realized-VOC admissions to the exact canonical ResearchProtocol
# and protocol-derived cohort before the scoring facade is imported by consumers.
from . import _voc_admission_identity_guard as _voc_admission_identity_guard  # noqa: F401,E402

# A durable SUCCEEDED VOC producer receipt may finish publishing its exact output
# after restart, but historical requests never become generically live again.
from . import _voc_restart_publication_guard as _voc_restart_publication_guard  # noqa: F401,E402

# Once both paired shadow outputs are canonical, freeze the pre-outcome scoring
# record from product-owned router authority instead of caller-authored values.
from . import _voc_product_evidence_guard as _voc_product_evidence_guard  # noqa: F401,E402

# Provider completeness is positive only for an exact live canonical acquisition.
# Local persisted bytes/journals remain integrity evidence and fail closed across
# restart because the current provider contract supplies no non-caller-mintable
# remote/OS attestation that could truthfully recreate provider origin.
from . import _provider_receipt_trust_root as _provider_receipt_trust_root  # noqa: F401,E402

# Authenticated complete-board acquisition must never follow an HTTP redirect:
# doing so could forward X-API-Key to another origin before response validation.
from . import _provider_transport_origin as _provider_transport_origin  # noqa: F401,E402

# Autosport-owned v1 provider evidence/request envelopes are exact schemas. Keep
# provider frame JSON extensible/content-bound, but reject unknown local envelope
# fields before normalization or monotonic integrity validation.
from . import _provider_observation_payload_strictness as _provider_observation_payload_strictness  # noqa: F401,E402

# Install the fail-closed predictive runtime authority bridge before callers import
# decision modules.  The import is intentionally private; public APIs remain in the
# owning opportunity/predictive modules.
from . import predictive_authority as _predictive_authority  # noqa: E402,F401
from . import _predictive_authority_type_fence as _predictive_authority_type_fence  # noqa: E402,F401

# Product chrome describes one finished Autosport product. Keep internal/versioned
# strategy and evidence identities intact while removing legacy V1 product framing.
from . import _whole_product_title_guard as _whole_product_title_guard  # noqa: F401,E402

# Preserve immutable schema-v1 DatasetSnapshot ancestry proof identities while
# requiring an authority-owned causal re-observation witness before those proofs
# may authorize a later-session activation.
from . import _dataset_snapshot_lineage_publication as _dataset_snapshot_lineage_publication  # noqa: F401,E402
from . import _dataset_snapshot_lineage_publication_provenance as _dataset_snapshot_lineage_publication_provenance  # noqa: F401,E402
from . import _dataset_snapshot_lineage_publication_trust_root as _dataset_snapshot_lineage_publication_trust_root  # noqa: F401,E402
