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

from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
import json
from typing import Callable

from .betdaq_account_readonly import (
    ADAPTER_ID,
    BetdaqAccountEvidence,
    BetdaqAccountReadOnlyClient,
    BetdaqAuthenticatedAccountContext,
    BetdaqCredentials,
)
from .bookmaker_capability import BookmakerAccountSnapshot, BookmakerCapability
from .bookmaker_account_reconciliation import BookmakerAccountReconciliationStore


_PRINCIPAL_ID_PREFIX = "betdaq-authenticated-principal:"
_PRINCIPAL_SCOPE = "AUTHENTICATED_BETDAQ_USERNAME_CONTINUITY"
_SESSION_ID_PREFIX = "betdaq-auth-context:"
_CANONICAL_VENUE_ID = "betdaq"


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


@dataclass(frozen=True, slots=True)
class BetdaqContinuousAccountEvidence:
    """Fresh canonical BETDAQ evidence with a restart-stable principal projection."""

    snapshot: BookmakerAccountSnapshot
    source_evidence: BetdaqAccountEvidence
    principal_context: BetdaqAuthenticatedPrincipalContext
    _issuer_token: object | None = field(default=None, repr=False, compare=False)

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
        self._credentials = credentials
        self._venue_id = _CANONICAL_VENUE_ID
        self._source = BetdaqAccountReadOnlyClient(
            credentials,
            venue_id=self._venue_id,
            account_id=account_id,
            timeout_seconds=timeout_seconds,
            clock=clock,
        )

    def read_account_evidence(
        self,
        requested_capabilities: frozenset[BookmakerCapability],
    ) -> BetdaqContinuousAccountEvidence:
        username_before = self._credentials.username
        source = self._source.read_account_evidence(requested_capabilities)
        username_after = self._credentials.username
        if username_after != username_before:
            raise BetdaqAccountContinuityError(
                "BETDAQ authenticated username changed during continuity acquisition"
            )
        principal_context = _principal_context(
            username_before,
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
    if not isinstance(store, BookmakerAccountReconciliationStore):
        raise TypeError("store must be BookmakerAccountReconciliationStore")
    if not _is_product_issued_continuity_evidence(evidence):
        raise BetdaqAccountContinuityError(
            "reconciliation requires product-issued BETDAQ continuity evidence"
        )
    latest = store.latest_snapshot()
    require_reconciliation_history_compatible(latest, evidence)
    return store.append_snapshot(evidence.snapshot)


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
    token = object()

    def issue(
        *,
        snapshot: BookmakerAccountSnapshot,
        source_evidence: BetdaqAccountEvidence,
        principal_context: BetdaqAuthenticatedPrincipalContext,
    ) -> BetdaqContinuousAccountEvidence:
        return BetdaqContinuousAccountEvidence(
            snapshot=snapshot,
            source_evidence=source_evidence,
            principal_context=principal_context,
            _issuer_token=token,
        )

    def is_issued(value: object) -> bool:
        return (
            type(value) is BetdaqContinuousAccountEvidence
            and value._issuer_token is token
        )

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
