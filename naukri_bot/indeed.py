"""Indeed apply bot.

Searches one or more Indeed country sites, checks each job against the same title / experience / skill
filters as the other bots, and applies to "Easily apply" (Indeed Apply) jobs step by step with the shared
form filler (company.py) and recruiter-question answers (answers.py). Jobs that apply on the company's own
site are queued for company_apply.py.
"""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import Frame, Page, sync_playwright

from .accounts import Inbox
from .answers import Answerer
from .company import SCAN_JS, CompanyApplier
from .config import Config
from .filters import evaluate, min_years_required
from .foreign import abroad_restriction, location_open
from .models import Job
from .storage import Storage

log = logging.getLogger("indeed")

LOGIN_URL = "https://secure.indeed.com/auth"
ACCOUNT_URL = "https://myjobs.indeed.com/applied"  # needs a login: sends you to secure.indeed.com when logged out
# the apply form lives on smartapply.indeed.com - in a new tab, the same tab or an iframe
FORM_URL = re.compile(r"smartapply\.indeed\.com|indeedapply|indeed\.com/m/apply", re.I)
SENT_TEXT = re.compile(r"(your )?application (has been|was) (submitted|sent)|you('ve| have) applied to", re.I)
HUMAN_CHECK = re.compile(r"just a moment|verify you are (a )?human|additional verification required|"
                         r"checking your browser|press (and|&) hold", re.I)
SUBMIT_BTN = re.compile(r"^\s*submit( your)?( application)?\s*$", re.I)
NEXT_BTN = re.compile(r"^\s*(continue|next|review( your application)?|continue to review|save and continue|proceed|"
                      r"continue applying|apply anyway)\s*$", re.I)
RESUME_STEP = re.compile(r"resume|\bcv\b", re.I)
EXPERIENCE_STEP = re.compile(r"relevant experience|past job|work[- ]experience|recent job", re.I)
# the country of each Indeed site, added to job locations that name only a city ("Pune")
DOMAIN_COUNTRY = {"in": "India", "www": "United States", "uk": "United Kingdom", "ca": "Canada",
                  "au": "Australia", "de": "Germany", "sg": "Singapore", "ae": "UAE", "ie": "Ireland",
                  "nl": "Netherlands", "fr": "France", "nz": "New Zealand"}

CARDS_JS = r"""
() => {
  const out = [], seen = new Set();
  for (const el of document.querySelectorAll('[data-jk]')) {
    const jk = el.getAttribute('data-jk');
    if (!jk || seen.has(jk)) continue;
    const card = el.closest('.job_seen_beacon, .cardOutline, .result, li') || el;
    const t = s => ((card.querySelector(s) || {}).innerText || '').replace(/\s+/g, ' ').trim();
    const h = card.querySelector('h2.jobTitle [title], h2 a span[title], h2 [id^="jobTitle"]') || card.querySelector('h2');
    const title = ((h && (h.getAttribute('title') || h.innerText)) || '').replace(/\s+/g, ' ').replace(/^new\s+/i, '').trim();
    if (!title) continue;
    seen.add(jk);
    out.push({jk, title, company: t('[data-testid="company-name"], .companyName'),
              location: t('[data-testid="text-location"], .companyLocation'),
              easy: /easily apply/i.test(card.innerText || '')});
  }
  return out;
}
"""

JOB_JS = r"""
() => {
  const t = s => { const e = document.querySelector(s); return e ? (e.innerText || '').trim() : ''; };
  return {title: t('[data-testid="jobsearch-JobInfoHeader-title"], h1.jobsearch-JobInfoHeader-title, h1'),
          company: t('[data-testid="inlineHeader-companyName"], [data-company-name], .jobsearch-CompanyInfoContainer a'),
          location: t('[data-testid="inlineHeader-companyLocation"], [data-testid="job-location"], #jobLocationText'),
          description: t('#jobDescriptionText, .jobsearch-jobDescriptionText')};
}
"""

