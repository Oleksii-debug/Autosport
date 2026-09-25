"""Autosport paper/replay laboratory."""

__version__ = "0.1.0"

# Install the PAPER execution durability/freshness guards before the public
# facade subclasses or calls the legacy compatibility implementation.
from . import _paper_execution_anti_rollback as _paper_execution_anti_rollback  # noqa: F401,E402
from . import _paper_execution_freshness as _paper_execution_freshness  # noqa: F401,E402
from . import _paper_execution_append_recovery as _paper_execution_append_recovery  # noqa: F401,E402
from . import _paper_value_execution_authority as _paper_value_execution_authority  # noqa: F401,E402
from . import _paper_value_risk_admission_recovery as _paper_value_risk_admission_recovery  # noqa: F401,E402

# Product PAPER execution must preserve which exact, already-durable DecisionLedger
# record existed before #623 RUN_RESERVED/attempt publication. This guard wraps the
# fully-composed execution runtime after the existing recovery/authority layers.
from . import _paper_execution_decision_origin as _paper_execution_decision_origin  # noqa: F401,E402
# Exact classes are insufficient if an instance shadows authority-bearing methods.
# Fence those dispatch points before product call-site authorization is installed.
from . import _paper_execution_decision_origin_instance_guard as _paper_execution_decision_origin_instance_guard  # noqa: F401,E402
# Canonical producer ancestry is not an ambient capability: only the exact direct
# product execute call may bind origin, while nested hooks fail before reservation.
from . import _paper_execution_decision_origin_callsite_guard as _paper_execution_decision_origin_callsite_guard  # noqa: F401,E402
# An origin-bound incomplete run may resume only after the product re-resolves the
# same durable DecisionLedger origin; generic/originless retry remains fail-closed.
from . import _paper_execution_decision_origin_resume_guard as _paper_execution_decision_origin_resume_guard  # noqa: F401,E402

# Bind explicit realized-VOC admissions to the exact canonical ResearchProtocol
# and protocol-derived cohort before the scoring facade is imported by consumers.
from . import _voc_admission_identity_guard as _voc_admission_identity_guard  # noqa: F401,E402

# A durable SUCCEEDED VOC producer receipt may finish publishing its exact output
# after restart, but historical requests never become generically live again.
from . import _voc_restart_publication_guard as _voc_restart_publication_guard  # noqa: F401,E402

# Once both paired shadow outputs are canonical, freeze the pre-outcome scoring
# record from product-owned router authority instead of caller-authored values.
from . import _voc_product_evidence_guard as _voc_product_evidence_guard  # noqa: F401,E402

# Failed/late/cancelled paired attempts close from immutable router execution
# authority exactly once, preserving denominator evidence across restart.
from . import _voc_product_negative_terminal_guard as _voc_product_negative_terminal_guard  # noqa: F401,E402

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

# Point-in-time feature evidence is positive only when the exact dataset lineage
# manifest already commits the exact typed DatasetSnapshot/FeatureSet/artifact
# provenance relation. The guard reuses the existing lineage/registry authorities.
from . import _point_in_time_feature_provenance_guard as _point_in_time_feature_provenance_guard  # noqa: F401,E402

# Exact-fence the lineage capability before any authority-bearing dispatch and let
# stale holdout process views re-resolve the same durable workspace binding/root.
from . import _point_in_time_authority_runtime_repair as _point_in_time_authority_runtime_repair  # noqa: F401,E402

# Structural cursor/range witnesses are useful legacy intake evidence but are not
# production provider-completeness authority. Install the fail-closed public gate;
# the supported denominator path consumes the exact live CompleteGameBoardSnapshot.
from . import _evaluation_universe_structural_gate as _evaluation_universe_structural_gate  # noqa: F401,E402

# Product-origin re-resolution must reject caller-polymorphic authority objects before
# any public property/method dispatch or iterable execution. Install this exact-type
# mint fence before #638 records the resulting canonical semantic-session issuance.
from . import _pre_evaluation_product_origin_type_fence as _pre_evaluation_product_origin_type_fence  # noqa: F401,E402

