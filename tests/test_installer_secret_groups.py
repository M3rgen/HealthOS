"""install.sh as root on Linux: a secret file is shared with the app container's group only when
no other host account can be in that group (security finding S3, CWE-732).

Everything runs in the install.sh sandbox of test_installer_unix: `id`, `getent`, `uname` and
`chgrp` are stubs, so no real group, account, /etc file or secret is read or changed. GID 10001
is the app container's numeric group; on a real host it may belong to an unrelated account.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from test_installer_unix import LINUX_UNAME, REAL, STUB, Sandbox, parse_env
from test_installer_unix import box as box
from test_installer_unix import pytestmark as pytestmark  # bash on a Unix host

# An unrelated host account in the app's numeric group: as a listed member, or as the account's
# primary group (getent group does not list primary members).
UNRELATED = {
    "member": {"GETENT_GROUP": "legacy:x:10001:alice"},
    "primary": {"GETENT_PASSWD": "alice:x:10001:10001::/home/alice:/bin/sh"},
}


def as_root(box: Sandbox, chgrp: str | None = None, **env: str) -> None:
    box.stub("uname", LINUX_UNAME)
    box.stub("chgrp", chgrp or STUB.replace("{name}", "chgrp"))
    box.env.update(FAKE_UID="0", FAKE_USER="root", **env)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def private_key(box: Sandbox) -> Path:
    key = box.home / "garmin.key"
    key.write_text("synthetic-garmin-key\n")
    key.chmod(0o600)
    return key


def assert_not_shared(box: Sandbox, result, path: Path) -> None:
    out = " ".join(result.stdout.split())
    assert mode(path) == 0o600, f"{path} was widened"
    assert not [c for c in box.calls() if c.startswith("chgrp")]
    assert "group 10001" in out and "other accounts" in out
    # The advice must not be the unsafe command itself.
    assert f"sudo chgrp 10001 {path}" not in out


@pytest.mark.parametrize("who", sorted(UNRELATED))
def test_root_keeps_an_existing_garmin_key_private_when_gid_10001_is_shared(box, who):
    as_root(box, **UNRELATED[who])
    key = private_key(box)
    result = box.run(
        *REAL, "--platform", "linux", "--sources", "garmin", "--garmin-key-file", str(key)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert_not_shared(box, result, key.resolve())
    values = parse_env(box.repo / ".env")
    assert values.get("GARMIN_ENABLED", "false") == "false"
    assert "garmin" not in values["HEALTHOS_SOURCES"].split(",")
    assert "GARMIN_API_KEY_HOST_FILE" not in values or not values["GARMIN_API_KEY_HOST_FILE"]
    assert "docker-compose.garmin.yml" not in values["COMPOSE_FILE"]
    assert "Garmin stays off" in result.stdout


@pytest.mark.parametrize("who", sorted(UNRELATED))
def test_root_keeps_an_existing_apple_token_private_when_gid_10001_is_shared(box, who):
    as_root(box, **UNRELATED[who])
    folder = box.home / ".config" / "healthos"
    folder.mkdir(parents=True)
    token = folder / "apple-receiver-token"
    token.write_text("synthetic-apple-token\n")
    token.chmod(0o600)
    result = box.run(*REAL, "--platform", "linux", "--apple-receiver", "yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert_not_shared(box, result, token)
    assert token.read_text() == "synthetic-apple-token\n"  # never rotated
    values = parse_env(box.repo / ".env")
    assert values.get("APPLE_RECEIVER_ENABLED", "false") == "false"
    assert "docker-compose.apple.yml" not in values["COMPOSE_FILE"]


@pytest.mark.parametrize("who", sorted(UNRELATED))
def test_root_creates_a_new_apple_token_private_in_an_existing_folder(box, who):
    as_root(box, **UNRELATED[who])
    folder = box.home / ".config" / "healthos"
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    token = folder / "apple-receiver-token"
    result = box.run(*REAL, "--platform", "linux", "--apple-receiver", "yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert token.is_file()  # made, so the operator can share it later
    assert_not_shared(box, result, token)
    assert mode(folder) == 0o755  # the operator's folder is not changed silently
    assert "Authorization: Bearer" not in result.stdout  # not switched on, nothing to paste
    assert parse_env(box.repo / ".env").get("APPLE_RECEIVER_ENABLED", "false") == "false"


def test_root_dry_run_reports_the_shared_group_and_shares_nothing(box):
    as_root(box, **UNRELATED["member"])
    key = private_key(box)
    result = box.run(
        "--dry-run", "--non-interactive", "--platform", "linux", "--sources", "garmin",
        "--garmin-key-file", str(key), "--apple-receiver", "yes",
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    assert "would give" not in result.stdout
    assert "Garmin stays off" in result.stdout and "Apple receiver stays off" in result.stdout
    assert mode(key) == 0o600
    assert not [c for c in box.calls() if c.startswith("chgrp")]


def test_root_shares_with_a_private_app_group(box):
    # Group 10001 exists but nobody else is in it (root's own primary group is 0).
    as_root(
        box,
        GETENT_GROUP="healthos:x:10001:",
        GETENT_PASSWD="root:x:0:0:root:/root:/bin/sh",
    )
    key = private_key(box)
    result = box.run(
        *REAL, "--platform", "linux", "--sources", "garmin", "--garmin-key-file", str(key),
        "--apple-receiver", "yes",
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    values = parse_env(box.repo / ".env")
    assert "GARMIN_ENABLED" not in values and values["APPLE_RECEIVER_ENABLED"] == "true"
    assert values["HEALTHOS_SOURCES"] == "garmin,apple"
    assert values["GARMIN_API_KEY_HOST_GID"] == values["APPLE_RECEIVER_TOKEN_HOST_GID"] == "10001"
    for path in (key.resolve(), Path(values["APPLE_RECEIVER_TOKEN_HOST_FILE"])):
        assert f"chgrp 10001 {path}" in box.calls()
        assert mode(path) == 0o640


def test_root_uses_the_group_set_in_env(box):
    # An operator-made private group in .env is the one checked and used, not 10001.
    as_root(box)
    key = private_key(box)
    (box.repo / ".env").write_text(
        f"GARMIN_API_KEY_HOST_FILE={key.resolve()}\nGARMIN_API_KEY_HOST_GID=20001\n"
    )
    result = box.run(*REAL, "--platform", "linux", "--sources", "garmin")
    assert result.returncode == 0, result.stdout + result.stderr
    assert parse_env(box.repo / ".env")["GARMIN_API_KEY_HOST_GID"] == "20001"
    assert f"chgrp 20001 {key.resolve()}" in box.calls()
    assert not [c for c in box.calls() if c.startswith("chgrp 10001")]
    assert mode(key) == 0o640


def test_root_failed_chgrp_widens_nothing_and_is_not_reported_as_success(box):
    failing = (
        '#!/bin/sh\necho "chgrp $*" >> "$STUB_LOG"\n'
        'echo "chgrp: Operation not permitted" >&2\nexit 1\n'
    )
    as_root(box, chgrp=failing)
    key = private_key(box)
    result = box.run(
        *REAL, "--platform", "linux", "--sources", "garmin", "--garmin-key-file", str(key)
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert mode(key) == 0o600  # chmod 640 would hand it to the file's old group
    assert "chgrp 10001" in " ".join(box.calls())
    assert "could not" in (result.stdout + result.stderr).lower()
    assert "Configured" not in result.stdout and "HealthOS is running" not in result.stdout


def test_non_root_shared_group_check_is_unchanged(box):
    # The non-root rule: a private, not yet group-readable file in a private group is shared;
    # in a group another account uses it is not (no real file group is needed: file_gid is set).
    key = private_key(box)
    snippet = (
        f"HOST_OS=Linux; IS_ROOT=0; file_gid() {{ echo 4242; }}; "
        f'if secret_blocked "{key}" GARMIN_API_KEY_HOST_GID; then echo blocked; else echo open; fi'
    )
    assert box.source(snippet, FAKE_USER="me").stdout.strip() == "open"
    shared = box.source(snippet, FAKE_USER="me", GETENT_GROUP="team:x:4242:me,other")
    assert shared.stdout.strip() == "blocked"
    key.chmod(0o640)  # already group-readable: nothing would be widened
    assert (
        box.source(snippet, FAKE_USER="me", GETENT_GROUP="team:x:4242:me,other").stdout.strip()
        == "open"
    )
