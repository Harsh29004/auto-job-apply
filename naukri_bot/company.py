"""Apply on company career sites saved by the Naukri bot (external_jobs table).

For every job it decides a route:
  form   - finds the application form (clicking "Apply" if needed), fills it from your profile,
           uploads the resume PDF and submits
  email  - the page says "mail your resume to hr@..." -> sends an email with the resume (needs SMTP in .env)
  manual - login-based ATS (Workday, Oracle, Taleo...), WhatsApp, captcha, or anything it isn't sure
           about. These are listed in data/manual_apply.html with the link so you can finish them yourself.
"""
from __future__ import annotations

import html
import logging
import mimetypes
import re
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import Frame, Page, sync_playwright

from .answers import Answerer
from .ats import detect_ats, needs_login
from .config import Config
from .gforms import GoogleFormFlow, is_google_form
from .resumes import ResumePicker
from .storage import Storage

log = logging.getLogger("company")

SUCCESS_TEXT = re.compile(
    r"thank(s| you) for (applying|your (application|interest|submission))|application (has been |was )?"
    r"(received|submitted|sent)|successfully (submitted|applied|sent)|we (have )?received your (application|resume)|"
    r"we('ll| will) (get back|be in touch|contact you|review)|form (has been )?submitted", re.I)
ERROR_TEXT = re.compile(r"(this field is|is) required|please (fill|enter|select|complete)|invalid (email|phone|value)", re.I)
APPLY_TEXT = re.compile(r"^\s*(apply|apply now|apply here|apply online|apply for (this|the)? ?(job|position|role|opening)|"
                        r"apply with resume|submit (your )?(resume|cv|application)|i'?m interested|send (your )?(resume|cv))\s*!?\s*$", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Collects every fillable field in a frame and tags it with data-nb-idx so Python can find it again.
SCAN_JS = r"""
(rootSel) => {
  const deep = (sel, r = document) => {  // querySelector that also looks inside open shadow roots
    const hit = r.querySelector(sel);
    if (hit) return hit;
    for (const el of r.querySelectorAll('*')) if (el.shadowRoot) { const x = deep(sel, el.shadowRoot); if (x) return x; }
    return null;
  };
  const root = (rootSel && deep(rootSel)) || document;
  const vis = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length)) &&
                   getComputedStyle(e).visibility !== 'hidden';
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const labelOf = e => {
    let t = '';
    if (e.labels && e.labels.length) t = [...e.labels].map(l => l.innerText).join(' ');
    if (!t && e.getAttribute('aria-labelledby'))
      t = e.getAttribute('aria-labelledby').split(' ').map(id => ((e.getRootNode().getElementById ? e.getRootNode() : document).getElementById(id) || {}).innerText || '').join(' ');
    if (!t) t = e.getAttribute('aria-label') || '';
    if (!t && e.closest('label')) t = e.closest('label').innerText;
    if (!t) t = e.getAttribute('placeholder') || '';
    if (!t) {  // nearest preceding text in the same small container
      let p = e.parentElement;
      for (let i = 0; i < 3 && p && !t; i++, p = p.parentElement) {
        const txt = clean(p.innerText);
        if (txt && txt.length < 160) t = txt;
      }
    }
    return clean(t).slice(0, 200);
  };
  const out = [];
  let idx = 0;
  const groups = {};
  const els = [...root.querySelectorAll('input, textarea, select')];
  for (const e of els) {
    const type = (e.getAttribute('type') || e.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    const choice = type === 'radio' || type === 'checkbox';
    const labelVisible = choice && ((e.labels && e.labels[0] && vis(e.labels[0])) || vis(e.parentElement));
    if (type !== 'file' && ((!vis(e) && !labelVisible) || e.disabled)) continue;
    const combo = e.readOnly && e.tagName === 'INPUT';
    if (e.readOnly && !combo) continue;
    if (type === 'file' && e.disabled) continue;
    e.setAttribute('data-nb-idx', String(idx));
    const required = e.required || e.getAttribute('aria-required') === 'true';
    const base = {idx, tag: e.tagName.toLowerCase(), type, name: e.name || e.id || '',
                  placeholder: e.getAttribute('placeholder') || '', required, accept: e.getAttribute('accept') || ''};
    if (type === 'radio' || type === 'checkbox') {
      const key = type + ':' + (e.name || ('_' + idx));
      const optLabel = clean((e.labels && e.labels[0] && e.labels[0].innerText) || (e.closest('label') || {}).innerText || e.value);
      if (!groups[key]) {
        const fs = e.closest('fieldset');
        let q = fs && fs.querySelector('legend') ? clean(fs.querySelector('legend').innerText) : '';
        if (!q) {
          let p = e.parentElement;
          for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
            if (p.querySelectorAll(`input[type=${type}]`).length > (e.name ? 1 : 0)) {
              const txt = clean(p.innerText);
              q = txt.slice(0, 200); break;
            }
          }
        }
        groups[key] = {...base, label: q || optLabel, options: [], optionIdx: []};
        out.push(groups[key]);
      }
      groups[key].options.push(optLabel);
      groups[key].optionIdx.push(idx);
      groups[key].required = groups[key].required || required;
    } else {
      const f = {...base, label: labelOf(e), options: []};
      if (combo) f.type = 'combo';
      if (e.tagName === 'SELECT') f.options = [...e.options].map(o => clean(o.text)).filter(Boolean);
      if (/\*/.test(f.label)) f.required = true;
      f.value = e.value || '';
      out.push(f);
    }
    idx++;
  }
  for (const e of root.querySelectorAll('[role=combobox]:not(input), [aria-haspopup=listbox]:not(input)')) {
    if (!vis(e) || e.querySelector('input:not([type=hidden])')) continue;
    e.setAttribute('data-nb-idx', String(idx));
    const lab = labelOf(e);
    out.push({idx, tag: 'div', type: 'combo', name: e.id || '', placeholder: '', label: lab,
              required: /\*/.test(lab) || e.getAttribute('aria-required') === 'true', options: [],
              value: clean(e.innerText)});
    idx++;
  }
  return out;
}
"""

# Visible options of an open custom dropdown.
OPEN_OPTIONS_JS = r"""
() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const sel = "[role=option], [role=listbox] li, ul[class*=dropdown] li, ul[class*=menu] li, ul[class*=list] li, " +
              "div[class*=option]:not([class*=options]), li[class*=option], .dropdown-item, mat-option";
  const roots = [document];
  for (let k = 0; k < roots.length; k++)
    for (const el of roots[k].querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
  let i = 0;
  const out = [];
  for (const e of roots.flatMap(r => [...r.querySelectorAll(sel)])) {
    const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
    if (!vis(e) || !t || t.length > 80 || (e.parentElement && e.parentElement.closest(sel))) continue;
    e.setAttribute('data-nb-opt', String(i));
    out.push({i, text: t});
    i++;
  }
  return out;
}
"""

FIND_APPLY_JS = r"""
(args) => {
  const [title, reText] = args;
  const re = new RegExp(reText, 'i');
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const words = title.toLowerCase().split(/[^a-z0-9+#]+/).filter(w => w.length > 2);
  const cands = [...document.querySelectorAll('a, button, input[type=button], input[type=submit], [role=button]')]
    .filter(e => vis(e) && re.test((e.innerText || e.value || '').trim()));
  const scored = cands.map((e, i) => {
    let score = 0, p = e;
    for (let d = 0; d < 7 && p; d++, p = p.parentElement) {
      const t = (p.innerText || '').toLowerCase();
      if (t.length > 4000) break;
      const hit = words.filter(w => t.includes(w)).length;
      if (words.length && hit / words.length >= 0.6) { score = 10 - d; break; }
    }
    e.setAttribute('data-nb-apply', String(i));
    return {i, score, text: (e.innerText || e.value || '').trim().slice(0, 60), href: e.href || ''};
  });
  return scored;
}
"""


def split_name(full: str) -> tuple[str, str]:
    parts = full.split()
    return (parts[0], parts[-1]) if len(parts) > 1 else (full, "")


def email_from_href(href: str) -> str | None:
    """Recipient of a mailto: or Gmail-compose link."""
    if not href:
        return None
    h = unquote(href)
    if h.lower().startswith("mailto:"):
        m = EMAIL_RE.search(h[7:].split("?")[0])
        return m.group(0) if m else None
    if "mail.google.com" in h:
        to = parse_qs(urlparse(h).query).get("to", [""])[0]
        m = EMAIL_RE.search(to)
        return m.group(0) if m else None
    return None


class FieldFiller:
    """Maps a form field's label to a value from the profile."""

    # where the job was found -> answer for "How did you hear about us?"
    SOURCE_NAMES = {"naukri": "Naukri.com", "linkedin": "LinkedIn", "himalayas": "Himalayas", "remotive": "Remotive",
                    "remoteok": "Remote OK", "jobicy": "Jobicy", "weworkremotely": "We Work Remotely",
                    "arbeitnow": "Arbeitnow", "greenhouse": "Company website", "lever": "Company website",
                    "ashby": "Company website"}

    def __init__(self, cfg: Config, answerer: Answerer):
        self.p = cfg.profile
        self.c = cfg.company_apply
        self.answerer = answerer
        self.source = ""  # set per job by CompanyApplier

    def heard_about(self, options: list[str] | None = None) -> str | None:
        name = self.SOURCE_NAMES.get(self.source) or self.c.get("heard_about_us") or "Job board"
        if not options:
            return name
        for want in (re.escape(name), r"linkedin" if self.source == "linkedin" else r"naukri",
                     r"job ?board|job ?portal|job ?site|online|internet|website|other"):
            hit = next((o for o in options if re.search(want, o, re.I)), None)
            if hit:
                return hit
        return None

    def cover_letter(self, title: str, company: str) -> str:
        return (self.c.get("cover_letter") or "").format(title=title, company=company or "your company").strip()

    def value_for(self, field: dict, title: str, company: str):
        """Return a value (str) for a text/select field, or None if unknown.

        Personal-field rules ("Country", "City", "Company name") only apply to short labels - a long
        question that happens to contain "state" or "country" goes to the Answerer instead."""
        label = re.sub(r"[✱*]", " ", field.get("label") or "").strip()
        text = " ".join([label, field.get("placeholder", ""), field.get("name", "")]).lower()
        text = re.sub(r"[_\-\[\]]+", " ", text)
        words = re.findall(r"[a-z]+", (label or field.get("placeholder") or field.get("name") or "").lower())
        short = len(words) <= 6
        textual = field.get("tag") in ("input", "textarea") and field.get("type") not in ("radio", "checkbox", "combo")
        p = self.p
        first, last = split_name(p.get("name", ""))
        city = p.get("current_location") or ""
        country = p.get("country") or "India"
        location = city
        if city and country.lower() not in city.lower():
            location = f"{city}, {country}"
        # unambiguous personal fields - any label length (but only typed into text boxes)
        strong = [
            (r"first and last name|full ?name|your name|legal name|candidate name|applicant name", p.get("name")),
            (r"preferred (first )?name|nick ?name", first),
            (r"first ?name|given name|\bfname\b", first),
            (r"last ?name|surname|family name|\blname\b", last),
            (r"middle ?name", ""),
            (r"e-?mail", p.get("email")),
            (r"phone|mobile|contact (no|number)|whatsapp|\btel\b", p.get("phone")),
            (r"linked ?in", p.get("linkedin") or None),
            (r"git ?hub|portfolio|personal (site|website|url)|website|other links|links you may have", p.get("github") or None),
            (r"how did you (hear|find|come)|where did you (hear|find|see)|referr?al source", "__SOURCE__"),
            (r"cover ?letter", self.cover_letter(title, company)),
        ]
        # field names - short labels only
        short_rules = [
            (r"father|mother|guardian|spouse", None),
            (r"country ?code|\bisd\b|dial code", "+91"),
            (r"\bsource\b|how did you (hear|find)|where did you (hear|find|see)", "__SOURCE__"),
            (r"message|additional (info|information)|comments?|anything else|motivation|tell us|about (you|yourself)",
             self.cover_letter(title, company)),
            (r"subject", f"Application for {title}"),
            (r"position|job title|role (applied|applying)|applying for|post applied|job opening|opening", "__TITLE__"),
            (r"primary skills|key skills|skill ?set|technical skills|^skills|\bskills\b", ", ".join(
                s for s in ["Python", "Machine Learning", "Deep Learning", "TensorFlow", "PyTorch", "OpenCV",
                            "NLP", "LLMs", "SQL", "FastAPI", "React", "JavaScript"])),
            (r"degree type|highest (educational )?qualification|education level|qualification|\bdegree\b",
             p.get("highest_qualification")),
            (r"previously (worked|employed|applied)|worked (with|for) us (before)?|ex-?employee", "No"),
            (r"gender", p.get("gender") or "__PREFER_NOT__"),
            (r"current (company|employer|organi[sz]ation)|company name|employer|organi[sz]ation", p.get("current_company")),
            (r"current (designation|title|role|position)|designation", p.get("current_designation")),
            (r"\bname\b", p.get("name")),
            (r"country|nationality|citizenship", country),
            (r"\bcity\b", city),
            (r"location|address|residence|where do you live|based in", location),
            (r"\bstate\b|province|region", p.get("state") or None),
            (r"pin ?code|zip|postal", p.get("postal_code") or None),
            (r"school|college|university|institut", p.get("college")),
            (r"graduation|pass(ing)? year|year of (passing|graduation)", p.get("graduation_year")),
        ]
        rules = (strong if textual else []) + (short_rules if short else [])
        for pattern, value in rules:
            if re.search(pattern, text):
                if value == "__PREFER_NOT__":
                    return next((o for o in field.get("options") or []
                                 if re.search(r"prefer not|not (to )?disclose|do not prefer|rather not", o, re.I)), None)
                if value == "__TITLE__":
                    return self.match_title(title, field) if field.get("tag") == "select" else title
                if value == "__SOURCE__":
                    return self.heard_about(field.get("options") if field.get("tag") == "select" else None)
                if field.get("tag") == "select" and value:
                    return self.answerer.match_option(str(value), field.get("options") or [])
                return value
        # generic recruiter-style question (experience, notice, CTC, degree ...)
        options = [o for o in field.get("options") or [] if not re.match(r"^(select|choose|--|please)", o, re.I)]
        label = field.get("label") or field.get("placeholder") or field.get("name")
        if not label:
            return None
        return self.answerer.answer(label, options or None)


    GENERIC_WORDS = {"engineer", "developer", "senior", "junior", "associate", "intern", "trainee", "executive",
                     "software", "sr", "jr", "the", "and", "of", "i", "ii", "iii", "fresher", "lead"}

    def match_title(self, title: str, field: dict) -> str | None:
        """Pick the dropdown option for our job title; needs a real (non-generic) word in common."""
        words = lambda t: set(re.findall(r"[a-z0-9+#.]+", t.lower().replace("full-stack", "fullstack")
                                          .replace("full stack", "fullstack")))
        mine = words(title) - self.GENERIC_WORDS
        best, best_n = None, 0
        for o in field.get("options") or []:
            n = len(mine & (words(o) - self.GENERIC_WORDS))
            if n > best_n:
                best, best_n = o, n
        return best


class CompanyApplier:
    def __init__(self, cfg: Config, db: Storage, submit: bool = True, headless: bool = False,
                 assist_seconds: int = 0, profile_dir: Path | None = None):
        self.cfg = cfg
        self.profile_dir = Path(profile_dir) if profile_dir else cfg.root / "browser_profile_company"
        self.db = db
        self.submit = submit
        self.headless = headless
        self.assist_seconds = assist_seconds
        self.answerer = Answerer(cfg.profile, cfg.skills, cfg.custom_answers)
        self.filler = FieldFiller(cfg, self.answerer)
        resume = cfg.company_apply.get("resume_pdf") or ""
        self.default_resume = (cfg.root / resume) if resume else None
        self.resume = self.default_resume
        # role-specific resumes built by build_resumes.py (falls back to resume_pdf)
        self.resumes = ResumePicker(cfg.root, resume or None, cfg.data_dir / "upload")
        self.gforms = GoogleFormFlow(self)
        self.debug_dir = cfg.data_dir / "company_debug"
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
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    def snapshot(self, name: str, page: Page | None = None) -> str:
        page = page or self.page
        base = self.debug_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:60]}"
        try:
            page.screenshot(path=str(base) + ".png", full_page=True)
        except PWError:
            pass
        return str(base) + ".png"

    # ------------------------------------------------------------- helpers
    def open(self, url: str):
        for attempt in range(2):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
                break
            except PWError as e:
                msg = str(e)
                if "interrupted by another navigation" in msg or "ERR_ABORTED" in msg:
                    break  # site redirected itself - that's fine
                if attempt == 1:
                    raise
                time.sleep(3)
        try:
            self.page.wait_for_load_state("networkidle", timeout=12000)
        except PWError:
            pass
        self.page.wait_for_timeout(1500)

    def body_text(self, page: Page | None = None) -> str:
        page = page or self.page
        parts = []
        for fr in page.frames:
            try:
                parts.append(fr.inner_text("body", timeout=3000))
            except PWError:
                pass
        return "\n".join(parts)

    def has_visible_captcha(self) -> bool:
        """A captcha the user must solve (checkbox / challenge). The invisible reCAPTCHA badge
        (256x60 in the corner, size=invisible) is not one - forms with it submit normally."""
        for fr in self.page.frames:
            if re.search(r"recaptcha/(api2|enterprise)/anchor|hcaptcha.com/captcha|challenges.cloudflare", fr.url):
                if "size=invisible" in fr.url:
                    continue
                try:
                    box = fr.frame_element().bounding_box()
                except PWError:
                    continue
                if not box or box["width"] < 60 or box["height"] < 40:
                    continue
                if abs(box["width"] - 256) < 4 and abs(box["height"] - 60) < 4:
                    continue  # invisible reCAPTCHA badge
                return True
        return False

    def bot_wall(self) -> bool:
        """Cloudflare-style 'checking your browser' page. We don't try to get past these."""
        title = ""
        try:
            title = self.page.title()
        except PWError:
            pass
        return bool(re.search(r"just a moment|attention required|access denied", title, re.I)) or bool(
            re.search(r"performing security verification|verify you are human|checking your browser|"
                      r"enable javascript and cookies to continue", self.body_text()[:1500], re.I))

    def scan(self) -> tuple[Frame | None, list[dict]]:
        """Frame holding the best-looking application form, with its fields."""
        best, best_fields, best_score = None, [], 0
        for fr in self.page.frames:
            try:
                fields = fr.evaluate(SCAN_JS)
            except PWError:
                continue
            kinds = " ".join((f.get("label", "") + " " + f.get("name", "") + " " + f.get("type", "")).lower()
                             for f in fields)
            score = len(fields)
            if "file" in kinds:
                score += 5
            if "mail" in kinds:
                score += 3
            if re.search(r"newsletter|subscribe", kinds) and len(fields) <= 2:
                score = 0
            # job boards' own "upload your CV to be discovered" / job-alert widgets are not applications
            if re.search(r"be discovered|unlock remote|job alerts?|talent (pool|network|community)|"
                         r"get (job|new) (alerts|jobs)|sign ?up|create (an )?account|log ?in", kinds):
                score = 0
            if score > best_score:
                best, best_fields, best_score = fr, fields, score
        is_form = best_score >= 5 and any(
            f["type"] == "file" or re.search(r"mail", (f.get("label", "") + f.get("name", "")).lower())
            for f in best_fields)
        return (best, best_fields) if is_form else (None, [])

    # ------------------------------------------------------------ routes
    def apply_job(self, job: dict) -> tuple[str, str]:
        url = job.get("apply_url") or ""
        if not url:
            return "manual", "no apply link"
        self.resume = self.resumes.pick(job.get("title", ""), job.get("description") or "") or self.default_resume
        self.answerer.job_location = job.get("location") or ""
        self.filler.source = job.get("source") or "naukri"
        ats = detect_ats(url)
        if needs_login(ats):
            return "manual", f"{ats} needs an account - apply manually"
        try:
            self.open(url)
        except PWError as e:
            return "manual", f"site did not open: {str(e).splitlines()[0][:120]}"
        if self.bot_wall():
            self.page.wait_for_timeout(6000)  # some walls clear by themselves for a normal browser
            if self.bot_wall():
                return "manual", "site shows a bot check (Cloudflare) - open the link and apply yourself"
        final_ats = detect_ats(self.page.url)
        if needs_login(final_ats):
            self.db.update_external(job["job_id"], "pending", apply_url=self.page.url, ats=final_ats)
            return "manual", f"{final_ats} needs an account - apply manually"

        title, company = job.get("title", ""), job.get("company", "")
        for hop in range(3):
            if is_google_form(self.page.url):
                return self.gforms.apply(job)
            # on a job board's listing page the application is behind its Apply button - never
            # fill the board's own widgets (CV upload for "be discovered", alerts ...)
            on_board = re.search(r"(^|\.)(jobicy|remotive|remoteok|weworkremotely|himalayas|arbeitnow)\.",
                                 urlparse(self.page.url).netloc.lower() + ".")
            frame, fields = (None, []) if on_board and not re.search(r"/apply\b", self.page.url) else self.scan()
            if frame:
                return self.fill_and_submit(frame, fields, job)
            route = self.click_apply(title)
            if route is None:
                break
            kind, info = route
            if kind in ("email", "manual"):
                if kind == "email":
                    return self.send_email(info, job)
                return "manual", info
        # no form - maybe the page just lists an email address
        emails = [e for e in EMAIL_RE.findall(self.body_text())
                  if re.match(r"(hr|career|careers|jobs|job|recruit|talent|hiring|resume|cv)", e.lower())]
        if emails:
            return self.send_email(emails[0], job)
        return "manual", "no application form found"

    def click_apply(self, title: str):
        """Click the best Apply button. Returns None (nothing found), ('email', addr), ('manual', why) or ('page', url)."""
        page = self.page
        try:
            cands = page.evaluate(FIND_APPLY_JS, [title, APPLY_TEXT.pattern])
        except PWError:
            return None
        if not cands:
            return None
        cands.sort(key=lambda c: -c["score"])
        distinct = {c["href"] or c["text"] for c in cands}
        best = cands[0]
        if best["score"] == 0 and len(distinct) > 1 and len(cands) > 2:
            return "manual", "page lists several openings - pick the right one manually"
        addr = email_from_href(best["href"])
        if addr:
            return "email", addr
        if re.search(r"wa\.me|whatsapp", best["href"], re.I):
            return "manual", f"apply via WhatsApp: {best['href']}"
        before = len(self.ctx.pages)
        try:
            with page.expect_navigation(timeout=8000):
                page.locator(f"[data-nb-apply='{best['i']}']").first.click()
        except PWError:
            pass  # modal form or same-page anchor
        page.wait_for_timeout(2500)
        if len(self.ctx.pages) > before:  # opened in a new tab -> continue there
            new = self.ctx.pages[-1]
            try:
                new.wait_for_load_state("domcontentloaded", timeout=15000)
            except PWError:
                pass
            if new.url.startswith("mailto:") or "mail.google.com" in new.url:
                addr = email_from_href(new.url)
                new.close()
                return ("email", addr) if addr else ("manual", "email link without address")
            self.page = new
        if needs_login(detect_ats(self.page.url)):
            return "manual", f"{detect_ats(self.page.url)} needs an account - apply manually"
        return "page", self.page.url

    def fill_and_submit(self, frame: Frame, fields: list[dict], job: dict) -> tuple[str, str]:
        title, company = job.get("title", ""), job.get("company", "")
        for step in range(5):
            missing = self.fill(frame, fields, title, company)
            missing = self.still_missing(frame, fields, missing)
            if missing:
                log.info("   required fields not filled: %s", missing)
            if self.has_visible_captcha() and not self.assist_seconds:
                self.snapshot(f"captcha_{job['job_id']}")
                return "manual", "captcha on the form - finish manually"
            if not self.submit:
                shot = self.snapshot(f"filled_{job['job_id']}")
                return "filled", f"filled, not submitted (--no-submit). screenshot: {shot}"
            if missing and not self.assist_seconds:
                shot = self.snapshot(f"missing_{job['job_id']}")
                return "manual", f"could not fill required: {', '.join(missing)[:200]}"
            if self.assist_seconds:
                return self.wait_for_user(job)
            clicked = self.click_submit(frame)
            if not clicked:
                return "manual", "submit button not found"
            self.page.wait_for_timeout(5000)
            text = self.body_text()
            if SUCCESS_TEXT.search(text) or re.search(r"thank|success|confirm|submitted", self.page.url, re.I):
                self.snapshot(f"applied_{job['job_id']}")
                return "applied", "company site form"
            # multi-step form: a new set of fields appeared
            frame2, fields2 = self.scan()
            new_names = {f["name"] for f in fields2} - {f["name"] for f in fields}
            if frame2 and new_names and clicked == "next":
                frame, fields = frame2, fields2
                continue
            errors = ERROR_TEXT.findall(text)
            shot = self.snapshot(f"unclear_{job['job_id']}")
            if errors:
                return "failed", f"form shows errors ({len(errors)}). screenshot: {shot}"
            return "unconfirmed", f"submitted but no confirmation seen. screenshot: {shot}"
        return "manual", "form has too many steps"

    @staticmethod
    def still_missing(frame: Frame, fields: list[dict], missing: list[str]) -> list[str]:
        """Drop 'missing' fields the page itself considers valid/filled (false alarms)."""
        if not missing:
            return missing
        by_label = {}
        for f in fields:
            key = (f.get("label") or f.get("placeholder") or f.get("name") or "")[:80]
            by_label.setdefault(key, f)
        keep = []
        for m in missing:
            f = by_label.get(m)
            if not f:
                keep.append(m)
                continue
            idx = (f.get("optionIdx") or [f["idx"]])[0]
            try:
                ok = frame.evaluate(
                    """(i) => {
                       const roots = [document];
                       for (let k = 0; k < roots.length; k++)
                         for (const el of roots[k].querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
                       const e = roots.map(r => r.querySelector(`[data-nb-idx="${i}"]`)).find(Boolean);
                       if (!e) return false;
                       if (e.type === 'radio' || e.type === 'checkbox')
                         return !!e.getRootNode().querySelector(`input[name="${CSS.escape(e.name)}"]:checked`);
                       if (e.getAttribute('aria-invalid') === 'true') return false;
                       const v = (e.value !== undefined ? e.value : e.innerText) || '';
                       if (e.type === 'file') return e.files && e.files.length > 0;
                       return v.trim() !== '' && !/^(select|choose)/i.test(v.trim())
                              && (!e.checkValidity || e.checkValidity()); }""", idx)
            except PWError:
                ok = False
            if not ok:
                keep.append(m)
        return keep

    def fill(self, frame: Frame, fields: list[dict], title: str, company: str) -> list[str]:
        missing = []
        for f in fields:
            label = (f.get("label") or f.get("placeholder") or f.get("name") or "")[:80]
            try:
                if f["type"] == "file":
                    text = (label + " " + f.get("name", "")).lower()
                    if re.search(r"cover|photo|picture|image|avatar|autofill|auto-fill|parse", text) and \
                            not re.search(r"^(resume|cv)\b", text):
                        if f["required"] and "autofill" not in text:
                            missing.append(label or "file")
                        continue
                    if not (self.resume and self.resume.exists()):
                        if f["required"]:
                            missing.append("resume upload (run build_resumes.py or set company_apply.resume_pdf)")
                        continue
                    if not self.submit:
                        # preview: many sites upload the file the moment it is chosen -> don't send it
                        log.info("   (preview) would upload %s -> %s", self.resume.parent.name, label or f.get("name"))
                        continue
                    frame.locator(f"[data-nb-idx='{f['idx']}']").set_input_files(str(self.resume))
                    log.info("   upload resume (%s) -> %s", self.resume.parent.name, label or f.get("name"))
                    continue
                if f["type"] in ("radio", "checkbox"):
                    self._fill_choice(frame, f, missing)
                    continue
                if f["type"] == "combo":
                    self._fill_combo(frame, f, title, company, missing)
                    continue
                if f.get("value") and f["tag"] != "select":
                    continue  # prefilled
                value = self.filler.value_for(f, title, company)
                if value in (None, ""):
                    if f["required"]:
                        missing.append(label)
                    continue
                loc = frame.locator(f"[data-nb-idx='{f['idx']}']")
                if f["tag"] == "select":
                    loc.select_option(label=str(value))
                else:
                    if f["type"] == "number" and not re.fullmatch(r"-?\d+(\.\d+)?", str(value)):
                        if f["required"]:
                            missing.append(label)
                        continue
                    loc.fill(str(value))
                    if loc.get_attribute("role") == "combobox" or loc.get_attribute("aria-autocomplete"):
                        frame.page.wait_for_timeout(1500)  # typeahead (e.g. city): take the first suggestion
                        opts = frame.page.locator("[role=listbox] [role=option], .basic-typeahead__selectable")
                        if opts.count() and opts.first.is_visible():
                            opts.first.click()
                log.info("   %s = %s", label[:50], str(value)[:60].replace("\n", " "))
            except PWError as e:
                log.info("   could not fill '%s': %s", label, str(e).splitlines()[0][:80])
                if f["required"]:
                    missing.append(label)
        return missing

    def _fill_combo(self, frame: Frame, f: dict, title: str, company: str, missing: list[str]):
        """Custom (non-<select>) dropdown: open it, read the options, click the right one."""
        label = (f.get("label") or f.get("placeholder") or f.get("name") or "")[:80]
        el = frame.locator(f"[data-nb-idx='{f['idx']}']")
        el.evaluate("e => e.scrollIntoView({block: 'center'})")
        if f.get("value") and not re.match(r"^(select|choose|--|please)", f["value"], re.I)                 and f["value"].strip("* ") not in label:
            return  # already has a value
        try:
            el.click(timeout=4000)
        except PWError:  # covered by a sticky banner etc.
            el.click(force=True, timeout=4000)
        opts = []
        for _ in range(4):  # options can load a moment after the dropdown opens
            frame.page.wait_for_timeout(600)
            opts = frame.evaluate(OPEN_OPTIONS_JS)
            if opts:
                break
        texts = [o["text"] for o in opts if not re.match(r"^(select|choose|--|please)", o["text"], re.I)]
        choice = self.filler.value_for(dict(f, tag="select", options=texts), title, company) if texts else None
        if choice in texts:
            opt = frame.locator(f"[data-nb-opt='{opts[[o['text'] for o in opts].index(choice)]['i']}']").first
            try:
                opt.scroll_into_view_if_needed(timeout=2000)
                opt.click(timeout=4000)
            except PWError:
                opt.evaluate("e => e.click()")  # outside viewport / covered: click via DOM
            log.info("   %s -> %s", label[:50], choice)
        else:
            log.info("   %s: no matching option in %s", label[:50], texts[:12])
            self._close_overlay(frame)
            if f["required"]:
                missing.append(label)

    @staticmethod
    def _close_overlay(frame: Frame):
        page = frame.page
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        backdrop = page.locator(".cdk-overlay-backdrop")
        if backdrop.count():
            try:
                backdrop.first.click(force=True, timeout=2000)
            except PWError:
                pass
        page.wait_for_timeout(300)

    @staticmethod
    def _check(loc):
        """Tick a radio/checkbox even when the real input is hidden behind a styled label."""
        try:
            loc.check(force=True, timeout=3000)
            return
        except PWError:
            pass
        if not loc.evaluate("e => e.checked"):
            loc.evaluate("e => { (e.labels && e.labels[0] ? e.labels[0] : e).click(); }")
        if not loc.evaluate("e => e.checked"):
            loc.evaluate("e => e.click()")

    def _fill_choice(self, frame: Frame, f: dict, missing: list[str]):
        label, options = f.get("label", ""), f.get("options") or []
        if f["type"] == "checkbox" and len(options) == 1:
            text = (label + " " + options[0]).lower()
            if re.search(r"agree|consent|terms|privacy|accept|authori[sz]e|confirm|declare|acknowledge", text):
                self._check(frame.locator(f"[data-nb-idx='{f['optionIdx'][0]}']"))
                log.info("   [x] %s", options[0][:60])
            elif f["required"]:
                missing.append(label[:80])
            return
        choice = self.answerer.answer(label, options)
        if choice is None:
            if f["required"]:
                missing.append(label[:80])
            return
        i = options.index(choice)
        self._check(frame.locator(f"[data-nb-idx='{f['optionIdx'][i]}']"))
        log.info("   %s -> %s", label[:50], choice)

    def click_submit(self, frame: Frame) -> str | None:
        """Click submit (or Next on multi-step forms). Returns 'submit', 'next' or None."""
        for kind, pattern in (("submit", r"^\s*(submit|apply|apply now|send|send application|submit application|"
                                          r"apply for this (job|position)|finish|complete)\s*$"),
                              ("next", r"^\s*(next|continue|save (and|&) continue|proceed)\s*$")):
            loc = frame.locator("button, input[type=submit], input[type=button], [role=button], a.btn, a.button")
            for i in range(min(loc.count(), 60)):
                el = loc.nth(i)
                try:
                    txt = (el.inner_text(timeout=1000) or el.get_attribute("value") or "").strip()
                    if not txt:
                        txt = el.get_attribute("value") or ""
                    if re.match(pattern, txt, re.I) and el.is_visible() and el.is_enabled():
                        el.scroll_into_view_if_needed()
                        el.click()
                        log.info("   clicked '%s'", txt)
                        return kind
                except PWError:
                    continue
        sub = frame.locator("button[type=submit], input[type=submit]")
        if sub.count() and sub.first.is_visible():
            sub.first.click()
            return "submit"
        return None

    def wait_for_user(self, job: dict) -> tuple[str, str]:
        log.info("   ASSIST: check the form in the browser and submit it yourself (waiting %ss)...", self.assist_seconds)
        deadline = time.time() + self.assist_seconds
        while time.time() < deadline:
            try:
                self.page.wait_for_timeout(2000)
                if SUCCESS_TEXT.search(self.body_text()):
                    return "applied", "submitted by you (assist mode)"
            except PWError:
                break
        return "manual", "assist mode: not submitted in time"

    # --------------------------------------------------------------- email
    def send_email(self, to: str, job: dict) -> tuple[str, str]:
        if not to:
            return "manual", "email link without address"
        if not (self.cfg.company_apply.get("send_emails") and self.cfg.smtp_email and self.cfg.smtp_password):
            return "manual", f"email your resume to {to} (set SMTP_EMAIL / SMTP_APP_PASSWORD in .env to automate)"
        if not (self.resume and self.resume.exists()):
            return "manual", f"email your resume to {to} (resume PDF missing)"
        if not self.submit:
            return "filled", f"would email resume to {to} (--no-submit)"
        msg = build_email(self.cfg, to, job, self.filler.cover_letter(job.get("title", ""), job.get("company", "")),
                          self.resume)
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
                s.login(self.cfg.smtp_email, self.cfg.smtp_password)
                s.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            return "failed", f"email to {to} failed: {e}"
        return "applied", f"emailed resume to {to}"

    # ----------------------------------------------------------------- run
    def run(self, limit: int | None = None, retry_manual: bool = False, sources: tuple[str, ...] | None = None,
            statuses: tuple[str, ...] | None = None) -> dict:
        limit = limit or int(self.cfg.company_apply.get("max_per_run", 15))
        if statuses is None:
            statuses = ("pending", "manual", "unconfirmed", "failed") if retry_manual else ("pending",)
        jobs = self.db.external_jobs(statuses, limit=limit, sources=sources)
        log.info("%d company-site jobs to process", len(jobs))
        stats: dict[str, int] = {}
        for job in jobs:
            log.info("[%s] %s @ %s  (%s)", job.get("ats"), job["title"], job["company"], job["apply_url"][:90])
            self.page = self.ctx.pages[0]
            for extra in self.ctx.pages[1:]:
                extra.close()
            try:
                status, detail = self.apply_job(job)
            except PWError as e:
                shot = self.snapshot(f"error_{job['job_id']}")
                status, detail = "failed", f"{str(e).splitlines()[0][:150]} ({shot})"
            if status == "filled" or not self.submit:
                # preview run (--no-submit): report only, keep the job pending for the real run
                log.info(" -> %s (preview, not saved) %s", status.upper(), detail)
            else:
                self.db.update_external(job["job_id"], status, detail)
                log.info(" -> %s %s", status.upper(), detail)
            stats[status] = stats.get(status, 0) + 1
            time.sleep(2)
        return stats


