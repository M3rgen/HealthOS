"""Resolve retired placeholder stamps without shadowing installed real revisions."""

from alembic.runtime.migration import MigrationContext
from sqlalchemy import Column, MetaData, String, Table


def resolve_legacy_placeholders(context: MigrationContext) -> None:
    """Translate only unknown stored heads explicitly claimed by a placeholder.

    Run inside Alembic's transaction, before it plans the migration. No application
    table is touched. A real revision always takes precedence over a legacy claim.
    Unknown stamps without a claim retain Alembic's usual failure.
    """
    if context.as_sql or context.script is None or context.connection is None:
        return
    heads = set(context.get_current_heads())
    revisions = list(context.script.walk_revisions())
    installed = {revision.revision for revision in revisions}
    version = Table(
        context.version_table,
        MetaData(),
        Column("version_num", String(32)),
        schema=context.version_table_schema,
    )
    for revision in revisions:
        legacy = getattr(revision.module, "legacy_revision", None)
        if legacy not in heads or legacy in installed:
            continue
        if revision.revision in heads:
            raise RuntimeError(
                "Both legacy and replacement revision stamps exist; refusing to merge"
            )
        context.connection.execute(
            version.update()
            .where(version.c.version_num == legacy)
            .values(version_num=revision.revision)
        )
        heads.remove(legacy)
        heads.add(revision.revision)
