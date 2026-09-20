"""Autosport paper/replay laboratory."""

__version__ = "0.1.0"

# Install the PAPER execution durability/freshness guards before the public
# facade subclasses or calls the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401,E402
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401,E402

# Preserve the point-in-time/historical capture authority guard from #620.
from . import _historical_capture_authority_guard as _historical_capture_authority_guard  # noqa: F401,E402

# A collector source admitted by the canonical durable application bridge must
# not retain a second caller-authored point-in-time authority family.
from . import _collector_point_in_time_authority_guard as _collector_point_in_time_authority_guard  # noqa: F401,E402

# The canonical provider source identity is reserved independently of caller-
# selected witness-kind spelling; generic PIT authority cannot impersonate it.
from . import _provider_point_in_time_source_guard as _provider_point_in_time_source_guard  # noqa: F401,E402

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
