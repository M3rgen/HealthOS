"""An app-only rebuild must also update the migration and role-provisioning code."""

from pathlib import Path

import pytest
import yaml

from scripts.verify_configuration import check_compose_build


@pytest.mark.parametrize("service", ["app", "migrate", "provision-app-role"])
def test_separate_service_image_is_rejected(service):
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    assert check_compose_build(compose) == []
    compose["services"][service]["image"] = "stale-service:latest"
    assert any(service in error for error in check_compose_build(compose))


@pytest.mark.parametrize("service", ["migrate", "provision-app-role"])
def test_separate_service_build_is_rejected(service):
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    compose["services"][service]["build"] = "."
    assert "only app must build the shared application image" in check_compose_build(compose)
