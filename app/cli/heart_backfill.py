"""Maintenance entry point for the initial heart fill; never started by the app itself."""

import argparse
import asyncio
import shutil
import signal
from contextlib import suppress

from app.db.contracts import SessionFactory
from app.services.readers.heart_fill import FillProgress
from app.services.readers.heart_maintenance import (
    backfill_heart_hours,
    fill_is_ready,
    fill_space_needed,
)

REPORT_EVERY = 30.0
GRACE = 15.0
INTERRUPTED = 130


async def _space(sessions: SessionFactory) -> tuple[int, int]:
    # Checked on this container's file system: in Compose, the Docker disk that also holds
    # the database volume (PostgreSQL's temporary files).
    return await fill_space_needed(sessions), shutil.disk_usage("/").free


async def _ready(sessions: SessionFactory) -> bool:
    return await fill_is_ready(sessions)


async def _report(progress: FillProgress) -> None:
    while True:
        await asyncio.sleep(REPORT_EVERY)
        print(progress.line(), flush=True)


async def _fill(
    sessions: SessionFactory, limit: int | None, progress: FillProgress, halt: asyncio.Event
) -> int:
    """Run the fill; a signal stops it after the current hour, or cancels it after GRACE.

    Cancellation rolls back only the uncommitted packets; the committed cursor resumes.
    """
    task = asyncio.ensure_future(
        backfill_heart_hours(sessions, limit=limit, progress=progress, halt=halt)
    )
    reporter = asyncio.ensure_future(_report(progress))
    try:
        waiter = asyncio.ensure_future(halt.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if not task.done():
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), GRACE)
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return progress.packets
    finally:
        reporter.cancel()


async def _run(limit: int | None, halt: asyncio.Event) -> tuple[int, bool | None]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.config import get_settings
    from app.db.session import database_lifespan

    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        # A stop request ends the fill cleanly at a committed hour instead of a traceback.
        loop.add_signal_handler(name, halt.set)
    async with database_lifespan(
        get_settings(), async_sessionmaker(expire_on_commit=False)
    ) as sessions:
        need, free = await _space(sessions)
        if halt.is_set():
            return 0, False
        if free < need:
            need_gb, free_gb = need / 1024**3, free / 1024**3
            print(f"Geçici dosyalara yer yok: gereken {need_gb:.1f} GB, boş {free_gb:.1f} GB.")
            return 0, None
        progress = FillProgress()
        written = await _fill(sessions, limit, progress, halt)
        print(progress.line(), flush=True)
        return written, await _ready(sessions)


def _hours(value: str) -> int:
    hours = int(value)
    if hours < 1:
        raise argparse.ArgumentTypeError("en az 1")
    return hours


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fill original Google heart packets during maintenance."
    )
    parser.add_argument("--hours", type=_hours, default=None, metavar="N (default: all)")
    args = parser.parse_args(argv)
    halt = asyncio.Event()
    try:
        written, ready = asyncio.run(_run(args.hours, halt))
    except Exception as exc:
        print(f"İlk dolum tamamlanamadı: {type(exc).__name__}")
        return 1
    if ready is None:
        return 1
    print(f"İşlenen saat: {written}")
    if ready:
        print("İlk dolum tamamlandı.")
    elif halt.is_set():
        print("Durduruldu; aynı komut kaldığı yerden sürer.")
        return INTERRUPTED
    else:
        print("İlk dolum bitmedi; aynı komut kaldığı yerden sürer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
