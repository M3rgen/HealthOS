"""Read unresolved deletion evidence without depending on the sync runner."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity_tables import IdentityDeferredDeletion as Deferral

from .identity_schema import identity_tables_missing
from .source_chain_repository import load_chains


async def deferred_deletions(session: AsyncSession, data_type: str | None = None) -> list[dict]:
    """The deletions complete fetches deferred and no later one decided: per data type and
    pair of sources, how many rows, since when and when last deferred. A pair that is one
    device now (linked since) is not listed: nothing is left to decide (the next complete
    fetch of the data type resolves its rows)."""
    if await identity_tables_missing(session):
        return []
    query = (
        select(
            Deferral.data_type,
            Deferral.fingerprint,
            Deferral.returned_by,
            func.count(),
            func.min(Deferral.first_deferred_at),
            func.max(Deferral.last_deferred_at),
        )
        .where(Deferral.resolved_at.is_(None))
        .group_by(Deferral.data_type, Deferral.fingerprint, Deferral.returned_by)
    )
    if data_type is not None:
        query = query.where(Deferral.data_type == data_type)
    rows = (await session.execute(query)).tuples().all()
    sources: dict[str, set[str]] = {}
    for name, fingerprint, other, *_ in rows:
        sources.setdefault(name, set()).update((fingerprint, other))
    chains = {name: await load_chains(session, name, names) for name, names in sources.items()}
    found = []
    rows = sorted(rows, key=lambda row: (row[0], -row[3]))
    for name, fingerprint, other, count, since, last in rows:
        if chains[name].same_device(fingerprint, other):
            continue

        def label(source: str, name: str = name) -> str:
            span = chains[name].spans.get(source)
            return span.label if span is not None else source[:12]

        found.append(
            {
                "data_type": name,
                "rows_of": label(fingerprint),
                "returned_by": label(other),
                "fingerprints": [fingerprint[:12], other[:12]],
                "deferred": count,
                "since": since.isoformat() if since else None,
                "at": last.isoformat() if last else None,
                "decide": "identity-decide distinct (two devices: mark them) or not-distinct "
                "and link (one device renamed)",
            }
        )
    return found
