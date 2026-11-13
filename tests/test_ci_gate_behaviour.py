"""What the release and CI gates do when their tool says no.

tests/test_ci_gates.py reads the workflow files. Here the steps run: each ``run`` block goes
through bash exactly as GitHub runs it (``-eo pipefail``), and only what cannot run on a developer's
machine is replaced. ``release_gate.py``, ``codeql_gate.py`` and ``compare_images.py`` are the real
scripts. A step that swallows its tool's failure passes the file checks and fails here.
"""

from __future__ import annotations

import functools
import json
import os
import stat
import subprocess
import sys
import tarfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import pytest
import yaml

from scripts import verify_compose as verifier

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
FAIL = "#!/bin/sh\nexit 3\n"
SUCCEED = "#!/bin/sh\nexit 0\n"
# The release exists or was just created; only the asset upload fails.
UPLOAD_FAILS = '#!/bin/sh\n[ "$2" = upload ] && exit 3\nexit 0\n'
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def step_of(workflow: str, job: str, fragment: str) -> dict:
    document = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    matches = [s for s in document["jobs"][job]["steps"] if fragment in s.get("run", "")]
    assert len(matches) == 1, f"{workflow} {job}: {fragment!r} matched {len(matches)} steps"
    return matches[0]


def tool_directory(tmp_path: Path, tools: dict[str, str]) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    for name, body in tools.items():
        path = directory / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return directory


def real_python(tmp_path: Path) -> None:
    """``python3`` in a step is this interpreter, without its virtual environment's site."""
    tool_directory(tmp_path, {"python3": f'#!/bin/sh\nexec "{sys.executable}" -I "$@"\n'})


