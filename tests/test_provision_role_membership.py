"""provision-app-role refuses a role that could SET ROLE back to the owner (no PostgreSQL here;
tests/integration covers the real grants)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import provision_app_role as provisioner


class Cursor:
    def __init__(self, borrowed):
        self.borrowed = borrowed
        self.statements: list[str] = []
        self.last = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        self.last = str(statement)
        self.statements.append(self.last)
        self.params = params

    def fetchone(self):
        if "current_database()" in self.last:
            return ("health", "health", "170000")
        if "SELECT 1 FROM pg_roles" in self.last:
            return (1,)
        if "SELECT rolsuper OR" in self.last:
            return (False,)
        return (True,)

    def fetchall(self):
        assert "pg_has_role" in self.last
        assert self.params == {"user": "healthos_app", "owner": "health"}
        return [(name,) for name in self.borrowed]


class Connection:
    def __init__(self, borrowed):
        self.cursor_object = Cursor(borrowed)
        self.committed = False
        self.pgconn = SimpleNamespace(encrypt_password=lambda *_: b"SCRAM-SHA-256$synthetic")

    def cursor(self):
        return self.cursor_object

    def commit(self):
        self.committed = True


@pytest.mark.parametrize(
    "borrowed", [["health"], ["pg_execute_server_program"], ["health", "postgres"]]
)
def test_member_of_the_owner_or_a_privileged_role_is_refused(borrowed):
    connection = Connection(borrowed)
    with pytest.raises(provisioner.ProvisionError) as refused:
        provisioner.provision(connection, "healthos_app", "x" * 64)
    message = str(refused.value)
    assert all(name in message for name in borrowed)
    assert "REVOKE" in message and "SET ROLE" in message
    assert not connection.committed  # the transaction is never committed


def test_a_role_without_borrowed_rights_is_committed():
    connection = Connection([])
    provisioner.provision(connection, "healthos_app", "x" * 64)
    assert connection.committed
    assert any("pg_has_role" in item for item in connection.cursor_object.statements)


def test_provisioning_and_runtime_queries_share_the_policy(monkeypatch):
    import importlib

    from app.core import role_policy
    from app.db import role_check

    # A policy change must reach both consumers, including direct attribute checks.
    with monkeypatch.context() as patch:
        patch.setattr(role_policy, "PRIVILEGED_ATTRIBUTES", ("rolsuper", "rolcanlogin"))
        patch.setattr(role_policy, "PRIVILEGED_BUILTIN_ROLES", ("synthetic_privileged",))
        try:
            importlib.reload(provisioner)
            importlib.reload(role_check)
            assert provisioner.BORROWED_PRIVILEGE == role_policy.borrowed_privilege_sql(
                "%(user)s", "%(owner)s"
            )
            runtime_sql = str(role_check._ROLE_QUERY)
            assert (
                role_policy.borrowed_privilege_sql(
                    "current_user", "pg_catalog.pg_get_userbyid(c.relowner)"
                )
                in runtime_sql
            )
            assert "r.rolcanlogin" in runtime_sql and "r.rolcreaterole" not in runtime_sql
            connection = Connection([])
            provisioner.provision(connection, "healthos_app", "x" * 64)
            assert any(
                "SELECT rolsuper OR rolcanlogin FROM" in statement
                for statement in connection.cursor_object.statements
            )
        finally:
            patch.undo()
            importlib.reload(provisioner)
            importlib.reload(role_check)
