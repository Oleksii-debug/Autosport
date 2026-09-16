from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from .domain import MarketEvent
from .market_mirror import MarketMirror, MirrorApplyResult, MirrorUpdate


MirrorQuoteKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class MirrorInvalidationBatch:
    """Bounded downstream work derived from already-applied durable market updates.

    ``full_refresh_required`` is an explicit fail-safe fence. When it is true,
    ``changed_keys`` is intentionally empty: bounded key tracking saturated, so a
    consumer must refresh from one coherent ``MarketMirror.view()`` rather than
    pretending that an incomplete affected-key list is authoritative.
    """

    changed_keys: tuple[MirrorQuoteKey, ...]
    full_refresh_required: bool
    has_more: bool


class BoundedMirrorInvalidationBuffer:
    """Persist-first Market Mirror subscriber with bounded downstream invalidations.

    Register :meth:`accept_persisted` as a ``MarketEventBus`` subscriber. The bus
    persists first, then calls subscribers, so this bridge never becomes storage
    authority. Every accepted event is applied synchronously to ``MarketMirror``;
    only the downstream invalidation keys are buffered.

    Repeated material updates to one quote coalesce to one dirty key. If more
    distinct keys become dirty than ``max_dirty_keys`` can represent, the buffer
    drops the incomplete key list and raises an explicit full-refresh fence. The
    mirror itself is still current, so no durable market update is lost merely to
    keep downstream recomputation bounded.
    """

    def __init__(
        self,
        mirror: MarketMirror,
        *,
        max_dirty_keys: int = 4096,
    ) -> None:
        if not isinstance(mirror, MarketMirror):
            raise TypeError("mirror must be a MarketMirror")
        if (
            isinstance(max_dirty_keys, bool)
            or not isinstance(max_dirty_keys, int)
            or max_dirty_keys <= 0
        ):
            raise ValueError("max_dirty_keys must be a positive non-boolean integer")
        self._mirror = mirror
        self._max_dirty_keys = max_dirty_keys
        self._dirty: dict[MirrorQuoteKey, None] = {}
        self._full_refresh_required = False
        self._lock = RLock()

    @property
    def mirror(self) -> MarketMirror:
        return self._mirror

    @property
    def max_dirty_keys(self) -> int:
        return self._max_dirty_keys

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._dirty)

    @property
    def full_refresh_required(self) -> bool:
        with self._lock:
            return self._full_refresh_required

    def accept_persisted(self, event: MarketEvent) -> MirrorApplyResult:
        """Apply one already-durable event and record its affected quote if material.

        The tracker lock covers both mirror mutation and invalidation publication so
        a concurrent ``drain`` cannot observe an applied mirror update before its
        downstream invalidation state has been established.
        """
        if not isinstance(event, MarketEvent):
            raise TypeError("event must be a MarketEvent")

        with self._lock:
            result = self._mirror.apply(event)
            if result.status is not MirrorUpdate.APPLIED:
                return result

            if self._full_refresh_required:
                return result

            key = (result.source_id, result.quote_key)
            if key in self._dirty:
                return result

            if len(self._dirty) >= self._max_dirty_keys:
                # Never publish a partial affected-key list as complete truth.
                # The mirror already contains this update, so degrade to one
                # coherent full refresh rather than dropping durable state.
                self._dirty.clear()
                self._full_refresh_required = True
                return result

            self._dirty[key] = None
            return result

    def drain(self, *, max_items: int = 250) -> MirrorInvalidationBatch:
        """Return at most ``max_items`` affected keys, or one full-refresh fence."""
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
            raise ValueError("max_items must be a positive non-boolean integer")

        with self._lock:
            if self._full_refresh_required:
                self._full_refresh_required = False
                self._dirty.clear()
                return MirrorInvalidationBatch(
                    changed_keys=(),
                    full_refresh_required=True,
                    has_more=False,
                )

            count = min(max_items, len(self._dirty))
            keys = tuple(list(self._dirty)[:count])
            for key in keys:
                del self._dirty[key]
            return MirrorInvalidationBatch(
                changed_keys=keys,
                full_refresh_required=False,
                has_more=bool(self._dirty),
            )
