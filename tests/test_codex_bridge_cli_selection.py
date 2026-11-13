"""The bridge measures and runs one native CLI binary by one absolute path, in the probe's
environment (R1 D1, D2).

A shell shim or npm's codex.js only starts the real binary: hashing it would leave the code
that serves Umay unmeasured. Synthetic files with the host's executable header stand in for
the CLI; nothing is executed.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from scripts import start_codex_bridge as bridge

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executable formats and shims")

with Path(sys.executable).resolve().open("rb") as _interpreter:
    HOST_MAGIC = _interpreter.read(4)


def native(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(HOST_MAGIC + b"\0" * 60)
    path.chmod(0o700)
    return path


def script(path, text='#!/bin/sh\nexec codex "$@"\n'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o700)
    return path


def test_native_executable_accepts_binaries_only(tmp_path):
    assert bridge.native_executable(Path(sys.executable).resolve())
    for name, content in {
        "shim": b"#!/bin/bash\nexec codex\n",
        "node": b"#!/usr/bin/env node\n",
        "pe": b"MZ\x90\x00",
        "short": b"\x7fEL",
        "empty": b"",
    }.items():
        (tmp_path / name).write_bytes(content)
        assert not bridge.native_executable(tmp_path / name), name
    assert not bridge.native_executable(tmp_path / "missing")


def test_shell_shim_first_on_path_is_skipped_for_the_native_binary(tmp_path, monkeypatch):
    shims, real = tmp_path / "shims", tmp_path / "real"
    script(shims / "codex")
    binary = native(tmp_path / "releases" / "0.161.0" / "codex")
    real.mkdir()
    (real / "codex").symlink_to(binary)
    monkeypatch.delenv("CODEX_BRIDGE_CLI", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([str(shims), str(real)]))
    command, executable = bridge.select_cli(tmp_path / "bridge-home")
    assert executable == binary.resolve()
    assert command == [str(binary.resolve())] and Path(command[0]).is_absolute()


def test_scripts_without_a_native_binary_are_refused(tmp_path, monkeypatch):
    shim = script(tmp_path / "shims" / "codex")
    monkeypatch.setenv("PATH", str(shim.parent))
    monkeypatch.delenv("CODEX_BRIDGE_CLI", raising=False)
    with pytest.raises(SystemExit, match="yerel bir ikili değil"):
        bridge.select_cli(tmp_path / "bridge-home")
    monkeypatch.setenv("CODEX_BRIDGE_CLI", str(shim))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(SystemExit, match="CODEX_BRIDGE_CLI ile yerel codex"):
        bridge.select_cli(tmp_path / "bridge-home")


@pytest.mark.skipif(bridge.npm_target() is None, reason="host has no npm target triple")
def test_npm_codex_js_runs_the_host_binary_of_the_package(tmp_path, monkeypatch):
    package = tmp_path / "lib" / "node_modules" / "@openai" / "codex"
    codex_js = script(package / "bin" / "codex.js", "#!/usr/bin/env node\n")
    for target in bridge.NPM_TARGETS.values():
        native(package / "vendor" / target / "codex" / "codex")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "codex").symlink_to(codex_js)
    monkeypatch.delenv("CODEX_BRIDGE_CLI", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    command, executable = bridge.select_cli(tmp_path / "bridge-home")
    expected = (package / "vendor" / bridge.npm_target() / "codex" / "codex").resolve()
    assert executable == expected and command == [str(expected)]


@pytest.fixture
def service_launch(tmp_path, monkeypatch):
    """The service launcher in process up to the app-server start, with a synthetic binary
    whose measurement ``verify_runtime`` records; ``supervise`` captures the launch."""
    binary = native(tmp_path / "cli" / "codex")
    source = tmp_path / "identity"
    state = source / "healthos-bridge"
    state.mkdir(parents=True)
    (source / "auth.json").write_text('{"synthetic": "login"}')
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(source))
    monkeypatch.setenv("CODEX_BRIDGE_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", "synthetic-bridge-token-0123456789-abcdef")
    monkeypatch.setattr(sys, "argv", ["start_codex_bridge.py", "--foreground"])
    monkeypatch.setattr(bridge, "listening", lambda _host: False)
    monkeypatch.setattr(bridge, "select_cli", lambda _home: ([str(binary)], binary))
    launches = []

    def supervise(command, workspace, child_env, ready):
        launches.append((command, workspace, child_env))
        return 0

    def never(*_args, **_kwargs):
        raise AssertionError("the app-server must not start")

    monkeypatch.setattr(bridge, "supervise", supervise)
    monkeypatch.setattr(bridge.subprocess, "Popen", never)

    def run(after_measurement=None):
        def verified(command, executable, bridge_home):
            report = {
                "catalog_text": "{}",
                "runtime": {"cli_sha256": bridge.file_digest(executable)},
            }
            if after_measurement:
                after_measurement(executable)
            return report

        monkeypatch.setattr(bridge, "verify_runtime", verified)
        return bridge.main()

    return run, launches, binary, state


def test_binary_replaced_after_verification_is_not_started(service_launch):
    run, launches, _, _ = service_launch

    def replace(executable):
        executable.write_bytes(HOST_MAGIC + b"\1" * 60)

    with pytest.raises(SystemExit, match="doğrulamadan sonra değişti"):
        run(replace)
    assert launches == []


def test_verified_binary_starts_by_its_absolute_path(local_sockets, service_launch):
    run, launches, binary, state = service_launch
    assert run() == 0
    ((command, workspace, _),) = launches
    # The launcher starts the TLS front; the front starts the measured binary.
    assert Path(command[1]).name == "codex_bridge_front.py"
    inner = json.loads(Path(command[2]).read_text())["command"]
    assert inner[0] == str(binary) and Path(inner[0]).is_absolute()
    assert inner[1:3] == ["app-server", "--listen"] and inner[3].startswith("ws://127.0.0.1:")
    assert workspace == state / "workspace"


def test_launcher_refuses_a_shim_before_measuring_or_copying_the_login(tmp_path, monkeypatch):
    source = tmp_path / "identity"
    (source / "healthos-bridge").mkdir(parents=True)
    (source / "auth.json").write_text('{"synthetic": "login"}')
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(source))
    monkeypatch.setenv("CODEX_BRIDGE_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", "synthetic-bridge-token-0123456789-abcdef")
    monkeypatch.delenv("CODEX_BRIDGE_CLI", raising=False)
    monkeypatch.setenv("PATH", str(script(tmp_path / "shims" / "codex").parent))
    monkeypatch.setattr(sys, "argv", ["start_codex_bridge.py", "--foreground"])
    monkeypatch.setattr(bridge, "listening", lambda _host: False)

    def never(*_args):
        raise AssertionError("a shim must not be measured")

    monkeypatch.setattr(bridge, "verify_runtime", never)
    with pytest.raises(SystemExit, match="yerel bir ikili değil"):
        bridge.main()
    assert sorted(path.name for path in (source / "healthos-bridge").iterdir()) == []


def test_served_runtime_gets_the_probe_allowlist_not_the_operator_environment(
    local_sockets, service_launch, monkeypatch, tmp_path
):
    """R1 D2: what was measured is what serves. Loader, Node and log variables of the
    operator never reach the app-server; only the provider transport is added."""
    from app.services.umay.codex_runtime_rpc import NETWORK_ENV, isolated_env

    leaking = {
        "NODE_OPTIONS": "--require=/tmp/synthetic.js",
        "LD_PRELOAD": "/tmp/synthetic.so",
        "DYLD_INSERT_LIBRARIES": "/tmp/synthetic.dylib",
        "RUST_LOG": "trace",
        "OPENAI_API_KEY": "synthetic",
        "CMUX_SURFACE_ID": "synthetic",
        "CODEX_BRIDGE_TOKEN": "synthetic-bridge-token-0123456789-abcdef",
    }
    for name, value in leaking.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    run, launches, _, state = service_launch
    assert run() == 0
    ((_, _, child_env),) = launches
    assert not set(leaking) & set(child_env)
    assert child_env["CODEX_HOME"] == str(state)
    assert child_env["HOME"] == str(state / "home") and (state / "home").is_dir()
    assert child_env["HTTPS_PROXY"] == "http://127.0.0.1:9"
    probe = isolated_env(tmp_path / "h", tmp_path / "c", tmp_path / "t")
    assert set(child_env) - set(NETWORK_ENV) == set(probe)
    assert not set(leaking) & set(probe)
