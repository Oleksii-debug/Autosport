from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class MatchbookApiGroupAuthorityError(RuntimeError):
    """Base error for deterministic Matchbook API-group classification."""


class UnknownEndpoint(MatchbookApiGroupAuthorityError):
    """No documented endpoint authority matches the exact method/path."""


class AmbiguousEndpointAuthority(MatchbookApiGroupAuthorityError):
    """Two endpoint templates could classify the same request."""


class InvalidEndpointEvidence(MatchbookApiGroupAuthorityError):
    """Endpoint authority metadata is malformed."""


class MatchbookApiGroup(str, Enum):
    ACCOUNT = "ACCOUNT"
    EVENTS = "EVENTS"
    SECURITY = "SECURITY"
    NAVIGATION = "NAVIGATION"
    REPORTS = "REPORTS"
    BETTING_WRITE = "BETTING_WRITE"
    BETTING_READ = "BETTING_READ"
    DEFAULT = "DEFAULT"


EVIDENCE_AS_OF = "2026-09-22"
_METHOD_RE = re.compile(r"^[A-Z]+$")
_PLACEHOLDER_RE = re.compile(r"^\{([a-z][a-z0-9_]*)\}$")


def _text(value: Any, *, field: str) -> str:
    if type(value) is not str or not value:
        raise InvalidEndpointEvidence(f"{field} must be a non-empty exact string")
    if value != value.strip():
        raise InvalidEndpointEvidence(f"{field} must not contain surrounding whitespace")
    return value


def _validate_method(value: Any) -> str:
    method = _text(value, field="method")
    if _METHOD_RE.fullmatch(method) is None:
        raise InvalidEndpointEvidence("method must be exact uppercase ASCII letters")
    return method


def _validate_path(value: Any, *, template: bool) -> str:
    path = _text(value, field="path_template" if template else "path")
    if not path.startswith("/"):
        raise InvalidEndpointEvidence("path must start with /")
    if path == "/" or path.endswith("/"):
        raise InvalidEndpointEvidence("path must not be root or end with /")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        raise InvalidEndpointEvidence("path must exclude ASCII control characters")
    if "?" in path or "#" in path or "\\" in path or "%" in path:
        raise InvalidEndpointEvidence("path must exclude query, fragment, backslash and percent encoding")
    if "//" in path:
        raise InvalidEndpointEvidence("path must not contain empty segments")
    segments = path[1:].split("/")
    for segment in segments:
        if segment in {".", ".."} or not segment:
            raise InvalidEndpointEvidence("path contains an unsafe segment")
        placeholder = _PLACEHOLDER_RE.fullmatch(segment)
        if template:
            if "{" in segment or "}" in segment:
                if placeholder is None:
                    raise InvalidEndpointEvidence("placeholder must occupy one full path segment")
        elif "{" in segment or "}" in segment:
            raise InvalidEndpointEvidence("concrete path must not contain template braces")
    return path


def _segments(path: str) -> tuple[str, ...]:
    return tuple(path[1:].split("/"))


def _is_placeholder(segment: str) -> bool:
    return _PLACEHOLDER_RE.fullmatch(segment) is not None


def _templates_overlap(a: str, b: str) -> bool:
    left = _segments(a)
    right = _segments(b)
    if len(left) != len(right):
        return False
    for x, y in zip(left, right):
        if x == y or _is_placeholder(x) or _is_placeholder(y):
            continue
        return False
    return True


def _template_matches(template: str, concrete: str) -> bool:
    expected = _segments(template)
    actual = _segments(concrete)
    if len(expected) != len(actual):
        return False
    for exp, got in zip(expected, actual):
        if _is_placeholder(exp):
            # IDs are opaque path-segment identities here.  Parsing/provider
            # type validation remains the endpoint adapter's authority.
            if not got:
                return False
        elif exp != got:
            return False
    return True


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


@dataclass(frozen=True)
class EndpointSpec:
    operation_id: str
    method: str
    path_template: str
    group: MatchbookApiGroup
    source_url: str
    evidence_as_of: str = EVIDENCE_AS_OF

    def __post_init__(self) -> None:
        _text(self.operation_id, field="operation_id")
        object.__setattr__(self, "method", _validate_method(self.method))
        object.__setattr__(
            self, "path_template", _validate_path(self.path_template, template=True)
        )
        if type(self.group) is not MatchbookApiGroup:
            raise InvalidEndpointEvidence("group must be MatchbookApiGroup")
        source = _text(self.source_url, field="source_url")
        if not source.startswith("https://developers.matchbook.com/"):
            raise InvalidEndpointEvidence("source_url must be an official Matchbook developer URL")
        as_of = _text(self.evidence_as_of, field="evidence_as_of")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
            raise InvalidEndpointEvidence("evidence_as_of must be YYYY-MM-DD")

    def canonical_record(self) -> dict[str, str]:
        return {
            "operation_id": self.operation_id,
            "method": self.method,
            "path_template": self.path_template,
            "group": self.group.value,
            "source_url": self.source_url,
            "evidence_as_of": self.evidence_as_of,
        }


