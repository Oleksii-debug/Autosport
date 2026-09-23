"""Restart-stable BETDAQ authenticated-principal account identity composition.

The canonical BETDAQ adapter deliberately uses a random process-local credential
context to detect credential/application mutation during one acquisition.  That
session identity is correct for acquisition safety but cannot be the durable account
identity consumed by account reconciliation across process restart.

This module composes *after* a successful canonical BETDAQ acquisition.  It keeps the
source session context intact as evidence and projects only the completed canonical
BookmakerAccountSnapshot onto a deterministic, pseudonymous authenticated-principal
identity derived from the exact BETDAQ username + product-owned provider namespace.
Password/application rotation therefore does not fork account history, while a
different username does.

The provider does not expose an immutable physical/legal account identifier in the
read contracts used here.  Accordingly this module proves authenticated-principal
continuity only; stronger account-identity truth remains explicitly false.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
from threading import Lock
from typing import Callable
from weakref import ReferenceType, ref

from .betdaq_account_readonly import (
    ADAPTER_ID,
    BetdaqAccountEvidence,
    BetdaqAccountReadOnlyClient,
    BetdaqAccountReadOnlyError,
    BetdaqAuthenticatedAccountContext,
    BetdaqCredentials,
)
from .bookmaker_capability import BookmakerAccountSnapshot, BookmakerCapability
from .bookmaker_account_reconciliation import BookmakerAccountReconciliationStore


_PRINCIPAL_ID_PREFIX = "betdaq-authenticated-principal:"
_PRINCIPAL_SCOPE = "AUTHENTICATED_BETDAQ_USERNAME_CONTINUITY"
_SESSION_ID_PREFIX = "betdaq-auth-context:"
_CANONICAL_VENUE_ID = "betdaq"

# Positive durable principal continuity is stronger than a structurally valid
# BetdaqAccountEvidence object. Freeze the exact #1610 source authority that is
# allowed to authenticate the principal before this module projects durable identity.
_CANONICAL_ACCOUNT_CLIENT_CLASS = BetdaqAccountReadOnlyClient
_CANONICAL_ACCOUNT_CLIENT_INIT = BetdaqAccountReadOnlyClient.__init__
_CANONICAL_ACCOUNT_CLIENT_INIT_CODE = BetdaqAccountReadOnlyClient.__init__.__code__
_CANONICAL_ACCOUNT_READ_EVIDENCE = BetdaqAccountReadOnlyClient.read_account_evidence
_CANONICAL_ACCOUNT_READ_EVIDENCE_CODE = (
    BetdaqAccountReadOnlyClient.read_account_evidence.__code__
)

# #790 is the sole durable reconciliation authority. Freeze the exact class and
# method dispatch consumed by this composition just as the authenticated #1610
# source dispatch is frozen above; accepting a subclass/instance shadow here would
# let caller code report reconciliation success without executing #790 durability.
_CANONICAL_RECONCILIATION_STORE_CLASS = BookmakerAccountReconciliationStore
_CANONICAL_RECONCILIATION_LATEST_SNAPSHOT = (
    BookmakerAccountReconciliationStore.latest_snapshot
)
_CANONICAL_RECONCILIATION_LATEST_SNAPSHOT_CODE = (
    BookmakerAccountReconciliationStore.latest_snapshot.__code__
)
_CANONICAL_RECONCILIATION_APPEND_SNAPSHOT = (
    BookmakerAccountReconciliationStore.append_snapshot
)
_CANONICAL_RECONCILIATION_APPEND_SNAPSHOT_CODE = (
    BookmakerAccountReconciliationStore.append_snapshot.__code__
)


class BetdaqAccountContinuityError(RuntimeError):
    """Base error for BETDAQ authenticated-principal continuity composition."""


class BetdaqAccountContinuityMigrationRequiredError(BetdaqAccountContinuityError):
    """Legacy process-local history cannot be silently relabelled as continuous."""


@dataclass(frozen=True, slots=True)
class BetdaqAuthenticatedPrincipalContext:
    """Pseudonymous continuity identity for one successfully authenticated username."""

    venue_id: str
    principal_context_id: str
    source_session_context_id: str
    identity_scope: str = _PRINCIPAL_SCOPE
    authenticated_principal_continuity_proven: bool = True
    immutable_physical_account_identity_proven: bool = False

    def __post_init__(self) -> None:
        venue = _required_text(self.venue_id, "venue_id")
        if venue != _CANONICAL_VENUE_ID:
            raise BetdaqAccountContinuityError(
                "BETDAQ continuity venue_id is product-owned and must be canonical"
            )
        if (
            type(self.principal_context_id) is not str
            or not self.principal_context_id.startswith(_PRINCIPAL_ID_PREFIX)
        ):
            raise BetdaqAccountContinuityError(
                "principal_context_id is not a canonical BETDAQ principal id"
            )
        _sha256_hex(
            self.principal_context_id.removeprefix(_PRINCIPAL_ID_PREFIX),
            "principal_context_id",
        )
        if (
            type(self.source_session_context_id) is not str
            or not self.source_session_context_id.startswith(_SESSION_ID_PREFIX)
        ):
            raise BetdaqAccountContinuityError(
                "source_session_context_id is not a canonical BETDAQ session context"
            )
        _sha256_hex(
            self.source_session_context_id.removeprefix(_SESSION_ID_PREFIX),
            "source_session_context_id",
        )
        if self.identity_scope != _PRINCIPAL_SCOPE:
            raise BetdaqAccountContinuityError(
                "BETDAQ principal continuity identity scope is product-owned"
            )
        if self.authenticated_principal_continuity_proven is not True:
            raise BetdaqAccountContinuityError(
                "BETDAQ principal continuity receipt must preserve positive principal truth"
            )
        if self.immutable_physical_account_identity_proven is not False:
            raise BetdaqAccountContinuityError(
                "BETDAQ read contracts do not prove immutable physical account identity"
            )


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetdaqContinuousAccountEvidence:
    """Fresh canonical BETDAQ evidence with a restart-stable principal projection."""

    snapshot: BookmakerAccountSnapshot
    source_evidence: BetdaqAccountEvidence
    principal_context: BetdaqAuthenticatedPrincipalContext

    def __post_init__(self) -> None:
        if type(self.snapshot) is not BookmakerAccountSnapshot:
            raise BetdaqAccountContinuityError(
                "snapshot must be canonical BookmakerAccountSnapshot"
            )
        if type(self.source_evidence) is not BetdaqAccountEvidence:
            raise BetdaqAccountContinuityError(
                "source_evidence must be canonical BetdaqAccountEvidence"
            )
        if type(self.principal_context) is not BetdaqAuthenticatedPrincipalContext:
            raise BetdaqAccountContinuityError(
                "principal_context must be canonical BetdaqAuthenticatedPrincipalContext"
            )
        source_context = self.source_evidence.account_context
        if type(source_context) is not BetdaqAuthenticatedAccountContext:
            raise BetdaqAccountContinuityError(
                "source account context is not canonical BETDAQ evidence"
            )
        if (
            self.principal_context.source_session_context_id
            != source_context.session_context_id
        ):
            raise BetdaqAccountContinuityError(
                "principal context is not bound to the source acquisition session"
            )
        if (
            self.snapshot.profile.venue_id != self.principal_context.venue_id
            or self.snapshot.profile.adapter_id != ADAPTER_ID
            or self.snapshot.profile.account_id
            != self.principal_context.principal_context_id
        ):
            raise BetdaqAccountContinuityError(
                "projected snapshot is not bound to authenticated principal continuity"
            )
        if (
            self.source_evidence.snapshot.profile.venue_id
            != self.principal_context.venue_id
            or self.source_evidence.snapshot.profile.adapter_id != ADAPTER_ID
            or self.source_evidence.snapshot.profile.account_id
            != source_context.session_context_id
        ):
            raise BetdaqAccountContinuityError(
                "source snapshot is not bound to its canonical BETDAQ session context"
            )


def _require_canonical_source_authority() -> None:
    """Fail closed if the authenticated #1610 source dispatch was rebound."""

    if BetdaqAccountReadOnlyClient is not _CANONICAL_ACCOUNT_CLIENT_CLASS:
        raise BetdaqAccountContinuityError(
            "canonical BETDAQ account source constructor was replaced"
        )
    if (
        _CANONICAL_ACCOUNT_CLIENT_CLASS.__init__ is not _CANONICAL_ACCOUNT_CLIENT_INIT
        or getattr(_CANONICAL_ACCOUNT_CLIENT_CLASS.__init__, "__code__", None)
        is not _CANONICAL_ACCOUNT_CLIENT_INIT_CODE
    ):
        raise BetdaqAccountContinuityError(
            "canonical BETDAQ account source constructor implementation changed"
        )
    if (
        _CANONICAL_ACCOUNT_CLIENT_CLASS.read_account_evidence
        is not _CANONICAL_ACCOUNT_READ_EVIDENCE
        or getattr(
            _CANONICAL_ACCOUNT_CLIENT_CLASS.read_account_evidence,
            "__code__",
            None,
        )
        is not _CANONICAL_ACCOUNT_READ_EVIDENCE_CODE
    ):
        raise BetdaqAccountContinuityError(
            "canonical BETDAQ account source read authority changed"
        )


