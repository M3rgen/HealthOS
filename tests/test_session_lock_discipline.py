"""Session advisory locks must keep their physical connection out of the pool."""

import ast
import re
from pathlib import Path
from textwrap import dedent

import pytest

ROOT = Path(__file__).resolve().parents[1]
FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)
SESSION_LOCK = re.compile(r"\bpg_(?:try_)?advisory_lock\s*\(", re.IGNORECASE)


def scope_nodes(node):
    """Do not credit a nested function's cleanup to its enclosing function."""
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (*FUNCTION, ast.ClassDef)):
            yield from scope_nodes(child)


def call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{call_name(node.value)}.{node.attr}"
    return ""


def calls_discard(function):
    return any(
        isinstance(node, ast.Call) and call_name(node.func).split(".")[-1] == "discard_connection"
        for node in scope_nodes(function)
    )


def is_context_manager(function):
    return any(
        call_name(decorator).split(".")[-1] in {"contextmanager", "asynccontextmanager"}
        for decorator in function.decorator_list
    )


def index_sources(sources):
    functions, symbols, owners, parents, constants = {}, {}, {}, {}, {}
    for module, source in sources.items():
        tree = ast.parse(source)
        bindings = symbols[module] = {}
        constants[module] = {
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name) and contains_lock_sql(node.value)
        }
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bindings[alias.asname or alias.name] = alias.name
            elif isinstance(node, (*FUNCTION, ast.ClassDef)):
                bindings[node.name] = f"{module}.{node.name}"
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, FUNCTION):
                continue
            names, parent = [node.name], parents[node]
            while not isinstance(parent, ast.Module):
                if isinstance(parent, (*FUNCTION, ast.ClassDef)):
                    names.insert(0, parent.name)
                parent = parents[parent]
            name = f"{module}.{'.'.join(names)}"
            functions[name] = (module, node)
            if isinstance(parents[node], ast.ClassDef):
                owners[name] = name.rpartition(".")[0]
    return functions, symbols, owners, parents, constants


def resolve(module, expression, symbols):
    name = call_name(expression)
    head, separator, tail = name.partition(".")
    return symbols[module].get(head, f"{module}.{head}") + (separator + tail if tail else "")


def resource_methods(statements, variable, module, functions, symbols, seen):
    """Follow a lock passed to a helper, without treating all class methods as safe."""
    methods = set()
    for statement in statements:
        for node in scope_nodes(statement):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and call_name(node.func.value) == variable:
                methods.add(node.func.attr)
            target = resolve(module, node.func, symbols)
            if target not in functions:
                continue
            helper_module, helper = functions[target]
            arguments = [*helper.args.posonlyargs, *helper.args.args]
            passed = [
                argument.arg
                for value, argument in zip(node.args, arguments, strict=False)
                if call_name(value) == variable
            ] + [keyword.arg for keyword in node.keywords if call_name(keyword.value) == variable]
            for parameter in passed:
                edge = (target, parameter)
                if edge in seen:
                    continue
                seen.add(edge)
                methods.update(
                    resource_methods(
                        helper.body, parameter, helper_module, functions, symbols, seen
                    )
                )
    return methods


def managed_methods(functions, symbols, owners):
    """Recognize split acquire/release lifecycles owned by a context manager.

    The ownership layer constructs a lock through a factory and calls its release
    method in finally. A release method alone is insufficient: its context manager
    must instantiate that class and call that method on the same local variable.
    """
    factories = {}
    for name, (module, function) in functions.items():
        for node in scope_nodes(function):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
                factories[name] = resolve(module, node.value.func, symbols)
    managed = set()
    for module, function in functions.values():
        if not is_context_manager(function):
            continue
        instances = {
            target.id: factories.get(resolve(module, node.value.func, symbols))
            for node in scope_nodes(function)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in scope_nodes(function):
            if not isinstance(node, ast.Try):
                continue
            for statement in node.finalbody:
                for call in scope_nodes(statement):
                    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                        continue
                    owner = instances.get(call_name(call.func.value))
                    method = f"{owner}.{call.func.attr}"
                    if method in owners and calls_discard(functions[method][1]):
                        methods = resource_methods(
                            node.body, call_name(call.func.value), module, functions, symbols, set()
                        )
                        managed.update(f"{owner}.{method}" for method in methods)
    return managed


def contains_lock_sql(expression):
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and SESSION_LOCK.search(node.value)
        for node in ast.walk(expression)
    )


