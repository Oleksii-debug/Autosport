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

# Autosport-owned v1 provider evidence/request envelopes are exact schemas. Keep
# provider frame JSON extensible/content-bound, but reject unknown local envelope
# fields before normalization or monotonic integrity validation.
from . import _provider_observation_payload_strictness as _provider_observation_payload_strictness  # noqa: F401,E402

# Positive pre-evaluation evidence must come from the product-owned, capability-
# guarded derivation path. Reject direct public construction of the exact evidence
# and binding classes before callers can mint self-consistent authoritative facts.
from . import _pre_evaluation_fact_gate as _pre_evaluation_fact_gate  # noqa: F401,E402

# The product-origin bridge must not be callable with caller-created lookalikes for
# provider snapshots, provider selection bindings or durable economic evidence.
# Install this after the fact-construction gate so both authorities compose.
from . import _pre_evaluation_provider_origin_gate as _pre_evaluation_provider_origin_gate  # noqa: F401,E402

# Structural cursor/range witnesses are useful legacy intake evidence but are not
# production provider-completeness authority. Install the fail-closed public gate;
# the supported denominator path consumes the exact live CompleteGameBoardSnapshot.
from . import _evaluation_universe_structural_gate as _evaluation_universe_structural_gate  # noqa: F401,E402

# Provider membership alone cannot authorize caller-created decision semantics. The
# production denominator must consume the exact product-owned pre-evaluation semantic
# capability before freezing the initial row set.
from . import _provider_evaluation_semantic_gate as _provider_evaluation_semantic_gate  # noqa: F401,E402