def _require_canonical_source_instance(source: object) -> None:
    """Require the exact source type and unshadowed bound read authority."""

    if type(source) is not _CANONICAL_ACCOUNT_CLIENT_CLASS:
        raise BetdaqAccountContinuityError(
            "continuity source is not the canonical BETDAQ account client"
        )
    bound_read = getattr(source, "read_account_evidence", None)
    if (
        getattr(bound_read, "__self__", None) is not source
        or getattr(bound_read, "__func__", None) is not _CANONICAL_ACCOUNT_READ_EVIDENCE
    ):
        raise BetdaqAccountContinuityError(
            "canonical BETDAQ account source read authority was shadowed"
        )


def _require_canonical_reconciliation_store(store: object) -> None:
    """Require the exact #790 durable store and unshadowed method dispatch."""

    if (
        BookmakerAccountReconciliationStore
        is not _CANONICAL_RECONCILIATION_STORE_CLASS
    ):
        raise BetdaqAccountContinuityError(
            "canonical BETDAQ reconciliation store class was replaced"
        )
    if (
        _CANONICAL_RECONCILIATION_STORE_CLASS.latest_snapshot
        is not _CANONICAL_RECONCILIATION_LATEST_SNAPSHOT
        or getattr(
            _CANONICAL_RECONCILIATION_STORE_CLASS.latest_snapshot,
            "__code__",
            None,
        )
        is not _CANONICAL_RECONCILIATION_LATEST_SNAPSHOT_CODE
        or _CANONICAL_RECONCILIATION_STORE_CLASS.append_snapshot
        is not _CANONICAL_RECONCILIATION_APPEND_SNAPSHOT
        or getattr(
            _CANONICAL_RECONCILIATION_STORE_CLASS.append_snapshot,
            "__code__",
            None,
        )
        is not _CANONICAL_RECONCILIATION_APPEND_SNAPSHOT_CODE
    ):
        raise BetdaqAccountContinuityError(
            "canonical BETDAQ reconciliation store implementation changed"
        )
    if type(store) is not _CANONICAL_RECONCILIATION_STORE_CLASS:
        raise BetdaqAccountContinuityError(
            "reconciliation target is not the canonical durable #790 store"
        )
    bound_latest = getattr(store, "latest_snapshot", None)
    bound_append = getattr(store, "append_snapshot", None)
    if (
        getattr(bound_latest, "__self__", None) is not store
        or getattr(bound_latest, "__func__", None)
        is not _CANONICAL_RECONCILIATION_LATEST_SNAPSHOT
        or getattr(bound_append, "__self__", None) is not store
        or getattr(bound_append, "__func__", None)
        is not _CANONICAL_RECONCILIATION_APPEND_SNAPSHOT
    ):
        raise BetdaqAccountContinuityError(
            "canonical BETDAQ reconciliation store dispatch was shadowed"
        )


