"""Workday (*.myworkdayjobs.com) apply flow: account (create or sign in) -> multi-step application.

Accounts are per company tenant (e.g. mastercard.wd1.myworkdayjobs.com) and stored in
data/accounts.csv. If Workday asks to verify the email, the link is read from Gmail (IMAP).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page

from .accounts import AccountVault, Inbox, site_key

log = logging.getLogger("company")

A = lambda aid: f"[data-automation-id='{aid}']"  # noqa: E731
DONE_TEXT = re.compile(r"application (was )?submitted|successfully submitted|thank you for (applying|your application)|"
                       r"congratulations|you('ve| have) applied", re.I)
EXISTS_TEXT = re.compile(r"already (exists|in use|registered)|account with this email", re.I)
VERIFY_TEXT = re.compile(r"verify (your )?(account|email)|verification (email|link)|check your email|"
                         r"activate your account", re.I)
BAD_LOGIN_TEXT = re.compile(r"(wrong|invalid|incorrect) (email|password|credentials|sign in)|"
                            r"email address or password.*(incorrect|invalid)|account (is )?locked", re.I)


class WorkdayFlow:
    def __init__(self, applier, vault: AccountVault, inbox: Inbox):
        self.ap = applier  # CompanyApplier: page, filler, fill(), snapshot(), submit flag ...
        self.vault = vault
        self.inbox = inbox
        self.new_accounts: list[dict] = []

    @property
    def page(self) -> Page:
        return self.ap.page

    def visible(self, sel: str) -> bool:
        loc = self.page.locator(sel)
        try:
            return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 3)))
        except PWError:
            return False

    def click(self, sel: str, timeout: int = 8000):
        """Workday puts an invisible 'click_filter' div over buttons; click that when present."""
        loc = self.page.locator(sel).first
        loc.wait_for(state="visible", timeout=timeout)
        box = loc.bounding_box()
        try:
            loc.click(timeout=3000)
        except PWError:
            if box:
                self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                loc.evaluate("e => e.click()")

    def body(self) -> str:
        try:
            return self.page.inner_text("body", timeout=5000)
        except PWError:
            return ""

    # ----------------------------------------------------------------- main
    def apply(self, job: dict) -> tuple[str, str]:
        page = self.page
        if not self.visible(A("adventureButton")):
            if re.search(r"doesn't exist|no longer|not available|closed", self.body(), re.I):
                return "expired", "job no longer on Workday"
            return "manual", "Workday Apply button not found"
        self.click(A("adventureButton"))
        page.wait_for_timeout(2500)
        if self.visible(A("applyManually")):
            self.click(A("applyManually"))
        page.wait_for_timeout(4000)

        status = self.account(job)
        if status:
            return status
        return self.steps(job)

    # -------------------------------------------------------------- account
    def account(self, job: dict) -> tuple[str, str] | None:
        """Sign in or create the account. None when we are in; else (status, detail)."""
        page = self.page
        site = site_key(page.url)
        email = self.ap.cfg.profile.get("email") or self.ap.cfg.email
        existing = self.vault.get(site)

        if not (self.visible(A("email")) or self.visible(A("signInLink")) or self.visible(A("createAccountLink"))):
            return None  # already signed in (session cookie)
        if not self.ap.submit:
            return "filled", f"would {'sign in to' if existing else 'create'} Workday account on {site} (--no-submit)"

        if existing:
            return self.sign_in(existing)

        if not self.visible(A("verifyPassword")) and self.visible(A("createAccountLink")):
            self.click(A("createAccountLink"))
            page.wait_for_timeout(2000)
        acc, _ = self.vault.get_or_create(site, "workday", email, note=f"{job.get('company')} Workday")
        started = datetime.now() - timedelta(minutes=1)
        page.locator(A("email")).first.fill(acc["email"])
        page.locator(A("password")).first.fill(acc["password"])
        page.locator(A("verifyPassword")).first.fill(acc["password"])
        if self.visible(A("createAccountCheckbox")):
            cb = page.locator(A("createAccountCheckbox")).first
            if not cb.is_checked():
                cb.check(force=True)
        submit = A("createAccountSubmitButton")
        self.click(f"{A('click_filter')}[aria-label*='Create Account'], {submit}" if self.visible(A("click_filter")) else submit)
        page.wait_for_timeout(6000)
        text = self.body()
        log.info("   Workday account created: %s / %s", acc["email"], acc["password"])
        self.new_accounts.append(acc)

        if EXISTS_TEXT.search(text):
            self.vault.set_note(site, "email already had an account here - password unknown, use 'Forgot password'")
            return "manual", f"a Workday account already exists on {site}; reset its password manually"
        if VERIFY_TEXT.search(text) or self.visible(A("signInLink")) and not self.visible(A("bottom-navigation-next-button")):
            ok = self.verify_email(site, started)
            if not ok:
                self.vault.set_note(site, "created - needs email verification")
                return "manual", f"verify the Workday email for {site}, then rerun (account saved in accounts.csv)"
            return self.sign_in(acc)
        return None

    def sign_in(self, acc: dict) -> tuple[str, str] | None:
        page = self.page
        if self.visible(A("signInLink")) and not self.visible(A("signInSubmitButton")):
            self.click(A("signInLink"))
            page.wait_for_timeout(2000)
        if not self.visible(A("email")):
            return None
        page.locator(A("email")).first.fill(acc["email"])
        page.locator(A("password")).first.fill(acc["password"])
        btn = A("signInSubmitButton")
        self.click(f"{A('click_filter')}[aria-label*='Sign In'], {btn}" if self.visible(A("click_filter")) else btn)
        page.wait_for_timeout(6000)
        if BAD_LOGIN_TEXT.search(self.body()) or self.visible(A("signInSubmitButton")):
            if VERIFY_TEXT.search(self.body()):
                return "manual", "Workday account not verified yet - open the verification email, then rerun"
            self.ap.snapshot("workday_signin_failed")
            return "manual", "Workday sign-in failed (see accounts.csv for the password)"
        return None

    def verify_email(self, site: str, since: datetime) -> bool:
        if not self.inbox.ready:
            log.info("   Workday wants email verification - no Gmail App Password in .env")
            return False
        log.info("   waiting for Workday verification email ...")
        tenant = site.split(".")[0]
        mail = self.inbox.wait_for(since, match=rf"workday|{re.escape(tenant)}", timeout=240)
        if not mail or not mail["links"]:
            return False
        link = next((l for l in mail["links"] if "workday" in l.lower()), mail["links"][0])
        self.page.goto(link, wait_until="domcontentloaded")
        self.page.wait_for_timeout(5000)
        log.info("   email verified")
        return True

    # ---------------------------------------------------------------- steps
    def current_step(self) -> str:
        try:
            return self.page.locator(A("progressBarActiveStep")).first.inner_text(timeout=3000).replace("\n", " ")
        except PWError:
            return ""

    def steps(self, job: dict) -> tuple[str, str]:
        page = self.page
        last, repeats = "", 0
        for _ in range(12):
            page.wait_for_timeout(2500)
            if DONE_TEXT.search(self.body()):
                self.ap.snapshot(f"applied_{job['job_id']}")
                return "applied", "Workday application submitted"
            step = self.current_step()
            log.info("   Workday step: %s", step or "?")
            if step == last:
                repeats += 1
                if repeats >= 2:
                    errs = self.errors()
                    shot = self.ap.snapshot(f"workday_stuck_{job['job_id']}")
                    return "manual", f"Workday stuck at '{step}': {errs[:200]} ({shot})"
            else:
                last, repeats = step, 0

            if re.search(r"review", step, re.I):
                if not self.ap.submit:
                    shot = self.ap.snapshot(f"workday_review_{job['job_id']}")
                    return "filled", f"reached Workday review page, not submitted (--no-submit) {shot}"
                self.click_bottom(r"submit")
                continue

            self.fill_step(job)
            if not self.ap.submit and not re.search(r"information|experience|question|disclosure|identify", step, re.I):
                return "filled", "not submitted (--no-submit)"
            self.click_bottom(r"save and continue|next|continue")
        return "manual", "Workday: too many steps"

    def errors(self) -> str:
        try:
            return " | ".join(t.strip() for t in self.page.locator(
                f"{A('errorMessage')}, [data-automation-id*='error'], [role=alert]").all_inner_texts() if t.strip())
        except PWError:
            return ""

    def click_bottom(self, pattern: str):
        btn = self.page.locator(f"{A('bottom-navigation-next-button')}, {A('pageFooterNextButton')}")
        if btn.count():
            self.click(f"{A('bottom-navigation-next-button')}, {A('pageFooterNextButton')}")
            return
        for b in self.page.locator("button").all():
            try:
                if re.search(pattern, b.inner_text(timeout=500), re.I) and b.is_visible():
                    b.click()
                    return
            except PWError:
                continue

    def fill_step(self, job: dict):
        page = self.page
        # "How did you hear about us" is a searchable multi-select prompt
        src = page.locator(f"{A('formField-source')} input, {A('formField-sourcePrompt')} input")
        if src.count() and src.first.is_visible():
            try:
                src.first.click()
                page.wait_for_timeout(1000)
                opts = page.locator(f"{A('promptOption')}, [role=option]")
                texts = [t.strip() for t in opts.all_inner_texts()]
                pick = next((i for i, t in enumerate(texts) if re.search(r"naukri", t, re.I)), None)
                if pick is None:
                    pick = next((i for i, t in enumerate(texts) if re.search(r"job ?board|job portal|online|website|internet", t, re.I)), None)
                if pick is not None:
                    opts.nth(pick).click()
                    page.wait_for_timeout(1000)
                    # categories open a second level: take the first leaf
                    sub = page.locator(A("promptOption"))
                    subtexts = [t.strip() for t in sub.all_inner_texts()]
                    leaf = next((i for i, t in enumerate(subtexts) if re.search(r"naukri", t, re.I)), None)
                    if leaf is not None:
                        sub.nth(leaf).click()
                    log.info("   How did you hear -> %s", texts[pick])
                page.keyboard.press("Escape")
            except PWError:
                pass
        frame, fields = self.page.main_frame, self.page.main_frame.evaluate(__import__(
            "naukri_bot.company", fromlist=["SCAN_JS"]).SCAN_JS)
        missing = self.ap.fill(frame, fields, job.get("title", ""), job.get("company", ""))
        missing = self.ap.still_missing(frame, fields, missing)
        if missing:
            log.info("   Workday fields not filled: %s", missing)
