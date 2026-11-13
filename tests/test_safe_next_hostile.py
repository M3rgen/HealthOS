"""``safe_next`` against hostile input: what a browser would do with the Location it produces.

A login redirect to another site is an open redirect. The table mirrors the spellings browsers
treat as another origin or strip silently (WHATWG URL), not only what ``urlsplit`` sees.
"""

from urllib.parse import urlsplit

import pytest
from support.auth import BASE, TOKEN, build
from test_auth import client

from app.core.auth import safe_next

ORIGIN = urlsplit(BASE).netloc


@pytest.fixture
def application(monkeypatch):
    # Explicit legacy interface: the shared-host session policy remains supported.
    return build(monkeypatch, app_public_url=BASE)


REFUSED = [
    # Scheme-relative and its backslash spellings: browsers read \ as / in these URLs.
    "//evil.example",
    "//evil.example/path",
    "///evil.example",
    "/\\evil.example",
    "/\\/evil.example",
    "\\\\evil.example",
    "\\/evil.example",
    "/path\\..\\..\\evil.example",
    # Absolute URLs and script schemes, in any case.
    "https://evil.example/",
    "HTTPS://evil.example/",
    "http:/evil.example",
    "http:evil.example",
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "data:text/html,x",
    "vbscript:x",
    # Not a path at all.
    "evil.example",
    "",
    # Control characters and spaces that browsers strip or that split the header.
    "/\t/evil.example",
    "/\n/evil.example",
    "/\r/evil.example",
    "/\r\nLocation: //evil.example",
    "/\x00/evil.example",
    "/\x0b/evil.example",
    "/\x1f/evil.example",
    "/ /evil.example",
    " //evil.example",
    "\t//evil.example",
    "\n//evil.example",
    # Would land back on the login page, or break out of the hidden form field.
    "/login",
    "/login?next=//evil.example",
    "/login/../evil.example",
    '/"onmouseover=x',
    "/<script>",
    "/>",
    # Over the length limit.
    "/" + "a" * 2048,
]

# Percent-encoding and dots stay inside the path: nothing decodes them before the browser, and
# the browser does not decode them when it resolves the Location.
KEPT = [
    "/%2F%2Fevil.example",
    "/%2f/evil.example",
    "/%5Cevil.example",
    "/%5c/evil.example",
    "/%09/evil.example",
    "/%0a/evil.example",
    "/%0d%0aLocation:%20//evil.example",
    "/%00/evil.example",
    "/.//evil.example",
    "/..//evil.example",
    "/@evil.example",
    "/:evil.example",
    "/evil.example:80",
    "/?next=//evil.example",
    "/#//evil.example",
    "/／evil.example",  # FULLWIDTH SOLIDUS: not a separator to any browser
    "/＼evil.example",  # FULLWIDTH REVERSE SOLIDUS
    "/sync?window=7#/sports",
]


def origin_a_browser_resolves(location: str) -> str:
    """The host a browser would go to, on the WHATWG rules ``urlsplit`` does not apply."""
    cleaned = location.strip("".join(chr(c) for c in range(0x21)))
    cleaned = "".join(c for c in cleaned if c not in "\t\r\n").replace("\\", "/")
    if cleaned.startswith("//"):
        return urlsplit(f"http:{cleaned}").netloc
    parts = urlsplit(cleaned)
    return parts.netloc if parts.scheme else ORIGIN


@pytest.mark.parametrize("target", REFUSED)
def test_hostile_targets_fall_back_to_the_dashboard(target):
    assert safe_next(target) == "/"


def test_no_target_falls_back_to_the_dashboard():
    assert safe_next(None) == "/"


@pytest.mark.parametrize("target", KEPT)
def test_lookalikes_stay_on_this_origin_and_are_not_rewritten(target):
    assert safe_next(target) == target
    assert origin_a_browser_resolves(safe_next(target)) == ORIGIN


@pytest.mark.parametrize("target", [*REFUSED, *KEPT])
async def test_no_redirect_ever_leaves_this_origin(application, target):
    async with client(application) as http:
        posted = await http.post("/login", data={"token": TOKEN, "next": target})
        fetched = await http.get("/login", params={"next": target})
    for response in (posted, fetched):
        assert response.status_code == 303
        location = response.headers["location"]
        assert origin_a_browser_resolves(location) == ORIGIN
        assert location.startswith("/") and not location.startswith(("//", "/\\"))


def test_the_model_of_a_browser_flags_what_it_should():
    # Guards the helper above: a check that always answered ORIGIN would pass everything.
    for hostile in (
        "//evil.example",
        "/\\evil.example",
        "\t//evil.example",
        "https://evil.example",
    ):
        assert origin_a_browser_resolves(hostile) == "evil.example"
