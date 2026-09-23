"""Career-portal accounts the bot creates, and reading verification emails from Gmail.

Every account is saved in data/accounts.csv (site, email, password) so you always know
the passwords. Keep that file private.
"""
from __future__ import annotations

import csv
import email
import imaplib
import re
import secrets
import string
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

FIELDS = ["site", "ats", "email", "password", "created_at", "note"]


def make_password(length: int = 14) -> str:
    """Strong password that satisfies common portal rules (upper, lower, digit, special, no spaces)."""
    alphabet = string.ascii_letters + string.digits
    specials = "!@#$%&*"
    while True:
        core = [secrets.choice(alphabet) for _ in range(length - 4)]
        core += [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
                 secrets.choice(string.digits), secrets.choice(specials)]
        secrets.SystemRandom().shuffle(core)
        pw = "".join(core)
        if pw[0].isalpha():  # some portals dislike a leading symbol
            return pw


def site_key(url: str) -> str:
    """Account scope: Workday/Oracle accounts are per tenant host (cisco.wd5.myworkdayjobs.com)."""
    return urlparse(url).netloc.lower()


class AccountVault:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(FIELDS)

    def all(self) -> list[dict]:
        with open(self.path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def get(self, site: str) -> dict | None:
        return next((r for r in self.all() if r["site"] == site), None)

    def get_or_create(self, site: str, ats: str, email_addr: str, note: str = "") -> tuple[dict, bool]:
        """(account, is_new). A new account's password is saved BEFORE the site sees it."""
        acc = self.get(site)
        if acc:
            return acc, False
        acc = {"site": site, "ats": ats, "email": email_addr, "password": make_password(),
               "created_at": datetime.now().isoformat(timespec="seconds"), "note": note}
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, FIELDS).writerow(acc)
        return acc, True

    def set_note(self, site: str, note: str):
        rows = self.all()
        for r in rows:
            if r["site"] == site:
                r["note"] = note
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, FIELDS)
            w.writeheader()
            w.writerows(rows)


# ------------------------------------------------------------------ Gmail inbox
def _decode(value: str | None) -> str:
    return str(make_header(decode_header(value or "")))


def _body(msg) -> str:
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                parts.append(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace"))
            except (AttributeError, LookupError):
                pass
    return "\n".join(parts)


def extract_code(text: str) -> str | None:
    """Verification code (4-8 digits, or a 6-8 char alnum code next to the word 'code')."""
    text = re.sub(r"<[^>]+>", " ", text)
    m = re.search(r"(?:code|otp|passcode|pin)[^0-9A-Za-z]{0,40}?\b([0-9]{4,8})\b", text, re.I) \
        or re.search(r"\b([0-9]{6})\b", text) \
        or re.search(r"(?:code|otp)[^A-Za-z0-9]{0,20}\b([A-Z0-9]{6,8})\b", text)
    return m.group(1) if m else None


def extract_links(text: str) -> list[str]:
    links = re.findall(r"https?://[^\s\"'<>)]+", text)
    return [l.rstrip(".,;") for l in links
            if re.search(r"verif|activat|confirm|validate|token|register", l, re.I)]


class Inbox:
    """Reads recent Gmail messages over IMAP using a Google App Password."""

    def __init__(self, user: str, app_password: str):
        self.user, self.password = user, app_password

    @property
    def ready(self) -> bool:
        return bool(self.user and self.password)

    def check_login(self) -> bool:
        try:
            with imaplib.IMAP4_SSL("imap.gmail.com") as m:
                m.login(self.user, self.password)
            return True
        except (imaplib.IMAP4.error, OSError):
            return False

    def wait_for(self, since: datetime, match: str = "", timeout: int = 180, poll: int = 8) -> dict | None:
        """Newest email received after `since` whose sender/subject/body matches `match` (regex).
        Returns {subject, sender, body, code, links} or None."""
        if not self.ready:
            return None
        deadline = time.time() + timeout
        since_utc = since.astimezone(timezone.utc)
        while time.time() < deadline:
            try:
                with imaplib.IMAP4_SSL("imap.gmail.com") as m:
                    m.login(self.user, self.password)
                    for box in ("INBOX", "[Gmail]/Spam"):
                        if m.select(f'"{box}"', readonly=True)[0] != "OK":
                            continue
                        day = since_utc.strftime("%d-%b-%Y")
                        _, data = m.search(None, f'(SINCE "{day}")')
                        for num in reversed(data[0].split()[-30:]):
                            _, msg_data = m.fetch(num, "(RFC822)")
                            msg = email.message_from_bytes(msg_data[0][1])
                            try:
                                when = parsedate_to_datetime(msg["Date"]).astimezone(timezone.utc)
                            except (TypeError, ValueError):
                                continue
                            if when < since_utc:
                                continue
                            subject, sender, body = _decode(msg["Subject"]), _decode(msg["From"]), _body(msg)
                            if match and not re.search(match, f"{sender}\n{subject}\n{body}", re.I):
                                continue
                            return {"subject": subject, "sender": sender, "body": body,
                                    "code": extract_code(subject + "\n" + body), "links": extract_links(body)}
            except (imaplib.IMAP4.error, OSError):
                pass
            time.sleep(poll)
        return None
