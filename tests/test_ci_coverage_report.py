"""Exercise the CI report with disjoint synthetic measurements and the real Make recipe."""

import os
import subprocess
import sys
from pathlib import Path

import coverage
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "postgres_lines,expected", [(range(6, 11), 0), (range(6, 10), 2), (None, 1)]
)
def test_combined_report_requires_both_measurements_and_original_floor(
    tmp_path, postgres_lines, expected
):
    source = tmp_path / "app/core/auth.py"
    source.parent.mkdir(parents=True)
    source.write_text("\n".join(f"value_{i} = {i}" for i in range(10)) + "\n")
    config = tmp_path / ".coveragerc"
    config.write_text("[run]\nsource = app\n[report]\nfail_under = 92.6\n")
    saved = tmp_path / ".test-tmp-ci-coverage"
    saved.mkdir()
    for name, lines in (("sqlite", range(1, 6)), ("postgres", postgres_lines)):
        if lines is not None:
            data = coverage.CoverageData(basename=str(saved / f".coverage.{name}"))
            data.add_lines({str(source): lines})
            data.write()
    result = subprocess.run(
        [
            "make",
            "-f",
            str(ROOT / "Makefile"),
            "ci-coverage-report",
            f"UV_RUN={sys.executable} -m",
            "SECURITY_MODULES=app/core/auth.py",
        ],
        cwd=tmp_path,
        env={**os.environ, "COVERAGE_RCFILE": str(config), "COVERAGE_FILE": ".coverage"},
        text=True,
        capture_output=True,
        check=False,
    )
    output = result.stdout + result.stderr
    if expected == 0:
        assert result.returncode == 0, output
        assert "100%" in output
    else:
        assert result.returncode != 0, output
        assert ("less than fail-under=92.6" if expected == 2 else "Couldn't combine") in output


def test_pytest_cov_append_keeps_the_workflows_saved_measurements(tmp_path):
    """pytest-cov consumes root .coverage.* files; saved inputs must survive both PG phases."""
    source = tmp_path / "app/core/auth.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n")
    config = tmp_path / ".coveragerc"
    config.write_text("[run]\nsource = app\nparallel = true\n[report]\nfail_under = 92.6\n")
    data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_lines({str(source): [1]})
    data.write()
    (tmp_path / "test_phase.py").write_text(
        "import runpy\ndef test_phase():\n    runpy.run_path('app/core/auth.py')\n"
    )
    steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["verify"][
        "steps"
    ]
    env = {**os.environ, "COVERAGE_FILE": ".coverage", "COVERAGE_RCFILE": str(config)}

    def run(command):
        result = subprocess.run(
            command, cwd=tmp_path, env=env, text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def save_after(command):
        step = next(step for step in steps if command in step.get("run", ""))
        run(["bash", "-ec", "\n".join(step["run"].splitlines()[1:])])

    save_after("make coverage-security")
    postgres = next(step for step in steps if "make test-postgres" in step.get("run", ""))
    for _ in range(2):
        run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-n",
                "2",
                "test_phase.py",
                *postgres["env"]["PYTEST_ADDOPTS"].split(),
            ]
        )
    save_after("make test-postgres")
    run(
        [
            "make",
            "-f",
            str(ROOT / "Makefile"),
            "ci-coverage-report",
            f"UV_RUN={sys.executable} -m",
            "SECURITY_MODULES=app/core/auth.py",
        ]
    )