class BetdaqAccountContinuityClient:
    """Canonical BETDAQ account acquisition with post-auth principal projection.

    No transport injection is accepted here.  Positive continuity is only emitted
    after ``BetdaqAccountReadOnlyClient.read_account_evidence`` has passed its existing
    exact product-owned HTTPS transport fence.
    """

    def __init__(
        self,
        credentials: BetdaqCredentials,
        *,
        venue_id: str = _CANONICAL_VENUE_ID,
        account_id: str = "default-account",
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(credentials) is not BetdaqCredentials:
            raise TypeError("credentials must be BetdaqCredentials")
        venue = _required_text(venue_id, "venue_id")
        if venue != _CANONICAL_VENUE_ID:
            raise BetdaqAccountContinuityError(
                "BETDAQ continuity venue_id is product-owned and must be canonical"
            )
        # Keep the caller-owned credential object only as a mutation sentinel.
        # The secure reader receives a private frozen value snapshot so an external
        # A->B->A mutation cannot alter one or more requests and then evade the
        # before/after context check by restoring the original values.
        self._credentials = credentials
        self._sealed_credentials = BetdaqCredentials(
            username=credentials.username,
            password=credentials.password,
            application_identifier=credentials.application_identifier,
            version=credentials.version,
            language_code=credentials.language_code,
        )
        self._venue_id = _CANONICAL_VENUE_ID
        _require_canonical_source_authority()
        self._source = _CANONICAL_ACCOUNT_CLIENT_CLASS(
            self._sealed_credentials,
            venue_id=self._venue_id,
            account_id=account_id,
            timeout_seconds=timeout_seconds,
            clock=clock,
        )
        _require_canonical_source_authority()
        _require_canonical_source_instance(self._source)

    def read_account_evidence(
        self,
        requested_capabilities: frozenset[BookmakerCapability],
    ) -> BetdaqContinuousAccountEvidence:
        sealed_material = _credential_material(self._sealed_credentials)
        if _credential_material(self._credentials) != sealed_material:
            raise BetdaqAccountReadOnlyError(
                "BETDAQ authenticated account context changed during acquisition"
            )
        _require_canonical_source_authority()
        _require_canonical_source_instance(self._source)
        source = _CANONICAL_ACCOUNT_READ_EVIDENCE(
            self._source,
            requested_capabilities,
        )
        _require_canonical_source_authority()
        _require_canonical_source_instance(self._source)
        if _credential_material(self._credentials) != sealed_material:
            raise BetdaqAccountReadOnlyError(
                "BETDAQ authenticated account context changed during acquisition"
            )
        principal_context = _principal_context(
            self._sealed_credentials.username,
            source.account_context,
        )
        snapshot = _project_snapshot(
            source.snapshot,
            principal_context,
        )
        return _issue_continuous_evidence(
            snapshot=snapshot,
            source_evidence=source,
            principal_context=principal_context,
        )

    def read_account_snapshot(
        self,
        requested_capabilities: frozenset[BookmakerCapability],
    ) -> BookmakerAccountSnapshot:
        return self.read_account_evidence(requested_capabilities).snapshot


def append_to_reconciliation(
    store: BookmakerAccountReconciliationStore,
    evidence: BetdaqContinuousAccountEvidence,
) -> bool:
    """Canonical #1733 -> #790 composition; account-id text alone is insufficient."""
    _require_canonical_reconciliation_store(store)
    if not _is_product_issued_continuity_evidence(evidence):
        raise BetdaqAccountContinuityError(
            "reconciliation requires product-issued BETDAQ continuity evidence"
        )
    latest = _CANONICAL_RECONCILIATION_LATEST_SNAPSHOT(store)
    require_reconciliation_history_compatible(latest, evidence)
    return _CANONICAL_RECONCILIATION_APPEND_SNAPSHOT(store, evidence.snapshot)


def require_reconciliation_history_compatible(
    latest_snapshot: BookmakerAccountSnapshot | None,
    current_evidence: BetdaqContinuousAccountEvidence,
) -> None:
    """Reject silent relabelling of legacy session-scoped #790 history.

    Existing history created before this composition uses a random
    ``betdaq-auth-context:*`` account id.  After a process restart there is no
    evidence that an arbitrary old random session id belongs to the newly
    authenticated principal, so automatic in-place migration would fabricate
    equivalence.  A future explicit migration packet may add stronger provider/
    operator proof; until then this boundary fails closed.
    """

    if latest_snapshot is None:
        return
    if type(latest_snapshot) is not BookmakerAccountSnapshot:
        raise TypeError("latest_snapshot must be BookmakerAccountSnapshot or None")
    if type(current_evidence) is not BetdaqContinuousAccountEvidence:
        raise TypeError("current_evidence must be BetdaqContinuousAccountEvidence")

    expected = current_evidence.principal_context.principal_context_id
    actual = latest_snapshot.profile.account_id
    if (
        latest_snapshot.profile.venue_id
        != current_evidence.principal_context.venue_id
        or latest_snapshot.profile.adapter_id != ADAPTER_ID
    ):
        raise BetdaqAccountContinuityError(
            "existing reconciliation history belongs to a different provider identity"
        )
    if actual == expected:
        return
    if actual.startswith(_SESSION_ID_PREFIX):
        raise BetdaqAccountContinuityMigrationRequiredError(
            "legacy BETDAQ session-scoped reconciliation history requires explicit migration proof"
        )
    raise BetdaqAccountContinuityError(
        "existing reconciliation history belongs to a different BETDAQ account identity"
    )


def _build_evidence_issuer():
    issued: dict[
        int,
        tuple[
            ReferenceType[BetdaqContinuousAccountEvidence],
            BetdaqContinuousAccountEvidence,
        ],
    ] = {}
    lock = Lock()

    def issue(
        *,
        snapshot: BookmakerAccountSnapshot,
        source_evidence: BetdaqAccountEvidence,
        principal_context: BetdaqAuthenticatedPrincipalContext,
    ) -> BetdaqContinuousAccountEvidence:
        value = BetdaqContinuousAccountEvidence(
            snapshot=snapshot,
            source_evidence=source_evidence,
            principal_context=principal_context,
        )
        # Exact outer-object identity is not enough for positive authority: frozen
        # dataclasses can still be changed with object.__setattr__. Keep a deep
        # immutable issuance snapshot so any same-object payload mutation revokes
        # the receipt before it can reach durable #790 reconciliation.
        baseline = deepcopy(value)
        key = id(value)

        def release(
            dead_ref: ReferenceType[BetdaqContinuousAccountEvidence],
            *,
            issued_key: int = key,
        ) -> None:
            with lock:
                record = issued.get(issued_key)
                if record is not None and record[0] is dead_ref:
                    issued.pop(issued_key, None)

        value_ref = ref(value, release)
        with lock:
            issued[key] = (value_ref, baseline)
        return value

    def is_issued(value: object) -> bool:
        if type(value) is not BetdaqContinuousAccountEvidence:
            return False
        with lock:
            record = issued.get(id(value))
            if record is None or record[0]() is not value:
                return False
            return value == record[1]

    return issue, is_issued


_issue_continuous_evidence, _is_product_issued_continuity_evidence = (
    _build_evidence_issuer()
)


def _principal_context(
    authenticated_username: str,
    source_context: BetdaqAuthenticatedAccountContext,
) -> BetdaqAuthenticatedPrincipalContext:
    if type(source_context) is not BetdaqAuthenticatedAccountContext:
        raise BetdaqAccountContinuityError(
            "principal continuity requires canonical authenticated source context"
        )
    venue = _required_text(source_context.venue_id, "venue_id")
    if venue != _CANONICAL_VENUE_ID:
        raise BetdaqAccountContinuityError(
            "BETDAQ continuity venue_id is product-owned and must be canonical"
        )
    username = _required_text(authenticated_username, "authenticated_username")
    material = json.dumps(
        {
            "schema": "autosport.betdaq-authenticated-principal-id-v1",
            "venue_id": _CANONICAL_VENUE_ID,
            "adapter_id": ADAPTER_ID,
            "username": username,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    principal_id = _PRINCIPAL_ID_PREFIX + sha256(material).hexdigest()
    return BetdaqAuthenticatedPrincipalContext(
        venue_id=_CANONICAL_VENUE_ID,
        principal_context_id=principal_id,
        source_session_context_id=source_context.session_context_id,
    )


def _project_snapshot(
    snapshot: BookmakerAccountSnapshot,
    principal_context: BetdaqAuthenticatedPrincipalContext,
) -> BookmakerAccountSnapshot:
    if type(snapshot) is not BookmakerAccountSnapshot:
        raise BetdaqAccountContinuityError(
            "source snapshot must be canonical BookmakerAccountSnapshot"
        )
    if type(principal_context) is not BetdaqAuthenticatedPrincipalContext:
        raise BetdaqAccountContinuityError(
            "principal_context must be canonical BetdaqAuthenticatedPrincipalContext"
        )
    if (
        snapshot.profile.venue_id != principal_context.venue_id
        or snapshot.profile.adapter_id != ADAPTER_ID
    ):
        raise BetdaqAccountContinuityError(
            "source snapshot provider identity does not match principal context"
        )
    if not snapshot.profile.account_id.startswith(_SESSION_ID_PREFIX):
        raise BetdaqAccountContinuityError(
            "source snapshot is not bound to a canonical BETDAQ acquisition session"
        )

    account_id = principal_context.principal_context_id
    profile = replace(snapshot.profile, account_id=account_id)
    balance = (
        None
        if snapshot.balance is None
        else replace(snapshot.balance, account_id=account_id)
    )
    open_positions = tuple(
        replace(item, account_id=account_id)
        for item in snapshot.open_positions
    )
    settled_positions = tuple(
        replace(item, account_id=account_id)
        for item in snapshot.settled_positions
    )
    return replace(
        snapshot,
        profile=profile,
        balance=balance,
        open_positions=open_positions,
        settled_positions=settled_positions,
    )


def _credential_material(credentials: BetdaqCredentials) -> tuple[object, ...]:
    """Snapshot the complete caller-visible authentication material for mutation checks."""
    return (
        credentials.username,
        credentials.password,
        credentials.application_identifier,
        credentials.version,
        credentials.language_code,
    )


def _required_text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise BetdaqAccountContinuityError(
            f"{field} must be non-empty canonical text"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BetdaqAccountContinuityError(
            f"{field} contains invalid Unicode"
        ) from exc
    return value


def _sha256_hex(value: str, field: str) -> str:
    text = _required_text(value, field)
    if (
        len(text) != 64
        or any(character not in "0123456789abcdef" for character in text)
    ):
        raise BetdaqAccountContinuityError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return text
