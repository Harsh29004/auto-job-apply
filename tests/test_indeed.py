"""Indeed bot tests. Browser tests run against local pages shaped like Indeed's, never indeed.com."""
import dataclasses
import re

import pytest

from naukri_bot.indeed import CARDS_JS, HUMAN_CHECK, SENT_TEXT, IndeedBot, search_url, with_country
from naukri_bot.models import Job
from naukri_bot.storage import Storage


def test_search_url():
    url = search_url("in.indeed.com", "python developer", "Pune", 7, 10)
    assert url.startswith("https://in.indeed.com/jobs?") and "q=python+developer" in url and "l=Pune" in url
    assert "fromage=7" in url and "start=10" in url and "iafilter" not in url
    assert "iafilter=1" in search_url("in.indeed.com", "ml", easy_apply_only=True)
    remote = search_url("www.indeed.com", "ml", "Remote")
    assert "l=&" in remote and "sc=0kf%3Aattr%28DSQF7%29%3B" in remote   # Indeed's remote filter
    assert "l=&" in search_url("in.indeed.com", "ml", "Anywhere") and "sc=" not in search_url("in.indeed.com", "ml", "Anywhere")


def test_with_country():
    assert with_country("Pune, Maharashtra", "in.indeed.com") == "Pune, Maharashtra, India"
    assert with_country("Remote", "in.indeed.com") == "Remote, India"
    assert with_country("Bengaluru, Karnataka, India", "in.indeed.com") == "Bengaluru, Karnataka, India"
    assert with_country("Austin, TX", "www.indeed.com") == "Austin, TX, United States"
    assert with_country("", "uk.indeed.com") == "United Kingdom"


def test_texts():
    assert SENT_TEXT.search("Your application has been submitted!")
    assert not SENT_TEXT.search("Research applied to real products; your application has a real impact.")
    assert HUMAN_CHECK.search("Additional Verification Required")
    assert not HUMAN_CHECK.search("Candidates must pass a background and security check.")


# ---------------------------------------------------------------- browser tests (server: conftest.py)
@pytest.fixture
def make_bot(cfg, tmp_path):
    bots = []

    def make(dry_run=False):
        # root=tmp_path: the bot's databases, debug files and queue stay out of your data/ folder
        safe = dataclasses.replace(cfg, root=tmp_path, smtp_email="", smtp_password="")
        bot = IndeedBot(safe, Storage(tmp_path / f"t{len(bots)}.db"), dry_run=dry_run, headless=True,
                        profile_dir=tmp_path / f"profile{len(bots)}").__enter__()
        bot.HOME = re.compile(r"(^|\.)indeed\.com$|^127\.0\.0\.1$")  # the local test pages play indeed.com
        bot.STEP_WAIT_MS = 300
        bots.append(bot)
        return bot
    yield make
    for b in bots:
        b.__exit__(None, None, None)


def job():
    return Job(job_id="indeed-T1", title="Python Developer", company="Acme", url="x", location="Pune, Maharashtra, India",
               description="We build ML services in Python and PyTorch.")


def test_search_cards(make_bot):
    bot = make_bot()
    bot.page.set_content("""
      <div class="job_seen_beacon"><h2 class="jobTitle"><a data-jk="a1"><span title="Python Developer">Python Developer</span>
        </a></h2><span data-testid="company-name">Acme</span><div data-testid="text-location">Remote</div>
        <span>Easily apply</span></div>
      <div class="job_seen_beacon"><h2 class="jobTitle"><a data-jk="b2"><span>new Data Scientist</span></a></h2>
        <span data-testid="company-name">Beta</span><div data-testid="text-location">Pune, Maharashtra</div></div>
      <div class="job_seen_beacon"><a data-jk="a1">same job again</a></div>""")
    cards = bot.page.evaluate(CARDS_JS)
    assert [(c["jk"], c["title"], c["company"], c["easy"]) for c in cards] == [
        ("a1", "Python Developer", "Acme", True), ("b2", "Data Scientist", "Beta", False)]


@pytest.mark.parametrize("kind", ["indeed", "tab"])
def test_apply_flow_submits(make_bot, server, kind):
    """Contact, resume, relevant experience, employer questions, review, submit - in this tab or a new one."""
    bot = make_bot()
    bot.page.goto(f"{server}/indeed_job.html?kind={kind}")
    status, detail = bot.apply(job())
    assert status == "applied", detail
    sub = bot.page.evaluate("JSON.parse(localStorage.getItem('indeed_submitted'))")
    p = bot.cfg.profile
    assert sub["firstName"] == p["name"].split()[0] and sub["lastName"] == p["name"].split()[-1]
    assert sub["phoneNumber"] == bot.forms.answerer.phone_numbers()[0]
    assert sub["resume"] == "Saved_Resume.pdf"                         # the resume chosen on Indeed is kept
    assert sub["jobTitle"] == p["current_designation"] and sub["companyName"] == p["current_company"]
    assert re.fullmatch(r"\d+(\.\d+)?", sub["pythonYears"]) and sub["relocate"] == "Yes"


def test_dry_run_never_submits(make_bot, server):
    bot = make_bot(dry_run=True)
    bot.page.goto(f"{server}/indeed_job.html")
    assert bot.apply(job())[0] == "planned"
    assert bot.page.evaluate("localStorage.getItem('indeed_submitted')") is None


def test_company_site_job_is_external(make_bot, server):
    bot = make_bot()
    bot.page.goto(f"{server}/indeed_job.html?kind=external")
    status, url = bot.apply(job())
    assert status == "external" and url.startswith("http://localhost:") and url.endswith("/job.html")


def test_already_applied(make_bot, server):
    bot = make_bot()
    bot.page.goto(f"{server}/indeed_job.html?kind=applied")
    assert bot.apply(job())[0] == "already_applied"


def test_unknown_required_question_stops(make_bot, server):
    """A question the bot can't answer: stop with needs_answer and log it - never submit a half-filled form."""
    bot = make_bot()
    bot.page.goto(f"{server}/indeed_job.html?kind=hard")
    status, detail = bot.apply(job())
    assert status == "needs_answer" and "Answer this question" in detail
    assert any("dates and time ranges" in q for q, _, _ in bot.db.unanswered())
    assert bot.page.evaluate("localStorage.getItem('indeed_submitted')") is None
