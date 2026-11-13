"""The non-superuser application role, on real PostgreSQL: scripts/provision_app_role.py."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from scripts import provision_app_role as provisioner

pytestmark = pytest.mark.integration

# Unique per run: PostgreSQL roles are cluster-wide and the suite runs in parallel workers.
ROLE = f"healthos_app_{uuid.uuid4().hex[:10]}"
PASSWORD = "synthetic-application-role-password-0123456789"


@pytest_asyncio.fixture
async def provisioned(database_url, migrated, pg_sessions):
    """Migrate as the owner, then provision; yields the owner session and an app-role engine."""
    url = make_url(database_url)
    import psycopg

    owner_dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(owner_dsn) as conn:
        provisioner.provision(conn, ROLE, PASSWORD)
    app_url = url.set(username=ROLE, password=PASSWORD).render_as_string(hide_password=False)
    engine = create_async_engine(app_url)
    try:
        yield pg_sessions, engine
    finally:
        await engine.dispose()
        with psycopg.connect(owner_dsn, autocommit=True) as conn:
            conn.execute(f"DROP OWNED BY {ROLE}")
            conn.execute(f"DROP ROLE IF EXISTS {ROLE}")


async def _refused(engine, statement: str) -> str:
    async with engine.connect() as connection:
        with pytest.raises(DBAPIError) as caught:
            await connection.execute(text(statement))
        await connection.rollback()
    return str(caught.value)


@pytest.mark.asyncio
async def test_application_role_is_no_superuser_and_owns_nothing(provisioned):
    _, engine = provisioned
    async with engine.connect() as connection:
        user, superuser = (
            await connection.execute(
                text("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        owned = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                    "WHERE r.rolname = current_user"
                )
            )
        ).scalar_one()
    assert user == ROLE and superuser is False
    assert owned == 0


@pytest.mark.asyncio
async def test_every_append_only_guard_stays_out_of_reach_of_the_application(provisioned):
    _, engine = provisioned
    async with engine.connect() as connection:
        triggers = (
            await connection.execute(
                text(
                    "SELECT c.relname, t.tgname FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE NOT t.tgisinternal AND t.tgenabled = 'A'"
                )
            )
        ).all()
    assert len(triggers) > 30, triggers
    assert any(name.endswith(("_no_truncate", "_no_delete")) for _, name in triggers)
    for table, trigger in triggers:
        message = await _refused(engine, f'ALTER TABLE "{table}" DISABLE TRIGGER "{trigger}"')
        assert "must be owner" in message or "permission denied" in message, message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    [
        "DROP TRIGGER webhook_status_events_append_only ON webhook_status_events",
        "ALTER TABLE raw_health_data DISABLE TRIGGER ALL",
        "ALTER TABLE raw_health_data ALTER COLUMN raw_json DROP NOT NULL",
        "DROP TABLE raw_health_data",
        "TRUNCATE raw_health_data",
        "TRUNCATE manual_measurements",
        "CREATE TABLE should_not_exist (id integer)",
        "CREATE ROLE should_not_exist",
        "UPDATE alembic_version SET version_num = version_num",
        "DELETE FROM alembic_version",
        "COPY manual_measurements TO PROGRAM 'cat > /dev/null'",
        "COPY manual_measurements FROM '/etc/passwd'",
        "SET session_replication_role = replica",
    ],
)
async def test_owner_only_operations_are_denied(provisioned, statement):
    _, engine = provisioned
    message = await _refused(engine, statement)
    assert "denied" in message or "must be owner" in message, message


@pytest.mark.asyncio
async def test_application_cannot_create_databases(provisioned):
    _, engine = provisioned
    autocommit = engine.execution_options(isolation_level="AUTOCOMMIT")
    async with autocommit.connect() as connection:
        with pytest.raises(DBAPIError, match="denied"):
            await connection.execute(text("CREATE DATABASE should_not_exist"))


@pytest.mark.asyncio
async def test_application_keeps_its_data_access_and_append_only_still_holds(provisioned):
    _, engine = provisioned
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO manual_measurements (id, metric, value, measured_at, created_at, "
                "updated_at) VALUES (gen_random_uuid(), 'weight', 70.5, now(), now(), now())"
            )
        )
        version = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    assert version
    # manual_measurements is append-only (migration 0011): the guard stops its own writer.
    for statement in (
        "DELETE FROM manual_measurements",
        "UPDATE manual_measurements SET value = 1",
    ):
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError, match="refus|append|immutable|not allowed"):
                await connection.execute(text(statement))
            await connection.rollback()
    # A guard denies the application like anyone else; no row is disturbed by provisioning.
    async with engine.connect() as connection:
        total = (
            await connection.execute(text("SELECT count(*) FROM manual_measurements"))
        ).scalar_one()
    assert total == 1


@pytest.mark.asyncio
async def test_provisioning_is_repeatable_and_rewrites_no_row(provisioned, database_url):
    sessions, engine = provisioned
    async with sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO manual_measurements (id, metric, value, measured_at, created_at, "
                "updated_at) VALUES (gen_random_uuid(), 'muscle', 41.2, now(), now(), now())"
            )
        )
        before = (
            await session.execute(
                text("SELECT id, value, updated_at FROM manual_measurements ORDER BY id")
            )
        ).all()
    import psycopg

    owner_dsn = (
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    with psycopg.connect(owner_dsn) as conn:
        provisioner.provision(conn, ROLE, PASSWORD + "-rotated")
    async with sessions.begin() as session:
        after = (
            await session.execute(
                text("SELECT id, value, updated_at FROM manual_measurements ORDER BY id")
            )
        ).all()
    assert before == after and len(after) == 1
    # A later table of a later migration reaches the role through the default privileges.
    async with sessions.begin() as session:
        await session.execute(text("CREATE TABLE later_migration (id integer)"))
    # Provisioning again rotated the password; the application connects with the new one.
    rotated = create_async_engine(engine.url.set(password=PASSWORD + "-rotated"))
    try:
        async with rotated.begin() as connection:
            await connection.execute(text("INSERT INTO later_migration VALUES (1)"))
            rows = (
                await connection.execute(text("SELECT count(*) FROM later_migration"))
            ).scalar_one()
    finally:
        await rotated.dispose()
    assert rows == 1
    async with sessions.begin() as session:
        await session.execute(text("DROP TABLE later_migration"))
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        cur = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
            "rolbypassrls, rolpassword IS NOT NULL FROM pg_authid WHERE rolname = %s",
            (ROLE,),
        )
        assert cur.fetchone() == (True, False, False, False, False, False, True)


@pytest.mark.asyncio
async def test_lock_holder_of_another_login_is_described_without_its_start_time(
    provisioned, database_url
):
    # pg_stat_activity hides backend_start from other logins; a refusal message must still work.
    import psycopg

    from app.services.identities.source_chain_locks import BACKFILL_LOCK, _lock_holder

    _, engine = provisioned
    owner_dsn = (
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    with psycopg.connect(owner_dsn, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s, %s)", BACKFILL_LOCK)
        async with engine.connect() as connection:
            message = await _lock_holder(connection)
    assert "session" in message or "another" in message


@pytest.mark.asyncio
async def test_new_install_without_a_role_is_refused_and_an_existing_one_is_left_alone(
    pg_sessions, database_url, monkeypatch, capsys
):
    from datetime import UTC, datetime, timedelta

    import psycopg

    from app.models.state import RawHealthData  # noqa: F401

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delenv("POSTGRES_APP_USER", raising=False)
    monkeypatch.delenv("POSTGRES_APP_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_ALLOW_OWNER_RUNTIME", raising=False)
    # The migrated database of a new install holds no archived record.
    assert provisioner.main() == 2
    assert "new install" in capsys.readouterr().err
    monkeypatch.setenv("POSTGRES_ALLOW_OWNER_RUNTIME", "1")
    assert provisioner.main() == 0
    monkeypatch.delenv("POSTGRES_ALLOW_OWNER_RUNTIME")
    # An archived record means an existing install: it keeps its previous behaviour.
    now = datetime.now(UTC) - timedelta(hours=1)
    async with pg_sessions() as session:
        session.add(
            RawHealthData(
                google_data_point_id="synthetic-existing-install",
                data_type="steps",
                source_start_time=now,
                source_end_time=now,
                raw_json={"value": 1},
                payload_hash="0" * 64,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        await session.commit()
    assert provisioner.main() == 0
    assert "existing install" in capsys.readouterr().out
    dsn = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn) as conn:
        assert provisioner.holds_archive(conn) is True


@pytest.mark.asyncio
async def test_role_risk_detects_superuser_and_owner_and_restricted_role(provisioned, pg_sessions):
    from app.db.role_check import SUPERUSER, database_role_risk

    async with pg_sessions() as session:
        assert await database_role_risk(session) == SUPERUSER
    _, engine = provisioned
    async with engine.connect() as connection:
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(bind=connection) as session:
            assert await database_role_risk(session) is None


@pytest_asyncio.fixture
async def risky_login(database_url, migrated, request):
    """Disposable login, intermediary and table owner, with no real archive involved."""
    import psycopg
    from psycopg import sql

    url = make_url(database_url)
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    login, middle, owner = [f"risk_{uuid.uuid4().hex[:12]}" for _ in range(3)]
    mode = request.param
    flags = mode.upper() if mode in {"createrole", "createdb", "replication", "bypassrls"} else ""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOINHERIT {} PASSWORD {}").format(
                sql.Identifier(login), sql.SQL(flags), sql.Literal(PASSWORD)
            )
        )
        conn.execute(
            sql.SQL("CREATE ROLE {} {} ").format(
                sql.Identifier(owner), sql.SQL("SUPERUSER" if mode == "exploit" else "")
            )
        )
        conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(middle)))
        if mode == "owner":
            owner_of_table = login
        else:
            owner_of_table = owner
        conn.execute(
            sql.SQL("ALTER TABLE raw_health_data OWNER TO {}").format(
                sql.Identifier(owner_of_table)
            )
        )
        target = {"direct": owner, "exploit": owner, "indirect": middle, "harmless": middle}.get(
            mode
        )
        if mode.startswith("pg_"):
            target = mode
        if target:
            conn.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(target), sql.Identifier(login))
            )
        if mode == "indirect":
            conn.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(owner), sql.Identifier(middle))
            )
    engine = create_async_engine(url.set(username=login, password=PASSWORD))
    try:
        yield mode, engine, owner
    finally:
        await engine.dispose()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("ALTER TABLE raw_health_data OWNER TO {}").format(
                    sql.Identifier(url.username)
                )
            )
            for name in (login, middle, owner):
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


@pytest.mark.parametrize(
    "risky_login",
    [
        "direct",
        "indirect",
        "exploit",
        "createrole",
        "createdb",
        "replication",
        "bypassrls",
        "pg_execute_server_program",
        "pg_read_server_files",
        "pg_write_server_files",
        "owner",
        "harmless",
    ],
    indirect=True,
)
async def test_role_risk_detects_privileged_attributes_and_borrowed_roles(risky_login):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.role_check import PRIVILEGED_ROLE, SCHEMA_OWNER, database_role_risk

    mode, engine, owner = risky_login
    expected = SCHEMA_OWNER if mode == "owner" else None if mode == "harmless" else PRIVILEGED_ROLE
    async with AsyncSession(engine) as session:
        assert await database_role_risk(session) == expected
        if mode == "exploit":
            # NOINHERIT does not prevent taking back the owner's ability to disable guards.
            await session.execute(text(f'SET ROLE "{owner}"'))
            await session.execute(text("ALTER TABLE raw_health_data DISABLE TRIGGER ALL"))
            assert (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger "
                        "WHERE tgrelid = 'raw_health_data'::regclass "
                        "AND tgenabled <> 'D'"
                    )
                )
            ).scalar_one() == 0
            await session.rollback()  # Restore every trigger's original ENABLE ALWAYS state.
    if mode == "exploit":
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger "
                        "WHERE tgrelid = 'raw_health_data'::regclass "
                        "AND NOT tgisinternal AND tgenabled = 'A'"
                    )
                )
            ).scalar_one() > 0


@pytest.mark.parametrize("privileged", [False, True])
async def test_role_risk_before_migrations(database_url, privileged):
    import psycopg
    from psycopg import sql
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.role_check import PRIVILEGED_ROLE, database_role_risk

    url = make_url(database_url)
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    login = f"risk_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN {} PASSWORD {}").format(
                sql.Identifier(login),
                sql.SQL("CREATEROLE" if privileged else ""),
                sql.Literal(PASSWORD),
            )
        )
    engine = create_async_engine(url.set(username=login, password=PASSWORD))
    try:
        async with AsyncSession(engine) as session:
            assert await database_role_risk(session) == (PRIVILEGED_ROLE if privileged else None)
    finally:
        await engine.dispose()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))
