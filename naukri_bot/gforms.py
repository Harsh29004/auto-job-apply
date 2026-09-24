"""Fill and submit Google Forms (docs.google.com/forms, forms.gle) used as job applications.

Google Forms don't use real <input type=radio>/<select>; questions are div[role=listitem] with
div[role=radio] / div[role=checkbox] / div[role=listbox] controls, so they need their own filler.
Forms that need a Google sign-in (file upload questions, "sign in to continue") go to the manual list.
"""
from __future__ import annotations

import logging
import re

from playwright.sync_api import Error as PWError

from .filters import contains_term

log = logging.getLogger("company")

DONE_TEXT = re.compile(r"your response has been recorded|response (has been )?(recorded|submitted)|"
                       r"thank(s| you) for (applying|your (response|application|submission))", re.I)
SIGNIN_TEXT = re.compile(r"sign in to (continue|google)|you need permission|requires? (you to )?sign in|"
                         r"to fill out this form, you must be signed in", re.I)


def is_google_form(url: str) -> bool:
    return bool(re.search(r"docs\.google\.com/forms|forms\.gle/|forms\.google\.com", url or ""))


ITEMS_JS = r"""
() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const vis = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
  const items = [...document.querySelectorAll('div[role=listitem]')].filter(vis);
  return items.map((it, i) => {
    it.setAttribute('data-gf-idx', String(i));
    const head = it.querySelector('[role=heading]');
    let title = head ? clean(head.innerText) : '';
    const required = !!it.querySelector('[aria-label="Required question"]') || /\*\s*$/.test(title);
    title = title.replace(/\s*\*\s*$/, '');
    const opt = (els, attr) => els.map((e, n) => {
      e.setAttribute('data-gf-opt', `${i}-${n}`);
      return clean(e.getAttribute(attr) || e.getAttribute('aria-label') || e.innerText);
    });
    const radios = [...it.querySelectorAll('[role=radio]')];
    const checks = [...it.querySelectorAll('[role=checkbox]')];
    const listbox = it.querySelector('[role=listbox]');
    const groups = it.querySelectorAll('[role=radiogroup]').length;
    let kind = 'unknown', options = [];
    if (/add file|upload/i.test(it.innerText) && it.querySelector('[role=button]')) kind = 'file';
    else if (groups > 1) kind = 'grid';
    else if (radios.length) { kind = 'radio'; options = opt(radios, 'data-value'); }
    else if (checks.length) { kind = 'checkbox'; options = opt(checks, 'data-answer-value'); }
    else if (listbox) {
      kind = 'dropdown';
      options = [...listbox.querySelectorAll('[role=option]')].map(o => clean(o.getAttribute('data-value')))
                  .filter(v => v);
    }
    else if (it.querySelector('textarea')) kind = 'textarea';
    else if (it.querySelector('input[type=date]')) kind = 'date';
    else if (it.querySelector('input[type=time], input[aria-label*=Hour]')) kind = 'time';
    else if (it.querySelector('input[type=text], input[type=email], input[type=url], input[type=tel], input[type=number]')) {
      const inp = it.querySelector('input[type=text], input[type=email], input[type=url], input[type=tel], input[type=number]');
      kind = 'text';
      if (!title) title = clean(inp.getAttribute('aria-label') || '');
      if (inp.type === 'email') kind = 'email';
    }
    const other = radios.some(r => /^__other_option__$/.test(r.getAttribute('data-value') || ''));
    return {idx: i, title, required, kind, options, other};
  });
}
"""