# Provider membership alone cannot authorize caller-created decision semantics. The
# production denominator must consume the exact product-owned pre-evaluation semantic
# capability before freezing the initial row set.
from . import _provider_evaluation_semantic_gate as _provider_evaluation_semantic_gate  # noqa: F401,E402

# A live #662 origin alone is transferable and cannot prove which exact semantic
# session the product derived. Record the canonical derivation result per live origin
# and require that exact semantic digest before #638 denominator admission.
from . import _provider_evaluation_semantic_issuance as _provider_evaluation_semantic_issuance  # noqa: F401,E402

# Campaign/provider applicability needs a stable authenticated Betfair account
# discriminator, but application/session credentials must never become evidence.
from . import _campaign_provider_scope_devapp_identity as _campaign_provider_scope_devapp_identity  # noqa: F401,E402

# A market-level commission receipt must stay tied to the exact canonical client
# origin that acquired/reacquired it. This prevents later source._client mutation
# from relabelling an already-issued receipt as another Betfair account.
from . import _betfair_market_commission_origin_binding as _betfair_market_commission_origin_binding  # noqa: F401,E402

# A later authenticated provider re-read validates the original T0 applicability
# scope; it must not mint a replacement projection merely because T1 evidence
# instance ids or timestamps changed after restart.
from . import _campaign_provider_scope_stable_projection as _campaign_provider_scope_stable_projection  # noqa: F401,E402

# Collector retention may physically delete historical rows only from durable desktop
# application acknowledgement. Pin those reads to the exact checkpoint class so a
# mutable exact instance cannot shadow methods and mint deletion authority.
from . import _collector_retention_desktop_ack_authority as _collector_retention_desktop_ack_authority  # noqa: F401,E402

# ScientificRegistry successors are protected by an independent machine authority;
# every read must consume the exact authority-current image before causal witnesses
# can be minted from it.
from . import _scientific_registry_read_authority as _scientific_registry_read_authority  # noqa: F401,E402

# Snapshot the final product-loaded lineage/registry concrete class surfaces. Exact
# authority instances must not dispatch through caller-replaced class implementations,
# and explicit runtime-repair reloads must restore this seal before positive use.
from . import _point_in_time_class_dispatch_seal as _point_in_time_class_dispatch_seal  # noqa: F401,E402

# Sequential multiplicity evidence and PromotionEvidence live in separate durable
# journals. Seal the registry prefix observed at look registration so a later write
# can never retroactively authorize an already-durable promotion record.
from . import _trial_family_cross_ledger_witness as _trial_family_cross_ledger_witness  # noqa: F401,E402

# Replay must recognize the cross-ledger witness kind, but callers must not mint that
# authority through the legacy generic trial-event append seam.
from . import _trial_family_witness_mint_guard as _trial_family_witness_mint_guard  # noqa: F401,E402

# Product PolicyEvaluation issuance already exact-fences the FactoryArtifactStore
# surface. Seal the lower canonical file reader that _stable_snapshot dispatches to
# so a class/module rebind cannot inject forged bytes beneath that trusted surface.
from . import _policy_evaluation_canonical_reader_authority as _policy_evaluation_canonical_reader_authority  # noqa: F401,E402

# Robust portfolio stakes are monetary grid values, not Decimal exponent values.
# Install the exact arbitrary-quantum floor after the owning proposal implementation.
from . import _robust_portfolio_quantum_grid as _robust_portfolio_quantum_grid  # noqa: F401,E402

# Reproducibility bundles contain Experiment outcome. Their outward file-export path
# therefore consumes the canonically resolved confirmation holdout before publication;
# legacy direct export is fail-closed so callers cannot substitute holdout identity.
from . import scientific_disclosure_export as _scientific_disclosure_export  # noqa: F401,E402
from . import _scientific_disclosure_export_reload_guard as _scientific_disclosure_export_reload_guard  # noqa: F401,E402
