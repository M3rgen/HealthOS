"""Record identity helpers shared by the identity tests (synthetic records only)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from identity_records import (
    HEART,
    NEW_NAME,
    PACKAGE,
    SEDENTARY,
    STEPS,
    SWIM,
    T0,
    fitbit,
    heart,
    legacy,
    phone,
    sedentary,
    steps,
    swim,
)

MINUTE = timedelta(minutes=1)


WATCH = "Example Watch"


async def worn_together(sessions, band: str, minutes, seen, watch_last=None) -> None:
    """One page (stored at ``seen``) with the band and a watch, different values."""
    for minute in minutes:
        at = T0 + minute * MINUTE
        await legacy(sessions, HEART, heart(60 + minute % 30, at=at, name=band), seen)
        await legacy(
            sessions, HEART, heart(90 + minute % 30, at=at, name=WATCH), seen, last=watch_last
        )


async def archive_with_duplicates(sessions) -> dict[str, uuid.UUID]:
    """Rows as the pre-v2 archive held them (each pair: before / after the change)."""
    day = timedelta(days=1)
    ids = {
        "renamed-old": await legacy(sessions, HEART, heart(60), T0, deleted=T0 + day),
        "renamed-new": await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + day),
        "lone-deleted": await legacy(
            sessions, HEART, heart(80, at=T0 + timedelta(minutes=9)), T0, deleted=T0 + day
        ),
        # The band (old name) was last seen half a day before the new name first appeared;
        # the watch, another device of another class (recording method), was seen with both.
        "band": await legacy(
            sessions, HEART, heart(70, at=T0 + timedelta(minutes=3)), T0, last=T0 + day / 2
        ),
        "watch": await legacy(
            sessions,
            HEART,
            {
                **heart(72, at=T0 + timedelta(minutes=3)),
                "dataSource": fitbit("Example Watch", method="PASSIVELY_MEASURED"),
            },
            T0 + timedelta(hours=2),
            last=T0 + day,
        ),
        "suffixed": await legacy(sessions, STEPS, steps(40), T0),
        "plain": await legacy(sessions, STEPS, steps(40, source=phone(PACKAGE)), T0 + day),
        "other-steps": await legacy(sessions, STEPS, steps(25, start=T0 + timedelta(hours=1)), T0),
        "swim-flat": await legacy(sessions, SWIM, swim(12), T0),
        "swim-civil": await legacy(sessions, SWIM, swim(12, civil=True), T0 + day),
    }
    for minutes in (15, 30, 45):
        ids[f"sedentary-{minutes}"] = await legacy(
            sessions, SEDENTARY, sedentary(minutes), T0 + timedelta(minutes=minutes)
        )
    return ids
