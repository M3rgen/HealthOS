"""Resume original-file retrieval; stdout contains only the number of settled activities."""

import argparse
import asyncio
import sys

from app.cli._context import context
from app.services.providers.garmin_activity_files import FilePullResult


async def _run(limit: int, dry_run: bool) -> FilePullResult:
    # The same runtime and Garmin ownership as `healthos garmin pull`: one lock per kind.
    runtime = context.runtime()
    try:
        if runtime.garmin is None:
            raise ValueError("garmin_disabled")
        return await runtime.garmin.activity_files(limit, dry_run=dry_run)
    finally:
        await runtime.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retrieve original Garmin activity files.")
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    try:
        report = asyncio.run(_run(args.limit, args.dry_run))
    except Exception:
        # Never print exception text, SQL parameters, identities or file names.
        print("0")
        print("garmin_activity_files_failed", file=sys.stderr)
        return 1
    settled = report.downloaded + report.unavailable + report.invalid
    print(report.requests if args.dry_run else settled)
    return int(bool(report.failed or report.stopped))


if __name__ == "__main__":
    raise SystemExit(main())
