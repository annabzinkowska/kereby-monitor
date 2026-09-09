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
from datetime import datetime, time, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = "https://kereby.dk/bolig/"
STATE_FILE = Path(__file__).resolve().parent / "listings_state.json"
REQUEST_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (compatible; kereby-monitor/1.0)"
TIME_ZONE = ZoneInfo("Europe/Copenhagen")

# Requested search area and monthly-rent ceiling. The postal-code
# mapping deliberately excludes adjacent areas such as Sydhavn, Valby and
# Amager, even when their city label starts with "København".
TARGET_AREAS = frozenset(
    ("Frederiksberg", "København K", "Nørrebro", "Østerbro", "Vesterbro")
)
MAX_MONTHLY_RENT = 20000
FREDERIKSBERG_ZIPS = frozenset(
    (
        "1800",
        "1810",
        "1820",
        "1850",
        "1860",
        "1870",
        "1900",
        "1920",
        "1950",
        "1960",
        "2000",
    )
)

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


def listing_area(entry):
    """Return the requested Kereby area represented by a listing, if any."""
    zip_code = re.sub(r"\D", "", entry.get("zip", ""))
    if zip_code in FREDERIKSBERG_ZIPS:
        return "Frederiksberg"
    if zip_code == "2200":
        return "Nørrebro"
    if zip_code == "2100":
        return "Østerbro"
    try:
        zip_number = int(zip_code)
    except ValueError:
        return ""
    if 1050 <= zip_number <= 1473:
        return "København K"
    if 1500 <= zip_number <= 1799:
        return "Vesterbro"
    return ""


def monthly_rent(entry):
    """Return a listing's numeric monthly rent, or None if it is unusable."""
    digits = re.sub(r"\D", "", entry.get("rent", ""))
    try:
        return int(digits) if digits else None
    except ValueError:
        return None


def matches_booking_preferences(entry):
    """Whether this available listing meets the agreed area and rent rules."""
    rent = monthly_rent(entry)
    return (
        listing_area(entry) in TARGET_AREAS
        and rent is not None
        and rent <= MAX_MONTHLY_RENT
    )


def booking_settings_from_env():
    """Read opt-in viewing-request settings without ever storing PII in git.

    The automation is deliberately disabled unless every required value is
    present. In particular, the privacy acceptance and screening confirmations
    must be set by the applicant, rather than assumed by this program.
    """
    if os.environ.get("AUTO_BOOK_VIEWINGS") != "1":
        return None

    required = ("BOOKING_NAME", "BOOKING_EMAIL", "BOOKING_PHONE")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        print("Automatic booking disabled: missing " + ", ".join(missing) + ".")
        return None
    if os.environ.get("BOOKING_PRIVACY_ACCEPTED") != "1":
        print("Automatic booking disabled: BOOKING_PRIVACY_ACCEPTED=1 is required.")
        return None

    phone = re.sub(r"\D", "", os.environ["BOOKING_PHONE"])
    if phone.startswith("0045"):
        phone = phone[4:]
    elif phone.startswith("45") and len(phone) == 10:
        phone = phone[2:]
    if len(phone) != 8:
        print("Automatic booking disabled: BOOKING_PHONE must be a Danish phone number.")
        return None

    confirmations = {}
    if os.environ.get("BOOKING_CONFIRM_RKI_NOT_REGISTERED") == "1":
        confirmations["rki_not_present"] = True
    if os.environ.get("BOOKING_CONFIRM_NO_PETS") == "1":
        confirmations["no_pet"] = True
    if os.environ.get("BOOKING_CONFIRM_TENANCY_TAKEOVER_BY_DATE") == "1":
        confirmations["tenancy_takeover_by_date"] = True

    return {
        "name": os.environ["BOOKING_NAME"].strip(),
        "email": os.environ["BOOKING_EMAIL"].strip(),
        "phone": phone,
        "screening_confirmations": confirmations,
    }