def build_email(cfg: Config, to: str, job: dict, body: str, resume: Path) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{cfg.profile.get('name', '')} <{cfg.smtp_email}>"
    msg["To"] = to
    msg["Subject"] = f"Application for {job.get('title', 'the open role')} - {cfg.profile.get('name', '')}"
    msg.set_content(body)
    ctype = mimetypes.guess_type(resume.name)[0] or "application/pdf"
    maintype, subtype = ctype.split("/", 1)
    msg.add_attachment(resume.read_bytes(), maintype=maintype, subtype=subtype, filename=resume.name)
    return msg


def write_manual_page(db: Storage, path: Path) -> int:
    """HTML list of jobs you need to finish yourself, with clickable links."""
    rows = db.external_jobs(("manual", "failed", "unconfirmed", "pending"))
    items = []
    for r in rows:
        items.append(
            f"<tr><td>{html.escape(r['status'])}</td><td><b>{html.escape(r['title'] or '')}</b><br>"
            f"{html.escape(r['company'] or '')}<br><small>{html.escape(r['location'] or '')} · "
            f"{html.escape(r['experience'] or '')}</small></td><td>{html.escape(r['ats'] or '')}</td>"
            f"<td><a href='{html.escape(r['apply_url'] or r['naukri_url'] or '')}' target=_blank>Open apply page</a><br>"
            f"<a href='{html.escape(r['naukri_url'] or '')}' target=_blank><small>Naukri post</small></a></td>"
            f"<td><small>{html.escape(r['detail'] or '')}</small></td></tr>")
    path.write_text(
        "<!doctype html><meta charset=utf-8><title>Manual applications</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:16px;color:#222}table{border-collapse:collapse;width:100%}"
        "td,th{border-bottom:1px solid #ddd;padding:8px;vertical-align:top;text-align:left}th{background:#f4f4f4}"
        "@media(prefers-color-scheme:dark){body{background:#161616;color:#ddd}th{background:#262626}td,th{border-color:#333}a{color:#8ab4f8}}</style>"
        f"<h2>Company-site jobs to apply manually ({len(rows)})</h2>"
        "<table><tr><th>Status</th><th>Job</th><th>ATS</th><th>Links</th><th>Why</th></tr>"
        + "".join(items) + "</table>", encoding="utf-8")
    return len(rows)
