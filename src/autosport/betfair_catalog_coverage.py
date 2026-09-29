from __future__ import annotations

"""Deterministic durable Betfair catalogue coverage over one bounded provider scope.

This module composes, rather than replaces, existing authorities:
- #1137 canonical Betfair catalogue requests/parsers;
- #1180 canonical CollectorDeltaStore/source-universe commitment;
- #2024 live authenticated discovery transport-origin receipts.

Coverage plans and leaf START/terminal rows live in the same collector SQLite
database. Durable rows prove plan/partition integrity only. Positive provider-visible
scope completeness additionally requires the exact current-process authenticated
receipts for every successful provider leaf; restart therefore fails closed until
canonical authenticated reacquisition.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence

from . import betfair_multisport_catalog as _catalog

from .betfair_account_identity import (
    require_authoritative_betfair_account_identity,
    resolve_betfair_authenticated_account_identity,
)
from .betfair_account_readonly import BetfairReadOnlyClient
from .betfair_discovery_provenance import (
    BetfairDiscoveryProvenanceError,
    BetfairDiscoveryVisibilityScope,
)
from .betfair_discovery_transport_origin import (
    BetfairAuthenticatedDiscoveryAcquisition,
    resolve_authoritative_betfair_authenticated_discovery_result,
)
from .betfair_multisport_catalog import (
    LIST_MARKET_CATALOGUE,
    BetfairCatalogRequest,
)
from .causal_collector import CollectorDeltaStore
from .source_universe_commitment import (
    SourceUniverseCommitment,
    SourceUniverseCommitmentError,
    build_source_universe_commitment,
    verify_source_universe_commitment,
)


SCHEMA_VERSION = 2
PROVIDER_ID = "BETFAIR"
PARTITION_ALGORITHM_VERSION = "betfair-catalog-categorical-partition-v2"
_TERMINAL_STATUSES = frozenset(
    {
        "SUCCESS",
        "EMPTY",
        "SATURATED_SPLIT",
        "SATURATED_UNSPLITTABLE",
        "FAILURE",
        "AFTER_CAUSAL_CUTOFF",
    }
)
_POSITIVE_PROVIDER_TERMINALS = frozenset(
    {"SUCCESS", "EMPTY", "SATURATED_SPLIT"}
)
_SCOPE_SPLIT_FIELDS = (
    "eventTypeIds",
    "competitionIds",
    "eventIds",
    "marketTypeCodes",
)
_CANONICAL_CONNECT = CollectorDeltaStore._connect


class BetfairCatalogCoverageError(RuntimeError):
    """Coverage evidence cannot support the requested authority."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairCatalogCoverageError(
            "catalogue coverage evidence is not canonical JSON"
        ) from exc


