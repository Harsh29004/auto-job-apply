"""Company-site bot tests. Browser tests run against local fixture pages only."""
import dataclasses
import functools
import http.server
import threading
from pathlib import Path

import pytest

from naukri_bot.answers import Answerer
from naukri_bot.ats import detect_ats, needs_login
from naukri_bot.company import CompanyApplier, FieldFiller, build_email, email_from_href, split_name
from naukri_bot.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"


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
    assert split_name("Harshkumar Rajubhai Panchal") == ("Harshkumar", "Panchal")


def test_field_filler(cfg):
    f = FieldFiller(cfg, Answerer(cfg.profile, cfg.skills))
    v = lambda label, **kw: f.value_for({"label": label, "name": "", "placeholder": "", "tag": "input", **kw},
                                        "AI Engineer", "Acme")
    assert v("First Name *") == "Harshkumar"
    assert v("Last Name") == "Panchal"
    assert v("Full Name") == "Harshkumar Rajubhai Panchal"
    assert v("Company Name") == "LogicGo Infotech"
    assert v("Email Address") == "harshpanchal2904@gmail.com"
    assert v("Mobile Number") == "9727309697"
    assert v("Current City") == "Surat"
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


# ---------------------------------------------------------------- browser tests
@pytest.fixture(scope="module")
def server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


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
    assert sub["first_name"] == "Harshkumar" and sub["last_name"] == "Panchal"
    assert sub["email"] == "harshpanchal2904@gmail.com" and sub["phone"] == "9727309697"
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
    assert ans["name"] == "Harshkumar Rajubhai Panchal" and ans["email"] == "harshpanchal2904@gmail.com"
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
