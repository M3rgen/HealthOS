"""Data-shaped files must never reach a public snapshot, even during a dry run."""

import subprocess

import pytest
from fit_factory import activity
from test_branch_scripts import ROOT, Repo
from test_branch_scripts import repo as repo

from scripts import github_snapshot_checks as checks

EXTENSIONS = (
    "fit tcx gpx xml csv tsv zip gz tgz tar 7z sqlite sqlite3 db dump sql gpg age "
    "parquet ndjson jsonl xlsx hdf5 pkl"
).split()
PAYLOAD = b"SYNTHETIC-EXPORT-CONTENT-MUST-NOT-APPEAR"
SIGNATURES = [
    ("fit", b"\x0e\x20\x00\x00\x00\x00\x00\x00.FIT", "txt"),
    ("zip", b"PK\x03\x04", "png"),
    ("gzip", b"\x1f\x8b", "txt"),
    ("sqlite", b"SQLite format 3\0", "txt"),
    ("postgresql", b"PGDMP", "txt"),
    ("openpgp-armor", b"-----BEGIN PGP MESSAGE-----\n", "txt"),
    ("openpgp-new", b"\xc1\x01", "txt"),
    ("openpgp-old", b"\x84\x01", "txt"),
    ("openpgp-password", b"\xc3\x01", "txt"),
    ("openpgp-encrypted", b"\xd2\x01", "txt"),
    ("openpgp-legacy-data", b"\xc9\x01", "txt"),
    ("openpgp-marker", b"\xa8\x03PGP", "txt"),
    ("openpgp-aead", b"\xd4\x01", "txt"),
    ("age", b"age-encryption.org/v1\n", "txt"),
    ("age-armor", b"-----BEGIN AGE ENCRYPTED FILE-----\n", "txt"),
    ("apple-health", b'<?xml version="1.0"?>\n<HealthData', "txt"),
    ("apple-doctype", b'<?xml version="1.0"?>\n<!DOCTYPE HealthData', "txt"),
    ("apple-bom", b'\xef\xbb\xbf<?xml version="1.0"?>\n<HealthData', "txt"),
    ("parquet", b"PAR1", "txt"),
]


def commit_bytes(repo: Repo, path: str, data: bytes) -> None:
    repo.write(path, "")
    (repo.path / path).write_bytes(data)
    repo.git("add", path)
    repo.git("commit", "-q", "-m", "synthetic export fixture")


def assert_blocked(repo: Repo, path: str, size: int, reason: str) -> None:
    before = repo.git("rev-parse", "github-main"), repo.git("write-tree")
    result = repo.run("github_snapshot.sh", "--dry-run", "--allow-references")
    assert result.returncode == 1, result.stdout + result.stderr
    assert path in result.stderr and reason in result.stderr
    assert f"{size} bytes" in result.stderr
    assert PAYLOAD.decode() not in result.stdout + result.stderr
    assert before == (repo.git("rev-parse", "github-main"), repo.git("write-tree"))


@pytest.mark.parametrize("extension", EXTENSIONS)
def test_data_extension_blocks_dry_run(repo: Repo, extension: str) -> None:
    path = f"exports/synthetic.{extension.upper()}"
    commit_bytes(repo, path, PAYLOAD)
    assert_blocked(repo, path, len(PAYLOAD), "extension")


@pytest.mark.parametrize("name,prefix,extension", SIGNATURES, ids=[s[0] for s in SIGNATURES])
def test_hidden_data_signature_blocks_dry_run(
    repo: Repo, name: str, prefix: bytes, extension: str
) -> None:
    path = f"exports/{name}.{extension}"
    commit_bytes(repo, path, prefix + PAYLOAD)
    # A clean worktree and export-ignore must not conceal the committed blob.
    repo.commit("archive attributes", {".gitattributes": f"{path} export-ignore\n"})
    (repo.path / path).write_bytes(b"clean worktree")
    assert_blocked(repo, path, len(prefix + PAYLOAD), "signature")


def test_signature_examples_in_source_text_are_not_data(repo: Repo) -> None:
    repo.commit("synthetic documentation", {"example.txt": 'example = "<HealthData"\n'})
    result = repo.run("github_snapshot.sh", "--dry-run")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("size,blocked", [(99_999, False), (100_000, False), (100_001, True)])
def test_json_size_boundary(repo: Repo, size: int, blocked: bool) -> None:
    path = "exports/synthetic.JSON"
    data = b'{"fixture":"' + PAYLOAD
    data += b"x" * (size - len(data) - 2) + b'"}'
    commit_bytes(repo, path, data)
    if blocked:
        assert_blocked(repo, path, size, "size")
    else:
        result = repo.run("github_snapshot.sh", "--dry-run")
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("opening", ["Özet", "Çıktı", "Ünite", "ölçüm"])
def test_text_starting_with_a_non_ascii_letter_is_not_a_packet(repo: Repo, opening: str) -> None:
    # 0xC3 leads these letters in UTF-8 and also looks like a new-format OpenPGP header.
    commit_bytes(repo, "docs/not.md", f"{opening} notu\n\nDüz metin.\n".encode())
    result = repo.run("github_snapshot.sh", "--dry-run")
    assert result.returncode == 0, result.stderr


