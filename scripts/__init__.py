"""Developer / operator command-line entry points.

Each module here is runnable directly (``python scripts/<name>.py``) and also
importable as ``scripts.<name>`` for tests. The runnable ones put the repo root
on ``sys.path`` themselves so ``import app`` works either way.
"""
