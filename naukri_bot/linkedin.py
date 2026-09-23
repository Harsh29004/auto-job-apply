"""LinkedIn Easy Apply bot.

Searches LinkedIn jobs (Easy Apply filter), checks each job against the same title / experience /
skill filters as the Naukri bot, and fills the Easy Apply dialog step by step using the
shared form filler (company.py) and recruiter-question answers (answers.py).
"""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime
from urllib.parse import urlencode

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

from .company import SCAN_JS, CompanyApplier
from .config import Config
from .filters import evaluate
from .models import Job
from .storage import Storage

log = logging.getLogger("linkedin")

BASE = "https://www.linkedin.com"
MODAL = ".jobs-easy-apply-modal, div[data-test-modal][role=dialog], .artdeco-modal[role=dialog]"
SENT_TEXT = re.compile(r"application (was )?sent|you applied|applied \d|your application was sent", re.I)
LIMIT_TEXT = re.compile(r"(reached|exceeded) (the|your)? ?(daily )?(easy apply|application) limit|"
                        r"limit daily submissions|try again tomorrow", re.I)

CARDS_JS = r"""
() => [...document.querySelectorAll('li[data-occludable-job-id], div[data-job-id]')].map(li => {
  const id = li.getAttribute('data-occludable-job-id') || li.getAttribute('data-job-id');
  const a = li.querySelector("a[href*='/jobs/view/']");
  const title = a ? ((a.querySelector('strong') || a.querySelector('span[aria-hidden=true]') || a).innerText || '').trim() : '';
  const sub = li.querySelector('.artdeco-entity-lockup__subtitle, .job-card-container__primary-description');
  const cap = li.querySelector('.artdeco-entity-lockup__caption, .job-card-container__metadata-wrapper');
  return {id, title: title.split('\n')[0], company: sub ? sub.innerText.trim() : '',
          location: cap ? cap.innerText.trim().split('\n')[0] : '',
          applied: /\bApplied\b/.test(li.innerText || '')};
}).filter(c => c.id)
"""


# Job description: the block under the "About the job" heading.
ABOUT_JOB_JS = r"""
() => {
  const old = document.querySelector('#job-details, .jobs-description__content');
  if (old && old.innerText.length > 100) return old.innerText;
  const h = [...document.querySelectorAll('h1, h2, h3')].find(e => /^about the job$/i.test((e.innerText || '').trim()));
  if (!h) return '';
  let p = h.parentElement;
  for (let i = 0; i < 4 && p; i++, p = p.parentElement) if ((p.innerText || '').length > 300) return p.innerText;
  return '';
}
"""


def search_url(keyword: str, location: str, levels: list[str], date_posted: str, start: int = 0,
               work_types: list[str] | None = None) -> str:
    params = {"keywords": keyword, "location": location, "f_AL": "true", "start": start}
    if levels:
        params["f_E"] = ",".join(levels)
    if work_types:
        params["f_WT"] = ",".join(work_types)  # 1 on-site, 2 remote, 3 hybrid
    if date_posted:
        params["f_TPR"] = date_posted
    return f"{BASE}/jobs/search/?{urlencode(params)}"


def min_years_required(text: str) -> int:
    """Smallest 'N+ years' / 'N-M years' of experience mentioned in a job description (0 if none)."""
    found = []
    for m in re.finditer(r"(\d{1,2})\s*(?:\+|-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years?|yrs?)", text, re.I):
        tail = text[m.end():m.end() + 40].lower()
        head = text[max(0, m.start() - 40):m.start()].lower()
        if "experience" in tail or "experience" in head or "exp" in tail:
            found.append(int(m.group(1)))
    return min(found) if found else 0


class LinkedInQuota(Exception):
    pass


