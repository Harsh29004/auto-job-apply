"""Playwright automation for naukri.com: login, search, apply, answer chatbot questions."""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, quote, urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

from .answers import Answerer
from .config import Config
from .filters import evaluate
from .models import BASE_URL, Job
from .storage import Storage

log = logging.getLogger("naukri")

LOGIN_URL = BASE_URL + "/nlogin/login"
HOME_URL = BASE_URL + "/mnjuser/homepage"

# Chatbot drawer that pops up after clicking Apply when the recruiter has questions.
DRAWER = "[class*='chatbot_Drawer'], [class*='chatbot_DrawerContentWrapper'], .chatbot_MessageContainer"
SUCCESS_TEXT = re.compile(r"successfully applied|you have applied|applied to\b|application (has been )?(sent|submitted)", re.I)
FAIL_TEXT = re.compile(r"not accepted|incomplete information|application (has )?failed|could not be (processed|submitted)", re.I)
QUOTA_TEXT = re.compile(r"daily (apply )?(limit|quota)|reached (the|your) (daily|maximum)|exceeded.*apply", re.I)

# Reads the current chatbot state from the DOM in one go.
READ_CHATBOT_JS = r"""
() => {
  const drawer = document.querySelector("[class*='chatbot_Drawer']") ||
                 document.querySelector("[class*='chatbot_DrawerContentWrapper']") ||
                 document.querySelector(".chatbot_MessageContainer");
  if (!drawer) return null;
  const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const txt = el => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();

  // question = bot messages after the last user message
  const items = [...drawer.querySelectorAll("li")].filter(li => txt(li));
  let lastUser = -1;
  items.forEach((li, i) => { if (/user/i.test(li.className)) lastUser = i; });
  let botItems = items.slice(lastUser + 1).filter(li => /bot/i.test(li.className) || li.querySelector("[class*='botMsg']"));
  if (!botItems.length) {
    const msgs = [...drawer.querySelectorAll("[class*='botMsg']")];
    botItems = msgs.slice(-1);
  }
  const question = botItems.map(txt).join(" ").trim();

  const options = [], kinds = new Set();
  drawer.querySelectorAll("input[type=radio], input[type=checkbox]").forEach(inp => {
    let label = inp.id ? drawer.querySelector(`label[for="${CSS.escape(inp.id)}"]`) : null;
    if (!label) label = inp.closest("label") || inp.parentElement;
    const t = label ? txt(label) : (inp.value || "");
    if (t) { options.push(t); kinds.add(inp.type); }
  });
  if (!options.length) {
    drawer.querySelectorAll("[class*='chatbot_Chip'], [class*='chipItem'], [class*='chip_']").forEach(c => {
      if (vis(c) && txt(c) && !c.querySelector("[class*='chatbot_Chip']")) { options.push(txt(c)); kinds.add("chip"); }
    });
  }
  const textBox = [...drawer.querySelectorAll("[contenteditable='true'], textarea, input[type=text], input[type=number], input:not([type])")]
                    .find(vis);
  return {question, options: [...new Set(options)], kinds: [...kinds], hasText: !!textBox};
}
"""


def is_remote(job: Job) -> bool:
    return bool(re.search(r"remote|work from home|\bwfh\b|anywhere", job.location + " " + job.title, re.I))


def search_url(keyword: str, location: str = "", page_no: int = 1, experience=None, job_age=None,
               remote: bool = False) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-") + "-jobs"
    if location:
        slug += "-in-" + re.sub(r"[^a-z0-9]+", "-", location.lower()).strip("-")
    if page_no > 1:
        slug += f"-{page_no}"
    params = [f"k={quote(keyword)}"]
    if location:
        params.append(f"l={quote(location)}")
    if experience is not None:
        params.append(f"experience={experience}")
    if job_age:
        params.append(f"jobAge={job_age}")
    if remote:
        params.append("wfhType=2")  # Naukri "Remote / Work from home" filter
    return f"{BASE_URL}/{slug}?{'&'.join(params)}"


