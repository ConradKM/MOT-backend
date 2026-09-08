"""Gunicorn config for the Render Docker Web Service.

Serves the REST API and the WebSocket routes (app/ws/*) from one process
group. The ``gevent`` worker is what makes flask-sock's WebSocket upgrade
work and keeps a long-lived ConversationRelay call from tying up a worker;
it also monkey-patches the stdlib before loading the app, so psycopg 3's
sync driver (which drives libpq in non-blocking mode via ``select``) and
redis-py both cooperate with the event loop.
"""

import os

# Render injects PORT; fall back to the port the old `flask run` CMD used so a
# misconfigured service still binds somewhere predictable.
bind = f"0.0.0.0:{os.getenv('PORT', '5000')}"

worker_class = "gevent"
# Each gevent worker handles many connections concurrently, so a small worker
# count is enough. Override with WEB_CONCURRENCY on Render if needed.
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
worker_connections = int(os.getenv("GUNICORN_WORKER_CONNECTIONS", "1000"))

# Each worker patches the stdlib itself, then imports the app - no preloading.
preload_app = False

# Long enough for a ConversationRelay voice call to stay connected without the
# arbiter reaping the worker; the gevent worker heartbeats in the background so
# this only bites a genuinely wedged worker.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
