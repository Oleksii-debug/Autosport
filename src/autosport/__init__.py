"""Autosport paper/replay laboratory."""

__version__ = "0.1.0"

# Install the PAPER execution durability/freshness guards before the public
# facade subclasses or calls the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401,E402
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401,E402

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

# Sport-memory durable positive materialization is a product composition authority,
# not a caller-mintable generation digest. Install the public authority guard first,
# then the durable cross-store transaction guard that composes with it.
from . import _sport_memory_authority_guard as _sport_memory_authority_guard  # noqa: F401,E402
from . import _sport_memory_cross_store_guard as _sport_memory_cross_store_guard  # noqa: F401,E402

# Product chrome describes one finished Autosport product. Keep internal/versioned
# strategy and evidence identities intact while removing legacy V1 product framing.
from . import _whole_product_title_guard as _whole_product_title_guard  # noqa: F401,E402

# Preserve immutable schema-v1 DatasetSnapshot ancestry proof identities while
# requiring an authority-owned causal re-observation witness before those proofs
# may authorize a later-session activation.
from . import _dataset_snapshot_lineage_publication as _dataset_snapshot_lineage_publication  # noqa: F401,E402
from . import _dataset_snapshot_lineage_publication_provenance as _dataset_snapshot_lineage_publication_provenance  # noqa: F401,E402
from . import _dataset_snapshot_lineage_publication_trust_root as _dataset_snapshot_lineage_publication_trust_root  # noqa: F401,E402

# Campaign/provider applicability needs a stable authenticated Betfair account
# discriminator, but application/session credentials must never become evidence.
from . import _campaign_provider_scope_devapp_identity as _campaign_provider_scope_devapp_identity  # noqa: F401,E402

# A later authenticated provider re-read validates the original T0 applicability
# scope; it must not mint a replacement projection merely because T1 evidence
# instance ids or timestamps changed after restart.
from . import _campaign_provider_scope_stable_projection as _campaign_provider_scope_stable_projection  # noqa: F401,E402
