"""Raw archive of Apple Health data (app/models/apple.py).

Four new tables: ``apple_payloads`` (every distinct Health Auto Export request body, byte for
byte) and ``apple_raw_records`` (one row per record, key and distinct payload), both
append-only, and ``apple_imports`` (per upload or request: status, progress, counts) and
``apple_sources`` (per Health source: device, last import, newest data), bookkeeping.

Only new tables, their indexes and guards: no ALTER of an existing table and no foreign key
onto one, so no lock is taken on anything that exists. No existing row is read or changed.
Frozen SQL, independent of application modules.

This revision belongs to the GitHub-only Apple extension (docs/BRANCHES.md): it follows
``20261107_0025`` of ``main``. When ``main`` gains a later revision, the extension adds a
merge revision whose ``down_revision`` is (this branch's head, main's head); this file's
``down_revision`` is never edited.

downgrade() refuses while either append-only table holds a row (the received Apple data
would be lost); the bookkeeping tables are dropped with them.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261107_a001"
down_revision = "20261107_0025"
branch_labels = None
depends_on = None

PAYLOADS = "apple_payloads"
RECORDS = "apple_raw_records"
IMPORTS = "apple_imports"
SOURCES = "apple_sources"
GUARDED = (PAYLOADS, RECORDS)
POSTGRESQL_DDL = []
SQLITE_DDL = []
for _table in GUARDED:
    POSTGRESQL_DDL += [
        f"CREATE TRIGGER {_table}_append_only\nBEFORE UPDATE OR DELETE ON {_table} "
        "FOR EACH ROW\nEXECUTE FUNCTION healthos_refuse_change()",
        f"CREATE TRIGGER {_table}_no_truncate\nBEFORE TRUNCATE ON {_table} "
        "FOR EACH STATEMENT\nEXECUTE FUNCTION healthos_refuse_change()",
    ]
    SQLITE_DDL += [
        f"CREATE TRIGGER {_table}_no_{operation.lower()}\nBEFORE {operation} ON {_table}\n"
        f"BEGIN\n    SELECT RAISE(ABORT, '{_table} keeps every record: {operation} refused');"
        "\nEND"
        for operation in ("UPDATE", "DELETE")
    ]
POSTGRESQL_DDL += [
    f"ALTER TABLE {_table} ENABLE ALWAYS TRIGGER {_table}_{suffix}"
    for _table in GUARDED
    for suffix in ("append_only", "no_truncate")
]


def _id() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        PAYLOADS,
        sa.Column("id", _id(), autoincrement=True, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("headers", sa.JSON(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("body_size", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{PAYLOADS}")),
        sa.UniqueConstraint("body_sha256", name=op.f(f"uq_{PAYLOADS}_body_sha256")),
    )
    op.create_table(
        IMPORTS,
        sa.Column("id", _id(), autoincrement=True, nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bytes_read", sa.BigInteger(), nullable=False),
        sa.Column("records_read", sa.BigInteger(), nullable=False),
        sa.Column("records_new", sa.BigInteger(), nullable=False),
        sa.Column("rows_written", sa.BigInteger(), nullable=False),
        sa.Column("export_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{IMPORTS}")),
    )
    op.create_index("ix_apple_imports_started", IMPORTS, ["started_at"])
    op.create_table(
        RECORDS,
        sa.Column("id", _id(), autoincrement=True, nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("type", sa.String(128), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("body", _json(), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=True),
        sa.Column("device", sa.String(255), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload_id", _id(), nullable=True),
        sa.Column("import_id", _id(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["payload_id"], [f"{PAYLOADS}.id"], name=op.f(f"fk_{RECORDS}_payload_id_{PAYLOADS}")
        ),
        sa.ForeignKeyConstraint(
            ["import_id"], [f"{IMPORTS}.id"], name=op.f(f"fk_{RECORDS}_import_id_{IMPORTS}")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{RECORDS}")),
        sa.UniqueConstraint("channel", "kind", "key", "body_sha256", name=f"uq_{RECORDS}_version"),
    )
    op.create_index(f"ix_{RECORDS}_key", RECORDS, ["channel", "kind", "key"])
    op.create_index(f"ix_{RECORDS}_type_start", RECORDS, ["type", "start_time"])
    op.create_table(
        SOURCES,
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("device", sa.String(255), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_data_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_types", sa.JSON(), nullable=False),
        sa.Column("channels", sa.JSON(), nullable=False),
        sa.Column("records", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("source_name", name=op.f(f"pk_{SOURCES}")),
    )
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Apple raw tables must be checked before an online downgrade")
    for table in GUARDED:
        if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}")):
            raise RuntimeError(f"{table} holds evidence; downgrade refused")
    op.drop_table(SOURCES)
    op.drop_table(RECORDS)
    op.drop_table(IMPORTS)
    op.drop_table(PAYLOADS)
