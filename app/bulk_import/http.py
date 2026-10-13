"""What every bulk-import endpoint shares: request/response schemas, the body
size cap, and the one rule for whether a run commits.

Preview and commit are the same call. The import function always runs in
full inside the request's transaction; afterwards the transaction is rolled
back for a ``dry_run``, for a payload with errors, or for one with warnings
the caller has not acknowledged - and committed otherwise. A commit
re-validates from the raw payload and never trusts a preview the client
hands back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import request
from flask_smorest import abort
from marshmallow import Schema, fields, validate

from .formats import import_formats
from .result import MAX_BODY_BYTES, ImportPayloadError, ImportResult

logger = logging.getLogger(__name__)


class BulkImportRequestSchema(Schema):
    # Defaults to a preview: a client that forgets the flag writes nothing.
    dry_run = fields.Bool(load_default=True)
    # Required to commit a payload whose rows carry warnings (clashes,
    # out-of-hours, ignored keys) - the UI's "Import anyway".
    acknowledge_warnings = fields.Bool(load_default=False)
    # The pasted JSON, as-is. Every rule about its contents lives in
    # app/bulk_import, so it is deliberately unconstrained here.
    payload = fields.Raw(required=True)


class ImportIssueSchema(Schema):
    path = fields.Str(dump_only=True)
    message = fields.Str(dump_only=True)


class ImportItemSchema(Schema):
    index = fields.Int(dump_only=True)
    status = fields.Str(dump_only=True, validate=validate.OneOf(("NEW", "EXISTS", "ERROR")))
    normalised = fields.Dict(dump_only=True)
    errors = fields.List(fields.Nested(ImportIssueSchema), dump_only=True)
    warnings = fields.List(fields.Nested(ImportIssueSchema), dump_only=True)


class ImportCountsSchema(Schema):
    new = fields.Int(dump_only=True)
    exists = fields.Int(dump_only=True)
    error = fields.Int(dump_only=True)
    warning = fields.Int(dump_only=True)


class BulkImportResultSchema(Schema):
    kind = fields.Str(dump_only=True)
    ok = fields.Bool(dump_only=True)
    dry_run = fields.Bool(dump_only=True)
    committed = fields.Bool(dump_only=True)
    counts = fields.Nested(ImportCountsSchema, dump_only=True)
    items = fields.List(fields.Nested(ImportItemSchema), dump_only=True)
    warnings = fields.List(fields.Nested(ImportIssueSchema), dump_only=True)
    context = fields.Dict(dump_only=True)


class ImportFormatSchema(Schema):
    example = fields.Dict(dump_only=True)
    json_schema = fields.Dict(dump_only=True)
    ai_prompt_template = fields.Str(dump_only=True)


class ImportLimitsSchema(Schema):
    max_body_bytes = fields.Int(dump_only=True)
    max_services = fields.Int(dump_only=True)
    max_appointments = fields.Int(dump_only=True)


class ImportDefaultsSchema(Schema):
    timezone = fields.Str(dump_only=True)
    statuses = fields.List(fields.Str(), dump_only=True)


class ImportFormatsSchema(Schema):
    services = fields.Nested(ImportFormatSchema, dump_only=True)
    appointments = fields.Nested(ImportFormatSchema, dump_only=True)
    defaults = fields.Nested(ImportDefaultsSchema, dump_only=True)
    limits = fields.Nested(ImportLimitsSchema, dump_only=True)


def limit_body_size(fn):
    """Refuse an oversized body before it is parsed. Sits *above* the
    ``@blp.arguments`` decorator so the JSON is never decoded."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        length = request.content_length
        if length is None:
            length = len(request.get_data(cache=True))
        if length > MAX_BODY_BYTES:
            abort(
                422,
                message=f"Import too large: at most {MAX_BODY_BYTES // 1024} KB per request. "
                "Split it into smaller batches.",
            )
        return fn(*args, **kwargs)

    return wrapper


def run_import(
    run: Callable[[], ImportResult],
    *,
    dry_run: bool,
    acknowledge_warnings: bool,
    session,
    on_commit: Callable[[ImportResult], None] | None = None,
    log_label: str,
) -> tuple[dict[str, Any], int]:
    """Run an import, then commit or roll back. Returns ``(body, status)``.

    200 for a preview (whatever it found) and for a successful commit; 422 for
    a commit refused because of errors, 409 for one refused because its
    warnings were not acknowledged. Either refusal writes nothing and still
    returns the full result, so the UI can show why.

    Logs counts only - rows carry customer personal data.
    """
    try:
        result = run()
    except ImportPayloadError as exc:
        session.rollback()
        abort(422, message=str(exc))

    blocked_by_errors = result.has_errors
    blocked_by_warnings = result.has_warnings and not acknowledge_warnings
    commit = not dry_run and not blocked_by_errors and not blocked_by_warnings

    if commit:
        if on_commit is not None:
            on_commit(result)
        session.commit()
    else:
        session.rollback()

    logger.info(
        "[bulk-import] %s dry_run=%s committed=%s counts=%s",
        log_label,
        dry_run,
        commit,
        result.counts,
    )

    status = 200
    if not dry_run and blocked_by_errors:
        status = 422
    elif not dry_run and blocked_by_warnings:
        status = 409
    return result.to_dict(dry_run=dry_run, committed=commit), status


__all__ = [
    "BulkImportRequestSchema",
    "BulkImportResultSchema",
    "ImportFormatsSchema",
    "import_formats",
    "limit_body_size",
    "run_import",
]
