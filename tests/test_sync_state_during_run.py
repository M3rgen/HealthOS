"""What a status reader sees while a sync of a previously failed type is still running."""

from __future__ import annotations

from retrieval_records import Client, Runner

from app.models.state import JobStatus, SyncState


async def test_a_new_run_clears_the_previous_error_before_fetching(sessions):
    async with sessions.begin() as session:
        session.add(SyncState(data_type="heart-rate", status=JobStatus.failed, last_error="old"))
    seen = []

    class Probing(Client):
        async def list_data_points(self, spec, start, end, page_token=None):
            async with sessions() as session:
                state = await session.get(SyncState, "heart-rate")
                seen.append((state.status, state.last_error))
            return await super().list_data_points(spec, start, end, page_token)

    await Runner(sessions, "sync").run(Probing())
    assert seen == [(JobStatus.running, None)]