_CANONICAL_OPERATION_PREFIX = "matchbook."


def _assert_canonical_operation_binding(spec: EndpointSpec) -> None:
    """Keep canonical Matchbook operation and route semantics non-rebindable."""

    canonical_specs = globals().get("DEFAULT_ENDPOINT_SPECS")
    if type(canonical_specs) is not tuple:
        raise InvalidEndpointEvidence(
            "canonical Matchbook endpoint authority is unavailable"
        )

    operation_matches = tuple(
        item for item in canonical_specs if item.operation_id == spec.operation_id
    )
    if spec.operation_id.startswith(_CANONICAL_OPERATION_PREFIX):
        if len(operation_matches) != 1:
            raise InvalidEndpointEvidence(
                f"operation_id {spec.operation_id!r} is reserved for canonical Matchbook authority"
            )
        if spec.canonical_record() != operation_matches[0].canonical_record():
            raise InvalidEndpointEvidence(
                f"canonical operation_id {spec.operation_id!r} cannot be rebound"
            )

    route_matches = tuple(
        item
        for item in canonical_specs
        if item.method == spec.method
        and _templates_overlap(item.path_template, spec.path_template)
    )
    if route_matches and not any(
        spec.canonical_record() == item.canonical_record() for item in route_matches
    ):
        raise InvalidEndpointEvidence(
            f"canonical Matchbook route {spec.method} {spec.path_template!r} cannot be rebound"
        )

@dataclass(frozen=True)
class ClassificationReceipt:
    operation_id: str
    method: str
    path: str
    path_template: str
    group: MatchbookApiGroup
    source_url: str
    evidence_as_of: str
    registry_sha256: str


class EndpointGroupRegistry:
    def __init__(self, specs: Iterable[EndpointSpec]) -> None:
        records = tuple(specs)
        if not records:
            raise InvalidEndpointEvidence("registry must contain at least one endpoint")
        by_operation: set[str] = set()
        for spec in records:
            if type(spec) is not EndpointSpec:
                raise InvalidEndpointEvidence("registry entries must be EndpointSpec")
            _assert_canonical_operation_binding(spec)
            if spec.operation_id in by_operation:
                raise AmbiguousEndpointAuthority(
                    f"duplicate operation_id {spec.operation_id!r}"
                )
            by_operation.add(spec.operation_id)
        for i, left in enumerate(records):
            for right in records[i + 1 :]:
                if left.method == right.method and _templates_overlap(
                    left.path_template, right.path_template
                ):
                    raise AmbiguousEndpointAuthority(
                        "overlapping endpoint templates: "
                        f"{left.method} {left.path_template!r} and {right.path_template!r}"
                    )
        self._specs = tuple(
            sorted(records, key=lambda item: (item.method, item.path_template, item.operation_id))
        )
        canonical = [item.canonical_record() for item in self._specs]
        self._registry_sha256 = hashlib.sha256(_canonical_json(canonical)).hexdigest()

    @property
    def registry_sha256(self) -> str:
        return self._registry_sha256

    @property
    def specs(self) -> tuple[EndpointSpec, ...]:
        return self._specs

    def classify(self, *, method: str, path: str) -> ClassificationReceipt:
        try:
            exact_method = _validate_method(method)
            exact_path = _validate_path(path, template=False)
        except InvalidEndpointEvidence as exc:
            raise UnknownEndpoint(str(exc)) from exc
        matches = [
            spec
            for spec in self._specs
            if spec.method == exact_method
            and _template_matches(spec.path_template, exact_path)
        ]
        if not matches:
            raise UnknownEndpoint(f"no documented authority for {exact_method} {exact_path}")
        if len(matches) != 1:
            # Constructor overlap checks make this unreachable for a valid
            # registry, but fail closed if internal state is ever corrupted.
            raise AmbiguousEndpointAuthority(
                f"multiple endpoint authorities matched {exact_method} {exact_path}"
            )
        spec = matches[0]
        return ClassificationReceipt(
            operation_id=spec.operation_id,
            method=exact_method,
            path=exact_path,
            path_template=spec.path_template,
            group=spec.group,
            source_url=spec.source_url,
            evidence_as_of=spec.evidence_as_of,
            registry_sha256=self._registry_sha256,
        )


