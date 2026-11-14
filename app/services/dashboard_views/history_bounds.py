"""Bounded process cache of validated archive bounds, fenced by database visibility.

No TTL or shared in-memory generation is used. Every request reads its own database
marker; any committed database mutation conservatively invalidates the entry. This
also covers bulk SQL, archive/identity maintenance, and Garmin publication writers.
Misses use exactly the existing bounded streaming readers and cursor cleanup.
"""

from collections import OrderedDict
from datetime import date
from threading import Lock
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.dialect import database
from app.services import interpretation, providers, readers

type CacheKey = tuple[object, ...]
type Revision = tuple[object, ...]


class BoundsCache:
    """One fixed-size LRU per process, also safe across request threads/event loops.

    Keys include the engine, so a different database cannot borrow a result. Values
    retain their exact visibility marker; no timestamp or hash can alias a revision.
    The lock protects only memory operations, never a database call or an await.
    """

    def __init__(self, capacity: int = 32) -> None:
        self.capacity = capacity
        self.entries: OrderedDict[CacheKey, tuple[Revision, readers.reading.ArchiveBounds]] = (
            OrderedDict()
        )
        self.lock = Lock()

    def get(self, key: CacheKey, revision: Revision) -> readers.reading.ArchiveBounds | None:
        with self.lock:
            cached = self.entries.get(key)
            if cached is None or cached[0] != revision:
                return None
            self.entries.move_to_end(key)
            return cached[1]

    def put(self, key: CacheKey, revision: Revision, bounds: readers.reading.ArchiveBounds) -> None:
        with self.lock:
            self.entries[key] = (revision, bounds)
            self.entries.move_to_end(key)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)


_CACHE = BoundsCache()


async def read_bounds(
    session: AsyncSession,
    metric: readers.HistoryMetric,
    today: date,
    zone: ZoneInfo,
    archives: readers.Archives,
    *,
    use_cache: bool = True,
) -> readers.reading.ArchiveBounds:
    # Custom readers can depend on state outside the database. Pending ORM writes
    # must not reuse a hit before the normal readers' autoflush has made them visible.
    cacheable = (
        use_cache
        and archives is providers.ingestion_readers.ARCHIVES
        and not (session.new or session.dirty or session.deleted)
    )
    revision = await database(session).read_revision(session) if cacheable else None
    key = (session.get_bind(), metric, today, zone.key, interpretation.canonical_daily.METHOD)
    cached = _CACHE.get(key, revision) if revision is not None else None
    if cached is not None:
        return cached
    bounds = [
        await archive.archive_bounds(session, metric, today, zone)
        for archive in archives
        if archive.archive_bounds is not None
    ]
    result = readers.reading.ArchiveBounds(
        min((item.earliest for item in bounds if item.earliest is not None), default=None),
        max((item.latest for item in bounds if item.latest is not None), default=None),
    )
    # A READ COMMITTED miss can span several snapshots. Never cache a mixed read,
    # nor an aborted/failed/cancelled computation. The next request retries it.
    if revision is not None and await database(session).read_revision(session) == revision:
        _CACHE.put(key, revision, result)
    return result
