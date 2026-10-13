"""Calendar feed tokens must never reach the gunicorn access log
(gunicorn.conf.py's RedactingLogger). Loads the real config file and drives the
real route through Flask so a change to either the route or the pattern fails
here rather than silently leaking tokens in production logs."""

import runpy
import secrets
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

CONF = runpy.run_path(str(Path(__file__).resolve().parent.parent / "gunicorn.conf.py"))


def _atoms_for(path: str, query: str = "") -> dict:
    logger = CONF["RedactingLogger"](SimpleNamespace(**_gunicorn_cfg()))
    environ = {
        "REQUEST_METHOD": "GET",
        "RAW_URI": f"{path}?{query}" if query else path,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_PROTOCOL": "HTTP/1.1",
        "HTTP_REFERER": f"https://example.test{path}",
    }
    resp = SimpleNamespace(status="200 OK", sent=10, headers=[])
    req = SimpleNamespace(headers=[("REFERER", f"https://example.test{path}")])
    atoms: dict = logger.atoms(resp, req, environ, timedelta(seconds=0))
    return atoms


def _gunicorn_cfg() -> dict:
    from gunicorn.config import Config

    cfg = Config()
    return {name: setting.get() for name, setting in cfg.settings.items()}


def test_feed_token_redacted_from_every_access_log_atom():
    token = secrets.token_urlsafe(32)
    atoms = _atoms_for(f"/api/calendar-feeds/{token}.ics")

    leaked = [key for key, value in atoms.items() if isinstance(value, str) and token in value]
    assert leaked == []
    assert atoms["U"] == "/api/calendar-feeds/[REDACTED].ics"
    assert atoms["r"] == "GET /api/calendar-feeds/[REDACTED].ics HTTP/1.1"


def test_other_paths_are_left_alone():
    atoms = _atoms_for("/api/appointments/", "start_date=2026-01-01")
    assert atoms["r"] == "GET /api/appointments/?start_date=2026-01-01 HTTP/1.1"


@pytest.mark.parametrize("suffix", ["", "?x=1"])
def test_pattern_matches_the_registered_feed_route(app, suffix):
    token = secrets.token_urlsafe(32)
    path = f"/api/calendar-feeds/{token}.ics"
    # The route really is shaped like this - not just the pattern's idea of it.
    adapter = app.url_map.bind("localhost")
    _endpoint, args = adapter.match(path)
    assert args == {"token": token}
    assert token not in CONF["redact_calendar_feed_tokens"](f"GET {path}{suffix} HTTP/1.1")