def extract_booking_config(html):
    """Extract the JSON configuration used by Kereby's own booking modal."""
    match = re.search(
        r"var\s+joratoTemplatesCaseDetail\s*=\s*(\{.*?\});\s*\n//",
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError(
            "Kereby booking configuration was not found on the detail page"
        )
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("Kereby booking configuration was not valid JSON") from exc


def first_booking_slot(time_slots, now=None):
    """Mirror Kereby's calendar: first weekday at least two days away, after 14:00."""
    allowed_times = []
    for value in time_slots:
        try:
            parsed = time.fromisoformat(str(value))
        except ValueError:
            continue
        if parsed >= time(14, 0):
            allowed_times.append((parsed, str(value)))
    if not allowed_times:
        raise ValueError("Kereby offered no viewing time at or after 14:00")

    local_now = now or datetime.now(TIME_ZONE)
    day = local_now.date() + timedelta(days=2)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    _, slot_time = min(allowed_times)
    return day, slot_time


def screening_question_text(row):
    """Return a human-readable screening question, including its value."""
    question = row.get("question", row) if isinstance(row, dict) else {}
    if not isinstance(question, dict):
        return "an unnamed screening question"

    template = (
        question.get("questionTemplateDanish")
        or question.get("questionTemplateEnglish")
        or question.get("id")
        or "an unnamed screening question"
    )
    # The API keeps the template on the question definition and supplies its
    # case-specific value alongside it.  Substitute it before including the
    # question in an email, so a literal {Parameter} is never shown.
    parameter = row.get("parameter", "") if isinstance(row, dict) else ""
    if parameter:
        return str(template).replace("{Parameter}", str(parameter))
    return str(template)


def screening_answers(questions, confirmations):
    """Build required true answers, or report questions the applicant must answer."""
    answers, missing = [], []
    for row in questions:
        question = row.get("question", row) if isinstance(row, dict) else {}
        question_id = question.get("id", "")
        if confirmations.get(question_id) is not True:
            missing.append(screening_question_text(row))
            continue
        answers.append({"questionId": question_id, "answer": True})
    return answers, missing


def booking_request(entry, settings, session=None, now=None):
    """Submit one Kereby viewing request and return a result safe for email logs.

    The site's public modal uses this same WordPress endpoint. A fresh detail
    page is fetched each time so the short-lived nonce and case id are current.
    Unknown screening questions are never guessed or submitted.
    """
    if not entry.get("url"):
        return False, "listing has no detail-page URL", None

    client = session or requests.Session()
    client.headers.update({"User-Agent": USER_AGENT})
    try:
        detail = client.get(entry["url"], timeout=REQUEST_TIMEOUT)
        detail.raise_for_status()
        config = extract_booking_config(detail.text)
        context = config.get("bookingContext") or {}
        endpoint = config.get("bookingRestUrl")
        case_id = context.get("caseId")
        screening_url = config.get("bookingScreeningUrl")
        if not endpoint or not case_id:
            return False, "booking is not available for this listing", None

        headers = {"Accept": "application/json"}
        if config.get("restNonce"):
            headers["X-WP-Nonce"] = config["restNonce"]
        questions = []
        if screening_url:
            screening = client.get(
                screening_url,
                params={"case_id": case_id},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            screening.raise_for_status()
            payload = screening.json()
            questions = payload.get("questions", payload) if isinstance(payload, dict) else payload
            if not isinstance(questions, list):
                return False, "could not read the required screening questions", None

        answers, missing = screening_answers(questions, settings["screening_confirmations"])
        if missing:
            return False, "needs your confirmation: " + " | ".join(missing), None

        day, slot_time = first_booking_slot(config.get("timeSlots") or [], now=now)
        starts_at = datetime.combine(day, time.fromisoformat(slot_time), TIME_ZONE)
        starts_at_utc = starts_at.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "case_id": case_id,
            "name": settings["name"],
            "email": settings["email"],
            "phoneNumber": settings["phone"],
            "phoneExtension": "45",
            "note": "",
            "communicationLanguage": "danish",
            "startsAt": starts_at_utc,
            "booking_time": "%s, kl. %s" % (day.isoformat(), slot_time),
            "message": "Jeg vil gerne komme til en fremvisning den %s kl. %s."
            % (day.isoformat(), slot_time),
            "screeningAnswers": answers,
            "website": "",
        }
        headers["Content-Type"] = "application/json"
        response = client.post(
            endpoint, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
        )
        data = response.json()
        if not response.ok or not data.get("ok"):
            return False, data.get("message", "Kereby rejected the viewing request"), None
        return (
            True,
            "requested for %s at %s" % (day.isoformat(), slot_time),
            starts_at_utc,
        )
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        return False, "booking request failed: %s" % exc, None


def preserve_booking_records(previous, current):
    """Keep successful booking records when replacing the listing snapshot."""
    for card_id, entry in current.items():
        old = previous.get(card_id, {})
        if "booking" in old:
            entry["booking"] = old["booking"]


def auto_book_viewings(candidates, settings):
    """Request viewings for qualifying actionable flats once, and record success."""
    results = []
    if settings is None:
        return results
    for card_id, entry in candidates:
        if not matches_booking_preferences(entry):
            continue
        booking = entry.get("booking", {})
        if booking.get("status") == "requested":
            continue
        success, detail, starts_at = booking_request(entry, settings)
        if success:
            entry["booking"] = {
                "status": "requested",
                "requested_at": datetime.now(TIME_ZONE).isoformat(),
                "starts_at": starts_at,
            }
            print("Viewing requested: %s (%s)" % (entry.get("address", "?"), detail))
            results.append((entry, True, detail))
        else:
            print("Viewing not requested: %s (%s)" % (entry.get("address", "?"), detail))
            results.append((entry, False, detail))
    return results


def build_email_body(new, reposted, truncation_note="", booking_results=None):
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

    for entry, success, detail in booking_results or []:
        label = "VIEWING REQUESTED" if success else "VIEWING NOT REQUESTED"
        sections.append(
            label + "\n" + "=" * 40 + "\n" + format_flat(entry) + "\n  " + detail
        )

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

    preserve_booking_records(previous, listings)
    new, reposted, just_reserved = diff_listings(previous, listings)
    booking_results = auto_book_viewings(new + reposted, booking_settings_from_env())

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
        send_email(
            subject, build_email_body(new, reposted, truncation_note, booking_results)
        )
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
