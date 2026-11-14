"""Which JSON number tokens the real Garmin pull can archive, on SQLite and on jsonb.

The JSON-text readers (garmin_heart_values, garmin_daily_summary, ...) decode archived
integers with Python's int, which refuses more than 4300 digits. jsonb keeps numbers as
numeric and prints an exponent token in full (1e400 as a 401-digit integer), so the
readers are safe only while the archive cannot hold a number beyond float range or an
integer beyond that limit. This pins that boundary at the HTTP decoder and the writer.
"""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from test_garmin_pull import STEPS_4_DAYS, TODAY, Clock, service
from test_garmin_pull import fake as fake
from test_garmin_pull import key_file as key_file

from app.models.garmin import GarminApiResponse, GarminRawRecord
from app.services.providers.garmin_daily_summary import daily_summaries

FIRST = TODAY - timedelta(days=3)
KEPT = b'{"calendarDate":"%s","totalSteps":10,"totalDistance":1.7976931348623157e308}' % (
    str(TODAY).encode()
)


async def pull_steps(sessions, fake, key_file, raw: bytes):
    def respond(request):
        if request.url.path == STEPS_4_DAYS:
            return httpx.Response(200, content=raw)
        return fake(request)

    fake.transport = lambda: httpx.MockTransport(respond)
    pull = service(sessions, fake, key_file, Clock())
    try:
        return (await pull.run(["steps"])).kinds["steps"]
    finally:
        await pull.aclose()


@pytest.mark.parametrize("token", ["1e400", "-1e10000", "-1e400"])
async def test_numbers_beyond_float_range_never_reach_the_archive(sessions, fake, key_file, token):
    leaf = b'{"calendarDate":"%s","totalSteps":%s}' % (str(FIRST).encode(), token.encode())
    raw = b"[" + leaf + b"," + KEPT + b"]"
    entry = await pull_steps(sessions, fake, key_file, raw)
    assert (entry.error, entry.new_records, entry.unstorable) == (None, 1, 1)
    async with sessions() as session:
        # Every archived body still decodes through the ORM and the JSON-text reader;
        # jsonb prints the largest finite float as a 309-digit integer.
        bodies = (await session.scalars(select(GarminRawRecord.body))).all()
        assert [body["calendarDate"] for body in bodies] == [str(TODAY)]
        assert float(bodies[0]["totalDistance"]) == 1.7976931348623157e308
        days = await daily_summaries(session, FIRST, TODAY)
        assert [(day["date"], day["steps"]) for day in days] == [(str(TODAY), 10)]
        responses = (await session.scalars(select(GarminApiResponse.body))).all()
        assert raw in responses  # The refused token stays only in the HTTP evidence.


async def test_an_integer_beyond_the_decoder_limit_refuses_the_whole_response(
    sessions, fake, key_file
):
    huge = b"1" + b"0" * 4300
    raw = b'[{"calendarDate":"%s","totalSteps":%s},%s]' % (str(FIRST).encode(), huge, KEPT)
    entry = await pull_steps(sessions, fake, key_file, raw)
    assert entry.error == "Garmin response could not be decoded as JSON"
    assert entry.new_records == 0
    async with sessions() as session:
        assert (await session.scalars(select(GarminRawRecord.id))).all() == []
        assert raw in (await session.scalars(select(GarminApiResponse.body))).all()