def _sha_payload(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairCatalogCoverageError(
            f"{field} must be non-empty trimmed text"
        )
    return value


def _sha(value: object, field: str) -> str:
    raw = _text(value, field)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise BetfairCatalogCoverageError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return raw


def _utc(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BetfairCatalogCoverageError(
                f"{field} must be ISO-8601"
            ) from exc
    else:
        raise BetfairCatalogCoverageError(
            f"{field} must be datetime or ISO-8601 text"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairCatalogCoverageError(f"{field} must be timezone-aware")
    if parsed.utcoffset() != timedelta(0):
        raise BetfairCatalogCoverageError(f"{field} must use UTC offset +00:00")
    return parsed


def _utc_text(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _request_payload(request: BetfairCatalogRequest) -> dict[str, object]:
    if type(request) is not BetfairCatalogRequest:
        raise BetfairCatalogCoverageError(
            "coverage request must be exact BetfairCatalogRequest"
        )
    if request.method != LIST_MARKET_CATALOGUE:
        raise BetfairCatalogCoverageError(
            "coverage leaves must use listMarketCatalogue"
        )
    return {"method": request.method, "params": request.rpc_params()}


def _request_sha256(request: BetfairCatalogRequest) -> str:
    return _sha_payload(_request_payload(request))


def _provider_result_from_acquisition(
    acquisition: BetfairAuthenticatedDiscoveryAcquisition,
) -> object:
    """Resolve semantics only through the producer-owned authenticated authority."""

    if type(acquisition) is not BetfairAuthenticatedDiscoveryAcquisition:
        raise TypeError(
            "acquisition must be exact BetfairAuthenticatedDiscoveryAcquisition"
        )
    try:
        return resolve_authoritative_betfair_authenticated_discovery_result(
            acquisition
        )
    except BetfairDiscoveryProvenanceError as exc:
        raise BetfairCatalogCoverageError(
            "coverage acquisition provider result is not authoritative"
        ) from exc

def _decode_request(request_json: str) -> BetfairCatalogRequest:
    try:
        raw = json.loads(request_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BetfairCatalogCoverageError(
            "persisted coverage request JSON is malformed"
        ) from exc
    if type(raw) is not dict or set(raw) != {"method", "params"}:
        raise BetfairCatalogCoverageError(
            "persisted coverage request schema is invalid"
        )
    if type(raw["method"]) is not str or type(raw["params"]) is not dict:
        raise BetfairCatalogCoverageError(
            "persisted coverage request types are invalid"
        )
    try:
        return BetfairCatalogRequest(raw["method"], raw["params"])
    except (TypeError, ValueError) as exc:
        raise BetfairCatalogCoverageError(
            "persisted coverage request is not canonical"
        ) from exc


def _bounded_market_time(request: BetfairCatalogRequest) -> tuple[datetime, datetime]:
    params = request.rpc_params()
    market_filter = params.get("filter")
    if type(market_filter) is not dict:
        raise BetfairCatalogCoverageError("coverage request filter is unavailable")
    raw = market_filter.get("marketStartTime")
    if type(raw) is not dict or set(raw) != {"from", "to"}:
        raise BetfairCatalogCoverageError(
            "coverage root requires a closed marketStartTime from/to window"
        )
    start = _utc(raw["from"], "marketStartTime.from")
    end = _utc(raw["to"], "marketStartTime.to")
    if end < start:
        raise BetfairCatalogCoverageError(
            "coverage marketStartTime window is reversed"
        )
    return start, end


def _clone_request_with_filter(
    request: BetfairCatalogRequest,
    market_filter: Mapping[str, object],
) -> BetfairCatalogRequest:
    params = request.rpc_params()
    params["filter"] = dict(market_filter)
    try:
        return BetfairCatalogRequest(request.method, params)
    except (TypeError, ValueError) as exc:
        raise BetfairCatalogCoverageError(
            "deterministic coverage child request is invalid"
        ) from exc


def split_catalog_coverage_request(
    request: BetfairCatalogRequest,
) -> tuple[BetfairCatalogRequest, BetfairCatalogRequest] | None:
    """Split a saturated request only across explicit categorical selectors.

    Betfair does not document listMarketCatalogue marketStartTime endpoint
    inclusivity/exclusivity. A disjoint time split would therefore invent
    provider semantics and could silently omit boundary markets. Until an
    overlap-guarded split also persists and reconciles exact market identities,
    time-only saturation is deliberately unsplittable.
    """

    _request_payload(request)
    params = request.rpc_params()
    market_filter = params["filter"]
    assert type(market_filter) is dict

    for field in _SCOPE_SPLIT_FIELDS:
        raw_values = market_filter.get(field)
        if raw_values is None:
            continue
        if type(raw_values) is not list or any(
            type(item) is not str or not item or item != item.strip()
            for item in raw_values
        ):
            raise BetfairCatalogCoverageError(
                f"{field} must be a canonical string array"
            )
        if len(set(raw_values)) != len(raw_values):
            raise BetfairCatalogCoverageError(
                f"{field} cannot contain duplicate scope identities"
            )
        if len(raw_values) > 1:
            ordered = sorted(raw_values)
            midpoint = len(ordered) // 2
            left_filter = dict(market_filter)
            right_filter = dict(market_filter)
            left_filter[field] = ordered[:midpoint]
            right_filter[field] = ordered[midpoint:]
            return (
                _clone_request_with_filter(request, left_filter),
                _clone_request_with_filter(request, right_filter),
            )

    return None

@dataclass(frozen=True, slots=True)
class CatalogCoveragePlan:
    provider_id: str
    source_id: str
    source_start_cycle_seq: int
    source_end_cycle_seq: int
    source_universe_commitment_sha256: str
    session_context_id: str
    account_scope_ref: str
    application_scope_ref: str
    key_class: str
    jurisdiction: str
    causal_cutoff_utc: str
    root_request_sha256: str
    partition_algorithm_version: str = PARTITION_ALGORITHM_VERSION
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.provider_id != PROVIDER_ID:
            raise BetfairCatalogCoverageError("provider_id is product-owned")
        _text(self.source_id, "source_id")
        for field, value in (
            ("source_start_cycle_seq", self.source_start_cycle_seq),
            ("source_end_cycle_seq", self.source_end_cycle_seq),
        ):
            if type(value) is not int or value <= 0:
                raise BetfairCatalogCoverageError(
                    f"{field} must be a positive integer"
                )
        if self.source_end_cycle_seq < self.source_start_cycle_seq:
            raise BetfairCatalogCoverageError(
                "source cycle window is reversed"
            )
        _sha(
            self.source_universe_commitment_sha256,
            "source_universe_commitment_sha256",
        )
        for field in (
            "session_context_id",
            "account_scope_ref",
            "application_scope_ref",
            "key_class",
            "jurisdiction",
        ):
            _text(getattr(self, field), field)
        _utc(self.causal_cutoff_utc, "causal_cutoff_utc")
        _sha(self.root_request_sha256, "root_request_sha256")
        if self.partition_algorithm_version != PARTITION_ALGORITHM_VERSION:
            raise BetfairCatalogCoverageError(
                "unsupported partition algorithm version"
            )
        if self.schema_version != SCHEMA_VERSION:
            raise BetfairCatalogCoverageError("unsupported coverage schema version")

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "source_id": self.source_id,
            "source_start_cycle_seq": self.source_start_cycle_seq,
            "source_end_cycle_seq": self.source_end_cycle_seq,
            "source_universe_commitment_sha256": (
                self.source_universe_commitment_sha256
            ),
            "session_context_id": self.session_context_id,
            "account_scope_ref": self.account_scope_ref,
            "application_scope_ref": self.application_scope_ref,
            "key_class": self.key_class,
            "jurisdiction": self.jurisdiction,
            "causal_cutoff_utc": self.causal_cutoff_utc,
            "root_request_sha256": self.root_request_sha256,
            "partition_algorithm_version": self.partition_algorithm_version,
        }

    @property
    def plan_id(self) -> str:
        return _sha_payload(self.payload())


@dataclass(frozen=True, slots=True)
class CatalogCoverageLeaf:
    plan_id: str
    leaf_id: str
    parent_leaf_id: str | None
    depth: int
    request: BetfairCatalogRequest

    @property
    def request_sha256(self) -> str:
        return _request_sha256(self.request)


@dataclass(frozen=True, slots=True)
class CatalogCoverageRecordResult:
    leaf_id: str
    status: str
    result_count: int | None
    child_leaf_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CatalogCoverageResolution:
    plan_id: str
    leaf_count: int
    terminal_count: int
    pending_count: int
    success_count: int
    empty_count: int
    saturated_split_count: int
    failure_count: int
    unsplittable_count: int
    after_cutoff_count: int
    live_authenticated_receipt_count: int
    durable_partition_complete: bool
    provider_visible_scope_complete: bool
    reason: str
    promotion_ready: bool = False


def _require_store(
    store: CollectorDeltaStore,
    expected_store_path: str | Path,
) -> Path:
    if type(store) is not CollectorDeltaStore:
        raise TypeError("store must be exact canonical CollectorDeltaStore")
    if CollectorDeltaStore._connect is not _CANONICAL_CONNECT:
        raise BetfairCatalogCoverageError(
            "canonical collector connection authority changed"
        )
    state = getattr(store, "__dict__", None)
    if type(state) is not dict or "_connect" in state:
        raise BetfairCatalogCoverageError(
            "collector connection authority is instance-rebound"
        )
    expected = (
        Path(expected_store_path)
        if isinstance(expected_store_path, str)
        else expected_store_path
    )
    if not isinstance(expected, Path):
        raise TypeError("expected_store_path must be str or Path")
    current = getattr(store, "path", None)
    if not isinstance(current, Path) or current != expected:
        raise BetfairCatalogCoverageError(
            "collector store path does not match product-expected authority path"
        )
    return expected


def _connect(store: CollectorDeltaStore):
    if CollectorDeltaStore._connect is not _CANONICAL_CONNECT:
        raise BetfairCatalogCoverageError(
            "canonical collector connection authority changed"
        )
    return _CANONICAL_CONNECT(store)


def _ensure_schema(store: CollectorDeltaStore) -> None:
    connection = _connect(store)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS betfair_catalog_coverage_plans_v1 ("
            "plan_id TEXT PRIMARY KEY NOT NULL,"
            "payload_sha256 TEXT NOT NULL,"
            "payload_json TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS betfair_catalog_coverage_leaf_starts_v1 ("
            "plan_id TEXT NOT NULL,"
            "leaf_id TEXT NOT NULL,"
            "parent_leaf_id TEXT,"
            "depth INTEGER NOT NULL CHECK(depth >= 0),"
            "request_sha256 TEXT NOT NULL,"
            "request_json TEXT NOT NULL,"
            "PRIMARY KEY(plan_id, leaf_id),"
            "FOREIGN KEY(plan_id) REFERENCES betfair_catalog_coverage_plans_v1(plan_id))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS betfair_catalog_coverage_leaf_terminals_v1 ("
            "plan_id TEXT NOT NULL,"
            "leaf_id TEXT NOT NULL,"
            "payload_sha256 TEXT NOT NULL,"
            "payload_json TEXT NOT NULL,"
            "PRIMARY KEY(plan_id, leaf_id),"
            "FOREIGN KEY(plan_id, leaf_id) "
            "REFERENCES betfair_catalog_coverage_leaf_starts_v1(plan_id, leaf_id))"
        )
        for trigger_name, table_name, timing in (
            (
                "betfair_catalog_coverage_plan_no_update_v1",
                "betfair_catalog_coverage_plans_v1",
                "UPDATE",
            ),
            (
                "betfair_catalog_coverage_plan_no_delete_v1",
                "betfair_catalog_coverage_plans_v1",
                "DELETE",
            ),
            (
                "betfair_catalog_coverage_leaf_start_no_update_v1",
                "betfair_catalog_coverage_leaf_starts_v1",
                "UPDATE",
            ),
            (
                "betfair_catalog_coverage_leaf_start_no_delete_v1",
                "betfair_catalog_coverage_leaf_starts_v1",
                "DELETE",
            ),
            (
                "betfair_catalog_coverage_leaf_terminal_no_update_v1",
                "betfair_catalog_coverage_leaf_terminals_v1",
                "UPDATE",
            ),
            (
                "betfair_catalog_coverage_leaf_terminal_no_delete_v1",
                "betfair_catalog_coverage_leaf_terminals_v1",
                "DELETE",
            ),
        ):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {trigger_name} "
                f"BEFORE {timing} ON {table_name} BEGIN "
                "SELECT RAISE(ABORT, 'Betfair catalogue coverage evidence is immutable'); "
                "END"
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _leaf_id(
    plan_id: str,
    *,
    parent_leaf_id: str | None,
    depth: int,
    request_sha256: str,
) -> str:
    return _sha_payload(
        {
            "plan_id": plan_id,
            "parent_leaf_id": parent_leaf_id,
            "depth": depth,
            "request_sha256": request_sha256,
        }
    )


def _persist_leaf_start(
    connection,
    *,
    plan_id: str,
    parent_leaf_id: str | None,
    depth: int,
    request: BetfairCatalogRequest,
) -> str:
    request_json = _canonical_json(_request_payload(request))
    request_sha = sha256(request_json.encode("utf-8")).hexdigest()
    leaf_id = _leaf_id(
        plan_id,
        parent_leaf_id=parent_leaf_id,
        depth=depth,
        request_sha256=request_sha,
    )
    existing = connection.execute(
        "SELECT parent_leaf_id, depth, request_sha256, request_json "
        "FROM betfair_catalog_coverage_leaf_starts_v1 "
        "WHERE plan_id=? AND leaf_id=?",
        (plan_id, leaf_id),
    ).fetchone()
    expected = (parent_leaf_id, depth, request_sha, request_json)
    if existing is not None:
        observed = (
            existing["parent_leaf_id"],
            int(existing["depth"]),
            existing["request_sha256"],
            existing["request_json"],
        )
        if observed != expected:
            raise BetfairCatalogCoverageError(
                "coverage leaf identity conflicts with durable START"
            )
        return leaf_id
    connection.execute(
        "INSERT INTO betfair_catalog_coverage_leaf_starts_v1("
        "plan_id, leaf_id, parent_leaf_id, depth, request_sha256, request_json"
        ") VALUES(?,?,?,?,?,?)",
        (plan_id, leaf_id, parent_leaf_id, depth, request_sha, request_json),
    )
    return leaf_id


def create_catalog_coverage_plan(
    store: CollectorDeltaStore,
    *,
    expected_store_path: str | Path,
    source_universe: SourceUniverseCommitment,
    expected_source_id: str,
    expected_start_cycle_seq: int,
    expected_end_cycle_seq: int,
    client: BetfairReadOnlyClient,
    visibility_scope: BetfairDiscoveryVisibilityScope,
    causal_cutoff: datetime,
    root_request: BetfairCatalogRequest,
) -> CatalogCoveragePlan:
    """Freeze one bounded provider-visible campaign before any catalogue dispatch."""

    expected_path = _require_store(store, expected_store_path)
    if type(source_universe) is not SourceUniverseCommitment:
        raise TypeError("source_universe must be exact SourceUniverseCommitment")
    try:
        canonical_source = verify_source_universe_commitment(
            store,
            source_universe,
            expected_store_path=expected_path,
            expected_source_id=expected_source_id,
            expected_start_cycle_seq=expected_start_cycle_seq,
            expected_end_cycle_seq=expected_end_cycle_seq,
        )
    except SourceUniverseCommitmentError as exc:
        raise BetfairCatalogCoverageError(
            "source-universe commitment is not canonical"
        ) from exc
    if not canonical_source.provider_observation_complete:
        raise BetfairCatalogCoverageError(
            "coverage plan requires a complete canonical collector observation window"
        )
    if type(client) is not BetfairReadOnlyClient:
        raise TypeError("client must be exact BetfairReadOnlyClient")
    if type(visibility_scope) is not BetfairDiscoveryVisibilityScope:
        raise TypeError(
            "visibility_scope must be exact BetfairDiscoveryVisibilityScope"
        )
    try:
        identity = resolve_betfair_authenticated_account_identity(client)
        require_authoritative_betfair_account_identity(identity, client=client)
    except Exception as exc:
        raise BetfairCatalogCoverageError(
            "coverage plan requires current authenticated Betfair session authority"
        ) from exc
    if visibility_scope.account_scope_ref != identity.session_context_id:
        raise BetfairCatalogCoverageError(
            "visibility account scope must equal the product-issued session context"
        )
    cutoff = _utc(causal_cutoff, "causal_cutoff")
    if _utc(identity.observed_at, "account_identity.observed_at") > cutoff:
        raise BetfairCatalogCoverageError(
            "causal cutoff predates authenticated account-context evidence"
        )
    _bounded_market_time(root_request)
    request_sha = _request_sha256(root_request)
    plan = CatalogCoveragePlan(
        provider_id=PROVIDER_ID,
        source_id=canonical_source.source_id,
        source_start_cycle_seq=canonical_source.start_cycle_seq,
        source_end_cycle_seq=canonical_source.end_cycle_seq,
        source_universe_commitment_sha256=canonical_source.commitment_sha256,
        session_context_id=identity.session_context_id,
        account_scope_ref=visibility_scope.account_scope_ref,
        application_scope_ref=visibility_scope.application_scope_ref,
        key_class=visibility_scope.key_class,
        jurisdiction=visibility_scope.jurisdiction,
        causal_cutoff_utc=_utc_text(cutoff),
        root_request_sha256=request_sha,
    )

    _ensure_schema(store)
    connection = _connect(store)
    try:
        connection.execute("BEGIN IMMEDIATE")
        payload_json = _canonical_json(plan.payload())
        payload_sha = sha256(payload_json.encode("utf-8")).hexdigest()
        existing = connection.execute(
            "SELECT payload_sha256, payload_json "
            "FROM betfair_catalog_coverage_plans_v1 WHERE plan_id=?",
            (plan.plan_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO betfair_catalog_coverage_plans_v1("
                "plan_id, payload_sha256, payload_json) VALUES(?,?,?)",
                (plan.plan_id, payload_sha, payload_json),
            )
        elif (
            existing["payload_sha256"] != payload_sha
            or existing["payload_json"] != payload_json
        ):
            raise BetfairCatalogCoverageError(
                "coverage plan id conflicts with durable payload"
            )
        _persist_leaf_start(
            connection,
            plan_id=plan.plan_id,
            parent_leaf_id=None,
            depth=0,
            request=root_request,
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return plan


def _load_plan(
    store: CollectorDeltaStore,
    *,
    expected_store_path: str | Path,
    plan_id: str,
) -> CatalogCoveragePlan:
    expected_path = _require_store(store, expected_store_path)
    _ensure_schema(store)
    connection = _connect(store)
    try:
        row = connection.execute(
            "SELECT payload_sha256, payload_json "
            "FROM betfair_catalog_coverage_plans_v1 WHERE plan_id=?",
            (_sha(plan_id, "plan_id"),),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise BetfairCatalogCoverageError("coverage plan is not durable")
    payload_json = row["payload_json"]
    if sha256(payload_json.encode("utf-8")).hexdigest() != row["payload_sha256"]:
        raise BetfairCatalogCoverageError("coverage plan payload digest mismatch")
    try:
        raw = json.loads(payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BetfairCatalogCoverageError("coverage plan JSON is malformed") from exc
    if type(raw) is not dict:
        raise BetfairCatalogCoverageError("coverage plan payload must be object")
    try:
        plan = CatalogCoveragePlan(**raw)
    except (TypeError, ValueError) as exc:
        raise BetfairCatalogCoverageError("coverage plan payload is invalid") from exc
    if plan.plan_id != plan_id:
        raise BetfairCatalogCoverageError("coverage plan id mismatch")
    try:
        rebuilt = build_source_universe_commitment(
            store,
            expected_store_path=expected_path,
            source_id=plan.source_id,
            start_cycle_seq=plan.source_start_cycle_seq,
            end_cycle_seq=plan.source_end_cycle_seq,
        )
    except SourceUniverseCommitmentError as exc:
        raise BetfairCatalogCoverageError(
            "coverage source-universe authority is unavailable"
        ) from exc
    if rebuilt.commitment_sha256 != plan.source_universe_commitment_sha256:
        raise BetfairCatalogCoverageError(
            "coverage source-universe commitment changed"
        )
    return plan


def _load_leaf(
    store: CollectorDeltaStore,
    *,
    plan_id: str,
    leaf_id: str,
) -> CatalogCoverageLeaf:
    connection = _connect(store)
    try:
        row = connection.execute(
            "SELECT parent_leaf_id, depth, request_sha256, request_json "
            "FROM betfair_catalog_coverage_leaf_starts_v1 "
            "WHERE plan_id=? AND leaf_id=?",
            (plan_id, _sha(leaf_id, "leaf_id")),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise BetfairCatalogCoverageError("coverage leaf START is not durable")
    request = _decode_request(row["request_json"])
    if _request_sha256(request) != row["request_sha256"]:
        raise BetfairCatalogCoverageError("coverage leaf request digest mismatch")
    expected_leaf_id = _leaf_id(
        plan_id,
        parent_leaf_id=row["parent_leaf_id"],
        depth=int(row["depth"]),
        request_sha256=row["request_sha256"],
    )
    if expected_leaf_id != leaf_id:
        raise BetfairCatalogCoverageError("coverage leaf identity mismatch")
    return CatalogCoverageLeaf(
        plan_id=plan_id,
        leaf_id=leaf_id,
        parent_leaf_id=row["parent_leaf_id"],
        depth=int(row["depth"]),
        request=request,
    )


def pending_catalog_coverage_leaves(
    store: CollectorDeltaStore,
    *,
    expected_store_path: str | Path,
    plan_id: str,
) -> tuple[CatalogCoverageLeaf, ...]:
    """Return exact planned leaves lacking a terminal, in deterministic order."""

    _load_plan(
        store,
        expected_store_path=expected_store_path,
        plan_id=plan_id,
    )
    connection = _connect(store)
    try:
        rows = connection.execute(
            "SELECT s.leaf_id, s.parent_leaf_id, s.depth, "
            "s.request_sha256, s.request_json "
            "FROM betfair_catalog_coverage_leaf_starts_v1 AS s "
            "LEFT JOIN betfair_catalog_coverage_leaf_terminals_v1 AS t "
            "ON t.plan_id=s.plan_id AND t.leaf_id=s.leaf_id "
            "WHERE s.plan_id=? AND t.leaf_id IS NULL "
            "ORDER BY s.depth, s.leaf_id",
            (plan_id,),
        ).fetchall()
    finally:
        connection.close()
    items = []
    for row in rows:
        request = _decode_request(row["request_json"])
        if _request_sha256(request) != row["request_sha256"]:
            raise BetfairCatalogCoverageError(
                "pending coverage leaf request digest mismatch"
            )
        items.append(
            CatalogCoverageLeaf(
                plan_id=plan_id,
                leaf_id=row["leaf_id"],
                parent_leaf_id=row["parent_leaf_id"],
                depth=int(row["depth"]),
                request=request,
            )
        )
    return tuple(items)


def _market_observation_payloads(markets: Sequence[object]) -> list[dict[str, object]]:
    """Freeze exact catalogue identities needed to reconcile split observations."""

    payloads: list[dict[str, object]] = []
    seen: set[str] = set()
    for market in markets:
        try:
            payload = {
                "market_id": market.market_id,
                "event_type_id": market.event_type_id,
                "event_id": market.event_id,
                "market_start_time": market.market_start_time,
                "market_type_code": market.market_type_code,
                "competition_id": market.competition_id,
            }
        except AttributeError as exc:
            raise BetfairCatalogCoverageError(
                "catalogue market observation is not canonical"
            ) from exc
        market_id = _text(payload["market_id"], "market_id")
        if market_id in seen:
            raise BetfairCatalogCoverageError(
                "catalogue market observation contains duplicate market_id"
            )
        seen.add(market_id)
        payloads.append(payload)
    payloads.sort(key=lambda item: item["market_id"])
    return payloads


def _terminal_market_map(
    terminal: Mapping[str, object],
) -> dict[str, str]:
    raw = terminal.get("market_observations")
    if type(raw) is not list:
        raise BetfairCatalogCoverageError(
            "coverage terminal market observations are malformed"
        )
    result_count = terminal.get("result_count")
    if type(result_count) is not int or isinstance(result_count, bool):
        raise BetfairCatalogCoverageError(
            "positive coverage terminal result_count is malformed"
        )
    if result_count != len(raw):
        raise BetfairCatalogCoverageError(
            "coverage terminal result_count disagrees with market observations"
        )
    mapped: dict[str, str] = {}
    required = {
        "market_id",
        "event_type_id",
        "event_id",
        "market_start_time",
        "market_type_code",
        "competition_id",
    }
    for item in raw:
        if type(item) is not dict or set(item) != required:
            raise BetfairCatalogCoverageError(
                "coverage terminal market identity payload is malformed"
            )
        market_id = _text(item.get("market_id"), "market_id")
        for field in (
            "event_type_id",
            "event_id",
            "market_start_time",
        ):
            _text(item.get(field), f"market_observations.{field}")
        for field in ("market_type_code", "competition_id"):
            value = item.get(field)
            if value is not None:
                _text(value, f"market_observations.{field}")
        identity = _canonical_json(item)
        if market_id in mapped:
            raise BetfairCatalogCoverageError(
                "coverage terminal repeats market_id"
            )
        mapped[market_id] = identity
    return mapped


def _validate_terminal_semantics(
    plan: CatalogCoveragePlan,
    leaf: CatalogCoverageLeaf,
    terminal: Mapping[str, object],
) -> None:
    """Reject status/count/evidence combinations that canonical writers never emit."""

    status = terminal.get("status")
    observations = terminal.get("market_observations")
    child_ids = terminal.get("child_leaf_ids")
    if type(observations) is not list or type(child_ids) is not list:
        raise BetfairCatalogCoverageError(
            "coverage terminal collections are malformed"
        )

    params = leaf.request.rpc_params()
    max_results = params.get("maxResults")
    if type(max_results) is not int or isinstance(max_results, bool) or max_results <= 0:
        raise BetfairCatalogCoverageError(
            "coverage terminal leaf maxResults is invalid"
        )

    provider_observed_statuses = {
        "SUCCESS",
        "EMPTY",
        "SATURATED_SPLIT",
        "SATURATED_UNSPLITTABLE",
    }
    if status in provider_observed_statuses:
        market_map = _terminal_market_map(terminal)
        result_count = terminal.get("result_count")
        if result_count != len(market_map):
            raise BetfairCatalogCoverageError(
                "coverage terminal market identity cardinality is inconsistent"
            )
        _sha(
            terminal.get("raw_response_sha256"),
            "terminal.raw_response_sha256",
        )
        observed = _utc(
            terminal.get("observed_at_utc"),
            "terminal.observed_at_utc",
        )
        if observed > _utc(plan.causal_cutoff_utc, "causal_cutoff_utc"):
            raise BetfairCatalogCoverageError(
                "provider-observed coverage terminal escaped causal cutoff"
            )
        if terminal.get("transport_authority_ref") != plan.session_context_id:
            raise BetfairCatalogCoverageError(
                "provider-observed coverage terminal escaped frozen session context"
            )

        if status == "EMPTY":
            if result_count != 0 or observations:
                raise BetfairCatalogCoverageError(
                    "EMPTY coverage terminal must contain zero markets"
                )
        elif status == "SUCCESS":
            if result_count <= 0 or result_count >= max_results:
                raise BetfairCatalogCoverageError(
                    "SUCCESS coverage terminal must be positive and unsaturated"
                )
        else:
            if result_count != max_results:
                raise BetfairCatalogCoverageError(
                    "saturated coverage terminal must equal requested maxResults"
                )

        if status == "SATURATED_SPLIT":
            if not child_ids or terminal.get("error_code") is not None:
                raise BetfairCatalogCoverageError(
                    "SATURATED_SPLIT terminal requires children and no error"
                )
        elif status == "SATURATED_UNSPLITTABLE":
            if child_ids or terminal.get("error_code") != "SATURATED_LEAF_CANNOT_SPLIT":
                raise BetfairCatalogCoverageError(
                    "SATURATED_UNSPLITTABLE terminal semantics are invalid"
                )
        elif child_ids or terminal.get("error_code") is not None:
            raise BetfairCatalogCoverageError(
                "unsaturated coverage terminal cannot own children or an error"
            )
        return

    if status == "FAILURE":
        if (
            terminal.get("result_count") is not None
            or terminal.get("raw_response_sha256") is not None
            or terminal.get("observed_at_utc") is not None
            or terminal.get("transport_authority_ref") is not None
            or observations
            or child_ids
        ):
            raise BetfairCatalogCoverageError(
                "FAILURE coverage terminal carries impossible provider evidence"
            )
        _text(terminal.get("error_code"), "terminal.error_code")
        return

    if status == "AFTER_CAUSAL_CUTOFF":
        if (
            terminal.get("result_count") is not None
            or observations
            or child_ids
            or terminal.get("error_code") != "OBSERVED_AFTER_CAUSAL_CUTOFF"
        ):
            raise BetfairCatalogCoverageError(
                "AFTER_CAUSAL_CUTOFF terminal semantics are invalid"
            )
        _sha(
            terminal.get("raw_response_sha256"),
            "terminal.raw_response_sha256",
        )
        observed = _utc(
            terminal.get("observed_at_utc"),
            "terminal.observed_at_utc",
        )
        if observed <= _utc(plan.causal_cutoff_utc, "causal_cutoff_utc"):
            raise BetfairCatalogCoverageError(
                "AFTER_CAUSAL_CUTOFF terminal does not cross causal cutoff"
            )
        if terminal.get("transport_authority_ref") != plan.session_context_id:
            raise BetfairCatalogCoverageError(
                "AFTER_CAUSAL_CUTOFF terminal escaped frozen session context"
            )
        return

    raise BetfairCatalogCoverageError("unsupported coverage terminal status")


def _terminal_payload(
    *,
    plan: CatalogCoveragePlan,
    leaf: CatalogCoverageLeaf,
    status: str,
    result_count: int | None,
    raw_response_sha256: str | None,
    observed_at_utc: str | None,
    transport_authority_ref: str | None,
    child_leaf_ids: Sequence[str],
    error_code: str | None,
    market_observations: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    if status not in _TERMINAL_STATUSES:
        raise BetfairCatalogCoverageError("unsupported coverage terminal status")
    return {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan.plan_id,
        "leaf_id": leaf.leaf_id,
        "request_sha256": leaf.request_sha256,
        "status": status,
        "result_count": result_count,
        "raw_response_sha256": raw_response_sha256,
        "observed_at_utc": observed_at_utc,
        "transport_authority_ref": transport_authority_ref,
        "child_leaf_ids": list(child_leaf_ids),
        "error_code": error_code,
        "market_observations": [dict(item) for item in market_observations],
    }


def _persist_terminal_and_children(
    store: CollectorDeltaStore,
    *,
    plan: CatalogCoveragePlan,
    leaf: CatalogCoverageLeaf,
    payload: dict[str, object],
    children: Sequence[BetfairCatalogRequest],
) -> tuple[str, ...]:
    connection = _connect(store)
    try:
        connection.execute("BEGIN IMMEDIATE")
        child_ids = tuple(
            _leaf_id(
                plan.plan_id,
                parent_leaf_id=leaf.leaf_id,
                depth=leaf.depth + 1,
                request_sha256=_request_sha256(child),
            )
            for child in children
        )
        if tuple(payload["child_leaf_ids"]) != child_ids:
            raise BetfairCatalogCoverageError(
                "coverage terminal child identity mismatch"
            )
        payload_json = _canonical_json(payload)
        payload_sha = sha256(payload_json.encode("utf-8")).hexdigest()
        existing = connection.execute(
            "SELECT payload_sha256, payload_json "
            "FROM betfair_catalog_coverage_leaf_terminals_v1 "
            "WHERE plan_id=? AND leaf_id=?",
            (plan.plan_id, leaf.leaf_id),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO betfair_catalog_coverage_leaf_terminals_v1("
                "plan_id, leaf_id, payload_sha256, payload_json) VALUES(?,?,?,?)",
                (plan.plan_id, leaf.leaf_id, payload_sha, payload_json),
            )
        elif (
            existing["payload_sha256"] != payload_sha
            or existing["payload_json"] != payload_json
        ):
            raise BetfairCatalogCoverageError(
                "coverage leaf already has a different immutable terminal"
            )
        for child in children:
            _persist_leaf_start(
                connection,
                plan_id=plan.plan_id,
                parent_leaf_id=leaf.leaf_id,
                depth=leaf.depth + 1,
                request=child,
            )
        connection.commit()
        return child_ids
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _record_catalog_coverage_acquisition_impl(
    store: CollectorDeltaStore,
    *,
    expected_store_path: str | Path,
    plan_id: str,
    leaf_id: str,
    acquisition: BetfairAuthenticatedDiscoveryAcquisition,
    _parse_catalogue_result,
) -> CatalogCoverageRecordResult:
    """Bind one live authenticated response and expand saturated leaves deterministically."""

    plan = _load_plan(
        store,
        expected_store_path=expected_store_path,
        plan_id=plan_id,
    )
    leaf = _load_leaf(store, plan_id=plan.plan_id, leaf_id=leaf_id)
    provider_result = _provider_result_from_acquisition(acquisition)
    receipt = acquisition.receipt
    if receipt.transport_authority_ref != plan.session_context_id:
        raise BetfairCatalogCoverageError(
            "coverage acquisition escaped the frozen session context"
        )
    if acquisition.account_identity.session_context_id != plan.session_context_id:
        raise BetfairCatalogCoverageError(
            "coverage account identity escaped the frozen session context"
        )
    if (
        acquisition.exchange.request_sha256 != leaf.request_sha256
        or _request_sha256(acquisition.exchange.request) != leaf.request_sha256
    ):
        raise BetfairCatalogCoverageError(
            "coverage acquisition does not bind the planned request"
        )
    observed = _utc(
        acquisition.exchange.observed_at,
        "coverage acquisition observed_at",
    )
    cutoff = _utc(plan.causal_cutoff_utc, "causal_cutoff_utc")
    if observed > cutoff:
        payload = _terminal_payload(
            plan=plan,
            leaf=leaf,
            status="AFTER_CAUSAL_CUTOFF",
            result_count=None,
            raw_response_sha256=receipt.raw_response_sha256,
            observed_at_utc=receipt.observed_at_utc,
            transport_authority_ref=receipt.transport_authority_ref,
            child_leaf_ids=(),
            error_code="OBSERVED_AFTER_CAUSAL_CUTOFF",
        )
        _persist_terminal_and_children(
            store, plan=plan, leaf=leaf, payload=payload, children=()
        )
        return CatalogCoverageRecordResult(
            leaf.leaf_id, "AFTER_CAUSAL_CUTOFF", None, ()
        )

    try:
        batch = _parse_catalogue_result(
            provider_result,
            request=acquisition.exchange.request,
        )
    except (TypeError, ValueError) as exc:
        raise BetfairCatalogCoverageError(
            "coverage acquisition cannot be parsed against exact request scope"
        ) from exc

    market_observations = _market_observation_payloads(batch.markets)
    children: tuple[BetfairCatalogRequest, ...] = ()
    if batch.continuation_required:
        split = split_catalog_coverage_request(leaf.request)
        if split is None:
            status = "SATURATED_UNSPLITTABLE"
        else:
            status = "SATURATED_SPLIT"
            children = split
    elif not batch.markets:
        status = "EMPTY"
    else:
        status = "SUCCESS"

    child_ids = tuple(
        _leaf_id(
            plan.plan_id,
            parent_leaf_id=leaf.leaf_id,
            depth=leaf.depth + 1,
            request_sha256=_request_sha256(child),
        )
        for child in children
    )
    payload = _terminal_payload(
        plan=plan,
        leaf=leaf,
        status=status,
        result_count=len(batch.markets),
        raw_response_sha256=receipt.raw_response_sha256,
        observed_at_utc=receipt.observed_at_utc,
        transport_authority_ref=receipt.transport_authority_ref,
        child_leaf_ids=child_ids,
        error_code=(
            "SATURATED_LEAF_CANNOT_SPLIT"
            if status == "SATURATED_UNSPLITTABLE"
            else None
        ),
        market_observations=market_observations,
    )
    durable_children = _persist_terminal_and_children(
        store,
        plan=plan,
        leaf=leaf,
        payload=payload,
        children=children,
    )
    return CatalogCoverageRecordResult(
        leaf.leaf_id,
        status,
        len(batch.markets),
        durable_children,
    )


def record_catalog_coverage_failure(
    store: CollectorDeltaStore,
    *,
    expected_store_path: str | Path,
    plan_id: str,
    leaf_id: str,
    error_code: str = "AUTHENTICATED_DISCOVERY_FAILED",
) -> CatalogCoverageRecordResult:
    """Persist one fail-closed terminal when provider acquisition cannot complete."""

    plan = _load_plan(
        store,
        expected_store_path=expected_store_path,
        plan_id=plan_id,
    )
    leaf = _load_leaf(store, plan_id=plan.plan_id, leaf_id=leaf_id)
    code = _text(error_code, "error_code")
    payload = _terminal_payload(
        plan=plan,
        leaf=leaf,
        status="FAILURE",
        result_count=None,
        raw_response_sha256=None,
        observed_at_utc=None,
        transport_authority_ref=None,
        child_leaf_ids=(),
        error_code=code,
    )
    _persist_terminal_and_children(
        store, plan=plan, leaf=leaf, payload=payload, children=()
    )
    return CatalogCoverageRecordResult(leaf.leaf_id, "FAILURE", None, ())


def _terminal_row_payload(row) -> dict[str, object] | None:
    payload_json = row["payload_json"]
    payload_sha = row["payload_sha256"]
    if payload_json is None and payload_sha is None:
        return None
    if payload_json is None or payload_sha is None:
        raise BetfairCatalogCoverageError(
            "coverage terminal evidence is structurally incomplete"
        )
    if sha256(payload_json.encode("utf-8")).hexdigest() != payload_sha:
        raise BetfairCatalogCoverageError("coverage terminal digest mismatch")
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BetfairCatalogCoverageError("coverage terminal JSON is malformed") from exc
    if type(payload) is not dict:
        raise BetfairCatalogCoverageError("coverage terminal must be object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise BetfairCatalogCoverageError("coverage terminal schema version is invalid")
    if payload.get("status") not in _TERMINAL_STATUSES:
        raise BetfairCatalogCoverageError("coverage terminal status is invalid")
    return payload


def _resolve_catalog_coverage_impl(
    store: CollectorDeltaStore,
    *,
    expected_store_path: str | Path,
    plan_id: str,
    live_acquisitions: Sequence[BetfairAuthenticatedDiscoveryAcquisition] = (),
    _parse_catalogue_result,
) -> CatalogCoverageResolution:
    """Verify the durable partition graph and current-process provider-origin receipts."""

    plan = _load_plan(
        store,
        expected_store_path=expected_store_path,
        plan_id=plan_id,
    )
    connection = _connect(store)
    try:
        rows = connection.execute(
            "SELECT s.leaf_id, s.parent_leaf_id, s.depth, "
            "s.request_sha256, s.request_json, "
            "t.payload_sha256, t.payload_json "
            "FROM betfair_catalog_coverage_leaf_starts_v1 AS s "
            "LEFT JOIN betfair_catalog_coverage_leaf_terminals_v1 AS t "
            "ON t.plan_id=s.plan_id AND t.leaf_id=s.leaf_id "
            "WHERE s.plan_id=? ORDER BY s.depth, s.leaf_id",
            (plan.plan_id,),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        raise BetfairCatalogCoverageError("coverage plan has no durable root leaf")

    starts: dict[str, tuple[CatalogCoverageLeaf, dict[str, object] | None]] = {}
    roots = []
    for row in rows:
        request = _decode_request(row["request_json"])
        if _request_sha256(request) != row["request_sha256"]:
            raise BetfairCatalogCoverageError(
                "coverage graph contains request digest mismatch"
            )
        leaf = CatalogCoverageLeaf(
            plan_id=plan.plan_id,
            leaf_id=row["leaf_id"],
            parent_leaf_id=row["parent_leaf_id"],
            depth=int(row["depth"]),
            request=request,
        )
        if _leaf_id(
            plan.plan_id,
            parent_leaf_id=leaf.parent_leaf_id,
            depth=leaf.depth,
            request_sha256=leaf.request_sha256,
        ) != leaf.leaf_id:
            raise BetfairCatalogCoverageError(
                "coverage graph contains invalid leaf identity"
            )
        terminal = _terminal_row_payload(row)
        starts[leaf.leaf_id] = (leaf, terminal)
        if leaf.parent_leaf_id is None:
            roots.append(leaf)
    if len(roots) != 1 or roots[0].depth != 0:
        raise BetfairCatalogCoverageError(
            "coverage graph must contain exactly one depth-zero root"
        )
    if roots[0].request_sha256 != plan.root_request_sha256:
        raise BetfairCatalogCoverageError(
            "coverage root request does not match frozen plan"
        )

    for leaf, terminal in starts.values():
        if leaf.parent_leaf_id is None:
            continue
        parent_entry = starts.get(leaf.parent_leaf_id)
        if parent_entry is None:
            raise BetfairCatalogCoverageError("coverage graph contains orphan leaf")
        parent, parent_terminal = parent_entry
        if leaf.depth != parent.depth + 1:
            raise BetfairCatalogCoverageError(
                "coverage child depth is not canonical"
            )
        if parent_terminal is None or parent_terminal.get("status") != "SATURATED_SPLIT":
            raise BetfairCatalogCoverageError(
                "coverage child lacks saturated parent authority"
            )
        if leaf.leaf_id not in parent_terminal.get("child_leaf_ids", []):
            raise BetfairCatalogCoverageError(
                "coverage child is absent from parent terminal"
            )

    counts = {status: 0 for status in _TERMINAL_STATUSES}
    pending_count = 0
    required_terminals: dict[
        tuple[str, str, str],
        tuple[CatalogCoverageLeaf, dict[str, object]],
    ] = {}
    for leaf, terminal in starts.values():
        if terminal is None:
            pending_count += 1
            continue
        if (
            terminal.get("plan_id") != plan.plan_id
            or terminal.get("leaf_id") != leaf.leaf_id
            or terminal.get("request_sha256") != leaf.request_sha256
        ):
            raise BetfairCatalogCoverageError(
                "coverage terminal conflicts with immutable START"
            )
        _validate_terminal_semantics(plan, leaf, terminal)
        status = terminal["status"]
        counts[status] += 1
        child_ids = terminal.get("child_leaf_ids")
        if type(child_ids) is not list or any(type(item) is not str for item in child_ids):
            raise BetfairCatalogCoverageError(
                "coverage terminal child ids are malformed"
            )
        if status == "SATURATED_SPLIT":
            split = split_catalog_coverage_request(leaf.request)
            if split is None:
                raise BetfairCatalogCoverageError(
                    "coverage terminal claims an impossible split"
                )
            expected_children = tuple(
                _leaf_id(
                    plan.plan_id,
                    parent_leaf_id=leaf.leaf_id,
                    depth=leaf.depth + 1,
                    request_sha256=_request_sha256(child),
                )
                for child in split
            )
            if tuple(child_ids) != expected_children:
                raise BetfairCatalogCoverageError(
                    "coverage saturated split is not deterministic"
                )
            if any(child_id not in starts for child_id in expected_children):
                raise BetfairCatalogCoverageError(
                    "coverage saturated split is missing planned child START"
                )
        elif child_ids:
            raise BetfairCatalogCoverageError(
                "non-split coverage terminal cannot own child leaves"
            )

        if status in _POSITIVE_PROVIDER_TERMINALS:
            request_sha = _sha(
                terminal.get("request_sha256"),
                "terminal.request_sha256",
            )
            raw_sha = _sha(
                terminal.get("raw_response_sha256"),
                "terminal.raw_response_sha256",
            )
            observed = _text(
                terminal.get("observed_at_utc"),
                "terminal.observed_at_utc",
            )
            if terminal.get("transport_authority_ref") != plan.session_context_id:
                raise BetfairCatalogCoverageError(
                    "coverage terminal escaped frozen session context"
                )
            key = (request_sha, raw_sha, observed)
            if key in required_terminals:
                raise BetfairCatalogCoverageError(
                    "coverage graph reuses one provider observation across leaves"
                )
            required_terminals[key] = (leaf, terminal)

    def reconcile_subtree(leaf_id: str) -> dict[str, str] | None:
        leaf, terminal = starts[leaf_id]
        del leaf
        if terminal is None:
            return None
        status = terminal["status"]
        if status in {"SUCCESS", "EMPTY"}:
            return _terminal_market_map(terminal)
        if status != "SATURATED_SPLIT":
            return None

        parent_observations = _terminal_market_map(terminal)
        child_ids = terminal["child_leaf_ids"]
        combined: dict[str, str] = {}
        for child_id in child_ids:
            child_observations = reconcile_subtree(child_id)
            if child_observations is None:
                return None
            for market_id, identity in child_observations.items():
                if market_id in combined:
                    raise BetfairCatalogCoverageError(
                        "categorical split child observations overlap by market_id"
                    )
                combined[market_id] = identity

        for market_id, identity in parent_observations.items():
            if combined.get(market_id) != identity:
                raise BetfairCatalogCoverageError(
                    "saturated parent observation is not reconciled by child coverage"
                )
        return combined

    reconciled_root = reconcile_subtree(roots[0].leaf_id)
    durable_complete = (
        pending_count == 0
        and counts["FAILURE"] == 0
        and counts["SATURATED_UNSPLITTABLE"] == 0
        and counts["AFTER_CAUSAL_CUTOFF"] == 0
        and reconciled_root is not None
    )

    live_by_key: dict[
        tuple[str, str, str],
        BetfairAuthenticatedDiscoveryAcquisition,
    ] = {}
    for acquisition in live_acquisitions:
        provider_result = _provider_result_from_acquisition(acquisition)
        receipt = acquisition.receipt
        if receipt.transport_authority_ref != plan.session_context_id:
            continue
        key = (
            receipt.request_sha256,
            receipt.raw_response_sha256,
            receipt.observed_at_utc,
        )
        if key in live_by_key:
            raise BetfairCatalogCoverageError(
                "duplicate live acquisition for one coverage observation"
            )
        live_by_key[key] = acquisition

        required = required_terminals.get(key)
        if required is None:
            continue
        leaf, terminal = required
        try:
            batch = _parse_catalogue_result(
                provider_result,
                request=leaf.request,
            )
        except (TypeError, ValueError) as exc:
            raise BetfairCatalogCoverageError(
                "live authenticated result cannot revalidate durable coverage terminal"
            ) from exc
        observed = _utc(receipt.observed_at_utc, "live receipt observed_at_utc")
        if observed > _utc(plan.causal_cutoff_utc, "causal_cutoff_utc"):
            expected_status = "AFTER_CAUSAL_CUTOFF"
            expected_children: tuple[str, ...] = ()
        elif batch.continuation_required:
            split = split_catalog_coverage_request(leaf.request)
            if split is None:
                expected_status = "SATURATED_UNSPLITTABLE"
                expected_children = ()
            else:
                expected_status = "SATURATED_SPLIT"
                expected_children = tuple(
                    _leaf_id(
                        plan.plan_id,
                        parent_leaf_id=leaf.leaf_id,
                        depth=leaf.depth + 1,
                        request_sha256=_request_sha256(child),
                    )
                    for child in split
                )
        elif not batch.markets:
            expected_status = "EMPTY"
            expected_children = ()
        else:
            expected_status = "SUCCESS"
            expected_children = ()
        expected_market_observations = _market_observation_payloads(batch.markets)
        if (
            terminal.get("status") != expected_status
            or terminal.get("result_count") != len(batch.markets)
            or tuple(terminal.get("child_leaf_ids", ())) != expected_children
            or terminal.get("market_observations") != expected_market_observations
        ):
            raise BetfairCatalogCoverageError(
                "live provider result does not match durable terminal semantics"
            )

    required_keys = set(required_terminals)
    live_bound_count = len(required_keys & set(live_by_key))
    provider_complete = durable_complete and required_keys <= set(live_by_key)
    if provider_complete:
        reason = "PROVIDER_VISIBLE_SCOPE_COMPLETE"
    elif durable_complete:
        reason = "AUTHENTICATED_REACQUISITION_REQUIRED"
    else:
        reason = "DURABLE_COVERAGE_INCOMPLETE"

    return CatalogCoverageResolution(
        plan_id=plan.plan_id,
        leaf_count=len(starts),
        terminal_count=len(starts) - pending_count,
        pending_count=pending_count,
        success_count=counts["SUCCESS"],
        empty_count=counts["EMPTY"],
        saturated_split_count=counts["SATURATED_SPLIT"],
        failure_count=counts["FAILURE"],
        unsplittable_count=counts["SATURATED_UNSPLITTABLE"],
        after_cutoff_count=counts["AFTER_CAUSAL_CUTOFF"],
        live_authenticated_receipt_count=live_bound_count,
        durable_partition_complete=durable_complete,
        provider_visible_scope_complete=provider_complete,
        reason=reason,
        promotion_ready=False,
    )


def _make_catalogue_coverage_runtime():
    """Seal the canonical catalogue parser behind the public coverage operations."""

    catalog_module = _catalog
    parser = catalog_module.parse_market_catalogue_result_for_request
    parser_code = parser.__code__
    record_impl = _record_catalog_coverage_acquisition_impl
    resolve_impl = _resolve_catalog_coverage_impl

    def parse_catalogue_result(result: object, *, request: BetfairCatalogRequest):
        if (
            catalog_module.parse_market_catalogue_result_for_request is not parser
            or parser.__code__ is not parser_code
        ):
            raise BetfairCatalogCoverageError(
                "canonical Betfair catalogue parser dispatch changed"
            )
        try:
            parsed = parser(result, request=request)
        except (TypeError, ValueError) as exc:
            raise BetfairCatalogCoverageError(
                "canonical Betfair catalogue result cannot be parsed"
            ) from exc
        if (
            catalog_module.parse_market_catalogue_result_for_request is not parser
            or parser.__code__ is not parser_code
        ):
            raise BetfairCatalogCoverageError(
                "canonical Betfair catalogue parser dispatch changed"
            )
        return parsed

    def record_catalog_coverage_acquisition(
        store: CollectorDeltaStore,
        *,
        expected_store_path: str | Path,
        plan_id: str,
        leaf_id: str,
        acquisition: BetfairAuthenticatedDiscoveryAcquisition,
    ) -> CatalogCoverageRecordResult:
        return record_impl(
            store,
            expected_store_path=expected_store_path,
            plan_id=plan_id,
            leaf_id=leaf_id,
            acquisition=acquisition,
            _parse_catalogue_result=parse_catalogue_result,
        )

    def resolve_catalog_coverage(
        store: CollectorDeltaStore,
        *,
        expected_store_path: str | Path,
        plan_id: str,
        live_acquisitions: Sequence[BetfairAuthenticatedDiscoveryAcquisition] = (),
    ) -> CatalogCoverageResolution:
        return resolve_impl(
            store,
            expected_store_path=expected_store_path,
            plan_id=plan_id,
            live_acquisitions=live_acquisitions,
            _parse_catalogue_result=parse_catalogue_result,
        )

    return record_catalog_coverage_acquisition, resolve_catalog_coverage


(
    record_catalog_coverage_acquisition,
    resolve_catalog_coverage,
) = _make_catalogue_coverage_runtime()
del _make_catalogue_coverage_runtime
del _record_catalog_coverage_acquisition_impl
del _resolve_catalog_coverage_impl
