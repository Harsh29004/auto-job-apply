"""Company-site bot tests. Browser tests run against local fixture pages only."""
import dataclasses

import pytest

from naukri_bot.answers import Answerer
from naukri_bot.ats import detect_ats, needs_login
from naukri_bot.company import SCAN_JS, CompanyApplier, FieldFiller, build_email, email_from_href, split_name
from naukri_bot.storage import Storage


def test_detect_ats():
    assert detect_ats("https://cisco.wd5.myworkdayjobs.com/en-US/x") == "workday"
    assert detect_ats("https://egug.fa.us2.oraclecloud.com/hcmUI/x") == "oracle"
    assert detect_ats("https://jobs.lever.co/acme/123") == "lever"
    assert detect_ats("https://docs.google.com/forms/d/e/x/viewform") == "googleforms"
    assert detect_ats("https://www.estontec.com/career/aiml-engineer") == "company-site"
    assert detect_ats("") == ""
    assert needs_login("workday") and not needs_login("lever") and not needs_login("company-site")


def test_email_from_href():
    assert email_from_href("mailto:hr@acme.com?subject=Job") == "hr@acme.com"
    assert email_from_href("https://mail.google.com/mail/?view=cm&fs=1&to=hr%40cyberimpulses.com&su=x") == "hr@cyberimpulses.com"
    assert email_from_href("https://wa.me/+91123") is None
    assert split_name("Asha Kumari Verma") == ("Asha", "Verma")


def test_field_filler(cfg):
    f = FieldFiller(cfg, Answerer(cfg.profile, cfg.skills))
    v = lambda label, **kw: f.value_for({"label": label, "name": "", "placeholder": "", "tag": "input", **kw},
                                        "AI Engineer", "Acme")
    P = cfg.profile
    first, last = split_name(P["name"])
    assert v("First Name *") == first
    assert v("Last Name") == last
    assert v("Full Name") == P["name"]
    assert v("Company Name") == P["current_company"]
    assert v("Email Address") == P["email"]
    assert v("Mobile Number") == P["phone"]
    assert v("Current City") == P["current_location"]
    assert v("Location") == f'{P["current_location"]}, {P.get("country") or "India"}'
    assert v("Country of residence") == "India"
    assert v("First and Last Name") == P["name"]
    assert v("How did you hear about us?") == "Naukri.com"
    assert "AI Engineer" in v("Cover Letter") and "Acme" in v("Cover Letter")
    assert v("Notice period") == "Immediate"
    assert v("Total Experience", tag="select", options=["Select", "Fresher", "1-2 years", "3-5 years"]) == "1-2 years"
    assert v("Father's name") is None


def test_match_title(cfg):
    f = FieldFiller(cfg, Answerer(cfg.profile, cfg.skills))
    field = {"options": ["Full-Stack Developer", "Flutter Developer", "QA Engineer", "HR Executive"]}
    assert f.match_title("Software Engineer", field) is None           # no real word in common
    assert f.match_title("Full Stack Developer", field) == "Full-Stack Developer"
    assert f.match_title("Senior QA Engineer", field) == "QA Engineer"


def test_build_email(cfg, tmp_path):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    cfg2 = dataclasses.replace(cfg, smtp_email="me@gmail.com")
    msg = build_email(cfg2, "hr@acme.com", {"title": "AI Engineer"}, "Hello", pdf)
    assert msg["To"] == "hr@acme.com" and "AI Engineer" in msg["Subject"]
    assert [p.get_filename() for p in msg.iter_attachments()] == ["cv.pdf"]


# ---------------------------------------------------------------- browser tests (server: conftest.py)
@pytest.fixture
def applier(cfg, tmp_path):
    db = Storage(tmp_path / "t.db")
    safe = dataclasses.replace(cfg, smtp_email="", smtp_password="")  # never send real email from tests
    with CompanyApplier(safe, db, submit=True, headless=True, profile_dir=tmp_path / "profile") as bot:
        yield bot


def job(url, title="AI/ML Engineer"):
    return {"job_id": "T1", "title": title, "company": "Acme", "apply_url": url}


def test_form_flow_submits(applier, server):
    status, detail = applier.apply_job(job(server + "/job.html"))
    assert status == "applied", detail
    sub = applier.page.evaluate("JSON.parse(localStorage.getItem('submitted'))")
    P = applier.cfg.profile
    first, last = split_name(P["name"])
    assert sub["first_name"] == first and sub["last_name"] == last
    assert sub["email"] == P["email"] and sub["phone"] == P["phone"]
    assert sub["experience"] == "1-2 years" and sub["reloc"] == "y"
    assert sub["notice"] == "Immediate" and sub["source"] == "Naukri.com"
    assert sub["resume"].endswith("_Resume.pdf") and sub["consent"] == "on"  # tailored copy, neutral name
    assert "AI/ML Engineer" in sub["cover"]
    assert sub["position"] == "AI/ML Engineer"          # title-matched <select>
    assert sub["qualification"] == "B.Tech/B.E."        # custom (non-select) dropdown


