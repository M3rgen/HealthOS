"""The bridge binds to loopback or a Docker bridge unless an explicit option says otherwise."""

import subprocess

import pytest

from scripts import start_codex_bridge as bridge

FLAG = "CODEX_BRIDGE_ALLOW_NON_LOCAL_BIND"
LAN = "192.0.2.50"
IP_OUTPUT = (
    "2: eth0    inet 192.0.2.50/24 brd 192.0.2.255 scope global eth0\\\n"
    "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\\n"
    "4: br-1a2b3c4d5e6f    inet 172.18.0.1/16 brd 172.18.255.255 scope global br-1a2b3c4d5e6f\\\n"
)


@pytest.fixture
def host(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.setattr(bridge, "docker0_address", lambda: None)
    monkeypatch.setattr(bridge, "docker_bridge_addresses", lambda: set(), raising=False)
    monkeypatch.delenv(FLAG, raising=False)

    def choose(address):
        monkeypatch.setenv("CODEX_BRIDGE_BIND_HOST", address)
        return bridge.bind_host()

    return choose


@pytest.mark.parametrize("address", ["127.0.0.1", "127.0.0.2"])
def test_loopback_needs_no_option(host, address):
    assert host(address) == address


def test_ipv6_loopback_needs_no_option(host):
    assert host("::1") == "[::1]"


def test_a_lan_address_is_refused_by_default_and_the_message_names_the_option(host):
    with pytest.raises(SystemExit) as refused:
        host(LAN)
    assert FLAG in str(refused.value)


@pytest.mark.parametrize("address", ["203.0.113.9", "169.254.1.1", "::ffff:192.0.2.50", "fd00::1"])
def test_other_non_local_addresses_are_refused(host, address):
    with pytest.raises(SystemExit):
        host(address)


def test_the_docker_gateway_needs_no_option(host, monkeypatch):
    monkeypatch.setattr(bridge, "docker_bridge_addresses", lambda: {"172.18.0.1"}, raising=False)
    assert host("172.18.0.1") == "172.18.0.1"


def test_docker_addresses_are_read_from_the_bridge_interfaces_only(monkeypatch):
    monkeypatch.setattr(bridge.sys, "platform", "linux")
    monkeypatch.setattr(bridge.shutil, "which", lambda _: "/usr/sbin/ip")
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, IP_OUTPUT, ""),
    )
    assert bridge.docker_bridge_addresses() == {"172.17.0.1", "172.18.0.1"}


def test_the_automatic_docker0_default_still_works(host, monkeypatch):
    monkeypatch.setattr(bridge, "docker0_address", lambda: "172.17.0.1")
    monkeypatch.setattr(bridge, "docker_bridge_addresses", lambda: {"172.17.0.1"}, raising=False)
    monkeypatch.delenv("CODEX_BRIDGE_BIND_HOST", raising=False)
    assert bridge.bind_host() == "172.17.0.1"


def test_an_installation_on_a_lan_address_continues_with_the_option_and_a_warning(
    host, monkeypatch, capsys
):
    monkeypatch.setenv(FLAG, "1")
    assert host(LAN) == LAN
    warning = capsys.readouterr().err
    assert LAN in warning and FLAG in warning


def test_the_option_can_live_in_dotenv_like_the_bind_address(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.setattr(bridge, "docker0_address", lambda: None)
    monkeypatch.setattr(bridge, "docker_bridge_addresses", lambda: set(), raising=False)
    monkeypatch.delenv("CODEX_BRIDGE_BIND_HOST", raising=False)
    monkeypatch.delenv(FLAG, raising=False)
    (tmp_path / ".env").write_text(f"CODEX_BRIDGE_BIND_HOST={LAN}\n{FLAG}=true\n", encoding="utf-8")
    assert bridge.bind_host() == LAN
    assert FLAG in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "false", "", "no"])
def test_a_false_or_empty_option_does_not_open_the_bind(host, monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    with pytest.raises(SystemExit):
        host(LAN)


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::", "[::]", "::ffff:0.0.0.0"])
def test_no_option_opens_every_interface(host, monkeypatch, wildcard):
    monkeypatch.setenv(FLAG, "1")
    with pytest.raises(SystemExit):
        host(wildcard)