def lock_sites(function, constants):
    # SQL is also stored in module constants (TRY_LOCK / WAIT_LOCK). Resolve those
    # instead of silently checking only inline SQL literals.
    for node in scope_nodes(function):
        if isinstance(node, ast.Name) and node.id in constants:
            yield node
        elif isinstance(node, ast.Constant) and contains_lock_sql(node):
            # Docstrings describe policy; they are not executable SQL.
            if node is not getattr(function.body[0], "value", None):
                yield node


def violations(sources):
    functions, symbols, owners, parents, constants = index_sources(sources)
    managed = managed_methods(functions, symbols, owners)
    failures = []
    for name, (module, function) in functions.items():
        for site in lock_sites(function, constants[module]):
            if calls_discard(function) or name in managed:
                continue
            parent, protected = parents[site], False
            while parent is not function:
                if isinstance(parent, (ast.With, ast.AsyncWith)):
                    for item in parent.items:
                        if not isinstance(item.context_expr, ast.Call):
                            continue
                        target = resolve(module, item.context_expr.func, symbols)
                        if target in functions:
                            manager = functions[target][1]
                            protected |= is_context_manager(manager) and calls_discard(manager)
                parent = parents[parent]
            if not protected:
                failures.append(f"{name}:{site.lineno}: session lock without discard_connection")
    return failures


def test_session_locks_discard_their_connection():
    sources = {
        ".".join(path.relative_to(ROOT).with_suffix("").parts): path.read_text()
        for path in sorted((ROOT / "app").rglob("*.py"))
    }
    assert not (failures := violations(sources)), "\n".join(failures)


@pytest.mark.parametrize(
    "sql, cleanup, rejected",
    [
        ("pg_try_advisory_lock(1)", "connection.close()", True),
        ("pg_advisory_lock(1)", "connection.close()", True),
        ("pg_try_advisory_lock(1)", "discard_connection(connection)", False),
        ("PG_ADVISORY_LOCK (1)", "discard_connection(connection)", False),
        ("pg_advisory_xact_lock(1)", "connection.close()", False),
        ("pg_try_advisory_xact_lock(1)", "connection.close()", False),
    ],
)
def test_guard_distinguishes_session_locks_and_physical_disposal(sql, cleanup, rejected):
    source = f"""
LOCK = text("SELECT {sql}")
async def owner(connection):
    try:
        await connection.execute(LOCK)
    finally:
        await {cleanup}
"""
    assert bool(violations({"app.synthetic": source})) is rejected


@pytest.mark.parametrize("cleanup", ["discard_connection(connection)", "connection.close()"])
def test_guard_follows_imported_context_manager(cleanup):
    sources = {
        "app.manager": dedent(f"""
            @asynccontextmanager
            async def managed():
                connection = await engine.connect()
                try:
                    yield connection
                finally:
                    await {cleanup}
        """),
        "app.caller": dedent("""
            from app.manager import managed as connection_scope
            async def owner():
                async with connection_scope() as connection:
                    await connection.execute(text("SELECT pg_try_advisory_lock(1)"))
        """),
    }
    assert bool(violations(sources)) is (cleanup == "connection.close()")


def test_guard_does_not_credit_nested_cleanup():
    source = """
async def owner(connection):
    await connection.execute(text("SELECT pg_try_advisory_lock(1)"))
    async def unused():
        await discard_connection(connection)
"""
    assert len(violations({"app.synthetic": source})) == 1


@pytest.mark.parametrize("release", ["lock.release()", "lock.close()"])
def test_guard_requires_context_manager_to_release_the_lock_object(release):
    source = f"""
LOCK = text("SELECT pg_advisory_lock(1)")
class Lock:
    async def acquire(self):
        await self.connection.execute(LOCK)
    async def release(self):
        await discard_connection(self.connection)
def factory():
    return Lock()
@asynccontextmanager
async def owned():
    lock = factory()
    try:
        await lock.acquire()
        yield
    finally:
        await {release}
"""
    assert bool(violations({"app.synthetic": source})) is (release == "lock.close()")


@pytest.mark.parametrize("acquire", ["lock.acquire()", "acquire(lock)"])
def test_guard_does_not_exempt_other_methods_of_a_managed_class(acquire):
    source = f"""
class Lock:
    async def acquire(self):
        await self.connection.execute(text("SELECT pg_try_advisory_lock(1)"))
    async def orphan(self, connection):
        await connection.execute(text("SELECT pg_try_advisory_lock(2)"))
        await connection.close()
    async def release(self):
        await discard_connection(self.connection)
def factory():
    return Lock()
async def acquire(lock):
    await lock.acquire()
@asynccontextmanager
async def owned():
    lock = factory()
    try:
        await {acquire}
        yield
    finally:
        await lock.release()
"""
    failures = violations({"app.synthetic": source})
    assert len(failures) == 1
    assert "Lock.orphan:" in failures[0]
