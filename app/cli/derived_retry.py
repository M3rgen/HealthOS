"""Explicit, bounded maintenance for failed derived days; no automatic retry policy."""

import argparse
import asyncio
from datetime import date

from app.services.interpretation.derived import RetryReport, check_retry_range, retry_failed_range


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Tarih YYYY-MM-DD biçiminde olmalı.") from exc


async def _run(first: date, last: date, timezone: str, dry_run: bool) -> RetryReport:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.config import get_settings
    from app.db.session import database_lifespan

    async with database_lifespan(
        get_settings(), async_sessionmaker(expire_on_commit=False)
    ) as sessions:
        return await retry_failed_range(sessions, first, last, timezone, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retry failed derived days.")
    parser.add_argument("first", type=_date, help="First day (inclusive, YYYY-MM-DD).")
    parser.add_argument("last", type=_date, help="Last day (inclusive, at most 366 days).")
    parser.add_argument("timezone", help="IANA time zone.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Count candidates only; write nothing."
    )
    args = parser.parse_args(argv)
    try:
        check_retry_range(args.first, args.last, args.timezone)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        report = asyncio.run(_run(args.first, args.last, args.timezone, args.dry_run))
    except Exception as exc:
        # Database exceptions may contain SQL parameters or source values.
        print(f"Bakım tamamlanamadı: {type(exc).__name__}")
        return 1
    prefix = "Salt okunur deneme; " if args.dry_run else ""
    print(
        f"{prefix}aday: {report['candidates']}, işlenen: {report['processed']}, "
        f"başarılı: {report['succeeded']}, başarısız: {report['failed']}, "
        f"kaynağı değişen: {report['stale']}, atlanan: {report['skipped']}"
    )
    return int(report["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