def test_no_submit_mode(cfg, tmp_path, server):
    with CompanyApplier(cfg, Storage(tmp_path / "t.db"), submit=False, headless=True,
                        profile_dir=tmp_path / "profile") as bot:
        status, _ = bot.apply_job(job(server + "/form.html"))
        assert status == "filled"
        assert bot.page.evaluate("localStorage.getItem('submitted')") is None or "Thank you" not in bot.body_text()


def test_picks_matching_opening(applier, server):
    status, detail = applier.apply_job(job(server + "/openings.html", "Python Developer Intern"))
    assert status == "applied", detail


def test_email_route_without_smtp_is_manual(applier, server):
    status, detail = applier.apply_job(job(server + "/mail.html", "Web Developer"))
    assert status == "manual" and "hr@example.com" in detail


def test_whatsapp_is_manual(applier, server):
    status, detail = applier.apply_job(job(server + "/whatsapp.html", "Fullstack Developer"))
    assert status == "manual" and "WhatsApp" in detail


def test_login_ats_is_manual(applier):
    status, detail = applier.apply_job(job("https://cisco.wd5.myworkdayjobs.com/en-US/x"))
    assert status == "manual" and "workday" in detail


def test_google_form_flow(applier, server):
    status, detail = applier.apply_job(job(server + "/gform.html?docs.google.com/forms", "Python Developer"))
    assert status == "applied", detail
    ans = applier.page.evaluate("JSON.parse(localStorage.getItem('gform'))")
    P = applier.cfg.profile
    assert ans["name"] == P["name"] and ans["email"] == P["email"]
    assert ans["pyexp"] == "0-1 years"               # 1 year of Python -> first range containing 1
    assert ans["skills"] == ["Python", "React"]      # only skills on the resume are ticked
    assert ans["notice"] == "Immediate"
    assert "ML" in ans["why"] or "Python" in ans["why"]  # why_join answer from config
    assert ans["linkedin"].startswith("https://www.linkedin.com/in/")


def test_google_form_no_submit(cfg, tmp_path, server):
    with CompanyApplier(cfg, Storage(tmp_path / "t.db"), submit=False, headless=True,
                        profile_dir=tmp_path / "profile") as bot:
        status, detail = bot.apply_job(job(server + "/gform.html?docs.google.com/forms", "Python Developer"))
        assert status == "filled", detail
        assert bot.page.evaluate("localStorage.getItem('gform')") is None


def test_tailored_resume_is_uploaded(applier, server):
    status, _ = applier.apply_job(job(server + "/job.html", "Generative AI Engineer"))
    assert status == "applied"
    variants = {p.stem for p in (applier.cfg.root / "resumes").glob("*.pdf")}
    if "genai_llm_engineer" in variants:  # resumes built with build_resumes.py
        assert applier.resume.parent.name == "genai_llm_engineer"
    assert applier.resume.exists()


@pytest.mark.parametrize("query,changes", [("", 0), ("?cc=none", 1)])
def test_linkedin_style_contact_step(applier, server, query, changes):
    """LinkedIn re-renders the phone box when the country code changes, and later fields after an answer.
    The phone must still be filled, the code must be India, and a dropdown that is already right is left alone."""
    applier.open(server + "/contact.html" + query)
    frame = applier.page.main_frame
    missing = applier.fill(frame, frame.evaluate(SCAN_JS, None), "AI Engineer", "Acme")
    state = applier.page.evaluate("""() => ({cc: document.getElementById('ef-phoneNumber-country').value,
        phone: document.getElementById('ef-phoneNumber-nationalNumber').value,
        city: document.getElementById('city').value, changes: window.changes})""")
    assert missing == [] and state["cc"] == "India (+91)" and state["changes"] == changes
    assert state["phone"] == applier.answerer.phone_numbers()[1]  # national number: the code has its own dropdown
    assert state["city"]


