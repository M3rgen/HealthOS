"""install.sh defaults: Google-less full mode and HEALTHOS_SOURCES (IP-2).

Runs in the stub sandbox of test_installer_unix (no Docker, no network, no real home).
"""

from __future__ import annotations

from test_installer_unix import DRY, HOST_PLATFORM, REAL, Sandbox, parse_env
from test_installer_unix import box as box
from test_installer_unix import pytestmark as pytestmark  # bash on a Unix host

GOOGLE_SECRETS = ("GOOGLE_TOKEN_ENCRYPTION_KEY", "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION")


def preview(box: Sandbox) -> dict[str, str]:
    return parse_env(box.repo / ".env.dry-run")


def test_a_new_install_defaults_to_full_mode_without_google_and_with_backups(box):
    result = box.run(*DRY, "--platform", HOST_PLATFORM)
    assert result.returncode == 0, result.stdout + result.stderr
    values = preview(box)
    assert values["COMPOSE_FILE"] == "docker-compose.yml"  # no development override
    assert values["HEALTHOS_SOURCES"] == "none"
    for name in GOOGLE_SECRETS:
        assert values[name] == "", name
    out = " ".join(result.stdout.split())
    assert "Mode: full" in out and "Daily backup: yes" in out
    assert "Still missing" not in out  # nothing Google-related is required


def test_full_mode_with_garmin_only_needs_no_google_value(box):
    key = box.home / "garmin.key"
    key.write_text("synthetic-garmin-key\n")
    result = box.run(
        *REAL, "--platform", HOST_PLATFORM, "--mode", "full", "--sources", "garmin",
        "--garmin-key-file", str(key),
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    values = parse_env(box.repo / ".env")
    assert values["HEALTHOS_SOURCES"] == "garmin"
    assert values["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.garmin.yml"
    for name in GOOGLE_SECRETS:
        assert values[name] == "", name
    assert "Still missing" not in result.stdout and "NOT started" not in result.stdout
    assert "Configured (not started: --no-start)" in result.stdout


def test_full_mode_with_google_still_lists_the_google_values(box):
    result = box.run(*REAL, "--platform", HOST_PLATFORM, "--mode", "full", "--sources", "google")
    assert result.returncode == 0, result.stdout + result.stderr
    values = parse_env(box.repo / ".env")
    assert values["HEALTHOS_SOURCES"] == "google"
    assert values["GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION"].startswith("Bearer ")
    assert "NOT started" in result.stdout
    for name in ("GOOGLE_CLIENT_ID is empty", "GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI must be https"):
        assert name in result.stdout


def test_trial_says_look_around_only(box):
    english = box.run(*DRY, "--platform", HOST_PLATFORM, "--mode", "trial")
    assert english.returncode == 0, english.stderr
    out = " ".join(english.stdout.split())
    assert "only for looking around: do not keep data in it, there are no backups" in out
    assert "Daily backup: no" in out
    turkish = box.run(*DRY, "--platform", HOST_PLATFORM, "--mode", "trial", "--lang", "tr")
    assert turkish.returncode == 0, turkish.stderr
    assert "veri saklamayın, yedek yok" in " ".join(turkish.stdout.split())
    full = box.run(*DRY, "--platform", HOST_PLATFORM, "--mode", "full")
    assert "do not keep data" not in full.stdout


def test_interactive_mode_menu_preselects_full(box, tmp_path):
    answers = tmp_path / "answers"
    for answer, mode in (("", "full"), ("2", "trial"), ("1", "full")):
        answers.write_text(answer + "\n")
        snippet = (
            f'exec 3<"{answers}"; INTERACTIVE=1; MODE=""; DIR="{box.repo}"; '
            'choose_mode 2>/dev/null; echo "MODE=$MODE"'
        )
        result = box.source(snippet)
        assert f"MODE={mode}" in result.stdout, result.stdout + result.stderr


def test_existing_env_keeps_its_sources_on(box):
    env = box.repo / ".env"
    # Before HEALTHOS_SOURCES existed a full install always ran Google: it stays on.
    env.write_text("COMPOSE_FILE=docker-compose.yml\nGOOGLE_CLIENT_ID=synthetic-id\n")
    result = box.run(*DRY, "--platform", HOST_PLATFORM, "--sources", "garmin,apple")
    assert result.returncode == 0, result.stdout + result.stderr
    assert preview(box)["HEALTHOS_SOURCES"] == "google,apple"  # no Garmin key: Garmin off
    assert "google was not selected but stays in HEALTHOS_SOURCES" in " ".join(
        result.stdout.split()
    )
    # A set value is the starting point; a chosen source is added, none is removed.
    env.write_text("COMPOSE_FILE=docker-compose.yml\nHEALTHOS_SOURCES=apple\n")
    result = box.run(*DRY, "--platform", HOST_PLATFORM, "--sources", "none")
    assert result.returncode == 0, result.stdout + result.stderr
    assert preview(box)["HEALTHOS_SOURCES"] == "apple"
    result = box.run(*DRY, "--platform", HOST_PLATFORM, "--sources", "google")
    assert preview(box)["HEALTHOS_SOURCES"] == "google,apple"
    # A trial install without Google settings does not get Google switched on.
    env.write_text("COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml\n")
    result = box.run(*DRY, "--platform", HOST_PLATFORM)
    assert result.returncode == 0, result.stdout + result.stderr
    assert preview(box)["HEALTHOS_SOURCES"] == "none"
    assert "Mode: trial" in result.stdout
