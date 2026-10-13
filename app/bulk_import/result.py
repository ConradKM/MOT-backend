"""What an import reports, row by row, and the limits every import enforces.

One shape for every kind of import and every caller (preview and commit,
owner app and Platform Admin): the frontends render it and never decide
validity themselves, so this is the whole contract between them and the
rules in this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STATUS_NEW = "NEW"
STATUS_EXISTS = "EXISTS"
STATUS_ERROR = "ERROR"

#: Hard caps, enforced before any row is looked at. Exceeding one is a 422 for
#: the whole request, not a per-row error - the payload is not worth reading.
MAX_BODY_BYTES = 1024 * 1024
MAX_SERVICES = 500
MAX_APPOINTMENTS = 1000


class ImportPayloadError(ValueError):
    """The payload as a whole is unusable (wrong shape, over a limit). Maps to
    422 with the message shown as-is, so it names the limit or the key."""


@dataclass
class Issue:
    #: A JSON path into the pasted payload, e.g. ``appointments[3].start``, so
    #: the UI can point at the exact field.
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


@dataclass
class ItemResult:
    index: int
    status: str = STATUS_NEW
    #: What the server resolved the row to - the values that would be (or
    #: were) written, and which existing records it matched.
    normalised: dict[str, Any] = field(default_factory=dict)
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    def error(self, path: str, message: str) -> None:
        self.errors.append(Issue(path, message))
        self.status = STATUS_ERROR

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(Issue(path, message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "status": self.status,
            "normalised": self.normalised,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
        }


@dataclass
class ImportResult:
    kind: str
    items: list[ItemResult] = field(default_factory=list)
    #: Issues with the payload rather than one row (an ignored top-level key).
    warnings: list[Issue] = field(default_factory=list)
    #: Facts the UI needs to present rows, e.g. the business timezone.
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "new": sum(1 for item in self.items if item.status == STATUS_NEW),
            "exists": sum(1 for item in self.items if item.status == STATUS_EXISTS),
            "error": sum(1 for item in self.items if item.status == STATUS_ERROR),
            # Rows with at least one warning; payload-level warnings are listed
            # separately in ``warnings``.
            "warning": sum(1 for item in self.items if item.warnings),
        }

    @property
    def has_errors(self) -> bool:
        return any(item.status == STATUS_ERROR for item in self.items)

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings) or any(item.warnings for item in self.items)

    def first_errors(self, limit: int = 5) -> list[Issue]:
        issues = [issue for item in self.items for issue in item.errors]
        return issues[:limit]

    def to_dict(self, *, dry_run: bool, committed: bool) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ok": not self.has_errors,
            "dry_run": dry_run,
            "committed": committed,
            "counts": self.counts,
            "items": [item.to_dict() for item in self.items],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "context": self.context,
        }


def read_rows(payload: Any, key: str, limit: int) -> tuple[list[Any], list[Issue]]:
    """The ``key`` list from a pasted payload, plus payload-level warnings.

    Same dialect as the onboarding spec: top-level keys starting with ``_``
    (``_meta``) and ``notes`` are ignored silently; any other stray top-level
    key is a warning, because an AI adding one is common and harmless.
    """
    if not isinstance(payload, dict):
        raise ImportPayloadError(f'Expected a JSON object like {{"{key}": [...]}}.')
    rows = payload.get(key)
    if not isinstance(rows, list):
        raise ImportPayloadError(f'Expected a "{key}" list at the top level.')
    if len(rows) > limit:
        raise ImportPayloadError(
            f"Too many {key}: {len(rows)}. At most {limit} can be imported at once - "
            "split the import into smaller batches."
        )

    warnings = [
        Issue(name, "Not part of the import format; ignored.")
        for name in payload
        if name != key and name != "notes" and not str(name).startswith("_")
    ]
    return rows, warnings


def unknown_key_warnings(item: ItemResult, row: dict, allowed: set[str], path: str) -> None:
    """Warn about every key in ``row`` outside ``allowed``. Keys starting with
    ``_`` are comments by convention and pass silently."""
    for name in row:
        if name in allowed or str(name).startswith("_"):
            continue
        item.warn(f"{path}.{name}", "Not part of the import format; ignored.")


def text_field(
    item: ItemResult,
    row: dict,
    name: str,
    path: str,
    *,
    max_length: int,
    required: bool = False,
) -> str | None:
    """A trimmed string field, ``None`` when empty. Numbers are accepted and
    stringified (an AI writing a phone number as a number is common); anything
    else is an error."""
    value = row.get(name)
    field_path = f"{path}.{name}"
    if isinstance(value, bool) or not (value is None or isinstance(value, (str, int, float))):
        item.error(field_path, "Expected text.")
        return None
    text = None if value is None else (str(value).strip() or None)

    if text is None:
        if required:
            item.error(field_path, "Required.")
        return None
    if len(text) > max_length:
        item.error(field_path, f"Too long: at most {max_length} characters.")
        return None
    return text
