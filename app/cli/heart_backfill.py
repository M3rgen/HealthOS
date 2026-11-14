"""Bounded maintenance entry point; never starts a background archive-wide scan."""

import argparse
import asyncio

from app.services.readers.heart_maintenance import backfill_heart_hours


async def _run(limit: int) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.config import get_settings
    from app.db.session import database_lifespan

    async with database_lifespan(
        get_settings(), async_sessionmaker(expire_on_commit=False)
    ) as sessions:
        return await backfill_heart_hours(sessions, limit=limit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fill original Google heart packets during maintenance."
    )
    parser.add_argument("--hours", type=int, default=16, choices=range(1, 169), metavar="1..168")
    args = parser.parse_args(argv)
    try:
        processed = asyncio.run(_run(args.hours))
    except Exception as exc:
        print(f"İlk dolum tamamlanamadı: {type(exc).__name__}")
        return 1
    print(f"İşlenen saat: {processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