def test_allowlisted_fit_requires_exact_digest(repo: Repo) -> None:
    path = "tests/fixtures/synthetic_activity.fit"
    data = activity()
    commit_bytes(repo, path, data)
    result = repo.run("github_snapshot.sh", "--dry-run")
    assert result.returncode == 0, result.stderr
    commit_bytes(repo, path, data[:-1] + bytes([data[-1] ^ 1]))
    assert_blocked(repo, path, len(data), "extension")


def test_fit_allowance_does_not_apply_to_other_paths(repo: Repo) -> None:
    path = "exports/copy.fit"
    commit_bytes(repo, path, activity())
    assert_blocked(repo, path, len(activity()), "extension")


@pytest.mark.parametrize("limit", ["100001", "0", "-1", "invalid"])
def test_json_limit_cannot_weaken_gate(repo: Repo, limit: str) -> None:
    repo.git("config", "--local", "healthos.githubDataJsonLimit", limit)
    before = repo.git("rev-parse", "github-main")
    result = repo.run("github_snapshot.sh", "--dry-run")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "healthos.githubDataJsonLimit" in result.stderr
    assert repo.git("rev-parse", "github-main") == before


def test_local_settings_only_tighten_gate(repo: Repo) -> None:
    repo.git("config", "--local", "healthos.githubDataJsonLimit", "64")
    commit_bytes(repo, "exports/synthetic.json", b" " * 65)
    assert_blocked(repo, "exports/synthetic.json", 65, "size")


@pytest.mark.parametrize("extension", [".BAK", "extra"])
def test_additional_forbidden_extensions_are_repeatable(repo: Repo, extension: str) -> None:
    for value in (".unused", extension):
        repo.git("config", "--local", "--add", "healthos.githubDataForbidExtension", value)
    path = "exports/synthetic." + extension.lstrip(".").lower()
    commit_bytes(repo, path, PAYLOAD)
    assert_blocked(repo, path, len(PAYLOAD), "extension")


@pytest.mark.parametrize("extension", ["", "*", "directory/export", "."])
def test_invalid_additional_extensions_fail_closed(repo: Repo, extension: str) -> None:
    repo.git("config", "--local", "healthos.githubDataForbidExtension", extension)
    result = repo.run("github_snapshot.sh", "--dry-run")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "healthos.githubDataForbidExtension" in result.stderr


@pytest.mark.parametrize("origin", ["extension", "override"])
def test_final_tree_includes_data_from_extensions_and_overrides(repo: Repo, origin: str) -> None:
    path = "exports/merged.csv"
    repo.git("switch", "-q", "eklenti/apple")
    commit_bytes(repo, path, PAYLOAD)
    repo.git("switch", "-q", "main")
    arguments = ("--dry-run",)
    if origin == "override":
        arguments += ("--no-extensions", "--override", f"{path}=eklenti/apple")
    before = repo.git("rev-parse", "github-main")
    result = repo.run("github_snapshot.sh", *arguments)
    assert result.returncode == 1, result.stdout + result.stderr
    assert path in result.stderr and "extension" in result.stderr
    assert PAYLOAD.decode() not in result.stdout + result.stderr
    assert repo.git("rev-parse", "github-main") == before


def test_excluded_data_is_not_in_publication_tree(repo: Repo) -> None:
    repo.git("config", "--local", "--add", "healthos.githubExclude", "exports")
    commit_bytes(repo, "exports/synthetic.csv", PAYLOAD)
    result = repo.run("github_snapshot.sh", "--dry-run")
    assert result.returncode == 0, result.stderr


def test_multiple_findings_and_normal_run_cannot_bypass_gate(repo: Repo) -> None:
    for path in ("exports/one.csv", "exports/two.sql"):
        commit_bytes(repo, path, PAYLOAD)
    before = repo.git("rev-parse", "github-main")
    result = repo.run("github_snapshot.sh", "--allow-references")
    assert result.returncode == 1, result.stdout + result.stderr
    assert all(path in result.stderr for path in ("exports/one.csv", "exports/two.sql"))
    assert PAYLOAD.decode() not in result.stdout + result.stderr
    assert repo.git("rev-parse", "github-main") == before


def test_rejected_data_is_not_printed_by_reference_diagnostics(repo: Repo) -> None:
    path = "exports/reference.csv"
    data = PAYLOAD + b" maintainer-log.md"
    commit_bytes(repo, path, data)
    assert_blocked(repo, path, len(data), "extension")


def test_current_tree_data_gate_without_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    entries = []
    for entry in checks.git("ls-files", "--stage", "-z").split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            entries.append((b"blob", metadata.split()[1], path))
    checks.check_data_files(entries)


def test_current_tree_dry_run_and_package_lock(repo: Repo) -> None:
    # Import immutable objects into the disposable Repo, never change the real clone's refs/config.
    repo.git("config", "healthos.githubExclude", "fixture-unpublished-" + repo.path.name)
    repo.git("config", "--unset-all", "healthos.githubForbidPattern")
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT).strip()
    pack = subprocess.run(
        ["git", "pack-objects", "--stdout", "--revs"],
        input=tree + b"\n",
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "unpack-objects"], input=pack.stdout, cwd=repo.path, env=repo.env, check=True
    )
    source = repo.git("commit-tree", tree.decode(), "-m", "synthetic source tree")
    before = repo.git("rev-parse", "github-main")
    result = repo.run("github_snapshot.sh", "--source", source, "--no-extensions", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert repo.git("rev-parse", "github-main") == before
    assert "frontend/package-lock.json" in repo.files(source)
