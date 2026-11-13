"""The two helpers CI runs: the image comparison and the CodeQL findings gate."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import marshal
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compare = load("compare_images")
codeql = load("codeql_gate")


def archive(files: dict[str, bytes], mtime: int = 1000) -> dict[str, tuple]:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = mtime
            tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return dict(compare.entries(buffer))


PYC = b"\xcb\r\r\n\x00\x00\x00\x00"  # magic and flags


def test_identical_trees_match_whatever_their_modification_times():
    files = {"app/a.py": b"print(1)\n"}
    assert compare.differences(archive(files, 1), archive(files, 2)) == []


def test_pyc_source_mtime_is_ignored_but_the_code_is_not():
    first = archive({"a.pyc": PYC + b"AAAA" + b"SIZE" + b"code"})
    same_code = archive({"a.pyc": PYC + b"BBBB" + b"SIZE" + b"code"})
    other_code = archive({"a.pyc": PYC + b"BBBB" + b"SIZE" + b"CODE"})
    other_size = archive({"a.pyc": PYC + b"AAAA" + b"size" + b"code"})
    assert compare.differences(first, same_code) == []
    assert compare.differences(first, other_code) == ["differs: a.pyc"]
    assert compare.differences(first, other_size) == ["differs: a.pyc"]


def real_pyc(source: str, version: int = marshal.version, mtime: bytes = b"AAAA") -> bytes:
    code = compile(source, "m.py", "exec")
    return importlib.util.MAGIC_NUMBER + b"\0" * 4 + mtime + b"SIZE" + marshal.dumps(code, version)


def test_the_same_code_marshalled_differently_is_the_same_file():
    # What differed between real builds: reference flags, which depend on interpreter state.
    source = "def f(x):\n    return x in {'a', 'b', 'c'}\n"
    assert real_pyc(source) != real_pyc(source, 2)
    assert (
        compare.differences(
            archive({"a.pyc": real_pyc(source)}), archive({"a.pyc": real_pyc(source, 2, b"BBBB")})
        )
        == []
    )


@pytest.mark.parametrize(
    "other",
    [
        "def f(x):\n    return x in {'a', 'b', 'd'}\n",  # a frozenset constant
        "def f(y):\n    return y in {'a', 'b', 'c'}\n",  # a name
        "\ndef f(x):\n    return x in {'a', 'b', 'c'}\n",  # a line number
    ],
)
def test_different_code_is_a_different_file(other):
    source = "def f(x):\n    return x in {'a', 'b', 'c'}\n"
    assert compare.differences(
        archive({"a.pyc": real_pyc(source)}), archive({"a.pyc": real_pyc(other)})
    ) == ["differs: a.pyc"]


def test_uv_install_timestamp_is_ignored_only_where_uv_writes_it():
    info = "site/x-1.dist-info"
    a = {
        f"{info}/uv_cache.json": b'{"secs":1}',
        f"{info}/RECORD": b"x.py,sha256=A,1\nx-1.dist-info/uv_cache.json,sha256=B,9\n",
    }
    b = {
        f"{info}/uv_cache.json": b'{"secs":2}',
        f"{info}/RECORD": b"x.py,sha256=A,1\nx-1.dist-info/uv_cache.json,sha256=C,9\n",
    }
    assert compare.differences(archive(a), archive(b)) == []
    elsewhere = archive({"app/uv_cache.json": b"1"}), archive({"app/uv_cache.json": b"2"})
    assert compare.differences(*elsewhere) == ["differs: app/uv_cache.json"]
    changed_record = dict(b)
    changed_record[f"{info}/RECORD"] = b"x.py,sha256=Z,1\nx-1.dist-info/uv_cache.json,sha256=C,9\n"
    assert compare.differences(archive(a), archive(changed_record)) == [f"differs: {info}/RECORD"]


def test_added_removed_and_mode_changes_are_reported():
    a = archive({"keep": b"1", "gone": b"2"})
    b = archive({"keep": b"1", "new": b"3"})
    assert compare.differences(a, b) == ["only in first: gone", "only in second: new"]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("keep")
        info.size = 1
        info.mode = 0o777
        tar.addfile(info, io.BytesIO(b"1"))
    buffer.seek(0)
    assert compare.differences(archive({"keep": b"1"}), dict(compare.entries(buffer))) == [
        "differs: keep"
    ]


def test_a_recorded_manifest_compares_like_a_live_tree(tmp_path, monkeypatch, capsys):
    tested = archive({"app/main.py": b"tested"})
    monkeypatch.setattr(compare, "tree", lambda image: tested)
    manifest = tmp_path / "smoke-image-manifest.json"
    assert compare.main(["compare_images.py", "--write-manifest", "smoked", str(manifest)]) == 0
    arguments = ["compare_images.py", "--against-manifest", str(manifest), "release"]
    assert compare.main(arguments) == 0
    assert "images match" in capsys.readouterr().out

    monkeypatch.setattr(compare, "tree", lambda image: archive({"app/main.py": b"rebuilt"}))
    assert compare.main(arguments) == 1
    assert "differs: app/main.py" in capsys.readouterr().err


def test_a_manifest_from_another_python_is_refused_not_compared(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(compare, "tree", lambda image: archive({"a.pyc": b"1"}))
    manifest = tmp_path / "manifest.json"
    assert compare.main(["compare_images.py", "--write-manifest", "smoked", str(manifest)]) == 0
    recorded = json.loads(manifest.read_text())
    manifest.write_text(json.dumps(recorded | {"bytecode": "00000000"}))
    arguments = ["compare_images.py", "--against-manifest", str(manifest), "release"]
    assert compare.main(arguments) == 2
    assert "not compared" in capsys.readouterr().err


def test_only_the_password_change_day_of_shadow_is_ignored():
    day_one = b"root:*:20400:0:99999:7:::\napp:!:20400:0:99999:7:::\n"
    day_two = day_one.replace(b"20400", b"20401")
    other_hash = day_one.replace(b"app:!", b"app:$y$hash")
    other_policy = day_one.replace(b":99999:", b":30:", 1)
    assert compare.fingerprint("etc/shadow", day_one) == compare.fingerprint("etc/shadow", day_two)
    assert compare.fingerprint("etc/shadow", day_one) != compare.fingerprint(
        "etc/shadow", other_hash
    )
    assert compare.fingerprint("etc/shadow", day_one) != compare.fingerprint(
        "etc/shadow", other_policy
    )
    # Anywhere else the same bytes are ordinary contents.
    assert compare.fingerprint("app/shadow", day_one) != compare.fingerprint("app/shadow", day_two)


def test_fingerprint_is_the_digest_of_the_kept_bytes():
    assert compare.fingerprint("a.txt", b"x") == hashlib.sha256(b"x").hexdigest()


def sarif(results: list[dict], rules: list[dict], *, extension: bool = False) -> dict:
    tool: dict = {"driver": {"rules": [] if extension else rules}}
    if extension:
        tool["extensions"] = [{"rules": rules}]
    return {"runs": [{"tool": tool, "results": results}]}


RULE_HIGH = {"id": "py/sql", "properties": {"security-severity": "8.8"}}
RULE_MEDIUM = {"id": "py/weak", "properties": {"security-severity": "5.0"}}
RULE_ERROR = {"id": "js/bad", "defaultConfiguration": {"level": "error"}}


def result(rule_id: str, **extra: object) -> dict:
    return {
        "ruleId": rule_id,
        "message": {"text": "m"},
        "locations": [
            {"physicalLocation": {"artifactLocation": {"uri": "a.py"}, "region": {"startLine": 3}}}
        ],
        **extra,
    }


@pytest.mark.parametrize(
    ("results", "rules", "blocked"),
    [
        ([result("py/sql")], [RULE_HIGH], 1),
        ([result("py/weak")], [RULE_MEDIUM], 0),
        ([result("js/bad")], [RULE_ERROR], 1),
        ([result("py/weak", level="error")], [RULE_MEDIUM], 1),
        ([result("py/unknown")], [RULE_MEDIUM], 1),
        pytest.param(
            [result("py/sql", suppressions=[])], [RULE_HIGH], 1, id="empty-suppressions-block"
        ),
        pytest.param(
            [result("py/sql", suppressions=[{"kind": "inSource"}])],
            [RULE_HIGH],
            0,
            id="in-source-suppression-does-not-block",
        ),
        ([], [RULE_HIGH], 0),
    ],
)
def test_blocking_decision(results, rules, blocked):
    assert len(codeql.findings(sarif(results, rules))) == blocked


def test_rules_in_query_pack_extensions_are_found():
    assert len(codeql.findings(sarif([result("py/sql")], [RULE_HIGH], extension=True))) == 1


def test_finding_names_rule_and_location():
    assert codeql.findings(sarif([result("py/sql")], [RULE_HIGH]))[0].startswith("py/sql a.py:3 ")


def test_gate_exit_codes(tmp_path, capsys):
    assert codeql.main(["gate", str(tmp_path)]) == 1  # no SARIF: nothing analysed
    clean = tmp_path / "python.sarif"
    clean.write_text(json.dumps(sarif([], [RULE_HIGH])), encoding="utf-8")
    assert codeql.main(["gate", str(tmp_path)]) == 0
    clean.write_text(json.dumps(sarif([result("py/sql")], [RULE_HIGH])), encoding="utf-8")
    assert codeql.main(["gate", str(tmp_path)]) == 1
    assert "py/sql" in capsys.readouterr().err
    assert codeql.main(["gate"]) == 2


def write_exceptions(tmp_path: Path, entries: list[dict]) -> str:
    path = tmp_path / "exceptions.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def test_exception_clears_only_its_rule_in_its_file(tmp_path, capsys):
    (tmp_path / "python.sarif").write_text(
        json.dumps(sarif([result("py/sql")], [RULE_HIGH])), encoding="utf-8"
    )
    accepted = write_exceptions(
        tmp_path, [{"rule": "py/sql", "path": "a.py", "reason": "reviewed: parameterised"}]
    )
    assert codeql.main(["gate", str(tmp_path), accepted]) == 0
    assert "accepted by exception" in capsys.readouterr().out
    for other in ({"rule": "py/sql", "path": "b.py"}, {"rule": "py/x", "path": "a.py"}):
        entries = [{**other, "reason": "reviewed"}]
        assert codeql.main(["gate", str(tmp_path), write_exceptions(tmp_path, entries)]) == 1


@pytest.mark.parametrize(
    "entry",
    [
        {"rule": "py/sql", "path": "a.py"},
        {"rule": "py/sql", "path": "a.py", "reason": "  "},
        {"path": "a.py", "reason": "x"},
    ],
)
def test_exception_without_a_reason_is_refused(tmp_path, entry):
    with pytest.raises(ValueError):
        codeql.main(["gate", str(tmp_path), write_exceptions(tmp_path, [entry])])


def test_the_repository_starts_with_no_exceptions():
    # Accepting a finding is the maintainer's decision, one reviewed line at a time.
    assert codeql.load_exceptions(codeql.DEFAULT_EXCEPTIONS) == set()
