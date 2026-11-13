"""scripts/release_gate.py: the tag, version, ancestry and CI checks behind release.yml."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from support.release_tags import RELEASE_TAGS

from scripts import release_gate as gate

COMMIT = "a" * 40
OTHER = "b" * 40
PYPROJECT = '[project]\nname = "healthos"\nversion = "1.2.3"\n'


@pytest.mark.parametrize("tag, accepted", RELEASE_TAGS)
def test_release_tag_format(tag, accepted):
    if accepted:
        gate.check_tag(tag, PYPROJECT.replace("1.2.3", tag[1:]))
    else:
        with pytest.raises(gate.GateError, match="interim, N ≥ 1"):
            gate.check_tag(tag, PYPROJECT)


@pytest.mark.parametrize("tag", ["v1.2.4", "v1.2.3.4"])
def test_mismatched_tag_is_refused(tag):
    with pytest.raises(gate.GateError, match="does not match project version"):
        gate.check_tag(tag, PYPROJECT)


def _git_result(returncode, stderr=""):
    return subprocess.CompletedProcess([], returncode, "", stderr)


def test_commit_must_be_on_main():
    gate.check_on_main(COMMIT, lambda *_: _git_result(0))
    with pytest.raises(gate.GateError, match="not on main"):
        gate.check_on_main(COMMIT, lambda *_: _git_result(1, "fatal"))


def run(status="completed", conclusion="success", sha=COMMIT):
    return {
        "status": status,
        "conclusion": conclusion,
        "head_sha": sha,
        "html_url": "https://example.invalid/run/1",
        "url": "https://example.invalid/api/run/1",
    }


def fake_github(runs, jobs=None):
    jobs = [{"name": "summary", "conclusion": "success"}] if jobs is None else jobs

    def fetch(url, token):
        assert token == "synthetic-token"
        if url.endswith("/jobs?per_page=100"):
            return {"jobs": jobs}
        assert "head_sha=" + COMMIT in url
        assert "event=push" in url and "branch=main" in url
        assert "/actions/workflows/ci.yml/runs" in url
        return {"workflow_runs": runs}

    return fetch


def require(runs, jobs=None, now=lambda: 0.0, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return gate.require_green_ci(
        api="https://example.invalid",
        repository="owner/repo",
        commit=COMMIT,
        token="synthetic-token",
        fetch=fake_github(runs, jobs),
        now=now,
        sleep=sleeps.append,
    )


def test_green_ci_passes_only_with_a_successful_summary_job():
    assert require([run()]) == "https://example.invalid/run/1"
    # A successful workflow whose summary job did not run or failed is not a green release.
    for jobs in (
        [],
        [{"name": "summary", "conclusion": "skipped"}],
        [{"name": "verify", "conclusion": "success"}],
    ):
        with pytest.raises(gate.GateError, match="did not succeed"):
            require([run()], jobs=jobs)


def test_failed_cancelled_or_foreign_runs_are_refused():
    for conclusion in ("failure", "cancelled", "timed_out", "skipped"):
        with pytest.raises(gate.GateError, match="did not succeed"):
            require([run(conclusion=conclusion)])
    # A run for another commit must not satisfy this commit's gate even if the API returns it.
    clock = iter([0.0, 1e9])
    with pytest.raises(gate.GateError, match="no successful CI run"):
        require([run(sha=OTHER)], now=lambda: next(clock))


def test_pending_ci_is_awaited_and_never_passes_on_deadline():
    sleeps = []
    clock = iter([0.0, 1.0, 1e9])
    with pytest.raises(gate.GateError, match="no successful CI run"):
        require(
            [run(status="in_progress", conclusion=None)], now=lambda: next(clock), sleeps=sleeps
        )
    assert sleeps == [gate.POLL_SECONDS]


def test_pending_then_green_passes():
    sequence = iter([[run(status="in_progress", conclusion=None)], [run()]])
    jobs = [{"name": "summary", "conclusion": "success"}]

    def fetch(url, token):
        if url.endswith("/jobs?per_page=100"):
            return {"jobs": jobs}
        return {"workflow_runs": next(sequence)}

    url = gate.require_green_ci(
        api="https://example.invalid",
        repository="owner/repo",
        commit=COMMIT,
        token="synthetic-token",
        fetch=fetch,
        now=lambda: 0.0,
        sleep=lambda _: None,
    )
    assert url.endswith("/run/1")


def test_no_run_yet_does_not_pass():
    clock = iter([0.0, 1.0, 1e9])
    with pytest.raises(gate.GateError, match="no successful CI run"):
        require([], now=lambda: next(clock))


def test_fetch_json_over_real_http(local_sockets):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            seen["auth"] = self.headers.get("Authorization")
            seen["path"] = self.path
            body = json.dumps({"workflow_runs": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/runs"
        assert gate.fetch_json(url, "synthetic-token") == {"workflow_runs": []}
    finally:
        server.shutdown()
        server.server_close()
    assert seen == {"auth": "Bearer synthetic-token", "path": "/runs"}


def test_main_preflight_reports_refusal(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    monkeypatch.chdir(tmp_path)
    assert gate.main(["preflight", "--tag", "v9.9.9", "--commit", COMMIT]) == 1
    assert "does not match project version" in capsys.readouterr().err


def test_main_ci_refuses_when_no_run_exists(monkeypatch, capsys):
    monkeypatch.setenv("GH_REPO", "owner/repo")
    monkeypatch.setenv("GH_TOKEN", "synthetic-token")
    monkeypatch.setenv("RELEASE_CI_WAIT_SECONDS", "0")
    monkeypatch.setattr(gate, "fetch_json", lambda url, token: {"workflow_runs": []})
    assert gate.main(["ci", "--commit", COMMIT]) == 1
    assert "no successful CI run" in capsys.readouterr().err


def test_green_ci_run_exposes_the_run_id_for_the_smoke_manifest_download(
    monkeypatch, tmp_path, capsys
):
    jobs = [{"name": "summary", "conclusion": "success"}]

    def fetch(url, token):
        if url.endswith("/jobs?per_page=100"):
            return {"jobs": jobs}
        return {"workflow_runs": [run() | {"id": 987654}]}

    monkeypatch.setattr(gate, "fetch_json", fetch)
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GH_REPO", "owner/repo")
    assert gate.main(["ci", "--commit", COMMIT]) == 0
    assert output.read_text() == "run_id=987654\n"
