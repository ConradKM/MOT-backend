"""Standalone garage onboarding - the same operation as ``flask onboard-garage``.

Use this when you'd rather not go through the ``flask`` CLI. It loads ``.env``,
builds the app, and calls ``app.garages.onboarding`` inside one transaction.

    .venv/Scripts/python scripts/onboard_garage.py --file new_garage.json
    .venv/Scripts/python scripts/onboard_garage.py --file new_garage.json --dry-run
    .venv/Scripts/python scripts/onboard_garage.py \
        --name "Kingsway MOT & Service Centre" \
        --owner-email owner@kingswaymot.co.uk --owner-password 's3cret-pass'

The onboarding spec never contains a ``slug`` - it is generated (a readable
stem from the name plus a random suffix) and is immutable afterwards. See
``docs/GARAGE_ONBOARDING.md``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app import create_app
from app.garages.onboarding import (
    GarageSpec,
    OnboardingError,
    OwnerSpec,
    load_spec_file,
    onboard_garage,
    validate,
)


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Onboard a new garage tenant.")
    parser.add_argument(
        "--file",
        help="Path to a JSON (or YAML) onboarding spec. Must not contain a slug.",
    )
    parser.add_argument("--name", help="Garage display name (if not using --file).")
    parser.add_argument("--owner-email", help="First owner's login email.")
    parser.add_argument("--owner-password", help="First owner's initial password.")
    parser.add_argument("--owner-first-name", default=None)
    parser.add_argument("--owner-last-name", default=None)
    parser.add_argument(
        "--layout-variant",
        default=None,
        help="Optional registered layout variant key (default: the shared layout).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and exit without creating anything.",
    )
    return parser.parse_args(argv)


def _specs(args: argparse.Namespace) -> tuple[GarageSpec, OwnerSpec]:
    if args.file:
        return load_spec_file(args.file)

    missing = [
        flag
        for flag, value in (
            ("--name", args.name),
            ("--owner-email", args.owner_email),
            ("--owner-password", args.owner_password),
        )
        if not value
    ]
    if missing:
        raise OnboardingError(f"Provide --file, or all of: {', '.join(missing)}.")

    return (
        GarageSpec(name=args.name, layout_variant=args.layout_variant),
        OwnerSpec(
            email=args.owner_email,
            password=args.owner_password,
            first_name=args.owner_first_name,
            last_name=args.owner_last_name,
        ),
    )


def main(argv=None) -> int:
    args = _parse_args(argv)
    app = create_app()

    with app.app_context():
        try:
            garage_spec, owner_spec = _specs(args)

            if args.dry_run:
                validate(garage_spec, owner_spec)
                print(
                    f"OK (dry run): '{garage_spec.name}' with owner "
                    f"{owner_spec.email} would be onboarded. Nothing was written."
                )
                return 0

            result = onboard_garage(garage=garage_spec, owner=owner_spec)
        except OnboardingError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    print("Garage onboarded.")
    print(f"  id:     {result.garage.id}")
    print(f"  name:   {result.garage.name}")
    print(f"  slug:   {result.garage.slug}")
    print(f"  layout: {result.garage.layout_variant or 'default'}")
    print(f"  owner:  {result.owner.email} (OWNER)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