DEFAULT_ENDPOINT_SPECS: tuple[EndpointSpec, ...] = (
    EndpointSpec(
        "matchbook.security.login",
        "POST",
        "/bpapi/rest/security/session",
        MatchbookApiGroup.SECURITY,
        "https://developers.matchbook.com/reference/login",
    ),
    EndpointSpec(
        "matchbook.security.get_session",
        "GET",
        "/bpapi/rest/security/session",
        MatchbookApiGroup.SECURITY,
        "https://developers.matchbook.com/reference/get-session",
    ),
    EndpointSpec(
        "matchbook.security.logout",
        "DELETE",
        "/bpapi/rest/security/session",
        MatchbookApiGroup.SECURITY,
        "https://developers.matchbook.com/reference/logout",
    ),
    EndpointSpec(
        "matchbook.account.get",
        "GET",
        "/edge/rest/account",
        MatchbookApiGroup.ACCOUNT,
        "https://developers.matchbook.com/reference/get-account",
    ),
    EndpointSpec(
        "matchbook.account.balance",
        "GET",
        "/edge/rest/account/balance",
        MatchbookApiGroup.ACCOUNT,
        "https://developers.matchbook.com/reference/get-new-wallet-balance",
    ),
    EndpointSpec(
        "matchbook.account.sports",
        "GET",
        "/edge/rest/account/sports",
        MatchbookApiGroup.ACCOUNT,
        "https://developers.matchbook.com/reference/account-sports",
    ),
    EndpointSpec(
        "matchbook.account.positions",
        "GET",
        "/edge/rest/account/positions",
        MatchbookApiGroup.BETTING_READ,
        "https://developers.matchbook.com/reference/get-positions",
    ),
    EndpointSpec(
        "matchbook.navigation.get",
        "GET",
        "/edge/rest/navigation",
        MatchbookApiGroup.NAVIGATION,
        "https://developers.matchbook.com/reference/get-navigation",
    ),
    EndpointSpec(
        "matchbook.events.list",
        "GET",
        "/edge/rest/events",
        MatchbookApiGroup.EVENTS,
        "https://developers.matchbook.com/reference/get-events",
    ),
    EndpointSpec(
        "matchbook.events.get",
        "GET",
        "/edge/rest/events/{event_id}",
        MatchbookApiGroup.EVENTS,
        "https://developers.matchbook.com/reference/get-event",
    ),
    EndpointSpec(
        "matchbook.markets.list",
        "GET",
        "/edge/rest/events/{event_id}/markets",
        MatchbookApiGroup.EVENTS,
        "https://developers.matchbook.com/reference/get-markets",
    ),
    EndpointSpec(
        "matchbook.runners.list",
        "GET",
        "/edge/rest/events/{event_id}/markets/{market_id}/runners",
        MatchbookApiGroup.EVENTS,
        "https://developers.matchbook.com/reference/get-runners",
    ),
    EndpointSpec(
        "matchbook.prices.get",
        "GET",
        "/edge/rest/events/{event_id}/markets/{market_id}/runners/{runner_id}/prices",
        MatchbookApiGroup.EVENTS,
        "https://developers.matchbook.com/reference/get-prices",
    ),
    EndpointSpec(
        "matchbook.offers.list_unsettled",
        "GET",
        "/edge/rest/v2/offers",
        MatchbookApiGroup.BETTING_READ,
        "https://developers.matchbook.com/reference/get-offers-v2",
    ),
    EndpointSpec(
        "matchbook.offers.get_unsettled",
        "GET",
        "/edge/rest/v2/offers/{offer_id}",
        MatchbookApiGroup.BETTING_READ,
        "https://developers.matchbook.com/reference/get-offer-v2",
    ),
    EndpointSpec(
        "matchbook.offers.submit",
        "POST",
        "/edge/rest/v2/offers",
        MatchbookApiGroup.BETTING_WRITE,
        "https://developers.matchbook.com/reference/submit-offers-v2",
    ),
    EndpointSpec(
        "matchbook.offers.cancel",
        "DELETE",
        "/edge/rest/v2/offers",
        MatchbookApiGroup.BETTING_WRITE,
        "https://developers.matchbook.com/reference/cancel-offers-v2",
    ),
    EndpointSpec(
        "matchbook.offers.edit",
        "PUT",
        "/edge/rest/v2/offers/{offer_id}",
        MatchbookApiGroup.BETTING_WRITE,
        "https://developers.matchbook.com/reference/edit-offer-v2",
    ),
    EndpointSpec(
        "matchbook.heartbeat.post",
        "POST",
        "/edge/rest/v1/heartbeat",
        MatchbookApiGroup.DEFAULT,
        "https://developers.matchbook.com/reference/post-heartbeat",
    ),
    EndpointSpec(
        "matchbook.reports.current_bets",
        "GET",
        "/edge/rest/reports/v2/bets/current",
        MatchbookApiGroup.REPORTS,
        "https://developers.matchbook.com/reference/get-current-bets-v2",
    ),
    EndpointSpec(
        "matchbook.reports.settled_bets",
        "GET",
        "/edge/rest/reports/v2/bets/settled",
        MatchbookApiGroup.REPORTS,
        "https://developers.matchbook.com/reference/get-settled-bets-v2",
    ),
    EndpointSpec(
        "matchbook.reports.current_offers",
        "GET",
        "/edge/rest/reports/v2/offers/current",
        MatchbookApiGroup.REPORTS,
        "https://developers.matchbook.com/reference/get-current-offers-v2",
    ),
    EndpointSpec(
        "matchbook.reports.wallet_transactions",
        "GET",
        "/edge/rest/reports/v1/transactions",
        MatchbookApiGroup.REPORTS,
        "https://developers.matchbook.com/reference/get-new-wallet-transactions",
    ),
)

DEFAULT_ENDPOINT_GROUP_REGISTRY = EndpointGroupRegistry(DEFAULT_ENDPOINT_SPECS)
