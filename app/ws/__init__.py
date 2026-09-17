"""WebSocket transport routes (flask-sock).

Kept separate from the flask-smorest REST blueprints: WebSocket handlers are
long-lived connections, not request/response, and only work behind a
WebSocket-capable server (gunicorn's gevent worker - see gunicorn.conf.py -
or the Werkzeug dev server). Under a plain sync WSGI server these routes are
simply never upgradable; the REST API is unaffected.

Importing this package registers every ``@sock.route`` handler on the ``sock``
instance from ``app.extensions`` (done in ``app/__init__.py::create_app``).
"""

from . import health, twilio_voice  # noqa: F401
