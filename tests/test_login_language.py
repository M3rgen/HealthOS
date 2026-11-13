"""Login language negotiation, safe links and localized authentication failures."""

import base64
import hashlib
import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from support.auth import BASE, TOKEN, build

from app.api import login


@pytest.fixture
def application(monkeypatch):
    monkeypatch.setattr(login, "FAILED_LOGIN_DELAY_SECONDS", 0)
    # Explicit legacy interface: the shared-host session policy remains supported.
    return build(monkeypatch, app_public_url=BASE)


@pytest.mark.parametrize(
    "query,accept,language",
    [
        (None, "", "tr"),
        ("en", "tr", "en"),
        ("tr", "en-GB", "tr"),
        (None, "en-GB", "en"),
        (None, "EN-us;q=0.8,tr;q=0.5", "en"),
        ("", "tr-TR,en;q=0.9", "tr"),
        ("", "fr,en;q=0.9", "tr"),
        ("invalid", "", "tr"),
        ("invalid", "en-GB", "en"),
        ("EN", "tr", "tr"),
        ('en"><script>', "tr", "tr"),
    ],
)
async def test_login_language_selection(application, query, accept, language):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(application), base_url=BASE) as http:
        response = await http.get(
            "/login",
            params={"lang": query} if query is not None else {},
            headers={"Accept-Language": accept},
        )
    assert response.status_code == 200
    assert f'<html lang="{language}">' in response.text
    assert f'name="lang" value="{language}"' in response.text
    title, label, button = (
        ("Sign in", "Access key", "Sign in")
        if language == "en"
        else ("Giriş", "Erişim anahtarı", "Giriş yap")
    )
    assert f"<title>HealthOS · {title}</title>" in response.text
    assert f'<label for="token">{label}</label>' in response.text
    assert f'<button type="submit">{button}</button>' in response.text
    other, name = ("tr", "Türkçe") if language == "en" else ("en", "English")
    assert re.search(rf'hreflang="{other}"\s+lang="{other}">{name}</a>', response.text)
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    style = re.search(r"<style>(.*?)</style>", response.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    assert response.headers["content-security-policy"] == login.CSP
    assert f"'sha256-{digest}'" in login.CSP


@pytest.mark.parametrize("language", ["tr", "en"])
@pytest.mark.parametrize(
    "target,safe",
    [
        ("/page-probe?a=1&b=iki#part", "/page-probe?a=1&b=iki#part"),
        ("//attacker.example/", "/"),
        ('/"><script>x</script>', "/"),
    ],
)
async def test_login_language_link_preserves_only_safe_next(application, language, target, safe):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(application), base_url=BASE) as http:
        response = await http.get("/login", params={"lang": language, "next": target})
        href = re.search(r'<a href="([^"]+)" hreflang=', response.text).group(1)
        link = urlsplit(unescape(href))
        assert not link.netloc and link.path == "/login"
        assert parse_qs(link.query) == {
            "lang": ["en" if language == "tr" else "tr"],
            "next": [safe],
        }
        switched = await http.get(unescape(href))
    assert f'name="next" value="{safe.replace("&", "&amp;")}"' in switched.text


@pytest.mark.parametrize("language", ["tr", "en"])
@pytest.mark.parametrize("error,status", [("token", 401), ("origin", 403), ("rate", 429)])
async def test_login_failures_keep_form_language(application, language, error, status):
    if error == "rate":
        application.state.login_throttle = login.LoginThrottle(clock=lambda: 100.0)
        while application.state.login_throttle.reserve() is not None:
            pass
    headers = {"Accept-Language": "tr" if language == "en" else "en"}
    headers["Origin"] = "https://attacker.example" if error == "origin" else BASE
    async with httpx.AsyncClient(transport=httpx.ASGITransport(application), base_url=BASE) as http:
        response = await http.post(
            "/login",
            data={"lang": language, "next": "/page-probe", "token": "wrong"},
            headers=headers,
        )
    assert response.status_code == status
    assert f'<html lang="{language}">' in response.text
    assert f'name="lang" value="{language}"' in response.text
    assert 'name="next" value="/page-probe"' in response.text
    assert 'role="alert"' in response.text
    assert "set-cookie" not in response.headers
    assert response.headers["content-security-policy"] == login.CSP
    if error == "rate":
        assert response.headers["retry-after"] == str(login.LOGIN_MAX_WAIT_SECONDS)
    expected = {
        "tr": {
            "token": "Erişim anahtarı yanlış.",
            "origin": "İstek doğrulanamadı.",
            "rate": "Çok fazla deneme yapıldı.",
        },
        "en": {
            "token": "The access key is incorrect.",
            "origin": "The request could not be verified.",
            "rate": "Too many attempts.",
        },
    }
    assert expected[language][error] in response.text


@pytest.mark.parametrize("language", ["tr", "en"])
async def test_localized_login_still_issues_strict_session(application, language):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(application), base_url=BASE) as http:
        response = await http.post(
            "/login",
            data={"lang": language, "token": TOKEN, "next": "/api/probe"},
            headers={"Origin": BASE},
        )
        assert response.status_code == 303 and response.headers["location"] == "/api/probe"
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert (await http.get("/api/probe")).status_code == 200


@pytest.mark.parametrize(
    "language,short,long", [("tr", "24 saat", "30 gün"), ("en", "24 hours", "30 days")]
)
async def test_login_localizes_session_and_isolated_host_hints(application, language, short, long):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(application), base_url=BASE) as http:
        local = await http.get("/login", params={"lang": language})
        isolated = await http.get("http://healthos.localhost:8000/login", params={"lang": language})
    assert short in local.text and long in isolated.text
    assert (
        "uses its own cookie space." if language == "en" else "adresi kendi çerez alanını kullanır."
    ) in local.text
    assert 'href="http://healthos.localhost:8000/login?' in local.text
    assert 'href="http://healthos.localhost:8000/login?' not in isolated.text
