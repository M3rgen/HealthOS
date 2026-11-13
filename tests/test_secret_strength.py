"""Secrets must have the strength of a generated one; older values get an explicit way through."""

import base64
import logging
import random
import secrets
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, strong_secret
from app.core.secret_strength import legacy_strong_secret
from scripts import start_codex_bridge as launcher

ROOT = Path(__file__).resolve().parents[1]
FLAG = "HEALTHOS_ALLOW_LEGACY_SECRETS"
# Pass the previous rule and fail the current one: what an older installation can hold.
HUMAN_MADE = "Password1234567!"
SHORT_RANDOM = "aB3xQ7mN9pL2wR8tZ5vK"
BELOW_ANY_RULE = "short-123456789"
UI_TOKEN = secrets.token_urlsafe(32)


def production(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "healthos_sources": "none",
        "app_ui_token": UI_TOKEN,
        "database_url": "postgresql+asyncpg://health:synthetic-db-Q3v8Lr2Xk9Tw5Zp7Nm4Hc6Jd@postgres:5432/health",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def test_the_value_from_the_finding_no_longer_passes():
    assert strong_secret(HUMAN_MADE) is False


@pytest.mark.parametrize("value", [HUMAN_MADE, SHORT_RANDOM, "Bearer " + SHORT_RANDOM])
def test_what_an_older_installation_can_hold_fails_now_but_passed_before(value):
    assert strong_secret(value) is False
    assert legacy_strong_secret(value) is True


def test_a_long_value_built_on_a_common_password_is_refused():
    assert strong_secret("Password-1234567890-Qx7Zk2-MwP4vR") is False


def test_random_hex_with_coincidentally_repeated_letters_is_strong():
    assert strong_secret("ad614d16d20c9691ad8124d97d269c18")


@pytest.mark.parametrize(
    "encoding,size", [("hex", 16), ("hex", 32), ("urlsafe", 32), ("urlsafe", 48)]
)
def test_every_documented_generator_passes_without_any_option(encoding, size):
    # Reproduce the documented formats, not production randomness: failures must be repeatable.
    rng = random.Random(20261112)
    for index in range(200_000):
        if encoding == "hex":
            value = f"{rng.getrandbits(size * 8):0{size * 2}x}"
        else:
            value = base64.urlsafe_b64encode(rng.randbytes(size)).rstrip(b"=").decode("ascii")
        assert strong_secret(value), (encoding, size, index, value)
        if encoding == "urlsafe" and size == 32 and index < 300:
            assert strong_secret("Bearer " + value)


@pytest.mark.parametrize(
    "value",
    [
        "Parola1Parola2Parola3",
        "Merhaba1Merhaba22Merhaba333Merhaba4444",
        "Parola1-Parola22_Parola333.Parola4444",
        "Gezgin12!Gezgin34?Gezgin56#Gezgin78",
        "Kelebek1_Kelebek2-Kelebek3.Kelebek4",
        "Bulut11-Bulut22_Bulut33.Bulut44!Bulut55",
        "Kapi1!Kapi22?Kapi333#Kapi4444-Kapi55555",
        "Deniz1_Deniz23-Deniz456.Deniz7890!",
        "Yolcu12+Yolcu34/Yolcu56=Yolcu78!Yolcu90",
        "MeRhAbA1-mErHaBa22_MERHABA333.merhaba4444",
        "Pa1ro2la3Pa4ro5la6Pa7ro8la9Pa0ro1la2",
        "Par1ola2Paro3la4Parol5a6Pa7rola8Parola9",
        "gez1gin2ge3zgin4gezg5in6gezgi7n8gezgin9",
    ],
)
def test_changing_numbers_and_separators_do_not_disguise_repeated_words(value):
    assert strong_secret(value) is False
    assert legacy_strong_secret(value) is False


@pytest.mark.parametrize("value", [HUMAN_MADE, SHORT_RANDOM])
def test_an_older_value_stops_production_loudly_and_says_how_to_continue(value):
    with pytest.raises(ValidationError) as refused:
        production(codex_bridge_token=value)
    message = str(refused.value)
    assert "CODEX_BRIDGE_TOKEN" in message
    assert FLAG in message
    assert value not in message


def test_a_value_weak_by_the_previous_rule_too_is_not_offered_the_option():
    with pytest.raises(ValidationError) as refused:
        production(codex_bridge_token=BELOW_ANY_RULE)
    assert "CODEX_BRIDGE_TOKEN" in str(refused.value)
    assert FLAG not in str(refused.value)


def test_the_option_keeps_an_older_installation_running_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        settings = production(codex_bridge_token=HUMAN_MADE, healthos_allow_legacy_secrets=True)
    assert settings.codex_bridge_token == HUMAN_MADE
    assert "CODEX_BRIDGE_TOKEN" in caplog.text
    assert FLAG in caplog.text
    assert HUMAN_MADE not in caplog.text


def test_the_option_does_not_admit_a_value_below_the_previous_rule():
    with pytest.raises(ValidationError):
        production(codex_bridge_token=BELOW_ANY_RULE, healthos_allow_legacy_secrets=True)


def test_the_option_is_passed_to_the_container_from_dotenv():
    # The compose file hands the app only the names it lists: unlisted, the option would not work.
    assert f"      {FLAG}:" in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_no_option_needed_when_every_secret_was_generated(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        production(codex_bridge_token=secrets.token_urlsafe(48))
    assert FLAG not in caplog.text


# The launcher, which starts the bridge on the host and checked nothing


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.delenv(FLAG, raising=False)
    return tmp_path


@pytest.mark.parametrize("value", [HUMAN_MADE, SHORT_RANDOM, BELOW_ANY_RULE])
def test_the_launcher_refuses_a_weak_token_by_default(clean_env, value):
    with pytest.raises(SystemExit) as refused:
        launcher.require_strong_token(value)
    assert value not in str(refused.value)
    assert "CODEX_BRIDGE_TOKEN" in str(refused.value)


def test_the_launcher_accepts_what_it_generates(clean_env):
    for _ in range(100):
        launcher.require_strong_token(secrets.token_urlsafe(48))


def test_the_launcher_keeps_an_older_token_with_the_option_from_the_environment(
    clean_env, monkeypatch, capsys
):
    monkeypatch.setenv(FLAG, "true")
    launcher.require_strong_token(HUMAN_MADE)
    assert FLAG in capsys.readouterr().err


def test_the_launcher_reads_the_option_from_dotenv_like_its_other_settings(clean_env, capsys):
    (clean_env / ".env").write_text(f"{FLAG}=1\n", encoding="utf-8")
    launcher.require_strong_token(HUMAN_MADE)
    assert HUMAN_MADE not in capsys.readouterr().err


def test_the_launcher_option_does_not_admit_a_value_below_the_previous_rule(clean_env, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    with pytest.raises(SystemExit):
        launcher.require_strong_token(BELOW_ANY_RULE)


def test_a_weak_token_never_starts_a_bridge(clean_env, monkeypatch):
    """Through main(): refused before a bridge is checked, launched or a token written."""
    home = clean_env / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", HUMAN_MADE)
    monkeypatch.setenv("CODEX_BRIDGE_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr("sys.argv", ["start_codex_bridge.py"])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a weak token must stop the launcher before this")

    monkeypatch.setattr(launcher, "healthy", forbidden)
    monkeypatch.setattr(launcher, "select_cli", forbidden)
    with pytest.raises(SystemExit) as refused:
        launcher.main()
    assert "CODEX_BRIDGE_TOKEN" in str(refused.value)
    assert not (home / "healthos-bridge" / "bridge.token").exists()


@pytest.mark.parametrize(
    "value",
    [
        "ab1ab2ab3ab4ab5ab6ab7ab8ab9ab0ab1ab2",
        "xy12xy34xy56xy78xy90xy12xy34xy56",
        "Ab!Ab?Ab#Ab$Ab%Ab&Ab*Ab+Ab=Ab-Ab_Ab",
        "qz7qz8qz9qz1qz2qz3qz4qz5qz6qz7qz8",
        "no1no2no3no4no5no6no7no8no9no0no1no",
        "Tr9Tr8Tr7Tr6Tr5Tr4Tr3Tr2Tr1Tr0Tr9Tr8",
        "a1a2a3a4a5a6a7a8a9a0a1a2a3a4a5a6a7",
        "k-1k-2k-3k-4k-5k-6k-7k-8k-9k-0k-1k",
    ],
)
def test_short_units_repeated_with_changing_numbers_stay_weak(value):
    # One- and two-letter units: few letters, but repeated far more often than chance allows.
    assert not strong_secret(value)
    assert not legacy_strong_secret(value)