def test_form_on_hidden_tab_and_screenshot_box(applier, server):
    """The form is on a hidden tab behind 'Apply': open it, fill it, and put the resume only in the CV box."""
    status, detail = applier.apply_job(job(server + "/tabbed.html", "Junior Full-Stack Developer"))
    assert status == "applied", detail
    sub = applier.page.evaluate("JSON.parse(localStorage.getItem('tabbed'))")
    assert sub["name"] == applier.cfg.profile["name"] and sub["email"] == applier.cfg.profile["email"]
    assert sub["cv"].endswith("_Resume.pdf") and sub["shots"] == 0


def test_resume_only_goes_into_resume_boxes():
    ok = CompanyApplier.resume_upload
    assert ok("cv or resume *") and ok("resume/cv attach resume/cv") and ok("upload file") and ok("")
    assert not ok("attach a screenshot of your internet speed") and not ok("cover letter") and not ok("profile photo")
    assert not ok("autofill from resume")


def test_label_beats_placeholder(cfg):
    f = FieldFiller(cfg, Answerer(cfg.profile, cfg.skills))
    field = {"label": "LinkedIn profile URL *", "placeholder": "https://linkedin.com/in/your-name", "name": "",
             "tag": "input", "type": "text", "options": []}
    assert f.value_for(field, "AI Engineer", "Acme") == (cfg.profile.get("linkedin") or None)


def test_share_job_widget_is_not_a_form(applier):
    applier.page.set_content("""<form><label for=a>Your Name</label><input id=a>
        <label for=b>Recipient's Email address</label><input id=b type=email>
        <label for=c>Your Email address</label><input id=c type=email><button>Send</button></form>""")
    assert applier.scan() == (None, [])


def test_yes_no_toggle_checkboxes_are_answered_honestly(applier, server):
    """Ashby Yes/No toggles (one unlabelled checkbox each) must not all be ticked as 'consent'."""
    applier.answerer.job_location = "United States (Remote)"
    applier.open(server + "/ashby_toggles.html")
    frame, fields = applier.scan()
    missing = applier.fill(frame, fields, "AI Research Intern", "Acme")
    ticked = applier.page.evaluate("[...document.querySelectorAll('input[type=checkbox]:checked')].map(e => e.name)")
    assert "auth" not in ticked and "masters" not in ticked          # never claim US work rights / a degree plan
    assert "sponsor" in ticked and "accurate" in ticked              # true: needs sponsorship; a plain confirmation
    assert any("authorized to work" in m for m in missing)            # required and the answer is No -> you decide


def test_gmail_login_rejected_once_stops_emails(cfg, tmp_path, monkeypatch):
    import smtplib
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    bot = CompanyApplier(dataclasses.replace(cfg, smtp_email="me@gmail.com", smtp_password="x"),
                         Storage(tmp_path / "t.db"), submit=True, headless=True)
    bot.resume = pdf
    logins = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            logins.append(1)
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    job = {"job_id": "1", "title": "AI Engineer", "company": "Acme"}
    assert bot.send_email("hr@acme.com", job)[0] == "failed"
    assert bot.send_email("hr@beta.com", job)[0] == "failed"
    assert len(logins) == 1  # tried Gmail once, not once per job


def test_ashby_required_country_and_source(applier, server):
    """Ashby: required-by-CSS-class fields, a country type-ahead labelled by its box, a 'how did you hear' radio."""
    status, detail = applier.apply_job(job(server + "/ashby_form.html", "Full-Stack Engineer"))
    assert status == "applied", detail
    sub = applier.page.evaluate("JSON.parse(localStorage.getItem('ashby'))")
    assert sub == {"loc": "India", "src": "Job board"}


def test_only_the_application_form_is_filled(applier, server):
    status, detail = applier.apply_job(job(server + "/multiform.html", "ML Engineer"))
    assert status == "applied", detail
    sub = applier.page.evaluate("JSON.parse(localStorage.getItem('multi'))")
    assert sub["email"] == applier.cfg.profile["email"] and sub["cv"] == 1
    assert applier.page.evaluate("localStorage.getItem('connected')") is None


def test_form_cleared_after_submit_is_not_failed(applier, server):
    """A form that clears itself after sending: 'unconfirmed' (never retried), not 'failed' (would apply twice)."""
    status, detail = applier.apply_job(job(server + "/reset_form.html", "Data Engineer"))
    assert status == "unconfirmed" and "cleared" in detail, detail
    assert applier.page.evaluate("localStorage.getItem('sent')") == "1"


def test_company_asks_for_email_not_contact_form(applier, server):
    status, detail = applier.apply_job(job(server + "/reset_form.html?ask=email", "Data Engineer"))
    assert "jobs@acme.example" in detail      # routed to email (manual here: no SMTP in tests)
    assert applier.page.evaluate("localStorage.getItem('sent')") is None
