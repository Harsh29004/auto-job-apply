"""New job-board parsers (sample data, no network), the combined applied CSV, LinkedIn's 'sent' check."""
import csv
import json

from naukri_bot.applied import export_applied
from naukri_bot.foreign import (fetch_4dayweek, fetch_arc, fetch_landingjobs, fetch_themuse, fetch_workingnomads)
from naukri_bot.models import Job
from naukri_bot.storage import Storage


class FakeFetcher:
    """Serves canned responses by URL prefix."""

    def __init__(self, responses):
        self.responses = responses

    def _find(self, url):
        return next(v for k, v in self.responses.items() if url.startswith(k))

    def get(self, url, as_json=True, max_age=None):
        return self._find(url)

    def get_or_none(self, url, max_age=None):
        return self._find(url)


def test_workingnomads():
    f = FakeFetcher({"https://www.workingnomads.com": [
        {"url": "https://www.workingnomads.com/job/go/123/", "title": "Python Developer", "company_name": "Acme",
         "category_name": "Development", "tags": "python,django", "location": "Anywhere in India", "description": "<p>Hi</p>"},
        {"url": "https://www.workingnomads.com/job/go/124/", "title": "Writer", "company_name": "B",
         "category_name": "Writing", "tags": "", "location": "Global", "description": ""}]})
    jobs = fetch_workingnomads(f)
    assert [(j.source_id, j.title, j.location, j.tags) for j in jobs] == [
        ("123", "Python Developer", "Anywhere in India", ["python", "django"])]


def test_themuse():
    f = FakeFetcher({"https://www.themuse.com": {"results": [
        {"id": 7, "name": "Junior Data Scientist", "company": {"name": "Muse Co"}, "locations": [{"name": "Flexible / Remote"}],
         "levels": [{"name": "Entry Level"}], "categories": [{"name": "Data Science"}], "contents": "<p>Python</p>",
         "refs": {"landing_page": "https://www.themuse.com/jobs/museco/junior-ds"}}]}})
    jobs = fetch_themuse(f)
    assert jobs[0].company == "Muse Co" and jobs[0].location == "Flexible / Remote" and jobs[0].seniority == "Entry Level"
    assert jobs[0].apply_url == "https://www.themuse.com/jobs/museco/junior-ds"


def test_4dayweek_keeps_remote_only():
    f = FakeFetcher({"https://4dayweek.io": {"data": [
        {"id": "a", "title": "ML Engineer", "company": {"name": "Four"}, "url": "https://4dayweek.io/job/a",
         "work_arrangement": "remote", "level": "junior", "locations": [{"country": "United Kingdom"}], "skills": []},
        {"id": "b", "title": "ML Engineer", "company": {"name": "Four"}, "url": "https://4dayweek.io/job/b",
         "work_arrangement": "office", "locations": []}]}})
    jobs = fetch_4dayweek(f)
    assert [(j.source_id, j.location) for j in jobs] == [("a", "United Kingdom")]


def test_landingjobs_relocation_and_company():
    f = FakeFetcher({"https://landing.jobs": [
        {"id": 5, "title": "Python Developer", "url": "https://landing.jobs/at/acme-labs/python-developer-in-lisbon",
         "relocation_paid": True, "remote": False, "locations": [{"country_code": "PT"}], "tags": ["Python"],
         "role_description": "Build APIs"}]})
    j = fetch_landingjobs(f)[0]
    assert j.company == "Acme Labs" and j.location == "Europe, PT" and "Relocation package provided." in j.description


def test_arc_links():
    props = {"props": {"pageProps": {
        "arcJobs": [{"randomKey": "pnbw", "urlString": "remote-software-developer", "title": "Remote - Software Developer",
                     "company": {"randomKey": None}, "requiredCountries": [], "experienceLevel": "junior",
                     "categories": [{"name": "Python"}]}],
        "externalJobs": [{"randomKey": "pnby", "urlString": "cavu-planner", "title": "Planner", "company": {"name": "Cavu"},
                          "requiredCountries": ["US"], "experienceLevels": [], "categories": []}]}}}
    html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(props)}</script></html>'.encode()
    jobs = fetch_arc(FakeFetcher({"https://arc.dev": html}))
    assert jobs[0].apply_url == "https://arc.dev/remote-jobs/details/remote-software-developer-pnbw"
    assert jobs[0].location == "Worldwide" and jobs[1].location == "US" and jobs[1].company == "Cavu"


def test_applied_csv_combines_every_bot(tmp_path):
    naukri, linkedin = Storage(tmp_path / "naukri.db"), Storage(tmp_path / "linkedin.db")
    naukri.record(Job(job_id="1", title="ML Engineer", company="Acme", url="https://naukri.com/1"), "applied")
    naukri.record(Job(job_id="2", title="QA", company="Skip Co", url="u"), "skipped")
    linkedin.record(Job(job_id="li-3", title="AI Engineer", company="Beta", url="https://linkedin.com/3"), "unconfirmed")
    naukri.save_external(Job(job_id="x", title="Data Scientist", company="Gamma", url="https://remoteok.com/x",
                             apply_url="https://gamma.com/apply", external=True), 3, source="remoteok")
    naukri.update_external("x", "applied", "emailed resume to hr@gamma.com")
    naukri.close()
    linkedin.close()
    path, n = export_applied(tmp_path)
    with open(path, encoding="utf-8-sig") as f:
        rows = {r["company"]: r for r in csv.DictReader(f)}
    assert n == 3 and set(rows) == {"Acme", "Beta", "Gamma"}
    assert rows["Gamma"]["platform"] == "Remote OK" and rows["Gamma"]["applied_via"] == "email"
    assert rows["Beta"]["status"] == "unconfirmed" and rows["Acme"]["applied_via"] == "Naukri apply"


def test_linkedin_sent_dialog_in_shadow_dom(cfg, tmp_path):
    """LinkedIn's success dialog sits in a shadow root among many hidden matches - it must still be seen."""
    from playwright.sync_api import sync_playwright
    from naukri_bot.linkedin import LinkedInBot
    bot = LinkedInBot(cfg, Storage(tmp_path / "t.db"))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        bot.page = browser.new_page()
        hidden = "".join('<span style="display:none">Applied 3 days ago</span>' for _ in range(8))
        bot.page.set_content(f"{hidden}<div id=host></div>")
        assert not bot.submitted()
        bot.page.evaluate("""() => { const r = document.getElementById('host').attachShadow({mode: 'open'});
            r.innerHTML = '<div role=dialog><h2>Your application was sent to Acme!</h2></div>'; }""")
        assert bot.submitted()
        browser.close()
    bot.queue.close()
