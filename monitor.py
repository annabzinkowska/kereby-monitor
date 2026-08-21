#!/usr/bin/env python3
"""Monitor kereby.dk for available flats and email when one shows up.

Two events matter:
  1. A brand new listing appears as available.
  2. A flat that was reserved becomes available again (reservation fell
     through).

State lives in a JSON snapshot keyed by data-card-id so the diff survives
between GitHub Actions runs.
"""

import json
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://kereby.dk/bolig/"
STATE_FILE = Path(__file__).resolve().parent / "listings_state.json"
REQUEST_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (compatible; kereby-monitor/1.0)"

# Fields lifted straight off each card's data attributes. Everything is kept in
# the snapshot so a later iteration can filter on zip or rent without needing a
# fresh baseline.
CARD_FIELDS = ("state", "zip", "rooms", "rent", "size", "req", "lat", "lng")


def fetch_html(url=URL):
    """Fetch the listing page. Server rendered, so a plain GET is enough."""
    response = requests.get(
        url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
    )
    response.raise_for_status()
    return response.text


def parse_listings(html):
    """Return {card_id: {...fields...}} for every card in the page.

    All markup knowledge lives here. If kereby or the jorato widget changes
    their HTML, this is the only function that needs touching.
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = {}

    for card in soup.select("article.jorato-case-card"):
        card_id = (card.get("data-card-id") or "").strip()
        if not card_id:
            # Without an id we cannot track the card across runs, so skip it.
            continue

        entry = {}
        for field in CARD_FIELDS:
            entry[field] = (card.get("data-" + field) or "").strip()

        # Only available cards are wrapped in an <a>. Reserved and completed
        # cards use a plain <div> with the same class, so there is no href.
        link = card.select_one("a.jorato-case-card__link")
        href = (link.get("href") or "").strip() if link else ""
        entry["url"] = requests.compat.urljoin(URL, href) if href else ""

        # The location element is present on every card regardless of state, so
        # prefer it and fall back to the URL slug only if it is missing.
        location = card.select_one(".jorato-case-card__location-text")
        entry["address"] = (
            location.get_text(strip=True) if location else address_from_url(entry["url"])
        )

        listings[card_id] = entry

    return listings


def parse_result_total(html):
    """Return the flat count from the "N resultater" header, or None.

    The jorato widget renders at most `data-jorato-limit` cards (24 at time of
    writing) into the HTML, but this header always reports the true total. If
    the total ever exceeds the number of cards we parsed, listings are being
    truncated and a new flat could be missed, so the caller warns about it.
    """
    match = re.search(r"(\d[\d.]*)\s*resultater", html)
    if not match:
        return None
    try:
        return int(match.group(1).replace(".", ""))
    except ValueError:
        return None


def address_from_url(url):
    """Derive a readable address from the detail page URL slug."""
    slug = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
    if not slug:
        return ""
    return slug.replace("-", " ").title()


def load_state(path=STATE_FILE):
    """Return the previous snapshot, or None when there is no usable state."""
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        print("Warning: could not read state file (%s). Treating as first run." % exc)
        return None
    if not isinstance(data, dict):
        print("Warning: state file is not an object. Treating as first run.")
        return None
    return data


def save_state(listings, path=STATE_FILE):
    with path.open("w", encoding="utf-8") as handle:
        json.dump(listings, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")


def diff_listings(previous, current):
    """Split current listings into new, reposted, and just unavailable.

    The page uses three states: available, reserved, and completed. Anything
    that is not available counts as unavailable, so a flat coming back from
    either reserved or completed is reported as a repost.

    Known blind spot: a flat that is reserved and then released again entirely
    between two runs looks unchanged in both snapshots, so it is never
    reported. A shorter polling interval narrows that window but cannot close
    it, because detection only ever compares two discrete points in time.
    """
    new, reposted, just_reserved = [], [], []

    for card_id, entry in current.items():
        was = previous.get(card_id)
        state = entry.get("state", "")

        if was is None:
            if state == "available":
                new.append((card_id, entry))
            continue

        old_state = was.get("state", "")
        if old_state != "available" and state == "available":
            reposted.append((card_id, entry))
        elif old_state == "available" and state != "available":
            just_reserved.append((card_id, entry))

    return new, reposted, just_reserved


def format_flat(entry):
    lines = []
    lines.append(entry.get("address") or "(unknown address)")

    req = entry.get("req", "")
    if req:
        lines.append("  Features: " + req.replace("|", ", "))

    details = []
    if entry.get("rooms"):
        details.append("%s rooms" % entry["rooms"])
    if entry.get("size"):
        details.append("%s m2" % entry["size"])
    if entry.get("rent"):
        details.append("%s kr" % entry["rent"])
    if entry.get("zip"):
        details.append(entry["zip"])
    if details:
        lines.append("  " + " / ".join(details))

    if entry.get("url"):
        lines.append("  " + entry["url"])

    return "\n".join(lines)


def build_email_body(new, reposted, truncation_note=""):
    sections = []

    if truncation_note:
        sections.append("WARNING: " + truncation_note)

    if new:
        block = ["NEW listings", "=" * 40]
        block.extend(format_flat(entry) for _, entry in new)
        sections.append("\n\n".join(block))

    if reposted:
        block = ["REPOSTED (reservation fell through)", "=" * 40]
        block.extend(format_flat(entry) for _, entry in reposted)
        sections.append("\n\n".join(block))

    sections.append("Source: " + URL)
    return "\n\n\n".join(sections) + "\n"


def send_email(subject, body):
    """Send via SMTP STARTTLS, or print to stdout when config is missing."""
    host = os.environ.get("SMTP_HOST", "")
    port = os.environ.get("SMTP_PORT", "")
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    recipient = os.environ.get("EMAIL_TO") or user

    if not all([host, port, user, password, recipient]):
        print("SMTP config incomplete. Email that would have been sent:")
        print("-" * 60)
        print("To: " + (recipient or "(unset)"))
        print("Subject: " + subject)
        print()
        print(body)
        print("-" * 60)
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = recipient
    message.set_content(body)

    with smtplib.SMTP(host, int(port), timeout=REQUEST_TIMEOUT) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(message)

    print("Email sent to %s: %s" % (recipient, subject))


def run_debug(listings):
    print("Parsed %d listing(s):" % len(listings))
    for card_id, entry in sorted(listings.items()):
        print(
            "  %-12s %-10s %-45s %s kr"
            % (
                card_id,
                entry.get("state", "") or "-",
                entry.get("address", "") or "-",
                entry.get("rent", "") or "-",
            )
        )
    print("\nDEBUG mode: no diff, no email, no state written.")


def main():
    try:
        html = fetch_html()
    except requests.RequestException as exc:
        print("Error: could not fetch %s (%s)" % (URL, exc))
        return 1

    listings = parse_listings(html)
    total = parse_result_total(html)

    truncation_note = ""
    if total is not None and total > len(listings):
        truncation_note = (
            "the page reports %d flats but only %d were rendered into the HTML. "
            "The catalog widget caps server side rendering, so %d listing(s) are "
            "invisible to this monitor and a new flat there would be missed."
            % (total, len(listings), total - len(listings))
        )
        print("Warning: " + truncation_note)

    if os.environ.get("DEBUG") == "1":
        run_debug(listings)
        if total is not None:
            print("Results header reports %d flat(s)." % total)
        return 0

    if not listings:
        # Never overwrite a good snapshot with an empty one, otherwise the next
        # successful run would report every flat as new.
        print("Warning: parsed zero listings. State file left untouched.")
        return 1

    previous = load_state()

    if previous is None:
        save_state(listings)
        print(
            "First run: recorded %d listing(s) as baseline. No email sent."
            % len(listings)
        )
        return 0

    new, reposted, just_reserved = diff_listings(previous, listings)

    for _, entry in just_reserved:
        print(
            "No longer available (%s): %s"
            % (
                entry.get("state") or "unknown",
                entry.get("address") or entry.get("url") or "?",
            )
        )

    if new or reposted:
        count = len(new) + len(reposted)
        subject = "[kereby] %d flat(s) available" % count
        send_email(subject, build_email_body(new, reposted, truncation_note))
    elif truncation_note:
        send_email(
            "[kereby] monitor may be missing listings",
            "WARNING: " + truncation_note + "\n\nSource: " + URL + "\n",
        )
    else:
        print("No new or reposted flats. Tracking %d listing(s)." % len(listings))

    save_state(listings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
