"""Seal Betfair Historical Data positive provenance across provider I/O.

The base historical client intentionally supports structural/synthetic transports for
non-authoritative parsing tests. Positive provider-origin authority, however, must
not depend on mutable live urllib dispatch between a before/after identity check.

This composition layer preserves the structural fallback while routing only a
still-canonical product transport through one closure-hidden opener whose
authority-bearing dispatch is snapshotted at product import time. It also binds
positive acquisition records and provider-origin witnesses to hidden issuance
registries so a later restored public surface cannot retroactively bless synthetic
or transiently substituted bytes.
"""
from __future__ import annotations

from threading import RLock
from types import MethodType
from weakref import WeakKeyDictionary

from . import betfair_historical_entitlement as _historical


def _install_guard() -> None:
    client_type = _historical.BetfairHistoricalEntitlementClient
    witness_type = _historical.HistoricalProviderOriginWitness
    error_type = _historical.BetfairHistoricalEntitlementError
    backpressure_type = _historical.BetfairHistoricalBackpressure

    original_client_init = client_type.__init__
    original_remember = client_type._remember
    original_require_context = client_type._require_context
    original_require_issued = client_type._require_issued
    original_issue_witness = client_type.issue_provider_origin_witness
    original_assert_witness = witness_type.assert_authoritative
    transport_is_canonical = _historical._canonical_historical_network_transport

    request_type = _historical._CANONICAL_REQUEST_TYPE
    quote_path = _historical._CANONICAL_QUOTE
    build_opener = _historical._CANONICAL_BUILD_OPENER
    opener_type = _historical.OpenerDirector
    redirect_type = _historical._SameOriginRedirectHandler
    https_handler_type = _historical._CANONICAL_HTTPS_HANDLER
    error_processor_type = _historical._CANONICAL_HTTP_ERROR_PROCESSOR

    opener_open = _historical._CANONICAL_OPENER_OPEN
    opener_internal_open = _historical._CANONICAL_OPENER_INTERNAL_OPEN
    opener_call_chain = _historical._CANONICAL_OPENER_CALL_CHAIN
    opener_error = _historical._CANONICAL_OPENER_ERROR
    https_request = _historical._CANONICAL_HTTPS_REQUEST
    http_do_open = _historical._CANONICAL_HTTP_DO_OPEN
    https_response = _historical._CANONICAL_HTTPS_RESPONSE
    stdlib_redirect = _historical._CANONICAL_STDLIB_REDIRECT_HANDLER.redirect_request
    urljoin_path = _historical.urljoin
    origin_of = _historical._origin

    http_module = _historical._CANONICAL_HTTPS_OPEN.__globals__.get("http")
    if http_module is None:
        raise RuntimeError("canonical urllib HTTPS handler lost http module")
    https_connection = http_module.client.HTTPSConnection

    product_datetime = _historical.datetime
    product_timezone = _historical.timezone
    http_error_type = _historical.HTTPError
    url_error_type = _historical.URLError

    strict_json = _historical._strict_json
    package_from_provider = _historical._package
    canonical_json_bytes = _historical._json_bytes
    canonical_path = _historical._path
    text_value = _historical._text
    digest = _historical.sha256

    snapshot_type = _historical.HistoricalEntitlementSnapshot
    filter_type = _historical.HistoricalDownloadFilter
    listing_type = _historical.HistoricalFileListing
    downloaded_type = _historical.HistoricalDownloadedFile
    api_base = _historical.HISTORICAL_API_BASE

    # This opener is created during trusted package initialization and never exposed
    # through BetfairHistoricalEntitlementClient.
    sealed_opener = build_opener(redirect_type())
    if type(sealed_opener) is not opener_type:
        raise RuntimeError("canonical Historical Data opener construction changed")

    def sealed_redirect(handler, request, fp, code, message, headers, new_url):
        resolved = urljoin_path(request.full_url, new_url)
        if origin_of(request.full_url) != origin_of(resolved):
            raise error_type("Betfair Historical Data cross-origin redirect blocked")
        return stdlib_redirect(
            handler,
            request,
            fp,
            code,
            message,
            headers,
            resolved,
        )

    def sealed_https_open(handler, request):
        # Capture HTTPSConnection itself instead of consulting mutable
        # urllib.request.http.client dispatch during the authority-bearing call.
        return http_do_open(
            handler,
            https_connection,
            request,
            context=handler._context,
            check_hostname=handler._check_hostname,
        )

    # OpenerDirector.open dynamically dereferences these lower methods. Instance
    # binding on a closure-hidden opener makes later class monkey-patches irrelevant
    # to the authority-bearing request.
    sealed_opener.open = MethodType(opener_open, sealed_opener)
    sealed_opener._open = MethodType(opener_internal_open, sealed_opener)
    sealed_opener._call_chain = MethodType(opener_call_chain, sealed_opener)
    sealed_opener.error = MethodType(opener_error, sealed_opener)

    for handler in sealed_opener.handlers:
        if type(handler) is https_handler_type:
            handler.https_open = MethodType(sealed_https_open, handler)
            handler.https_request = MethodType(https_request, handler)
            handler.do_open = MethodType(http_do_open, handler)
        elif type(handler) is redirect_type:
            handler.redirect_request = MethodType(sealed_redirect, handler)
        elif type(handler) is error_processor_type:
            handler.http_response = MethodType(https_response, handler)
            handler.https_response = MethodType(https_response, handler)

    io_lock = RLock()
    clients: WeakKeyDictionary = WeakKeyDictionary()
    witnesses: WeakKeyDictionary = WeakKeyDictionary()

    def record_for(self):
        record = clients.get(self)
        if record is None:
            raise error_type("historical positive provenance client origin is unknown")
        return record

    def guarded_context(self) -> None:
        record_for(self)
        original_require_context(self)

    def product_now() -> str:
        if (
            _historical.datetime is not product_datetime
            or _historical.timezone is not product_timezone
        ):
            raise error_type("historical product clock authority was rebound")
        return product_datetime.now(product_timezone.utc).isoformat()

    def sealed_request(request, *, timeout_seconds: float, limit: int) -> bytes:
        try:
            with io_lock:
                with sealed_opener.open(request, timeout=timeout_seconds) as response:
                    payload = response.read(limit + 1)
        except error_type:
            raise
        except http_error_type as exc:
            if exc.code == 429:
                raise backpressure_type(
                    "Betfair Historical Data API rate limit/backpressure"
                ) from None
            raise error_type(
                f"Betfair Historical Data HTTP request failed with status {exc.code}"
            ) from None
        except (url_error_type, TimeoutError, OSError):
            raise error_type("Betfair Historical Data network request failed") from None
        if len(payload) > limit:
            raise error_type("Betfair Historical Data response exceeded size limit")
        return payload

    def session_token(self) -> str:
        guarded_context(self)
        return record_for(self)["session_token"]

    def post_provider(self, operation: str, body: bytes) -> tuple[bytes, bool]:
        guarded_context(self)
        transport = self._transport
        origin_before = transport_is_canonical(transport)
        token = session_token(self)
        if origin_before:
            request = request_type(
                f"{api_base}/{operation}",
                data=body,
                headers={"Content-Type": "application/json", "ssoid": token},
                method="POST",
            )
            payload = sealed_request(
                request,
                timeout_seconds=self._timeout,
                limit=transport._json_limit,
            )
        else:
            # Preserve structural/synthetic acquisition, but it can never acquire
            # a hidden positive-origin record.
            payload = transport.post_json(
                f"{api_base}/{operation}",
                ssoid=token,
                body=body,
                timeout_seconds=self._timeout,
            )
        guarded_context(self)
        origin_after = transport_is_canonical(transport)
        return payload, bool(origin_before and origin_after)

    def get_provider_file(self, path: str) -> tuple[bytes, bool]:
        guarded_context(self)
        transport = self._transport
        origin_before = transport_is_canonical(transport)
        token = session_token(self)
        url = f"{api_base}/DownloadFile?filePath={quote_path(path, safe='')}"
        if origin_before:
            request = request_type(url, headers={"ssoid": token}, method="GET")
            payload = sealed_request(
                request,
                timeout_seconds=self._timeout,
                limit=transport._file_limit,
            )
        else:
            payload = transport.get_file(
                url,
                ssoid=token,
                timeout_seconds=self._timeout,
            )
        guarded_context(self)
        origin_after = transport_is_canonical(transport)
        return payload, bool(origin_before and origin_after)

    def mark_origin(self, kind: str, value: object, value_digest: str) -> None:
        record_for(self)["origins"].add((kind, id(value), value_digest))

    def has_origin(self, kind: str, value: object, value_digest: str) -> bool:
        return (kind, id(value), value_digest) in record_for(self)["origins"]

    def guarded_init(
        self,
        client,
        account_identity,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        original_client_init(
            self,
            client,
            account_identity,
            timeout_seconds=timeout_seconds,
        )
        original_require_context(self)
        try:
            credentials = client._credentials
            token = text_value(credentials.session_token, "session token")
        except AttributeError as exc:
            raise error_type("canonical Betfair client lost its session context") from exc
        # Re-resolve after sampling so a transient credential swap cannot seed the
        # closure record and then restore before construction returns.
        original_require_context(self)
        clients[self] = {"session_token": token, "origins": set()}

    def guarded_get_entitlement_snapshot(self):
        payload, provider_origin = post_provider(self, "GetMyData", b"{}")
        decoded = strict_json(payload)
        if not isinstance(decoded, list):
            raise error_type("GetMyData response must be a JSON array")
        packages = tuple(
            package_from_provider(value, index) for index, value in enumerate(decoded)
        )
        ids = [value.purchase_item_id for value in packages]
        if len(ids) != len(set(ids)):
            raise error_type(
                "GetMyData returned duplicate/conflicting purchaseItemId identity"
            )
        value = snapshot_type(
            self._identity.session_context_id,
            product_now(),
            digest(payload).hexdigest(),
            packages,
        )
        original_remember(
            self,
            "snapshot",
            value,
            value.snapshot_sha256,
            provider_origin=provider_origin,
        )
        if provider_origin:
            mark_origin(self, "snapshot", value, value.snapshot_sha256)
        return value

    def guarded_require_issued(
        self,
        kind: str,
        value: object,
        value_digest: str,
    ) -> bool:
        provider_origin = original_require_issued(self, kind, value, value_digest)
        return bool(provider_origin and has_origin(self, kind, value, value_digest))

    def guarded_require_snapshot(self, value) -> bool:
        guarded_context(self)
        if type(value) is not snapshot_type:
            raise error_type("entitlement snapshot is not canonical")
        provider_origin = guarded_require_issued(
            self,
            "snapshot",
            value,
            value.snapshot_sha256,
        )
        if value.session_context_id != self._identity.session_context_id:
            raise error_type("snapshot belongs to another context")
        return provider_origin

    def guarded_list_files(self, snapshot, download_filter):
        snapshot_origin = guarded_require_snapshot(self, snapshot)
        if type(download_filter) is not filter_type:
            raise error_type("download_filter is not canonical")
        months = {
            value.month
            for value in snapshot.packages
            if value.sport == download_filter.sport
            and value.plan == download_filter.plan
        }
        if not download_filter.required_months.issubset(months):
            raise error_type(
                "download filter month range is not covered by authenticated purchases"
            )
        body = canonical_json_bytes(download_filter.provider_payload())
        payload, provider_origin = post_provider(self, "DownloadListOfFiles", body)
        decoded = strict_json(payload)
        if not isinstance(decoded, list):
            raise error_type("DownloadListOfFiles response must be a JSON array")
        paths = tuple(canonical_path(value) for value in decoded)
        if len(paths) != len(set(paths)):
            raise error_type("DownloadListOfFiles returned duplicate provider paths")
        value = listing_type(
            self._identity.session_context_id,
            snapshot.snapshot_sha256,
            download_filter.filter_sha256,
            product_now(),
            digest(payload).hexdigest(),
            paths,
        )
        positive = bool(snapshot_origin and provider_origin)
        original_remember(
            self,
            "listing",
            value,
            value.listing_sha256,
            provider_origin=positive,
        )
        if positive:
            mark_origin(self, "listing", value, value.listing_sha256)
        return value

    def guarded_require_listing(self, value, snapshot) -> bool:
        if type(value) is not listing_type:
            raise error_type("listing is not canonical")
        provider_origin = guarded_require_issued(
            self,
            "listing",
            value,
            value.listing_sha256,
        )
        if value.session_context_id != self._identity.session_context_id:
            raise error_type("listing belongs to another context")
        if value.entitlement_snapshot_sha256 != snapshot.snapshot_sha256:
            raise error_type("listing binds another snapshot")
        return provider_origin

    def guarded_download_file(self, snapshot, listing, provider_path):
        snapshot_origin = guarded_require_snapshot(self, snapshot)
        listing_origin = guarded_require_listing(self, listing, snapshot)
        path = canonical_path(provider_path)
        if path not in listing.provider_paths:
            raise error_type(
                "requested provider path was not returned by this exact listing"
            )
        payload, provider_origin = get_provider_file(self, path)
        if (
            len(payload) < 4
            or payload[:3] != b"BZh"
            or payload[3:4] not in b"123456789"
        ):
            raise error_type(
                "DownloadFile response is not a canonical bzip2 historical payload"
            )
        value = downloaded_type(
            self._identity.session_context_id,
            snapshot.snapshot_sha256,
            listing.listing_sha256,
            path,
            product_now(),
            digest(payload).hexdigest(),
            len(payload),
        )
        positive = bool(snapshot_origin and listing_origin and provider_origin)
        original_remember(
            self,
            "download",
            value,
            value.file_identity_sha256,
            provider_origin=positive,
        )
        if positive:
            mark_origin(self, "download", value, value.file_identity_sha256)
        return value, payload

    def guarded_post(self, operation: str, body: bytes) -> tuple[bytes, bool]:
        # Compatibility seam for callers that used the old private helper. A
        # boolean may be observed, but hidden issuance still cannot be minted
        # without one of the exact guarded acquisition methods above.
        return post_provider(self, operation, body)

    def guarded_session_token(self) -> str:
        return session_token(self)

    def guarded_now(self) -> str:
        guarded_context(self)
        return product_now()

    def guarded_issue_witness(self, evidence, raw_bytes):
        guarded_context(self)
        if type(evidence) is not downloaded_type:
            raise error_type("download evidence is not canonical")
        if not guarded_require_issued(
            self,
            "download",
            evidence,
            evidence.file_identity_sha256,
        ):
            raise error_type(
                "download did not traverse the canonical Historical Data "
                "provider-origin transport chain"
            )
        if not isinstance(raw_bytes, bytes) or (
            len(raw_bytes) != evidence.byte_length
            or digest(raw_bytes).hexdigest() != evidence.raw_sha256
        ):
            raise error_type("download bytes do not match exact captured file evidence")
        if not transport_is_canonical(self._transport):
            raise error_type(
                "historical transport executable origin is no longer canonical"
            )
        witness = original_issue_witness(self, evidence, raw_bytes)
        witnesses[witness] = (witness._authority_fingerprint(), self, evidence)
        return witness

    def guarded_assert_witness(self) -> None:
        record = witnesses.get(self)
        if record is None:
            raise error_type(
                "historical provider-origin witness was not issued by the canonical client"
            )
        fingerprint, client, evidence = record
        if fingerprint != self._authority_fingerprint():
            raise error_type("historical provider-origin witness changed after issuance")
        guarded_context(client)
        if not guarded_require_issued(
            client,
            "download",
            evidence,
            evidence.file_identity_sha256,
        ):
            raise error_type("download chain no longer carries provider-origin capability")
        if not transport_is_canonical(client._transport):
            raise error_type(
                "historical transport executable origin changed after acquisition"
            )
        original_assert_witness(self)

    for function in (
        guarded_get_entitlement_snapshot,
        guarded_list_files,
        guarded_download_file,
        guarded_issue_witness,
    ):
        function._autosport_historical_io_snapshot_sealed = True

    client_type.__init__ = guarded_init
    client_type.get_entitlement_snapshot = guarded_get_entitlement_snapshot
    client_type.list_files = guarded_list_files
    client_type.download_file = guarded_download_file
    client_type._post = guarded_post
    client_type._session_token = guarded_session_token
    client_type._now = guarded_now
    client_type._require_context = guarded_context
    client_type._require_issued = guarded_require_issued
    client_type._require_snapshot = guarded_require_snapshot
    client_type._require_listing = guarded_require_listing
    client_type.issue_provider_origin_witness = guarded_issue_witness
    witness_type.assert_authoritative = guarded_assert_witness


_install_guard()
del _install_guard