def shorten(text: str, limit: int) -> str:
    """Cut text to <= limit chars at a sentence (or word) boundary."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    if end > limit * 0.4:
        return cut[:end + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "."


class QuotaReached(Exception):
    pass


class NaukriBot:
    def __init__(self, cfg: Config, storage: Storage, dry_run: bool = False, headless: bool | None = None):
        self.cfg = cfg
        self.db = storage
        self.dry_run = dry_run
        self.headless = cfg.apply.get("headless", False) if headless is None else headless
        self.answerer = Answerer(cfg.profile, cfg.skills, cfg.custom_answers)
        self.debug_dir = cfg.data_dir / "debug"
        self.debug_dir.mkdir(exist_ok=True)
        self._pw = self.ctx = self.page = None
        self.questionnaires = {}
        self.apply_responses = []

    # ------------------------------------------------------------ lifecycle
    def __enter__(self):
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(self.cfg.profile_dir),
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        )
        self.ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.ctx.set_default_timeout(20000)
        self.ctx.route("**/apply-workflow/v1/apply*", self._on_apply_request)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    # --------------------------------------------------- apply API intercept
    # Naukri's chatbot sometimes silently skips a mandatory question (e.g. one that has
    # prefilled profile data) and then rejects the application with code 406
    # "incomplete information". We intercept the final apply request, add any missing
    # mandatory answers, and record the server's response.
    questionnaires: dict[str, list[dict]]
    apply_responses: list[dict]

    def _on_apply_request(self, route):
        req = route.request
        post = req.post_data
        try:
            data = json.loads(post) if post else None
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("applyData"):
            self._fill_missing_answers(data)
            post = json.dumps(data)
        try:
            resp = route.fetch(post_data=post)
            js = self._json(resp)
            # Too-long text answers are rejected (customErrorCode 289): shorten and resend.
            for _ in range(4):
                if not (isinstance(data, dict) and data.get("applyData") and js and self._shorten_rejected(data, js)):
                    break
                post = json.dumps(data)
                resp = route.fetch(post_data=post)
                js = self._json(resp)
        except PWError:
            route.continue_(post_data=post)
            return
        if js:
            self.apply_responses.append(js)
            if isinstance(data, dict) and data.get("applyData"):
                log.info("   apply API -> HTTP %s %s", resp.status, json.dumps(js)[:300])
            for j in js.get("jobs") or []:
                if j.get("questionnaire"):
                    self.questionnaires[str(j.get("jobId"))] = j["questionnaire"]
        route.fulfill(response=resp)

    @staticmethod
    def _json(resp) -> dict | None:
        try:
            js = resp.json()
            return js if isinstance(js, dict) else None
        except (ValueError, PWError):
            return None

    @staticmethod
    def _shorten_rejected(data: dict, js: dict) -> bool:
        """Shorten answers Naukri rejected as too long. True if anything changed."""
        changed = False
        for j in js.get("jobs") or []:
            answers = (data["applyData"].get(str(j.get("jobId"))) or {}).get("answers") or {}
            for err in j.get("validationError") or []:
                field, msg = str(err.get("field")), str(err.get("message", ""))
                value = answers.get(field)
                if isinstance(value, str) and re.search(r"length", msg, re.I) and len(value) > 20:
                    answers[field] = shorten(value, int(len(value) * 0.6))
                    log.info("   answer too long for Naukri, shortened to %d chars", len(answers[field]))
                    changed = True
        return changed

    def _fill_missing_answers(self, data: dict):
        for job_id, block in (data.get("applyData") or {}).items():
            answers = block.setdefault("answers", {})
            for q in self.questionnaires.get(str(job_id), []):
                qid = str(q.get("questionId"))
                if qid in answers or not q.get("isMandatory"):
                    continue
                options = [str(v) for v in (q.get("answerOption") or {}).values()]
                prefill = q.get("prefillData")
                value = prefill[0] if prefill else self.answerer.answer(q.get("questionName", ""), options)
                if value in (None, ""):
                    log.warning("   missing mandatory answer, cannot fill: %s", q.get("questionName"))
                    self.db.record_unanswered(q.get("questionName", ""), options, str(job_id))
                    continue
                is_choice = bool(options) or re.search(r"radio|check|select|drop", str(q.get("questionType")), re.I)
                answers[qid] = [value] if is_choice else str(value)
                log.info("   filled skipped mandatory question: %s -> %s", q.get("questionName"), value)

    def __exit__(self, *exc):
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    def pause(self, lo: float | None = None, hi: float | None = None):
        d_lo, d_hi = self.cfg.apply.get("delay_seconds", [4, 9])
        time.sleep(random.uniform(lo if lo is not None else d_lo, hi if hi is not None else d_hi))

    def snapshot(self, name: str):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = self.debug_dir / f"{stamp}_{re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:60]}"
        try:
            self.page.screenshot(path=str(base) + ".png", full_page=False)
            (base.with_suffix(".html")).write_text(self.page.content(), encoding="utf-8")
        except PWError:
            pass
        return base

    # ---------------------------------------------------------------- login
    def is_logged_in(self) -> bool:
        self.page.goto(HOME_URL, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2500)
        return "nlogin" not in self.page.url and "login" not in self.page.url.split("?")[0]

    def login(self, wait_manual_seconds: int = 180) -> bool:
        if self.is_logged_in():
            log.info("Already logged in (saved session).")
            return True
        page = self.page
        if self.cfg.email and self.cfg.password:
            log.info("Logging in as %s ...", self.cfg.email)
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.wait_for_selector("#usernameField")
            page.fill("#usernameField", self.cfg.email)
            page.fill("#passwordField", self.cfg.password)
            page.wait_for_timeout(500)
            page.locator("button[type=submit]:has-text('Login')").first.click()
        else:
            log.warning("NAUKRI_EMAIL / NAUKRI_PASSWORD missing in .env - please log in manually in the browser window.")
            page.goto(LOGIN_URL, wait_until="domcontentloaded")

        deadline = time.time() + (wait_manual_seconds if not self.headless else 40)
        warned = False
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            url = page.url
            if "nlogin" not in url and "login" not in url.split("?")[0]:
                log.info("Login successful.")
                return True
            body = self._body_text()
            if re.search(r"invalid|incorrect|wrong", body, re.I) and "password" in body.lower() and not warned:
                log.error("Naukri says the email/password is incorrect.")
                self.snapshot("login_failed")
                return False
            if not warned and re.search(r"otp|verify|captcha", body, re.I):
                log.warning("OTP / captcha required - complete it in the browser window (waiting %ss).", wait_manual_seconds)
                warned = True
        self.snapshot("login_timeout")
        log.error("Login did not complete.")
        return False

    # --------------------------------------------------------------- search
    def search(self, keyword: str, location: str = "", page_no: int = 1, remote: bool = False) -> list[Job]:
        s = self.cfg.search
        url = search_url(keyword, location, page_no, s.get("experience_years"), s.get("job_age_days"), remote)
        try:
            with self.page.expect_response(lambda r: "jobapi/v3/search" in r.url, timeout=30000) as resp_info:
                self.page.goto(url, wait_until="domcontentloaded")
            data = resp_info.value.json()
        except (PWTimeout, PWError, ValueError) as e:
            log.warning("Search failed for '%s' %s p%s: %s", keyword, location, page_no, e)
            return []
        jobs = [Job.from_api(d) for d in data.get("jobDetails") or [] if d.get("jobId")]
        log.info("Search '%s'%s%s page %d -> %d jobs (total %s)", keyword, " [remote]" if remote else "",
                 f" in {location}" if location else "", page_no, len(jobs), data.get("noOfJobs"))
        return jobs

    def collect_jobs(self) -> list[tuple[Job, int, str]]:
        """Run every search and return relevant, not-yet-applied jobs sorted by score."""
        s = self.cfg.search
        locations = s.get("locations") or [""]
        found: dict[str, tuple[Job, int, str]] = {}
        new_external = 0
        modes = [True, False] if s.get("remote_first") else [False]
        searches = [(kw, loc, remote) for remote in modes for kw in s["keywords"]
                    for loc in (locations if not remote else [""])]
        for kw, loc, remote in searches:
            for page_no in range(1, int(s.get("pages_per_search", 1)) + 1):
                jobs = self.search(kw, loc, page_no, remote)
                for job in jobs:
                    if job.job_id in found or self.db.is_done(job.job_id):
                        continue
                    if job.external:
                        ok, reason, score = evaluate(job, self.cfg.filters, self.cfg.skills, allow_external=True)
                        if ok and self.db.save_external(job, score):
                            new_external += 1
                        self.db.record(job, "external", "company site" + ("" if ok else f" ({reason})"), score)
                        continue
                    ok, reason, score = evaluate(job, self.cfg.filters, self.cfg.skills)
                    if ok:
                        found[job.job_id] = (job, score, reason)
                    else:
                        self.db.record(job, "skipped", reason, score)
                self.pause(1.5, 3.5)
                if len(jobs) < 20:
                    break
        # Same posting is often listed once per city - keep one per (title, company).
        ranked, seen = [], set()
        # remote jobs first, then by relevance score
        for item in sorted(found.values(), key=lambda t: (is_remote(t[0]), t[1]), reverse=True):
            key = (item[0].title.lower().strip(), item[0].company.lower().strip())
            if key not in seen:
                seen.add(key)
                ranked.append(item)
        log.info("%d relevant new jobs found. %d new relevant company-site jobs saved for company_apply.py.",
                 len(ranked), new_external)
        return ranked

    # ---------------------------------------------------------------- apply
    def _body_text(self) -> str:
        try:
            return self.page.inner_text("body", timeout=5000)
        except PWError:
            return ""

    def _visible(self, selector: str) -> bool:
        try:
            loc = self.page.locator(selector)
            return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 5)))
        except PWError:
            return False

    def _apply_result(self, job_id: str) -> tuple[str, str] | None:
        """('applied'|'failed', detail) once Naukri shows the outcome, else None."""
        page = self.page
        if "saveApply" in page.url or "myapply" in page.url:
            page.wait_for_timeout(2000)
            code = None
            raw = parse_qs(urlparse(page.url).query).get("multiApplyResp", [""])[0]
            try:
                code = int(json.loads(raw).get(str(job_id), 0)) or None
            except (ValueError, AttributeError):
                pass
            body = self._body_text()
            fail = FAIL_TEXT.search(body)
            if fail or (code and code >= 300):
                line = next((l for l in body.splitlines() if FAIL_TEXT.search(l)), "")
                return "failed", f"naukri code {code}: {line.strip()[:160]}"
            return "applied", f"code {code}"
        if self._visible("#already-applied") or self._visible("[class*='already-applied']"):
            return "applied", "already-applied button"
        body = self._body_text()
        if FAIL_TEXT.search(body):
            return "failed", FAIL_TEXT.search(body).group(0)
        if SUCCESS_TEXT.search(body):
            return "applied", SUCCESS_TEXT.search(body).group(0)
        return None

    def apply(self, job: Job) -> tuple[str, str]:
        """Apply to one job. Returns (status, detail)."""
        page = self.page
        try:
            page.goto(job.url, wait_until="domcontentloaded")
        except PWError as e:
            return "error", f"open failed: {e}"
        page.wait_for_timeout(3000)

        if self._visible("#already-applied") or self._visible("[class*='already-applied']"):
            return "already_applied", ""
        if self._visible("#company-site-button"):
            return "external", "apply on company site"
        btns = page.locator("#apply-button")
        btn = next((btns.nth(i) for i in range(btns.count()) if btns.nth(i).is_visible()), None)
        if btn is None:
            if re.search(r"no longer (available|accepting)|job (has )?expired|expired", self._body_text(), re.I):
                return "expired", ""
            self.snapshot(f"no_apply_btn_{job.job_id}")
            return "error", "apply button not found"
        if "login" in btn.inner_text().lower():
            return "error", "not logged in"

        pages_before = len(self.ctx.pages)
        btn.click()
        page.wait_for_timeout(3000)
        if len(self.ctx.pages) > pages_before:  # opened company site in a new tab
            for extra in self.ctx.pages[pages_before:]:
                extra.close()
            return "external", "opened company site"
        return self._finish_apply(job)

    def _finish_apply(self, job: Job) -> tuple[str, str]:
        page = self.page
        last_question, repeats = None, 0
        for _ in range(30):
            body = self._body_text()
            if QUOTA_TEXT.search(body):
                raise QuotaReached(re.search(QUOTA_TEXT, body).group(0))
            state = page.evaluate(READ_CHATBOT_JS) if self._visible(DRAWER) else None
            if state is None:
                result = self._apply_result(job.job_id)
                if result:
                    return self._report_result(job, result)
                page.wait_for_timeout(1500)
                if self._visible(DRAWER):
                    continue
                result = self._apply_result(job.job_id)
                if result:
                    return self._report_result(job, result)
                self.snapshot(f"unknown_after_apply_{job.job_id}")
                return "error", "no success message / chatbot after clicking apply"

            question = state["question"]
            if re.match(r"^(thank you|thanks)\b", question, re.I) and not state["options"]:
                page.wait_for_timeout(2000)  # chatbot finished, apply request is being sent
                continue
            if not question and not state["options"] and not state["hasText"]:
                page.wait_for_timeout(1500)  # bot still typing
                continue

            if question == last_question:
                repeats += 1
                if repeats >= 3:
                    self.snapshot(f"stuck_{job.job_id}")
                    self._close_drawer()
                    return "needs_answer", f"answer rejected: {question}"
            else:
                last_question, repeats = question, 0

            answer = self.answerer.answer(question, state["options"])
            log.info("   Q: %s %s", question, state["options"] or "")
            log.info("   A: %s", answer)
            if answer is None:
                self.db.record_unanswered(question, state["options"], job.job_id)
                self._close_drawer()
                return "needs_answer", question
            self._give_answer(answer, state)
            page.wait_for_timeout(2500)
        self.snapshot(f"too_many_questions_{job.job_id}")
        self._close_drawer()
        return "error", "too many chatbot steps"

    def _report_result(self, job: Job, result: tuple[str, str]) -> tuple[str, str]:
        if result[0] != "applied":
            self.snapshot(f"{result[0]}_{job.job_id}")
        return result

    def _give_answer(self, answer: str, state: dict):
        page = self.page
        drawer = page.locator(DRAWER).first
        if state["options"]:
            clicked = False
            # radio / checkbox: click the label that matches
            for sel in ["label", "[class*='chatbot_Chip']", "[class*='chipItem']", "[class*='chip_']"]:
                loc = drawer.locator(sel).filter(has_text=answer)
                n = loc.count()
                for i in range(n):
                    el = loc.nth(i)
                    if el.is_visible() and el.inner_text().strip().lower() == answer.strip().lower():
                        el.click()
                        clicked = True
                        break
                if clicked:
                    break
            if not clicked:
                drawer.get_by_text(answer, exact=True).first.click()
        else:
            box = drawer.locator("[contenteditable='true'], textarea, input[type=text], input[type=number], input:not([type])").first
            box.click()
            try:
                box.fill(answer)
            except PWError:
                page.keyboard.press("Control+A")
                page.keyboard.type(answer, delay=40)
        page.wait_for_timeout(600)
        self._click_save(drawer, bool(state["options"]) and "chip" in state["kinds"])

    def _click_save(self, drawer, chip: bool):
        for sel in [".sendMsg", "[class*='sendMsg']", "[class*='send']:has-text('Save')", "button:has-text('Save')",
                    "div:text-is('Save')", "button:has-text('Submit')", "button:has-text('Send')"]:
            loc = drawer.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click()
                return
        if not chip:  # chips usually submit on click; text boxes accept Enter
            self.page.keyboard.press("Enter")

    def _close_drawer(self):
        for sel in ["[class*='crossIcon']", "[class*='chatBot-ic-cross']", "[class*='chatbot_Drawer'] [class*='close']"]:
            loc = self.page.locator(sel)
            if loc.count() and loc.first.is_visible():
                try:
                    loc.first.click()
                    return
                except PWError:
                    pass
        self.page.keyboard.press("Escape")

    def quota(self) -> tuple[int, int] | None:
        """(dailyApplied, dailyQuota) from the latest apply API response."""
        for js in reversed(self.apply_responses):
            q = js.get("quotaDetails") or {}
            if "dailyApplied" in q and q.get("dailyQuota"):
                return int(q["dailyApplied"]), int(q["dailyQuota"])
        return None

    # ------------------------------------------------------------------ run
    def run(self, max_applies: int | None = None) -> dict:
        limit = max_applies or int(self.cfg.apply.get("max_applies_per_run", 25))
        if not self.dry_run and not self.login():
            raise SystemExit("Login failed - check .env or log in manually in the opened browser.")
        jobs = self.collect_jobs()
        stats = {"applied": 0, "planned": 0}
        for job, score, reason in jobs:
            if stats["applied"] >= limit or stats["planned"] >= limit:
                break
            tag = f"[{score}] {job.title} @ {job.company} ({job.experience}, {job.location})"
            if self.dry_run:
                log.info("WOULD APPLY %s  -- %s", tag, reason)
                stats["planned"] += 1
                continue
            log.info("Applying %s", tag)
            try:
                status, detail = self.apply(job)
            except QuotaReached as e:
                log.error("Naukri daily apply limit reached: %s", e)
                break
            except PWError as e:
                self.snapshot(f"exception_{job.job_id}")
                status, detail = "error", str(e).splitlines()[0]
            self.db.record(job, status, detail or reason, score)
            stats[status] = stats.get(status, 0) + 1
            log.info(" -> %s %s", status.upper(), detail)
            quota = self.quota()
            if quota and quota[0] >= quota[1]:
                log.error("Naukri daily apply quota used up (%d/%d). Run again tomorrow.", *quota)
                break
            self.pause()
        return stats