class LinkedInBot:
    def __init__(self, cfg: Config, db: Storage, dry_run: bool = False, headless: bool = False):
        self.cfg = cfg
        self.li = cfg.linkedin
        self.db = db
        self.dry_run = dry_run
        self.headless = headless
        # reuse the company-site form filler (answers, dropdowns, resume upload ...)
        self.forms = CompanyApplier(cfg, db, submit=True, headless=headless)
        self.debug_dir = cfg.data_dir / "linkedin_debug"
        self.debug_dir.mkdir(exist_ok=True)
        self._pw = self.ctx = self.page = None

    # ------------------------------------------------------------ lifecycle
    def __enter__(self):
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(self.cfg.root / "browser_profile_linkedin"), headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"], viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        )
        self.ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.ctx.set_default_timeout(15000)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.forms.page = self.page
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    def pause(self, lo: float = 6, hi: float = 14):
        time.sleep(random.uniform(lo, hi))

    def snapshot(self, name: str) -> str:
        path = self.debug_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:60]}.png"
        try:
            self.page.screenshot(path=str(path))
        except PWError:
            pass
        return str(path)

    def visible(self, sel: str, root=None) -> bool:
        loc = (root or self.page).locator(sel)
        try:
            return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 5)))
        except PWError:
            return False

    def text(self, sel: str = "body") -> str:
        try:
            return self.page.locator(sel).first.inner_text(timeout=4000)
        except PWError:
            return ""

    # ---------------------------------------------------------------- login
    def logged_in(self) -> bool:
        self.page.goto(f"{BASE}/feed/", wait_until="domcontentloaded")
        self.page.wait_for_timeout(3000)
        return not re.search(r"/login|/authwall|/checkpoint|/uas/|signup", self.page.url)

    def login(self, wait_seconds: int = 240) -> bool:
        if self.logged_in():
            log.info("Already logged in to LinkedIn (saved session).")
            return True
        page = self.page
        page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        user = page.locator("#username, input[type=email], input[name=session_key]").locator("visible=true")
        pwd = page.locator("#password, input[type=password], input[name=session_password]").locator("visible=true")
        if self.li["email"] and self.li["password"] and user.count() and pwd.count():
            user.first.fill(self.li["email"])
            pwd.first.fill(self.li["password"])
            page.wait_for_timeout(400)
            # exact "Sign in" - the page also has "Sign in with Microsoft / Apple / Google"
            btn = page.get_by_role("button", name=re.compile(r"^\s*sign in\s*$", re.I)).locator("visible=true")
            if btn.count():
                btn.first.click()
            else:
                pwd.first.press("Enter")
            log.info("Logging in to LinkedIn as %s ...", self.li["email"])
        else:
            log.warning("LINKEDIN_EMAIL / LINKEDIN_PASSWORD missing - log in manually in the browser window.")
        deadline, warned = time.time() + wait_seconds, False
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            if re.search(r"/feed|/jobs|/in/|/mynetwork", page.url):
                log.info("LinkedIn login OK.")
                return True
            if not warned and re.search(r"checkpoint|challenge|verification|captcha", page.url + self.text(), re.I):
                log.warning("LinkedIn wants a verification (code / captcha) - complete it in the browser (%ss).",
                            wait_seconds)
                warned = True
            if re.search(r"wrong password|couldn.t find|incorrect", self.text(), re.I):
                log.error("LinkedIn says the email/password is wrong.")
                return False
        self.snapshot("login_timeout")
        return False

    # --------------------------------------------------------------- search
    def search(self, keyword: str, location: str, page_no: int) -> list[dict]:
        url = search_url(keyword, location, self.li["experience_levels"], self.li["date_posted"], page_no * 25,
                         self.li.get("work_types"))
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(4000)
        # the result list is lazy - scroll every card into view
        for _ in range(8):
            cards = self.page.locator("li[data-occludable-job-id]")
            n = cards.count()
            if not n:
                break
            try:
                cards.nth(n - 1).scroll_into_view_if_needed(timeout=3000)
            except PWError:
                break
            self.page.wait_for_timeout(700)
            if cards.count() == n:
                break
        found = self.page.evaluate(CARDS_JS)
        log.info("Search '%s' in %s page %d -> %d jobs", keyword, location, page_no + 1, len(found))
        return found

    def job_details(self, card: dict) -> Job | None:
        url = f"{BASE}/jobs/view/{card['id']}/"
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(3000)
        desc = self.page.evaluate(ABOUT_JOB_JS) or ""
        # page title is "<job title> | <company> | LinkedIn" (class names on the page are obfuscated)
        parts = [x.strip() for x in self.page.title().split("|")]
        title = parts[0] if len(parts) >= 3 and parts[0] else card["title"]
        if not card["company"] and len(parts) >= 3:
            card["company"] = parts[1]
        return Job(job_id=f"li-{card['id']}", title=title, company=card["company"], url=url,
                   description=desc, location=card["location"], min_exp=min_years_required(desc))

    def easy_apply_button(self):
        btns = self.page.locator("a[aria-label*='Easy Apply'], button[aria-label*='Easy Apply'], "
                                 "button.jobs-apply-button, .jobs-apply-button--top-card button")
        for i in range(btns.count()):
            b = btns.nth(i)
            try:
                if b.is_visible() and re.search(r"easy apply", b.inner_text(timeout=1000), re.I):
                    return b
            except PWError:
                continue
        return None

    # ---------------------------------------------------------------- apply
    # The Easy Apply dialog lives in a shadow root, so <body>.innerText can't see it.
    # Playwright text locators pierce shadow DOM - always use these for state checks.
    def has_text(self, pattern: re.Pattern) -> bool:
        loc = self.page.get_by_text(pattern)
        try:
            return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 5)))
        except PWError:
            return False

    def submitted(self) -> bool:
        return self.has_text(SENT_TEXT) or self.has_text(re.compile(r"^\s*Application submitted\s*$", re.I))

    def wait_submitted(self, seconds: int = 12) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.submitted():
                return True
            self.page.wait_for_timeout(1000)
        return False

    def apply(self, job: Job) -> tuple[str, str]:
        if self.has_text(re.compile(r"Application submitted|Applied \d+ \w+ ago", re.I)):
            return "already_applied", ""
        btn = self.easy_apply_button()
        if btn is None:
            return "external", "no Easy Apply (company site)"
        btn.click()
        try:
            self.page.locator(".jobs-easy-apply-modal").first.wait_for(state="visible", timeout=10000)
        except PWError:
            if self.has_text(LIMIT_TEXT):
                raise LinkedInQuota("Easy Apply limit")
            self.snapshot(f"no_modal_{job.job_id}")
            return "error", "Easy Apply dialog did not open"
        return self.fill_modal(job)

    def modal_button(self, *labels: str):
        modal = self.page.locator(MODAL).first
        for label in labels:
            b = modal.locator(f"button[aria-label='{label}']")
            if b.count() and b.first.is_visible():
                return b.first
        for b in modal.locator("footer button, button.artdeco-button--primary").all():
            try:
                t = b.inner_text(timeout=800).strip().lower()
                if b.is_visible() and any(l.lower().split(" ")[0] in t for l in labels):
                    return b
            except PWError:
                continue
        return None

    def errors(self) -> list[str]:
        modal = self.page.locator(MODAL).first
        try:
            return [t.strip() for t in modal.locator(
                ".artdeco-inline-feedback--error, [data-test-form-element-error-messages]").all_inner_texts() if t.strip()]
        except PWError:
            return []

    def fill_modal(self, job: Job) -> tuple[str, str]:
        page = self.page
        self.forms.answerer.job_location = job.location
        last_progress, stuck = None, 0
        for step in range(14):
            page.wait_for_timeout(1200)
            if self.submitted():
                self.close_dialogs()
                return "applied", "Easy Apply"
            if not self.visible(".jobs-easy-apply-modal"):
                break
            modal_text = self.text(".jobs-easy-apply-modal")
            progress = re.search(r"(\d+)\s*%", modal_text)
            progress = progress.group(1) if progress else modal_text[:80]

            resume_chosen = self.visible(".jobs-document-upload-redesign-card__container--selected, "
                                         "input[type=radio][id*=jobsDocumentCardToggle]:checked", page.locator(MODAL).first)
            fields = page.main_frame.evaluate(SCAN_JS, ".jobs-easy-apply-modal")
            if resume_chosen:
                fields = [f for f in fields if f["type"] != "file"]
            missing = self.forms.fill(page.main_frame, fields, job.title, job.company)
            missing = self.forms.still_missing(page.main_frame, fields, missing)
            if missing:
                log.info("   unanswered: %s", missing)
                for m in missing:
                    self.db.record_unanswered(m, [], job.job_id)

            submit = self.modal_button("Submit application")
            if submit:
                if self.dry_run:
                    self.discard()
                    return "planned", "reached Submit (dry run, discarded)"
                follow = page.locator("label[for='follow-company-checkbox']")
                if follow.count() and page.locator("#follow-company-checkbox").is_checked():
                    follow.first.click()  # don't auto-follow every company
                submit.click()
                if self.wait_submitted():
                    self.close_dialogs()
                    return "applied", "Easy Apply"
                # Submit was clicked but no confirmation seen: count it as sent, never retry it
                shot = self.snapshot(f"unconfirmed_{job.job_id}")
                self.close_dialogs()
                return "unconfirmed", f"clicked Submit, confirmation not seen ({shot})"
            nxt = self.modal_button("Review your application", "Continue to next step", "Next", "Review")
            if nxt is None:
                if self.submitted():
                    self.close_dialogs()
                    return "applied", "Easy Apply"
                shot = self.snapshot(f"no_next_{job.job_id}")
                self.discard()
                return "error", f"no Next/Submit button ({shot})"
            nxt.click()
            page.wait_for_timeout(1800)
            errs = self.errors()
            if progress == last_progress or errs:
                stuck += 1
                if stuck >= 2:
                    shot = self.snapshot(f"stuck_{job.job_id}")
                    self.discard()
                    detail = "; ".join(errs or missing or ["stuck"])[:200]
                    return "needs_answer", f"{detail} ({shot})"
            else:
                stuck = 0
            last_progress = progress

        if self.wait_submitted(5):
            self.close_dialogs()
            return "applied", "Easy Apply"
        if self.has_text(LIMIT_TEXT):
            raise LinkedInQuota("Easy Apply limit")
        shot = self.snapshot(f"unclear_{job.job_id}")
        self.discard()
        return "error", f"no confirmation ({shot})"

    def close_dialogs(self):
        for sel in ("button[aria-label='Dismiss']", "button[aria-label='Done']"):
            b = self.page.locator(sel)
            if b.count() and b.first.is_visible():
                try:
                    b.first.click()
                except PWError:
                    pass
        # post-apply upsell ("Not now / Update profile") would block the next job's Easy Apply
        for name in ("Not now", "Done"):
            b = self.page.get_by_role("button", name=name, exact=True)
            try:
                if b.count() and b.first.is_visible():
                    b.first.click()
            except PWError:
                pass
        self.page.wait_for_timeout(800)

    def discard(self):
        """Close the Easy Apply dialog without saving."""
        self.close_dialogs()
        for sel in ("button[data-control-name='discard_application_confirm_btn']",
                    "button[data-test-dialog-secondary-btn]"):
            b = self.page.locator(sel)
            if b.count() and b.first.is_visible():
                b.first.click()
                break
        else:
            b = self.page.get_by_role("button", name=re.compile(r"^discard$", re.I))
            if b.count():
                b.first.click()
        self.page.wait_for_timeout(1000)

    # ------------------------------------------------------------------ run
    def run(self, limit: int | None = None) -> dict:
        limit = limit or self.li["max_applies"]
        if not self.login():
            raise SystemExit("LinkedIn login failed - fill LINKEDIN_EMAIL / LINKEDIN_PASSWORD in .env "
                             "or log in manually in the opened browser.")
        stats: dict[str, int] = {}
        done = 0
        seen_titles = set()
        for kw in self.li["keywords"]:
            for loc in self.li["locations"]:
                for page_no in range(self.li["pages_per_search"]):
                    cards = self.search(kw, loc, page_no)
                    for card in cards:
                        if done >= limit:
                            return stats
                        job_id = f"li-{card['id']}"
                        if card["applied"] or self.db.is_done(job_id) or self.db.status(job_id):
                            continue
                        key = (card["title"].lower(), card["company"].lower())
                        if key in seen_titles:
                            continue
                        seen_titles.add(key)
                        # quick title check before opening the job
                        pre = Job(job_id=job_id, title=card["title"], company=card["company"],
                                  url="x")
                        ok, reason, _ = evaluate(pre, self.cfg.filters, self.cfg.skills)
                        if not ok and not reason.startswith("low skill"):
                            self.db.record(pre, "skipped", reason)
                            continue
                        job = self.job_details(card)
                        ok, reason, score = evaluate(job, self.cfg.filters, self.cfg.skills)
                        if ok and re.search(r"\bunpaid\b|no stipend|without stipend", job.description, re.I):
                            ok, reason = False, "unpaid (description)"
                        if not ok:
                            self.db.record(job, "skipped", reason, score)
                            continue
                        log.info("%s [%d] %s @ %s (%s)", "CHECK" if self.dry_run else "APPLY", score,
                                 job.title, job.company, job.location)
                        try:
                            status, detail = self.apply(job)
                        except LinkedInQuota as e:
                            log.error("LinkedIn Easy Apply limit reached: %s", e)
                            return stats
                        except PWError as e:
                            status, detail = "error", str(e).splitlines()[0][:150]
                            self.snapshot(f"exception_{job.job_id}")
                            self.discard()
                        if not self.dry_run or status != "planned":
                            self.db.record(job, status, detail or reason, score)
                        stats[status] = stats.get(status, 0) + 1
                        log.info(" -> %s %s", status.upper(), detail)
                        if status in ("applied", "planned", "unconfirmed"):
                            done += 1
                        self.pause()
                    if len(cards) < 20:
                        break
        return stats
