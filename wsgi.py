"""Production WSGI entrypoint.

``gunicorn --config gunicorn.conf.py wsgi:app`` (see the Dockerfile CMD).

gevent monkey-patching is done by gunicorn's ``gevent`` worker *before* it
imports this module (``GeventWorker.init_process`` -> ``patch()`` -> app
load), so it is deliberately NOT done here - doing it again, or here, would
either be redundant or (if this module were ever imported by a non-gevent
process, e.g. a test) patch a process that shouldn't be patched.
"""

from app import create_app

app = create_app()