class GoogleFormFlow:
    def __init__(self, applier):
        self.ap = applier

    @property
    def page(self):
        return self.ap.page

    def text(self) -> str:
        try:
            return self.page.inner_text("body", timeout=5000)
        except PWError:
            return ""

    def button(self, name: str):
        b = self.page.locator("div[role=button], button").filter(
            has_text=re.compile(rf"^\s*{name}\s*$", re.I))
        for i in range(b.count()):
            if b.nth(i).is_visible():
                return b.nth(i)
        return None

    # ------------------------------------------------------------------ main
    def apply(self, job: dict) -> tuple[str, str]:
        page = self.page
        page.wait_for_timeout(1500)
        if "accounts.google.com" in page.url or SIGNIN_TEXT.search(self.text()):
            return "manual", "Google Form needs a Google sign-in - fill it yourself"
        title, company = job.get("title", ""), job.get("company", "")
        for step in range(8):
            items = page.evaluate(ITEMS_JS)
            missing = []
            for it in items:
                try:
                    if not self.fill_item(it, title, company) and it["required"]:
                        missing.append(it["title"][:80] or it["kind"])
                except PWError as e:
                    log.info("   could not fill '%s': %s", it["title"][:50], str(e).splitlines()[0][:80])
                    if it["required"]:
                        missing.append(it["title"][:80])
            if missing:
                shot = self.ap.snapshot(f"gform_missing_{job['job_id']}")
                return "manual", f"Google Form: can't answer required {missing[:4]} ({shot})"
            nxt, submit = self.button("Next"), self.button("Submit")
            if submit:
                if not self.ap.submit:
                    shot = self.ap.snapshot(f"gform_filled_{job['job_id']}")
                    return "filled", f"Google Form filled, not submitted (--no-submit). screenshot: {shot}"
                submit.click()
                page.wait_for_timeout(3000)
                if DONE_TEXT.search(self.text()):
                    self.ap.snapshot(f"applied_{job['job_id']}")
                    return "applied", "Google Form submitted"
                errors = page.locator("[role=alert]").all_inner_texts()
                shot = self.ap.snapshot(f"gform_unclear_{job['job_id']}")
                return ("failed", f"Google Form errors: {[e for e in errors if e.strip()][:3]} ({shot})") if any(
                    e.strip() for e in errors) else ("unconfirmed", f"clicked Submit, no confirmation ({shot})")
            if nxt:
                nxt.click()
                page.wait_for_timeout(2000)
                if [e for e in page.locator("[role=alert]").all_inner_texts() if e.strip()]:
                    shot = self.ap.snapshot(f"gform_stuck_{job['job_id']}")
                    return "manual", f"Google Form rejected an answer ({shot})"
                continue
            return "manual", "Google Form: no Next/Submit button"
        return "manual", "Google Form has too many pages"

    # ------------------------------------------------------------ one question
    def fill_item(self, it: dict, title: str, company: str) -> bool:
        """True when the question is answered (or safely left empty because it is optional)."""
        kind, q, options = it["kind"], it["title"], it["options"]
        item = self.page.locator(f"[data-gf-idx='{it['idx']}']")
        filler, answerer = self.ap.filler, self.ap.answerer
        field = {"label": q, "placeholder": "", "name": "", "tag": "input", "type": kind, "options": []}

        if kind in ("file", "grid", "unknown"):
            return not it["required"]
        if kind in ("text", "email", "textarea"):
            box = item.locator("textarea, input").locator("visible=true").first
            if box.input_value():
                return True  # prefilled (e.g. collected email)
            value = self.ap.cfg.profile.get("email") if kind == "email" else filler.value_for(field, title, company)
            if value in (None, ""):
                return False
            box.fill(str(value))
            log.info("   %s = %s", q[:50], str(value)[:60].replace("\n", " "))
            return True
        if kind == "date":
            return not it["required"]

        if kind == "checkbox":
            picks = self.pick_checkboxes(q, options)
            for n in picks:
                item.locator(f"[data-gf-opt='{it['idx']}-{n}']").click()
            if picks:
                log.info("   %s -> %s", q[:50], [options[n] for n in picks])
            return bool(picks) or not it["required"]

        # radio / dropdown: one choice
        choice = filler.value_for(dict(field, tag="select", options=options), title, company)
        if choice not in options:
            choice = answerer.answer(q, options)
        if choice not in options:
            return False
        n = options.index(choice)
        if kind == "radio":
            item.locator(f"[data-gf-opt='{it['idx']}-{n}']").click()
        else:
            item.locator("[role=listbox]").click()
            self.page.wait_for_timeout(700)
            opt = self.page.locator(f"[role=option][data-value=\"{choice}\"]").locator("visible=true")
            opt.last.click()
            self.page.wait_for_timeout(500)
        log.info("   %s -> %s", q[:50], choice)
        return True

    def pick_checkboxes(self, question: str, options: list[str]) -> list[int]:
        skills = self.ap.cfg.skills
        # "Which of these skills / tools do you know?" -> tick the ones on your resume
        skill_hits = [n for n, o in enumerate(options) if any(contains_term(o, s) or contains_term(s, o.lower())
                                                               for s in skills if len(s) > 1)]
        if len(skill_hits) >= 1 and re.search(r"skill|tool|technolog|language|framework|stack|familiar|experience",
                                              question, re.I):
            return skill_hits
        # single consent box
        if len(options) == 1 and re.search(r"agree|consent|confirm|accept|acknowledge|authori", options[0] + question, re.I):
            return [0]
        choice = self.ap.answerer.answer(question, options)
        return [options.index(choice)] if choice in options else []
