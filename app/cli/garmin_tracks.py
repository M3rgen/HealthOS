"""Resume track derivation from local archives; output contains counts only."""

import argparse
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.db.session import database_lifespan
from app.services.providers.garmin_tracks import rebuild_tracks


async def rebuild() -> int:
    async with database_lifespan(
        get_settings(), async_sessionmaker(expire_on_commit=False)
    ) as sessions:
        return await rebuild_tracks(sessions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Yerel FIT izlerini yeniden türet / Rebuild local FIT tracks."
    )
    parser.add_argument("--rebuild", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        count = asyncio.run(rebuild())
    except Exception:
        # Even exception text/SQL parameters can contain a route. Exit status signals failure.
        print("0")
        return 1
    print(count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
