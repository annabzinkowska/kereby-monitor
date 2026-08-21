# Spec: kereby.dk flat monitor

## Goal

Build a Python tool that monitors `https://kereby.dk/bolig/` and emails me when a
flat becomes available. It must catch two events I care about:

1. A brand-new flat listing appears.
2. A previously reserved flat becomes available again (a reservation fell
   through). This is the important one and must not be missed within the polling
   interval.

Deployment target is GitHub Actions on a schedule, with the state file committed
back to the repo between runs. Build for that target specifically.

## Confirmed facts about the page

These were verified by inspecting the live page. Build against them; do not
re-derive them.

- The page is server-rendered. `data-card-id` appears in the raw HTML source, so
  a plain HTTP GET with `requests` is sufficient. No headless browser needed.
- Listings come from a third-party "jorato" catalog widget. Each flat is an
  `<article class="jorato-case-card" data-card-id="...">`.
- Available flats have `data-state="available"`.
- Reserved flats stay on the page. They do NOT disappear. They get an extra
  class `jorato-case-card--unavailable` and `data-state="reserved"`, plus an
  overlay div `jorato-case-card__inactive-overlay`.
- Each card carries these data attributes I want to surface in the email:
  `data-card-id`, `data-state`, `data-zip`, `data-rooms`, `data-rent`,
  `data-size`, `data-req` (feature tags like "Altan", "Penthouse|Tagterrasse",
  "Delevenlig"), `data-lat`, `data-lng`.
- The listing link is an inner `<a class="jorato-case-card__link" href="...">`
  pointing to the flat's detail page, e.g.
  `https://kereby.dk/bolig/sortedam-dossering-45-5-tv-2200-kobenhavn-n/`.
- The results header reads "N resultater" (17 at time of inspection). Useful as a
  sanity check but not required for logic.

## Detection logic

Keep a JSON snapshot of every card seen last run, keyed by `data-card-id`, each
storing at minimum its `state` and detail fields.

On each run, fetch the page, parse the current cards, then compare each current
card against the previous snapshot:

- **NEW**: `data-card-id` not present in the previous snapshot AND current state
  is `available`. Email it.
- **REPOST**: card was `reserved` in the previous snapshot AND is `available`
  now. Email it, clearly labelled as a repost / reservation fell through.
- **JUST RESERVED**: was `available`, now `reserved`. Log to stdout only, do not
  email (not actionable for me).
- No change: nothing.

Then overwrite the snapshot with the current state and persist it.

## Edge cases (must handle)

1. **First run / no prior state.** Record the current listings as the baseline
   and send NO email. Otherwise the first run would email all ~17 flats.
2. **Zero listings parsed.** If parsing returns zero cards (markup changed, page
   down, or an unexpected block), do NOT overwrite the state file, print a
   warning, and exit non-zero. Overwriting with an empty snapshot would flood
   false alerts on the next good run.
3. **Missing data attributes.** Any individual `data-*` field may be absent on a
   given card. Default missing fields to empty string; never crash. Only
   `data-card-id` is required for a card to be tracked; skip cards without it.
4. **Repost blind spot (document, don't fix).** A reserve-then-cancel that
   happens entirely between two runs is invisible, because the card looks
   unchanged across snapshots. Add a code comment noting this. Shrinking the
   polling interval reduces but does not eliminate the window.

## Email

- Send via SMTP using STARTTLS. Read all config from environment variables:
  `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`.
- Default `EMAIL_TO` to `SMTP_USER` if not set.
- If credentials are missing, do not crash: print the email that would have been
  sent to stdout instead. This makes local testing painless.
- One email per run covering all new + reposted flats, not one email per flat.
- Subject: `[kereby] N flat(s) available` where N is new + reposted count.
- Body: two sections, "NEW listings" and "REPOSTED (reservation fell through)".
  Per flat show: address (derived from the URL slug, title-cased), any feature
  tag from `data-req`, then rooms / size (m2) / rent (kr) / zip, then the full
  URL. Plain text is fine; no HTML email needed.

## Configuration and modes

- `DEBUG=1` env var: parse and print every listing with id, state, address, and
  rent, then exit without diffing, emailing, or writing state. For verifying the
  parser against the live page.
- State file path: `listings_state.json` in the repo root.
- Target URL and state filename should be module-level constants, easy to change.

## Deliverables

1. `monitor.py` — the script. Standard library plus `requests` and
   `beautifulsoup4` only. No other dependencies.
2. `requirements.txt` — `requests` and `beautifulsoup4`.
3. `.github/workflows/monitor.yml` — GitHub Actions workflow that:
   - Runs on a `*/5 * * * *` cron and on `workflow_dispatch`.
   - Uses a `concurrency` group so runs cannot overlap and corrupt the state
     file. Do not cancel in-progress runs.
   - Sets `permissions: contents: write` so it can commit state back.
   - Checks out the repo, sets up Python 3.12, installs requirements, runs
     `monitor.py` with the five SMTP secrets injected from `secrets.*`.
   - After the run, commits `listings_state.json` back if it changed, using a
     `[skip ci]` commit message and a guard so an unchanged file produces no
     commit. Push it.
4. `README.md` — setup steps: create the repo, run once locally to generate the
   baseline state and commit it, add the five Actions secrets, note that Gmail
   needs a 16-char App Password (not the account password) with 2FA on, and how
   to trigger a manual run to test.

## Constraints and style

- Python 3.12. Keep it a single readable file, functions not classes unless a
  class clearly earns its place.
- Isolate all HTML parsing in one `parse_listings(html)` function so future
  markup changes touch one place only.
- Fail safe over fail loud on state: never destroy a good snapshot on a bad run.
- No em dashes anywhere in code comments, output strings, or the README.
- Do not add features beyond this spec (no database, no web UI, no Telegram, no
  price filtering) unless I ask. A later iteration may add a filter for zip or
  rent, so keep the per-flat data available in the snapshot to make that easy.

## Acceptance criteria

- `DEBUG=1 python monitor.py` prints the current flats with correct states and
  does not write state or send email.
- A first real run with no existing state file writes the baseline and sends no
  email.
- Simulating a `reserved -> available` change in the state file (hand-edit, then
  run) produces exactly one repost email and no duplicate on the following run.
- A run that parses zero cards leaves the existing state file untouched and exits
  non-zero.
- The workflow runs green on `workflow_dispatch` and commits state only when it
  changed.