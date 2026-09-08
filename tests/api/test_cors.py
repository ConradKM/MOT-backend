"""Cross-origin access to /api/* for deployed static frontends.

Covers ConradKM/MOT-backend#44. The allowlist under test is
``TestConfig.CORS_ORIGINS`` (localhost + the two comaz.co.uk origins).
"""

import pytest

ALLOWED = "https://app.comaz.co.uk"
ALSO_ALLOWED = "https://comaz.co.uk"
DEV = "http://localhost:5173"
DISALLOWED = "https://evil.example"


def _acao(resp):
    return resp.headers.get("Access-Control-Allow-Origin")


@pytest.mark.parametrize("origin", [ALLOWED, ALSO_ALLOWED, DEV])
def test_allowed_origin_is_echoed_on_a_simple_request(client, origin):
    resp = client.get("/api/health/", headers={"Origin": origin})

    assert resp.status_code == 200
    assert _acao(resp) == origin


def test_unknown_origin_is_not_granted_cors(client):
    resp = client.get("/api/health/", headers={"Origin": DISALLOWED})

    # The request itself still succeeds server-side; the browser is what blocks
    # it - and it blocks it precisely because our origin is not echoed back.
    assert resp.status_code == 200
    assert _acao(resp) not in (DISALLOWED, "*")


def test_allow_origin_is_never_wildcard(client):
    resp = client.get("/api/health/", headers={"Origin": ALLOWED})

    assert _acao(resp) == ALLOWED
    assert _acao(resp) != "*"


def test_preflight_allows_authorized_json_requests(client):
    resp = client.options(
        "/api/appointments/",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert resp.status_code in (200, 204)
    assert _acao(resp) == ALLOWED
    allow_headers = resp.headers.get("Access-Control-Allow-Headers", "").lower()
    assert "authorization" in allow_headers
    assert "content-type" in allow_headers
    allow_methods = resp.headers.get("Access-Control-Allow-Methods", "")
    assert "POST" in allow_methods


def test_a_request_without_an_origin_is_never_granted_wildcard(client):
    # flask-cors with an allowlist echoes a concrete allowed origin rather than
    # "*"; a request with no Origin isn't a CORS request so the header is inert,
    # but it must still never be a wildcard.
    resp = client.get("/api/health/")

    assert resp.status_code == 200
    assert _acao(resp) != "*"
