"""Bulk import of services and appointments from pasted JSON.

The workflow: someone screenshots a price list or a diary, gives it to an AI
assistant of their choice together with the prompt from
:mod:`app.bulk_import.formats`, and pastes the JSON it returns. This package
is everything after the paste - parsing, validation, matching and creation -
used by every entry point:

* the owner app: ``POST /api/appointment-types/bulk`` and
  ``POST /api/appointments/bulk`` (OWNER only, business from the JWT);
* Platform Admin: ``POST /api/platform-admin/tenants/<id>/services/bulk``
  and the stateless onboarding preview ``POST /api/platform-admin/import/preview``;
* the onboarding spec's optional ``appointments`` list
  (``app/garages/business_onboarding.py``), and so the CLI.

The server is the single authority on validity: frontends check JSON syntax
and render the :class:`~app.bulk_import.result.ImportResult` this returns,
nothing more. Preview and commit run identical code - see
:func:`app.bulk_import.http.run_import`.
"""