def run_step(
    step: dict,
    tmp_path: Path,
    tools: dict[str, str] | None = None,
    cwd: Path = ROOT,
    **env: str,
) -> subprocess.CompletedProcess:
    """Run a ``run`` block in ``cwd``, by default the repository (the step's working directory)."""
    directory = tool_directory(tmp_path, tools or {})
    script = tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=cwd,
        env={"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path), **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def github_api(local_sockets) -> Iterator[dict]:
    """A local stand-in for the GitHub Actions API: ``state["conclusion"]`` is the CI verdict."""
    state = {"conclusion": "failure"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            base = f"http://127.0.0.1:{self.server.server_address[1]}/run/7"
            if "/jobs" in self.path:
                body = {"jobs": [{"name": "summary", "conclusion": state["conclusion"]}]}
            else:
                body = {
                    "workflow_runs": [
                        {
                            "id": 7,
                            "head_sha": COMMIT,
                            "status": "completed",
                            "conclusion": state["conclusion"],
                            "url": base,
                            "html_url": base,
                        }
                    ]
                }
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_arguments) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def ci_gate_step(tmp_path: Path, api: dict) -> tuple[subprocess.CompletedProcess, str]:
    real_python(tmp_path)
    output = tmp_path / "github_output"
    output.touch()
    step = step_of("release.yml", "gate", "release_gate.py ci")
    result = run_step(
        step,
        tmp_path,
        GITHUB_API_URL=api["url"],
        GH_REPO="owner/repo",
        GH_TOKEN="fake-token-not-a-secret",
        RELEASE_SHA=COMMIT,
        GITHUB_OUTPUT=str(output),
    )
    return result, output.read_text(encoding="utf-8")


def test_release_stops_when_the_commit_has_no_green_ci(tmp_path, github_api):
    result, output = ci_gate_step(tmp_path, github_api)
    assert result.returncode != 0, result.stdout
    assert "release gate refused" in result.stderr
    assert "run_id" not in output  # build needs this; nothing to download means no release


def test_release_gate_passes_on_green_ci_and_names_the_run(tmp_path, github_api):
    github_api["conclusion"] = "success"
    result, output = ci_gate_step(tmp_path, github_api)
    assert result.returncode == 0, result.stderr
    assert "run_id=7" in output


def test_release_preflight_refuses_a_tag_that_is_not_the_project_version(tmp_path):
    real_python(tmp_path)
    step = step_of("release.yml", "gate", "release_gate.py preflight")
    result = run_step(step, tmp_path, RELEASE_TAG="v999.999.999", RELEASE_SHA=COMMIT)
    assert result.returncode != 0, result.stdout
    assert "does not match project version" in result.stderr


GH_ATTESTATION = """#!/bin/bash
# Behaves like `gh attestation verify` for the two things decided here: every call is recorded,
# and an attestation signed on a self-hosted runner is refused only if the caller asked for that.
echo "$*" >> "$ATTESTATION_CALLS"
if [ "$BUILT_ON" = self-hosted ] && [[ " $* " == *" --deny-self-hosted-runners "* ]]; then
  echo "attestation was built on a self-hosted runner" >&2
  exit 1
fi
exit 0
"""


def verify_provenance(tmp_path: Path, built_on: str) -> tuple[subprocess.CompletedProcess, Path]:
    calls = tmp_path / "calls"
    step = step_of("release.yml", "verify-provenance", "gh attestation verify")
    result = run_step(
        step,
        tmp_path,
        {"gh": GH_ATTESTATION},
        ATTESTATION_CALLS=str(calls),
        BUILT_ON=built_on,
        GH_REPO="owner/repo",
        RELEASE_TAG="v1.2.3",
    )
    return result, calls


def test_a_self_hosted_attestation_stops_the_release(tmp_path):
    result, calls = verify_provenance(tmp_path, "self-hosted")
    assert result.returncode != 0, result.stdout
    assert "self-hosted" in result.stderr
    assert len(calls.read_text().splitlines()) == 1  # the first failed asset ends the step


def test_both_assets_are_verified_against_the_tag_and_this_workflow(tmp_path):
    result, calls = verify_provenance(tmp_path, "github-hosted")
    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    assert ["image" in lines[0], "spdx" in lines[1]] == [True, True]
    for line in lines:
        assert "--signer-workflow owner/repo/.github/workflows/release.yml" in line
        assert "--source-ref refs/tags/v1.2.3" in line


def sarif_directory(tmp_path: Path, severity: str | None) -> Path:
    directory = tmp_path / "sarif"
    directory.mkdir()
    results = [] if severity is None else [{"ruleId": "rule", "level": "warning"}]
    rules = [{"id": "rule", "properties": {"security-severity": severity or "0"}}]
    run = {"tool": {"driver": {"rules": rules}}, "results": results}
    (directory / "x.sarif").write_text(json.dumps({"runs": [run]}), encoding="utf-8")
    return directory


@pytest.mark.parametrize(
    ("severity", "code"),
    [(None, 0), ("6.9", 0), ("7.0", 1), ("7.1", 1), ("9.8", 1)],
)
def test_codeql_step_blocks_from_severity_seven_up(tmp_path, severity, code):
    real_python(tmp_path)
    step = step_of("ci.yml", "codeql", "codeql_gate.py")
    result = run_step(step, tmp_path, SARIF_DIR=str(sarif_directory(tmp_path, severity)))
    assert result.returncode == code, result.stdout + result.stderr


FAILING_RELEASE_STEPS = (
    ("build", "docker build", {"docker": FAIL}),
    ("build", "gh run download", {"gh": FAIL}),
    ("build", "gh run download", {"gh": SUCCEED, "python3": FAIL}),
    ("build", "docker save", {"docker": FAIL}),
    ("publish", "gh release upload", {"gh": FAIL}),
    ("publish", "gh release upload", {"gh": UPLOAD_FAILS}),
)


@pytest.mark.parametrize(("job", "fragment", "tools"), FAILING_RELEASE_STEPS)
def test_a_release_step_fails_when_its_tool_does(tmp_path, job, fragment, tools):
    step = step_of("release.yml", job, fragment)
    environment = {"IMAGE": "healthos:test", "RUNNER_TEMP": str(tmp_path), "RELEASE_TAG": "v1.2.3"}
    assert run_step(step, tmp_path, tools, cwd=tmp_path, **environment).returncode != 0


FAKE_DOCKER = """#!{python}
import sys
if sys.argv[1] == "create":
    print("container")
elif sys.argv[1] == "export":
    sys.stdout.buffer.write(open({archive!r}, "rb").read())
"""


def tar_bytes(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
    return buffer.getvalue()


def fake_image(tmp_path: Path) -> dict[str, str]:
    """A ``docker`` that exports a small fixed tree, and the environment that selects it."""
    archive = tmp_path / "image.tar"
    archive.write_bytes(tar_bytes({"app/main.py": b"print('hello')\n"}))
    docker = FAKE_DOCKER.format(python=sys.executable, archive=str(archive))
    directory = tool_directory(tmp_path, {"docker": docker})
    return {**os.environ, "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}


def test_smoke_records_the_tested_image_for_the_release_to_compare(tmp_path, monkeypatch):
    target = tmp_path / "smoke-image-manifest.json"
    monkeypatch.setenv("HEALTHOS_SMOKE_MANIFEST", str(target))
    env = fake_image(tmp_path)
    verifier.write_smoked_image_manifest(env)
    manifest = json.loads(target.read_text(encoding="utf-8"))
    assert "app/main.py" in manifest["entries"]
    # What release.yml then runs against that file: the same tree passes, another one does not.
    compare = str(ROOT / ".github/scripts/compare_images.py")
    arguments = [sys.executable, compare, "--against-manifest", str(target), "image"]
    assert subprocess.run(arguments, env=env, capture_output=True, check=False).returncode == 0
    (tmp_path / "image.tar").write_bytes(tar_bytes({"app/other.py": b"x"}))
    assert subprocess.run(arguments, env=env, capture_output=True, check=False).returncode == 1


def test_a_manifest_without_its_file_fails_the_smoke_run(tmp_path, monkeypatch):
    """A failing write must not pass silently: release.yml depends on the artifact."""
    monkeypatch.setenv("HEALTHOS_SMOKE_MANIFEST", str(tmp_path / "missing" / "manifest.json"))
    with pytest.raises((subprocess.CalledProcessError, OSError)):
        verifier.write_smoked_image_manifest(fake_image(tmp_path))


def record(steps: list[str], name: str, *_arguments, **_keywords) -> None:
    steps.append(name)


def run_smoke_main(monkeypatch, tmp_path, drills) -> list[str]:
    """``verify_compose.main`` with every Docker step stubbed; returns what ran, in order."""
    steps: list[str] = []
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    monkeypatch.setattr(verifier, "DOCKER_COMMAND", "/usr/bin/docker")
    monkeypatch.setattr(verifier, "BASH", "/usr/bin/bash")
    monkeypatch.setattr(verifier, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(verifier, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(verifier, "SMOKE_ENV_FILE", tmp_path / "config")
    monkeypatch.setattr(verifier, "SMOKE_KEY_FILE", tmp_path / "key")
    monkeypatch.setattr(verifier, "SMOKE_DRILL_DIR", tmp_path / "drills")
    monkeypatch.setattr(verifier, "free_port", lambda: "18080")
    monkeypatch.setattr(verifier, "write_smoke_env_file", lambda: None)
    monkeypatch.setattr(verifier, "remove_run_images", lambda env: None)
    monkeypatch.setattr(
        verifier, "compose", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="")
    )
    monkeypatch.setattr(
        verifier,
        "published_ports",
        lambda service, **k: (
            {"5432/tcp": None}
            if service == "postgres"
            else {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]}
        ),
    )
    for name in (
        "check_checkout_is_shared",
        "verify_started_app",
        "verify_backup_restore",
        "verify_live_restore",
        "verify_backup_failure",
        "verify_migration_lifecycle",
    ):
        monkeypatch.setattr(verifier, name, functools.partial(record, steps, name))
    monkeypatch.setattr(verifier, "run_deployment_drills", drills(steps))
    monkeypatch.setattr(
        verifier, "write_smoked_image_manifest", lambda env: steps.append("manifest")
    )
    verifier.main()
    return steps


def test_the_manifest_is_written_only_after_every_smoke_check_passed(monkeypatch, tmp_path):
    def drills(steps):
        return lambda **_kwargs: steps.append("drills")

    steps = run_smoke_main(monkeypatch, tmp_path, drills)
    assert steps[-2:] == ["drills", "manifest"]


def test_a_failed_smoke_check_writes_no_manifest(monkeypatch, tmp_path):
    def drills(steps):
        def fail(**_kwargs):
            steps.append("drills")
            raise RuntimeError("drill failed")

        return fail

    steps = run_smoke_main(monkeypatch, tmp_path, drills)
    assert "drills" in steps and "manifest" not in steps


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped"])
def test_the_summary_fails_when_the_end_to_end_journey_did_not_pass(tmp_path, outcome):
    job = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))["jobs"]["summary"]
    result = run_step(
        job["steps"][0],
        tmp_path,
        **{**dict.fromkeys(job["steps"][0]["env"], "success"), "E2E": outcome},
        GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md"),
    )
    assert result.returncode == 1, result.stdout
