import ast
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("initial_umask", [0o022, 0o027])
def test_umask_is_restored_between_tests(pytester, initial_umask):
    source = Path(__file__).with_name("conftest.py").read_text()
    fixture = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "preserved_umask"
    )
    pytester.makeconftest(
        "import os\nimport pytest\nfrom collections.abc import Iterator\n"
        f"os.umask({initial_umask})\n" + ast.unparse(fixture)
    )
    pytester.makepyfile(
        f"""
        import os

        def test_change_umask():
            assert os.umask(0o077) == {initial_umask}

        def test_initial_umask_is_preserved():
            assert os.umask(0o022) == {initial_umask}
        """
    )
    result = pytester.runpytest_subprocess("-q", "-p", "no:xdist", "-p", "no:randomly")
    result.assert_outcomes(passed=2)
