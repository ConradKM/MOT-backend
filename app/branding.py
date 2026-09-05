"""Central platform-brand constants.

CoMaz OS is the *platform* - the software powering every tenant. The
individual garage/business using it (e.g. "Kingsway MOT & Service Centre")
remains the primary customer-facing identity everywhere in the product;
nothing here should ever be used in place of a garage's own name.

Import from here rather than hard-coding the platform name again -
``PLATFORM_NAME_TM`` carries the trademark symbol for user-facing surfaces
(page titles, footers); ``PLATFORM_NAME`` is the plain form for places a
trademark glyph would be wrong (API titles, CLI output, email subject lines).
Formal trademark registration is handled outside this codebase.
"""

PLATFORM_NAME = "CoMaz OS"
PLATFORM_NAME_TM = "CoMaz OS™"
PLATFORM_TAGLINE = f"Powered by {PLATFORM_NAME_TM}"
