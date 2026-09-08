# onboarding/

One JSON file per real business, consumed by `scripts/onboard_business.py`.

**These files never contain a password** — a strong temporary one is generated
by the tool at creation time and printed once. They are safe to commit.

See [`../docs/BUSINESS_ONBOARDING.md`](../docs/BUSINESS_ONBOARDING.md) for the
spec format and the full offline → online (Render Shell) runbook.

| File | Business | Notes |
| --- | --- | --- |
| `test-online.json` | CoMaz Online Test | **throwaway** — for smoke-testing `app.comaz.co.uk`; hard-delete when done |
| `revive-n-drive.json` | Revive N Drive | real owner email; opening hours TBC (default kept) |
| `tints-on-demand.json` | Tints on Demand | **temporary** owner email `@admin.com` — replace before go-live; owner name TBC |
| `mot-doctors.json` | M.O.T Doctors | **temporary** owner email `@admin.com` — replace before go-live; standard MOT service set |
