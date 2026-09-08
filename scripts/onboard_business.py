"""Idempotent business onboarding from a JSON/YAML spec.

    # 1. Offline - validate a spec with no database and no network:
    python scripts/onboard_business.py onboarding/revive-n-drive.json --validate

    # 2. Online - see what would happen, write nothing:
    python scripts/onboard_business.py onboarding/revive-n-drive.json --dry-run

    # 3. Online - create it (prints a one-time temporary password):
    python scripts/onboard_business.py onboarding/revive-n-drive.json

    # Re-issue a temp password for an already-onboarded owner:
    python scripts/onboard_business.py onboarding/revive-n-drive.json --reset-password

Idempotent: rerunning the same spec does nothing once the owner's account
exists (matched on the globally-unique owner email) - no duplicate business,
owner or ids. No password is ever read from or written to a spec file; a
strong temporary one is generated here, printed once, and must be changed by
the owner on first login.

See docs/BUSINESS_ONBOARDING.md for the full runbook (offline -> online,
verification, editing, deactivation).
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The public booking origin the printed URL should use. Matches the frontend's
# VITE_BOOKING_BASE_URL default (src/lib/bookingUrl.ts).
BOOKING_BASE_URL = "https://app.comaz.co.uk"

_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _generate_temp_password() -> str:
    """A strong URL-safe temporary password (~16 chars)."""
    return secrets.token_urlsafe(12)


def _load_raw(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml  # optional dependency

        return yaml.safe_load(text)
    return json.loads(text)


def _print_hours(spec) -> None:
    if spec.opening_hours is None:
        print("  opening hours   default Mon-Fri 09:00-17:00 (edit in Settings > Availability)")
        return
    for idx in range(7):
        if idx not in spec.opening_hours:
            continue
        value = spec.opening_hours[idx]
        label = _WEEKDAY_LABELS[idx]
        print(f"  {label:<15} {'closed' if value is None else f'{value[0]}-{value[1]}'}")


def _print_result(spec, result, *, base_url: str) -> None:
    g = result.garage
    booking_url = f"{base_url.rstrip('/')}/book/{g.id}"
    print()
    if result.created:
        print("Business onboarded.")
    else:
        print("Business already onboarded - nothing was written (idempotent).")
    print("-" * 66)
    print(f"  name            {g.name}")
    print(f"  business id     {g.id}")
    print(f"  public slug     {g.slug}")
    print(f"  booking URL     {booking_url}")
    print(f"  owner login     {result.owner.email}")
    if result.temp_password:
        print(f"  TEMP PASSWORD   {result.temp_password}")
        print()
        print("  ^ Store this in your password manager and give it to the owner.")
        print("    It is shown ONCE and is not saved anywhere. The owner MUST change")
        print("    it on first login (Login > Forgot password, or Settings once in).")
    print(
        f"  services        {len(result.services)}: " + ", ".join(s.name for s in result.services)
    )
    _print_hours(spec)
    if spec.notes:
        print(f"  notes           {spec.notes}")
    print("-" * 66)
    if not result.created:
        print("  Re-run with --reset-password to issue a fresh temporary password.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Onboard a business from a spec file.")
    parser.add_argument("spec", help="Path to the JSON (or YAML) business spec.")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Parse and validate the spec only. No database, no network - safe offline.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check idempotency and report what would be created. Writes nothing.",
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="If the business already exists, set a new temporary owner password.",
    )
    parser.add_argument(
        "--booking-base-url",
        default=BOOKING_BASE_URL,
        help=f"Origin for the printed booking URL (default: {BOOKING_BASE_URL}).",
    )
    args = parser.parse_args(argv)

    path = Path(args.spec)
    if not path.is_file():
        print(f"error: no such spec file: {path}", file=sys.stderr)
        return 2

    from app.garages.business_onboarding import BusinessSpecError, parse_business_spec

    try:
        spec = parse_business_spec(_load_raw(path))
    except (BusinessSpecError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.validate:
        print(f"OK: '{spec.name}' spec is valid.")
        print(f"  owner login     {spec.owner_email}")
        print(
            f"  services        {len(spec.services)}: " + ", ".join(s.name for s in spec.services)
        )
        _print_hours(spec)
        print("  (offline check - nothing was created; run without --validate when online)")
        return 0

    from app import create_app
    from app.extensions import db
    from app.garages.business_onboarding import (
        find_existing_business,
        onboard_business,
        reset_owner_password,
    )

    app = create_app()
    with app.app_context():
        print(f"database: {app.config['SQLALCHEMY_DATABASE_URI'].split('@')[-1]}")
        existing = find_existing_business(spec.owner_email)

        if args.reset_password:
            if existing is None:
                print(
                    f"error: no business onboarded for {spec.owner_email} yet - "
                    "run without --reset-password to create it.",
                    file=sys.stderr,
                )
                return 1
            owner, _ = existing
            new_password = _generate_temp_password()
            reset_owner_password(owner, new_password)
            print(f"\nNew temporary password for {owner.email}:\n  {new_password}")
            print("  Shown once. The owner must change it on first login.")
            return 0

        if args.dry_run:
            if existing is not None:
                print(f"\nDRY RUN: '{spec.name}' is already onboarded - would do nothing.")
            else:
                print(
                    f"\nDRY RUN: would onboard '{spec.name}' with owner {spec.owner_email}, "
                    f"{len(spec.services)} service(s), and "
                    + ("default" if spec.opening_hours is None else "custom")
                    + " opening hours. Nothing written."
                )
            db.session.rollback()
            return 0

        try:
            result = onboard_business(spec, temp_password=_generate_temp_password())
        except BusinessSpecError as exc:
            db.session.rollback()
            print(f"error: {exc}", file=sys.stderr)
            return 1

        _print_result(spec, result, base_url=args.booking_base_url)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