# Which apply button the job page shows. Marks it with data-nb-apply so Python can click it.
APPLY_JS = r"""
() => {
  document.querySelectorAll('[data-nb-apply]').forEach(e => e.removeAttribute('data-nb-apply'));
  const vis = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
  const text = e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim();
  const aria = e => e.getAttribute('aria-label') || '';
  const href = e => (e.closest('a') || e).href || e.getAttribute('href') || '';
  const mark = (e, kind) => { e.setAttribute('data-nb-apply', kind); return {kind, text: text(e), href: href(e)}; };
  const applied = e => /^applied\b|^you applied|^application submitted$/i.test(text(e));
  const ia = [...document.querySelectorAll('#indeedApplyButton, button[id*="indeedApply"], ' +
              '.jobsearch-IndeedApplyButton-newDesign, .ia-IndeedApplyButton, [data-testid*="indeedApply"]')].filter(vis);
  if (ia.length) return applied(ia[0]) ? {kind: 'applied', text: text(ia[0]), href: ''} : mark(ia[0], 'indeed');
  const cands = [...document.querySelectorAll('a, button, [role=button]')].filter(vis);
  for (const e of cands) {
    const l = text(e) + ' ' + aria(e);
    if ((/apply/i.test(l) && /company|employer|new tab|new window/i.test(l)) ||
        (/^apply( now)?$/i.test(text(e)) && /applystart|\/rc\/clk|\/pagead\/clk/.test(href(e))))
      return mark(e, 'external');
  }
  const done = cands.find(applied);
  if (done) return {kind: 'applied', text: text(done), href: ''};
  const any = cands.find(e => /^(apply now|easily apply|apply)$/i.test(text(e)));
  return any ? mark(any, 'indeed') : {kind: '', text: '', href: ''};
}
"""

HEADING_JS = r"""
() => { const h = [...document.querySelectorAll('h1, h2')].find(e => e.offsetWidth || e.offsetHeight);
        return h ? h.innerText.replace(/\s+/g, ' ').trim().slice(0, 120) : ''; }
"""


class IndeedBlocked(Exception):
    """Indeed shows a bot check nobody solved - stop instead of hammering it."""


def search_url(domain: str, keyword: str, location: str = "", days: int = 7, start: int = 0,
               easy_apply_only: bool = False) -> str:
    """Indeed search. Location "Remote" uses Indeed's remote filter; "Anywhere" searches the whole country."""
    remote = location.strip().lower() in ("remote", "work from home", "wfh")
    anywhere = location.strip().lower() in ("", "anywhere", "all", "*")
    params = {"q": keyword, "l": "" if remote or anywhere else location}
    if days:
        params["fromage"] = days
    if start:
        params["start"] = start
    if easy_apply_only:
        params["iafilter"] = 1  # "Easily apply"
    if remote:
        params["sc"] = "0kf:attr(DSQF7);"  # "Remote"
    return f"https://{domain}/jobs?{urlencode(params)}"


def with_country(location: str, domain: str) -> str:
    """'Pune, Maharashtra' on in.indeed.com -> 'Pune, Maharashtra, India' (visa / phone answers need the country)."""
    country = DOMAIN_COUNTRY.get(domain.split(".")[0])
    if not country or Answerer.COUNTRY_RE.search(location or "") or re.search(rf"\b{re.escape(country)}\b",
                                                                                 location or "", re.I):
        return location
    return f"{location}, {country}" if location else country


