"""Archive verification use case shared by command entry points."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.state import RawHealthData, SyncState


async def verify_archive(session: AsyncSession):
    from app.parsers.common import payload_hash
    from app.services.identities.identity_schema import identity_tables_missing
    from app.services.identities.identity_verification import verify_identities

    from .manual_flags import flag_schema_errors
    from .raw_history_verification import verify_guards, verify_versions
    from .responses import verify_responses

    errors = []
    count = 0
    if await identity_tables_missing(session):
        # An older schema: say so instead of failing on the first missing table.
        _, identity_errors, notes = await verify_identities(session)
        return 0, 0, 0, 0, identity_errors, notes, []
    if old_schema := await flag_schema_errors(session):  # not at 20261024_0014 yet
        return 0, 0, 0, 0, old_schema, [], []
    # First: a missing or disabled guard means deletes could go through unnoticed.
    errors.extend(await verify_guards(session))
    rows = await session.stream(
        select(
            RawHealthData.data_type,
            RawHealthData.google_data_point_id,
            RawHealthData.raw_json,
            RawHealthData.payload_hash,
        ).execution_options(yield_per=1_000)
    )
    async for data_type, google_data_point_id, raw_json, expected_hash in rows:
        count += 1
        if payload_hash(raw_json) != expected_hash:
            errors.append(f"payload hash mismatch: {data_type}/{google_data_point_id}")
    versions, version_errors = await verify_versions(session)
    errors.extend(version_errors)
    responses, response_errors = await verify_responses(session)
    errors.extend(response_errors)
    aliases, identity_errors, notes = await verify_identities(session)
    errors.extend(identity_errors)
    failed = (
        await session.scalars(select(SyncState).where(SyncState.last_error.is_not(None)))
    ).all()
    failed = [{"data_type": row.data_type, "last_error": row.last_error} for row in failed]
    return count, versions, responses, aliases, errors, notes, failed
