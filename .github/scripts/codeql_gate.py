#!/usr/bin/env python3
"""Fail on CodeQL findings of high severity or error level, read from the SARIF the analysis wrote.

  codeql_gate.py SARIF_DIR [EXCEPTIONS.json]

Uploading to code scanning never fails a job, so this is what makes CodeQL block. A result blocks
when its rule has a ``security-severity`` of 7.0 or more (GitHub's high and critical), when its
level is ``error``, or when its rule cannot be found (fail closed). Suppressed results do not
block, and a directory without SARIF means nothing was analysed, which is a failure.

Dismissing an alert on GitHub does not change the SARIF, so a finding the maintainer has judged
acceptable goes in .github/codeql-exceptions.json: a list of objects with ``rule``, ``path`` and a
non-empty ``reason``. An exception covers that rule in that one file. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HIGH = 7.0
DEFAULT_EXCEPTIONS = Path(__file__).resolve().parents[1] / "codeql-exceptions.json"


def rules_of(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tool = run["tool"]
    components = [tool["driver"], *tool.get("extensions", [])]
    return {rule["id"]: rule for component in components for rule in component.get("rules", [])}


def blocks(result: dict[str, Any], rule: dict[str, Any] | None) -> bool:
    if rule is None:
        return True
    level = result.get("level") or rule.get("defaultConfiguration", {}).get("level")
    severity = float(rule.get("properties", {}).get("security-severity") or 0)
    return level == "error" or severity >= HIGH


def load_exceptions(path: Path) -> set[tuple[str, str]]:
    entries = json.loads(path.read_text("utf-8"))
    for entry in entries:
        if not (entry.get("rule") and entry.get("path") and str(entry.get("reason", "")).strip()):
            raise ValueError(f"exception needs rule, path and a reason: {entry}")
    return {(entry["rule"], entry["path"]) for entry in entries}


def findings(sarif: dict[str, Any], exceptions: set[tuple[str, str]] | None = None) -> list[str]:
    found = []
    for run in sarif.get("runs", []):
        rules = rules_of(run)
        for result in run.get("results", []):
            if result.get("suppressions"):
                continue
            rule_id = result.get("ruleId") or result.get("rule", {}).get("id", "")
            if blocks(result, rules.get(rule_id)):
                locations = result.get("locations") or [{}]
                location = locations[0].get("physicalLocation", {})
                path = location.get("artifactLocation", {}).get("uri", "?")
                if (rule_id, path) in (exceptions or set()):
                    print(f"codeql_gate: accepted by exception: {rule_id} {path}")
                    continue
                line = location.get("region", {}).get("startLine", "?")
                found.append(f"{rule_id} {path}:{line} {result.get('message', {}).get('text', '')}")
    return found


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    exceptions = load_exceptions(Path(argv[2]) if len(argv) == 3 else DEFAULT_EXCEPTIONS)
    files = sorted(Path(argv[1]).glob("*.sarif"))
    if not files:
        print(f"codeql_gate: no SARIF in {argv[1]}; nothing was analysed", file=sys.stderr)
        return 1
    found = [
        finding
        for path in files
        for finding in findings(json.loads(path.read_text("utf-8")), exceptions)
    ]
    for finding in found:
        print(finding, file=sys.stderr)
    print(f"codeql_gate: {len(files)} SARIF file(s), {len(found)} blocking finding(s)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
