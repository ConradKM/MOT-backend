"""Collect and render the "how do I reach my local garage" summary.

Backs ``flask dev-info`` and the summary block ``flask seed-dev`` prints.
Read-only apart from nothing - it never writes. It reports the customer
booking URL using the identifier the frontend route actually expects (the
garage **UUID** - ``/book/:garageId`` loads the garage via
``GET /api/public/garages/<uuid>`` and only then uses the slug for the
availability + booking-request calls), the slug-based public API URL, and,
for the DEV-ONLY seeded garage, its documented shared password. Garages that
were onboarded normally are listed without credentials - their owner password
is only stored hashed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flask import current_app

from app.dev.seed import DEV_ACCOUNTS, DEV_GARAGE_NAME, DEV_PASSWORD, DEV_PRIMARY_ACCOUNT
from app.models.employee import Employee
from app.models.garage import Garage


@dataclass
class Account:
    email: str
    label: str
    #: The plaintext password, only ever set for the DEV-ONLY seeded garage.
    password: str | None = None


@dataclass
class GarageInfo:
    name: str
    garage_id: str
    slug: str
    booking_url: str
    public_api_url: str
    login_url: str
    is_seeded: bool
    accounts: list[Account] = field(default_factory=list)


def _frontend_base() -> str:
    # APP_BASE_URL is the frontend origin (also used for password-reset links).
    return str(current_app.config["APP_BASE_URL"]).rstrip("/")


def _api_base() -> str:
    return str(current_app.config["PUBLIC_API_BASE_URL"]).rstrip("/")


def booking_url(garage: Garage) -> str:
    """The customer booking wizard URL. The frontend route ``/book/:garageId``
    resolves the garage by **UUID**, not slug - so that's what goes here."""
    return f"{_frontend_base()}/book/{garage.id}"


def public_api_url(garage: Garage) -> str:
    """The unauthenticated garage endpoint - slug-addressed."""
    return f"{_api_base()}/api/public/{garage.slug}"


def _accounts_for(garage: Garage) -> list[Account]:
    if garage.name == DEV_GARAGE_NAME:
        present = {e.email for e in Employee.query.filter_by(garage_id=garage.id).all()}
        return [
            Account(email=email, label=label, password=DEV_PASSWORD)
            for email, label in DEV_ACCOUNTS
            if email in present
        ]

    # Onboarded normally: list the logins but never a password - it's hashed.
    return [
        Account(
            email=e.email,
            label="OWNER" if e.has_role("OWNER") else "staff",
            password=None,
        )
        for e in Employee.query.filter_by(garage_id=garage.id).order_by(Employee.email).all()
    ]


def _garage_info(garage: Garage) -> GarageInfo:
    return GarageInfo(
        name=garage.name,
        garage_id=str(garage.id),
        slug=garage.slug,
        booking_url=booking_url(garage),
        public_api_url=public_api_url(garage),
        login_url=f"{_frontend_base()}/login",
        is_seeded=garage.name == DEV_GARAGE_NAME,
        accounts=_accounts_for(garage),
    )


def collect_local_garages(*, include_all: bool = False) -> list[GarageInfo]:
    """Every garage in the current database, DEV-seeded one(s) first. With
    ``include_all=False`` (the default for ``dev-info``) only garages with
    known DEV-ONLY seeded credentials are returned."""
    garages = Garage.query.order_by(Garage.name).all()
    infos = [_garage_info(g) for g in garages]
    if not include_all:
        infos = [i for i in infos if i.is_seeded]
    infos.sort(key=lambda i: (not i.is_seeded, i.name))
    return infos


# --- Rendering ------------------------------------------------------------


def _account_lines(info: GarageInfo) -> list[str]:
    if not info.accounts:
        return ["  (no employee accounts)"]

    if not info.is_seeded:
        lines = ["  Onboarded garage - passwords are hashed, not shown."]
        lines += [f"    {a.email}  ({a.label})" for a in info.accounts]
        return lines

    primary = next(
        (a for a in info.accounts if a.email == DEV_PRIMARY_ACCOUNT),
        info.accounts[0],
    )
    others = [a for a in info.accounts if a is not primary]
    lines = [
        f"  Email:    {primary.email}",
        f"  Password: {primary.password}",
        f"  Role:     {primary.label}",
    ]
    if others:
        lines.append("  Other seeded logins (same password): " + ", ".join(a.email for a in others))
    return lines


def render_garage_block(info: GarageInfo) -> str:
    lines = [
        f"Garage: {info.name}",
        f"Garage ID: {info.garage_id}",
        f"Public slug: {info.slug}",
        "",
        "Customer booking:",
        f"{info.booking_url}",
        "",
        "Public API:",
        f"{info.public_api_url}",
        "",
        "Garage login:",
        f"{info.login_url}",
        "",
        "Seeded account:" if info.is_seeded else "Accounts:",
        *_account_lines(info),
    ]
    return "\n".join(lines)


def render_seed_result(info: GarageInfo, *, created: bool, counts: dict) -> str:
    slug_note = (
        "new randomised slug minted"
        if created
        else "existing garage reused - id and slug unchanged"
    )
    tally = "  ".join(f"{n} {label}" for label, n in counts.items())
    return "\n".join(
        [
            "",
            "Seeded garage ready",
            f"({slug_note})",
            "",
            render_garage_block(info),
            "",
            "Contents:",
            f"  {tally}",
        ]
    )


def render_dev_info(infos: list[GarageInfo]) -> str:
    if not infos:
        return (
            "No seeded garages in this database.\n"
            "Run  flask seed-dev  to create one (add --all to this command to "
            "list onboarded garages too)."
        )
    sep = "\n" + ("-" * 68) + "\n"
    return sep.join(render_garage_block(i) for i in infos)
