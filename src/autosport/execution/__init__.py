"""Execution-domain composition guards."""

# Import the owning feasibility module first, then bind its positive-result
# convenience property to the exact canonical issuance verifier.  This keeps the
# existing resolver/issuance authority canonical while removing a writable
# module-global verifier seam from ordinary imports.
from . import feasibility as _feasibility  # noqa: F401,E402
from . import _feasibility_result_authority_guard as _feasibility_result_authority_guard  # noqa: F401,E402