class IndeedBot:
    HOME = re.compile(r"(^|\.)indeed\.com$", re.I)
    STEP_WAIT_MS = 1500  # pause before reading each apply step

    def __init__(self, cfg: Config, db: Storage, dry_run: bool = False, headless: bool = False,
                 profile_dir: Path | None = None):
        self.cfg = cfg
        self.opt = cfg.indeed
        self.db = db
        self.dry_run = dry_run
        self.headless = headless
        self.profile_dir = Path(profile_dir) if profile_dir else cfg.root / "browser_profile_indeed"
        # the company-site form filler also fills Indeed's apply steps (answers, dropdowns, resume upload).
        # submit=False on a dry run: a preview never uploads your resume.
        self.forms = CompanyApplier(cfg, db, submit=not dry_run, headless=headless)
        self.forms.filler.source = "indeed"
        self.queue = Storage(cfg.data_dir / "naukri.db")  # external_jobs -> company_apply.py
        self.debug_dir = cfg.data_dir / "indeed_debug"
        self.debug_dir.mkdir(exist_ok=True)
        self._pw = self.ctx = self.page = None

    # ------------------------------------------------------------ lifecycle
    def __enter__(self):
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"], viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        )
        self.ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.ctx.set_default_timeout(15000)
        # "Leave site?" when moving on from a half-done application: leave. Other dialogs: dismiss.
        self.ctx.on("page", self._watch_dialogs)
        for p in self.ctx.pages:
            self._watch_dialogs(p)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.forms.page = self.page
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
        finally:
            self._pw.stop()
            self.queue.close()

    @staticmethod
    def _watch_dialogs(page: Page):
        page.on("dialog", lambda d: d.accept() if d.type == "beforeunload" else d.dismiss())

    def pause(self, lo: float = 8, hi: float = 16):
        time.sleep(random.uniform(lo, hi))

    def snapshot(self, name: str, page: Page | None = None) -> str:
        """Screenshot + HTML of the page (the HTML shows which selectors changed when Indeed updates its site)."""
        page = page or self.page
        base = self.debug_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:60]}"
        try:
            page.screenshot(path=str(base) + ".png")
            base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
        except PWError:
            pass
        return str(base) + ".png"

    def at_home(self, url: str) -> bool:
        return bool(self.HOME.search(urlparse(url or "").hostname or ""))

    def reset_tabs(self):
        """Back to one tab (the apply form may have opened another)."""
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        for p in self.ctx.pages[1:]:
            try:
                p.close()
            except PWError:
                pass
        self.forms.page = self.page

    # ------------------------------------------------------------ bot checks
    def human_check(self, page: Page | None = None) -> bool:
        page = page or self.page
        try:
            text = page.title() + "\n" + page.inner_text("body", timeout=3000)[:1500]
        except PWError:
            return False
        return bool(HUMAN_CHECK.search(text))

    def pass_human_check(self, page: Page | None = None, seconds: int = 180) -> bool:
        """True when no bot check is shown, or you solved it in the browser within `seconds`."""
        page = page or self.page
        if not self.human_check(page):
            return True
        if self.headless:
            log.warning("Indeed shows a bot check - run without --headless and solve it in the browser.")
            return False
        log.warning("Indeed shows a bot check - solve it in the browser window (waiting %ss).", seconds)
        deadline = time.time() + seconds
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            if not self.human_check(page):
                return True
        return False

    # ---------------------------------------------------------------- login
    def logged_in(self) -> bool:
        try:
            self.page.goto(ACCOUNT_URL, wait_until="domcontentloaded")
            self.page.wait_for_timeout(2500)
        except PWError:
            return False
        host = urlparse(self.page.url).hostname or ""
        return self.at_home(self.page.url) and host != "secure.indeed.com" and not self.human_check()

    def login(self, wait_seconds: int = 300) -> bool:
        if self.logged_in():
            log.info("Already logged in to Indeed (saved session).")
            return True
        page, email = self.page, self.opt.get("email") or ""
        started = datetime.now() - timedelta(minutes=1)
        try:
            page.goto(f"{LOGIN_URL}?hl=en&continue={quote(ACCOUNT_URL, safe='')}", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            box = page.locator("input[type=email], input[name='__email']").locator("visible=true")
            if email and box.count():
                box.first.fill(email)
                box.first.press("Enter")
                log.info("Logging in to Indeed as %s ...", email)
        except PWError:
            pass
        if not email:
            log.warning("INDEED_EMAIL missing in .env - log in manually in the browser window.")
        log.warning("Finish the Indeed sign-in in the browser (password, emailed code or captcha) - waiting up to %ss.",
                    wait_seconds)
        # Indeed usually emails a sign-in code: read it from Gmail when INDEED_EMAIL is the SMTP_EMAIL mailbox
        same_box = email and email.lower() == (self.cfg.smtp_email or "").lower()
        inbox = Inbox(self.cfg.smtp_email, self.cfg.smtp_password) if same_box else None
        deadline, typed_password, typed_code = time.time() + wait_seconds, False, False
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            if self.at_home(page.url) and (urlparse(page.url).hostname or "") != "secure.indeed.com" \
                    and not self.human_check(page):
                log.info("Indeed login OK.")
                return True
            try:
                pw = page.locator("input[type=password]").locator("visible=true")
                if self.opt.get("password") and not typed_password and pw.count():
                    pw.first.fill(self.opt["password"])
                    pw.first.press("Enter")
                    typed_password = True
                    continue
                code = page.locator("input[autocomplete='one-time-code'], input[name*='passcode' i], "
                                    "input[name*='verification' i]").locator("visible=true")
                if inbox and inbox.ready and not typed_code and code.count():
                    typed_code = True
                    log.info("   waiting for Indeed's sign-in code in %s ...", email)
                    mail = inbox.wait_for(started, match=r"indeed", timeout=120)
                    if mail and mail.get("code"):
                        code.first.fill(mail["code"])
                        code.first.press("Enter")
                        log.info("   sign-in code entered from your email")
            except PWError:
                pass
        self.snapshot("login_timeout")
        return False

    # --------------------------------------------------------------- search
    def search(self, domain: str, keyword: str, location: str, page_no: int) -> list[dict]:
        url = search_url(domain, keyword, location, self.opt.get("days", 7), page_no * 10,
                         self.opt.get("easy_apply_only", False))
        try:
            self.page.goto(url, wait_until="domcontentloaded")
            self.page.wait_for_timeout(3000)
        except PWError as e:
            log.warning("Search failed for '%s' %s: %s", keyword, location, str(e).splitlines()[0][:100])
            return []
        if not self.pass_human_check():
            raise IndeedBlocked("bot check on the search page")
        cards = self.page.evaluate(CARDS_JS)
        log.info("Search '%s'%s on %s page %d -> %d jobs", keyword, f" in {location}" if location else "", domain,
                 page_no + 1, len(cards))
        return cards

    def job_details(self, card: dict, domain: str) -> Job:
        url = f"https://{domain}/viewjob?jk={card['jk']}"
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2500)
        if not self.pass_human_check():
            raise IndeedBlocked("bot check on the job page")
        info = self.page.evaluate(JOB_JS) or {}
        first = lambda v: (v or "").split("\n")[0].strip()  # noqa: E731
        title = re.sub(r"\s*-\s*job post$", "", first(info.get("title")), flags=re.I) or card["title"]
        desc = info.get("description") or ""
        return Job(job_id=f"indeed-{card['jk']}", title=title, company=first(info.get("company")) or card["company"],
                   url=url, description=desc, min_exp=min_years_required(desc),
                   location=with_country(first(info.get("location")) or card["location"], domain))

    # ---------------------------------------------------------------- apply
    def apply(self, job: Job) -> tuple[str, str]:
        """Apply from the job page that is open in self.page. Returns (status, detail)."""
        info = self.page.evaluate(APPLY_JS)
        kind = info.get("kind")
        if kind == "applied":
            return "already_applied", info.get("text", "")
        if kind == "external":
            url = self.resolve_external(info.get("href") or "") or self.click_external()
            return "external", url or "company-site link not found"
        if kind != "indeed":
            body = self.page.inner_text("body", timeout=5000) if self.page.url.startswith("http") else ""
            if re.search(r"no longer (available|accepting)|this job has expired|job (has been|was) removed", body, re.I):
                return "expired", ""
            return "error", f"apply button not found ({self.snapshot(f'no_apply_btn_{job.job_id}')})"
        job_url, before = self.page.url, len(self.ctx.pages)
        self.page.locator("[data-nb-apply='indeed']").first.click()
        where, target = self.open_form(job_url, before)
        if where == "external":
            return "external", target
        if where is None:
            return "error", f"Indeed Apply form did not open ({self.snapshot(f'no_form_{job.job_id}')})"
        return self.fill_steps(job, target)

    def resolve_external(self, href: str) -> str:
        """Company-site URL behind Indeed's redirect link (applystart / rc/clk)."""
        if not href.startswith("http"):
            return ""
        if not self.at_home(href):
            return href
        p = self.ctx.new_page()
        try:
            p.goto(href, wait_until="domcontentloaded", timeout=30000)
            for _ in range(15):
                if p.url.startswith("http") and not self.at_home(p.url):
                    return p.url
                p.wait_for_timeout(1000)
        except PWError:
            pass
        finally:
            p.close()
        return href  # still Indeed's redirect link - company_apply.py follows it

    def click_external(self) -> str:
        """The company-site button has no link: click it and read the tab it opens."""
        before = len(self.ctx.pages)
        try:
            self.page.locator("[data-nb-apply='external']").first.click()
            for _ in range(15):
                self.page.wait_for_timeout(1000)
                for p in self.ctx.pages[before:]:
                    if p.url.startswith("http") and not self.at_home(p.url):
                        return p.url
        except PWError:
            pass
        return ""

    def open_form(self, job_url: str, before: int) -> tuple[str | None, Frame | str | None]:
        """After clicking Indeed Apply: ('form', frame), ('external', url) or (None, None)."""
        for _ in range(20):
            self.page.wait_for_timeout(1000)
            for p in [*self.ctx.pages[before:], self.page]:
                new, url = p is not self.page, p.url or ""
                if new and url.startswith("http") and not self.at_home(url):
                    return "external", url
                try:
                    frame = next((fr for fr in p.frames if FORM_URL.search(fr.url or "")), None)
                except PWError:
                    continue
                if frame is None and self.at_home(url) and (new or url.split("#")[0] != job_url.split("#")[0]):
                    frame = p.main_frame if self.step_button(p.main_frame)[0] else None
                if frame is not None:
                    return "form", frame
        return None, None

    def step_button(self, frame: Frame):
        """('submit', button) on the last step, ('next', button) before it, else (None, None)."""
        found = {}
        try:
            loc = frame.locator("button, input[type=submit], [role=button]")
            for i in range(min(loc.count(), 80)):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                t = (el.inner_text(timeout=500) or el.get_attribute("value") or "").strip()
                kind = "submit" if SUBMIT_BTN.match(t) else "next" if NEXT_BTN.match(t) else None
                if kind:
                    found.setdefault(kind, el)
        except PWError:
            pass
        kind = "submit" if "submit" in found else "next" if "next" in found else None
        return kind, found.get(kind)

    def sent(self, page: Page) -> bool:
        for fr in page.frames:
            try:
                if "post-apply" in (fr.url or "") or SENT_TEXT.search(fr.inner_text("body", timeout=2000)):
                    return True
            except PWError:
                continue
        return False

    def wait_sent(self, page: Page, seconds: int = 15) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.sent(page):
                return True
            page.wait_for_timeout(1000)
        return False

    @staticmethod
    def step_key(frame: Frame) -> str | None:
        """Which step the form is on: its URL and heading (None while it loads)."""
        try:
            return f"{frame.url} {frame.evaluate(HEADING_JS)}"
        except PWError:
            return None

    @staticmethod
    def errors(frame: Frame) -> list[str]:
        try:
            texts = frame.locator("[role=alert], [id*='error' i]").all_inner_texts()
        except PWError:
            return []
        return list(dict.fromkeys(t.strip() for t in texts if t.strip() and len(t.strip()) < 200))[:5]

    def step_fields(self, frame: Frame, fields: list[dict], step: str) -> list[dict]:
        """Indeed's own steps. Resume: keep the resume you chose on Indeed (or upload yours when none is);
        never tick resume cards as if they were questions. 'Relevant experience': your current job."""
        if RESUME_STEP.search(step):
            fields = [f for f in fields if f["type"] not in ("radio", "checkbox")]
            chosen = frame.locator("input[type=radio]:checked, [role=radio][aria-checked=true]").count() > 0
            if chosen:
                return [f for f in fields if f["type"] != "file"]
            can_upload = any(f["type"] == "file" for f in fields) and self.forms.resume and self.forms.resume.exists()
            cards = frame.locator("input[type=radio]")
            if not can_upload and cards.count():
                self.forms._check(cards.first)  # the resume already saved on Indeed
                log.info("   resume: the one saved on Indeed")
            return fields
        if EXPERIENCE_STEP.search(step):
            p = self.cfg.profile
            for f in fields:
                if f["tag"] != "input" or f["type"] not in ("text", "input") or f.get("value"):
                    continue
                label = (f.get("label") or f.get("name") or "").lower()
                value = p.get("current_designation") if re.search(r"title|role|designation", label) else \
                    p.get("current_company") if re.search(r"company|employer", label) else None
                if value:
                    frame.locator(f"[data-nb-idx='{f['idx']}']").fill(str(value))
                    f["value"] = str(value)
                    log.info("   %s = %s", label[:50], value)
        return fields

    def fill_steps(self, job: Job, frame: Frame) -> tuple[str, str]:
        page = frame.page
        self.forms.answerer.job_location = job.location
        self.forms.resume = self.forms.resumes.pick(job.title, job.description) or self.forms.default_resume
        last, stuck, missing = None, 0, []
        for _ in range(15):
            page.wait_for_timeout(self.STEP_WAIT_MS)
            frame = next((fr for fr in page.frames if FORM_URL.search(fr.url or "")), page.main_frame)
            if self.sent(page):
                return "applied", "Indeed Apply"
            if not self.pass_human_check(page):
                return "manual", f"Indeed bot check - finish this one yourself ({self.snapshot(f'captcha_{job.job_id}', page)})"
            step = self.step_key(frame)
            if step is None:
                continue  # the step is loading
            if step == last:
                stuck += 1
                if stuck >= 2:
                    detail = "; ".join(self.errors(frame) or missing or ["stuck"])[:200]
                    return "needs_answer", f"{detail} ({self.snapshot(f'stuck_{job.job_id}', page)})"
            else:
                stuck = 0
            last = step
            try:
                fields = self.step_fields(frame, frame.evaluate(SCAN_JS, None), step)
                missing = self.forms.still_missing(frame, fields, self.forms.fill(frame, fields, job.title, job.company))
            except PWError as e:  # the page moved on while we read it
                log.info("   step changed while filling: %s", str(e).splitlines()[0][:80])
                continue
            if missing:
                log.info("   unanswered: %s", missing)
                for m in missing:
                    self.db.record_unanswered(m, [], job.job_id)
            kind, btn = self.step_button(frame)
            if kind == "submit":
                if self.dry_run:
                    return "planned", "reached Submit (dry run, not sent)"
                btn.click()
                if self.wait_sent(page):
                    return "applied", "Indeed Apply"
                # Submit was clicked: count it as sent, never retry it
                return "unconfirmed", f"clicked Submit, confirmation not seen ({self.snapshot(f'unconfirmed_{job.job_id}', page)})"
            if kind is None:
                if self.sent(page):
                    return "applied", "Indeed Apply"
                return "error", f"no Continue/Submit button ({self.snapshot(f'no_next_{job.job_id}', page)})"
            btn.click()
            for _ in range(12):  # wait for the next step (or an error) before reading the page again
                page.wait_for_timeout(500)
                if self.errors(frame) or self.step_key(frame) != step:
                    break
        return "error", f"too many steps ({self.snapshot(f'too_many_steps_{job.job_id}', page)})"

    # ------------------------------------------------------------------ run
    def run(self, limit: int | None = None) -> dict:
        limit = limit or self.opt["max_applies"]
        if not self.dry_run:
            # several runs a day must not add up to a flagged account
            sent_today = self.db.applied_today(("applied", "unconfirmed"))
            left_today = int(self.opt.get("max_applies_per_day", 30)) - sent_today
            if left_today <= 0:
                log.warning("Indeed daily cap reached (%d sent today). Run again tomorrow.", sent_today)
                return {}
            limit = min(limit, left_today)
        if not self.login():
            raise SystemExit("Indeed login failed - run `python main.py indeed --login` and sign in in the browser.")
        stats: dict[str, int] = {}
        done, seen_ids, seen_titles = 0, set(), set()
        for domain in self.opt["domains"]:
            # your own country's site: every location you set. Other countries: remote jobs only (on-site
            # jobs there need a local work permit)
            home = DOMAIN_COUNTRY.get(domain.split(".")[0], "").lower() == str(
                self.cfg.profile.get("country") or "India").lower()
            for kw in self.opt["keywords"]:
                for loc in (self.opt["locations"] or [""]) if home else ["Remote"]:
                    for page_no in range(self.opt["pages_per_search"]):
                        try:
                            cards, fresh = self.search(domain, kw, loc, page_no), 0
                        except IndeedBlocked as e:
                            log.error("Indeed %s - stopping. Run again later (or without --headless and "
                                      "solve it in the browser).", e)
                            return stats
                        for card in cards:
                            if done >= limit:
                                return stats
                            job_id = f"indeed-{card['jk']}"
                            if job_id in seen_ids or self.db.status(job_id):
                                continue
                            seen_ids.add(job_id)
                            fresh += 1
                            key = (card["title"].lower(), card["company"].lower())
                            if key in seen_titles:  # same posting listed for several cities
                                continue
                            seen_titles.add(key)
                            # quick title check before opening the job
                            pre = Job(job_id=job_id, title=card["title"], company=card["company"], url="x")
                            ok, reason, _ = evaluate(pre, self.cfg.filters, self.cfg.skills)
                            if not ok and not reason.startswith("low skill"):
                                self.db.record(pre, "skipped", reason)
                                continue
                            try:
                                status, detail, score = self.process(card, domain)
                            except IndeedBlocked as e:
                                log.error("Indeed %s - stopping. Run again later (or without --headless and "
                                          "solve it in the browser).", e)
                                return stats
                            stats[status] = stats.get(status, 0) + 1
                            if status in ("applied", "planned", "unconfirmed"):
                                done += 1
                            if status not in ("skipped",):
                                self.pause()
                        if not cards or not fresh:
                            break
                        time.sleep(random.uniform(2, 5))
        return stats

    def process(self, card: dict, domain: str) -> tuple[str, str, int]:
        """Open one job, filter it on its full description, apply or queue it. Returns (status, detail, score)."""
        self.reset_tabs()
        try:
            job = self.job_details(card, domain)
        except PWError as e:
            log.info("   could not open job %s: %s", card["jk"], str(e).splitlines()[0][:100])
            return "error", "job page did not open", 0
        ok, reason, score = evaluate(job, self.cfg.filters, self.cfg.skills)
        if ok and re.search(r"\bunpaid\b|no stipend|without stipend", job.description, re.I):
            ok, reason = False, "unpaid (description)"
        countries = self.cfg.foreign.get("countries") or ["india"]
        if ok and not location_open(job.location, countries):
            # job abroad: skip when the title or ad requires local work rights you don't have
            why = abroad_restriction(job.title, job.description, countries)
            if why:
                ok, reason = False, why
        if not ok:
            self.db.record(job, "skipped", reason, score)
            return "skipped", reason, score
        log.info("%s [%d] %s @ %s (%s)", "CHECK" if self.dry_run else "APPLY", score, job.title, job.company,
                 job.location)
        try:
            status, detail = self.apply(job)
        except PWError as e:
            status, detail = "error", f"{str(e).splitlines()[0][:150]} ({self.snapshot(f'exception_{job.job_id}')})"
        if status == "external" and detail.startswith("http"):
            job.apply_url, job.external = detail, True
            new = self.queue.save_external(job, score, source="indeed")
            detail = ("queued for company_apply.py: " if new else "already queued: ") + detail[:120]
        # a dry run only looks: recording its outcome would skip the job in every real run
        if not self.dry_run:
            self.db.record(job, status, detail or reason, score)
        log.info(" -> %s %s", status.upper(), detail)
        return status, detail, score
