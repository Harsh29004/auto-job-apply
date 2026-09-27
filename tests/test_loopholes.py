"""Regression tests for false positives / unsafe inputs found in the code audit."""
import dataclasses
import json
from pathlib import Path

import pytest

from naukri_bot.accounts import extract_code
from naukri_bot.answers import Answerer
from naukri_bot.bot import QUOTA_TEXT, SUCCESS_TEXT, NaukriBot
from naukri_bot.company import build_email, is_web_url, success_is_new, write_manual_page
from naukri_bot.foreign import Fetcher, location_open
from naukri_bot.models import Job
from naukri_bot.storage import Storage


@pytest.mark.parametrize("loc,ok", [
    ("USA Remote", False), ("United States - Remote", False), ("UK, Remote", False),
    ("Remote", True), ("Fully Remote", True), ("Remote - Worldwide", True),
])
def test_country_then_remote_is_not_open(loc, ok):
    assert location_open(loc, ["india"]) is ok


def test_naukri_ignores_phrases_already_in_job_description(cfg, tmp_path):
    bot = NaukriBot(cfg, Storage(tmp_path / "t.db"))
    jd = "ML techniques applied to real-world problems. Sales staff who reached the daily targets."
    bot._baseline = jd
    assert bot._new_hit(SUCCESS_TEXT, jd) is None
    assert bot._new_hit(QUOTA_TEXT, jd) is None
    assert bot._new_hit(SUCCESS_TEXT, jd + "\nYou have successfully applied").group(0) == "successfully applied"


def test_company_success_must_be_new():
    form = "Apply below. We will review your application and get back to you."
    assert not success_is_new(form, form)
    assert success_is_new(form, form + " Thank you for applying!")


def test_manual_page_never_links_javascript(tmp_path):
    db = Storage(tmp_path / "t.db")
    db.save_external(Job(job_id="1", title="AI Engineer", company="Acme", url="https://ok.example/post",
                         apply_url="javascript:alert(1)", external=True))
    write_manual_page(db, tmp_path / "m.html")
    page = (tmp_path / "m.html").read_text(encoding="utf-8")
    assert "javascript:" not in page and "https://ok.example/post" in page
    assert not is_web_url("file:///C:/x") and is_web_url("https://a.b/c")
    db.close()


def test_email_subject_with_newline_in_title(cfg, tmp_path):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    msg = build_email(dataclasses.replace(cfg, smtp_email="me@gmail.com"), "hr@acme.com",
                      {"title": "AI Engineer\r\nBcc: spam@evil.com"}, "Hi", pdf)
    assert "\n" not in msg["Subject"] and msg["Bcc"] is None


def test_otp_needs_a_code_word():
    assert extract_code("Your order 482913 has shipped to 395007") is None
    assert extract_code("Your verification code is 482913") == "482913"
    assert extract_code("482913 is your one-time passcode") == "482913"


def test_work_permit_unknown_city_not_guessed(cfg):
    a = Answerer(dict(cfg.profile, work_authorized_countries=["India"]), cfg.skills)
    q = "Are you authorized to work in the country where this job is located?"
    a.job_location = "London"
    assert a.answer(q, ["Yes", "No"]) is None
    a.job_location = "Remote"
    assert a.answer(q, ["Yes", "No"]) == "Yes"
    a.job_location = "Pune, India"
    assert a.answer(q, ["Yes", "No"]) == "Yes"


def test_fetcher_does_not_cache_bad_json(tmp_path, monkeypatch):
    calls = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

    bodies = [b"<html>rate limited</html>", json.dumps({"jobs": []}).encode()]
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: calls.append(1) or Resp(bodies[len(calls) - 1]))
    monkeypatch.setattr("time.sleep", lambda s: None)
    f = Fetcher(Path(tmp_path))
    with pytest.raises(ValueError):
        f.get("https://example.com/api")
    assert f.get("https://example.com/api") == {"jobs": []}  # fetched again, not the cached error page


def test_applied_today_statuses(tmp_path):
    db = Storage(tmp_path / "t.db")
    db.record(Job(job_id="1", title="a", company="b", url="u"), "applied")
    db.record(Job(job_id="2", title="a", company="c", url="u"), "unconfirmed")
    assert db.applied_today() == 1 and db.applied_today(("applied", "unconfirmed")) == 2
    db.close()
