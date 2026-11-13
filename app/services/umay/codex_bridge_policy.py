"""Capability policy of the Umay bridge: one place for the launcher, the chat and the probe.

The bridge runs the external Codex CLI app-server. Configuration flags alone are not proof
(runtime measurement, 7 November 2026, CLI 0.161.0): ``features.multi_agent=false`` left the
``collaboration.spawn_agent`` tool working, because the model metadata of the bridge model
(``multi_agent_version: "v2"``) enables multi-agent tools on its own. The policy therefore acts
on three levels, each measured by ``app.services.umay.codex_runtime_probe`` before the bridge
starts:

* ``features.view_image=false`` removes the built-in image reader (catalog and dispatch);
* a bridge-owned ``model_catalog_json`` derived from the very binary's bundled catalog drops
  ``multi_agent_version`` (no collaboration namespace) and ``apply_patch_tool_type`` (no patch
  tool) for the bridge model;
* ``features.multi_agent_v2.max_concurrent_threads_per_session = 1`` refuses any spawn at
  dispatch even when another catalog (for example a provider-supplied one) offers the tool.

The chat fails closed against a bridge it cannot tie to this policy: every thread repeats all
limits (``thread_config``), and before a thread exists the bridge's effective configuration
(app-server ``config/read``) must match the policy, including the policy fingerprint in the
name of the bridge model catalog (``bridge_problem``).

Only stdlib imports: the launcher loads this module outside the web application.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

MODEL = "gpt-6.1-sol"
EFFORT = "high"
# CLI releases whose tool and model-catalog surface was measured with the runtime probe.
# A new release is supported only after the real-runtime tests pass for it
# (tests/test_codex_bridge_runtime.py with HEALTHOS_CODEX_CLI set) and this set is updated.
SUPPORTED_CLI_VERSIONS = frozenset({"0.161.0"})

DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "multi_agent",
    "view_image",
    "code_mode",
    "computer_use",
    "browser_use",
    "image_generation",
    "hooks",
    "goals",
    "sleep_tool",
)
# Model metadata keys removed from the bridge model's catalog entry (see module docstring).
CATALOG_REMOVED_KEYS = ("multi_agent_version", "apply_patch_tool_type")

# The complete tool surface the model may see. ``exec`` is the CLI's code-mode wrapper (a V8
# isolate without file system or network) whose nested tools are listed separately;
# ``wait`` only waits for that isolate; ``request_user_input*`` become client requests that
# the relay rejects (only ``item/tool/call`` reaches the health archive).
ALLOWED_TOOLS = frozenset(
    {
        "functions.exec",
        "functions.wait",
        "functions.request_user_input",
        "functions.request_user_input_async",
    }
)
ALLOWED_NESTED_TOOLS = frozenset({"health_archive", "clock__curr_time"})
REQUIRED_NESTED_TOOLS = frozenset({"health_archive"})


def bridge_config(catalog_path: str | None) -> str:
    """config.toml of the isolated bridge CODEX_HOME (never the user's global config)."""
    lines = [
        'cli_auth_credentials_store = "file"',
        'web_search = "disabled"',
        f'model = "{MODEL}"',
        'model_reasoning_effort = "medium"',
    ]
    if catalog_path is not None:
        lines.append(f"model_catalog_json = {json.dumps(catalog_path)}")
    lines += ["", "[features]", *(f"{feature} = false" for feature in DISABLED_FEATURES)]
    lines += [
        "",
        "[features.multi_agent_v2]",
        "enabled = false",
        "max_concurrent_threads_per_session = 1",
        "",
        "[analytics]",
        "enabled = false",
    ]
    return "\n".join(lines) + "\n"


def bridge_catalog(bundled: dict[str, Any]) -> dict[str, Any]:
    """The bridge model's entry of the binary's own bundled catalog, without agent/patch tools."""
    models = bundled.get("models")
    if not isinstance(models, list):
        raise ValueError("model catalog has no model list")
    for entry in models:
        if isinstance(entry, dict) and entry.get("slug") == MODEL:
            narrowed = {
                key: value for key, value in entry.items() if key not in CATALOG_REMOVED_KEYS
            }
            return {"models": [narrowed]}
    raise ValueError(f"model catalog does not contain {MODEL}")


def thread_config() -> dict[str, Any]:
    """Per-thread overrides sent by the chat; they repeat every limit of the bridge config file.

    A bridge started from an older or hand-written configuration then still refuses agents at
    dispatch for this thread (measured with CLI 0.161.0: the thread-level
    ``multi_agent_v2`` limit alone stopped a forced spawn on the pre-BRIDGE-02 configuration).
    The keys are the CLI's own effective-config names (``config/read`` lists them).
    """
    config: dict[str, Any] = {"model_reasoning_effort": EFFORT, "web_search": "disabled"}
    config.update({f"features.{feature}": False for feature in DISABLED_FEATURES})
    config["features.multi_agent_v2.enabled"] = False
    config["features.multi_agent_v2.max_concurrent_threads_per_session"] = 1
    return config


def initialize_params() -> dict[str, Any]:
    """The opening of every bridge connection; the front refuses any other."""
    return {
        "clientInfo": {"name": "healthos_chat", "title": "HealthOS", "version": "0.1.0"},
        "capabilities": {"experimentalApi": True},
    }


def thread_start_params(
    base_instructions: str, developer_instructions: str, tools: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "ephemeral": True,
        "model": MODEL,
        "allowProviderModelFallback": False,
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "baseInstructions": base_instructions,
        "developerInstructions": developer_instructions,
        "dynamicTools": tools,
        "serviceName": "healthos_chat",
        "config": thread_config(),
    }


# The probe's logic is part of what a verified bridge was verified with, and so is what its
# front lets through. The front compares thread/start with this file and persona once, when it
# starts: a chat from another version is refused here, before it opens a thread at the front.
PROBE_SOURCES = (
    "codex_bridge_policy.py",
    "codex_runtime_probe.py",
    "codex_runtime_rpc.py",
    "codex_bridge_filter.py",
    "codex_chat_thread.py",
    "../interpretation/archive_query.py",
    "../../personas/umay/soul.md",
)


def policy_fingerprint() -> str:
    """Identifies the capability policy (and probe) a bridge was verified with.

    The launcher records it in the attestation and names the bridge model catalog after it;
    the chat compares the catalog name the running bridge reports (``config/read``) with its
    own value, so a bridge started under another policy never receives a conversation.
    """
    here = Path(__file__).resolve().parent
    material = json.dumps(
        [
            bridge_config("<catalog>"),
            thread_config(),
            sorted(SUPPORTED_CLI_VERSIONS),
            sorted(ALLOWED_TOOLS | ALLOWED_NESTED_TOOLS),
            sorted(REQUIRED_NESTED_TOOLS),
            list(CATALOG_REMOVED_KEYS),
            [hashlib.sha256((here / name).read_bytes()).hexdigest() for name in PROBE_SOURCES],
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def catalog_file_name(fingerprint: str | None = None) -> str:
    """File name of the bridge model catalog, bound to the policy it was verified under."""
    return f"model-catalog-{fingerprint or policy_fingerprint()}.json"


def bridge_problem(config_read: dict[str, Any]) -> str | None:
    """Why the effective configuration of a running bridge (``config/read``) is not the
    verified policy, or None. Checked by the chat before any thread exists."""
    config = config_read.get("config") or {}
    if config.get("model") != MODEL or config.get("web_search") != "disabled":
        return "unexpected bridge model settings"
    catalog = config.get("model_catalog_json")
    # The bridge may run on Windows while the application runs in a Linux container.
    if not isinstance(catalog, str) or re.split(r"[\\/]", catalog)[-1] != catalog_file_name():
        return "bridge not verified under the current policy"
    features = config.get("features") or {}
    if any(features.get(feature) is not False for feature in DISABLED_FEATURES):
        return "bridge feature not disabled"
    agents = features.get("multi_agent_v2")
    if (
        not isinstance(agents, dict)
        or agents.get("enabled") is not False
        or agents.get("max_concurrent_threads_per_session") != 1
    ):
        return "bridge agent limit missing"
    return None


def cli_version(text: str) -> str:
    """``codex-cli 0.161.0`` -> ``0.161.0``; anything else is an unknown runtime."""
    parts = text.strip().split()
    if len(parts) != 2 or parts[0] != "codex-cli":
        raise ValueError("unrecognised Codex CLI version output")
    return parts[1]


def thread_problem(result: dict[str, Any]) -> str | None:
    """Why an app-server thread does not match the policy, or None.

    The launcher proves the capability boundary before the bridge starts; this cheap check
    keeps the chat from talking to a different (unattested) runtime or to a thread whose
    effective sandbox differs from the requested one.
    """
    thread = result.get("thread") or {}
    if thread.get("cliVersion") not in SUPPORTED_CLI_VERSIONS:
        return "unsupported runtime version"
    if result.get("model") != MODEL:
        return "unexpected model"
    if result.get("approvalPolicy") != "never":
        return "unexpected approval policy"
    sandbox = result.get("sandbox") or {}
    if sandbox.get("type") != "readOnly" or sandbox.get("networkAccess") is not False:
        return "unexpected sandbox"
    return None
