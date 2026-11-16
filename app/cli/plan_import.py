"""Validate a private JSON plan and optionally append it to the local database."""

import argparse
import asyncio
import sys
from pathlib import Path

from app.services.operations.plan_validation import (
    MAX_DOCUMENT_BYTES,
    InvalidPlan,
    load_document,
    section_counts,
)


async def store(document: dict) -> tuple[int, bool, bool]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.config import get_settings
    from app.db.session import database_lifespan
    from app.services.operations.plans import active_version, import_document, log_import

    async with database_lifespan(
        get_settings(), async_sessionmaker(expire_on_commit=False)
    ) as sessions:
        async with sessions.begin() as session:
            version, added = await import_document(session, document)
            active = await active_version(session)
        log_import(version, added)
        # Existing content is never re-added, so an older version stays inactive.
        return version.id, added, active is not None and active.id == version.id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate/import a private plan JSON document.")
    parser.add_argument("file", type=Path)
    parser.add_argument(
        "--check", action="store_true", help="Validate without opening the database."
    )
    args = parser.parse_args(argv)
    try:
        with args.file.open("rb") as source:
            document = load_document(source.read(MAX_DOCUMENT_BYTES + 1))
    except InvalidPlan as exc:
        print(f"Geçersiz plan / Invalid plan: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Plan dosyası okunamadı / Cannot read plan file", file=sys.stderr)
        return 2
    counts = " ".join(f"{key}={value}" for key, value in section_counts(document).items())
    if args.check:
        print(f"Geçerli / Valid: {counts}")
        return 0
    try:
        version, added, active = asyncio.run(store(document))
    except Exception as exc:
        print(f"İçe aktarılamadı / Import failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"Sürüm / Version {version}: added={int(added)} active={int(active)} {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
