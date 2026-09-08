"""The WebSocket transport (flask-sock) is wired into the app.

A true upgrade/echo can't go through Flask's test client - flask-sock needs a
real WebSocket-capable server. That path is smoke-tested against the gunicorn
+ gevent Docker image (see the PR); here we just prove the route is
registered and rejects a non-WebSocket request rather than 404ing.
"""


def test_ws_ping_route_is_registered(app):
    assert any(str(rule) == "/api/ws/ping" for rule in app.url_map.iter_rules())


def test_ws_ping_rejects_a_plain_http_request(client):
    # flask-sock returns 400 for a request without the WebSocket upgrade
    # headers - not a 404, which would mean the route never registered.
    resp = client.get("/api/ws/ping")
    assert resp.status_code == 400


def test_rest_api_is_unaffected(client):
    # The transport change must not touch the REST surface.
    assert client.get("/api/health/").status_code == 200
