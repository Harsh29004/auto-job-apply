from naukri_bot.bot import search_url
from naukri_bot.filters import contains_term, evaluate
from naukri_bot.models import Job, is_external
from naukri_bot.storage import Storage

API_JOB = {
    "title": "Machine Learning (ML) Trainee Engineer", "jobId": "100926918039", "companyName": "Interface Infosys",
    "tagsAndSkills": "Python,Machine Learning,TensorFlow,Deep Learning,NLP",
    "placeholders": [{"type": "experience", "label": "0-1 Yrs"}, {"type": "salary", "label": "Not disclosed"},
                     {"type": "location", "label": "Chennai, Bengaluru"}],
    "jdURL": "/job-listings-machine-learning-ml-trainee-engineer-interface-infosys-chennai-bengaluru-0-to-1-years-100926918039",
    "jobDescription": "Work on <b>PyTorch</b> models", "minimumExperience": "0", "maximumExperience": "1",
    "companyApplyJob": False,
}


def job(**kw):
    d = dict(API_JOB)
    d.update(kw)
    return Job.from_api(d)


def test_from_api():
    j = job()
    assert j.url.startswith("https://www.naukri.com/job-listings-")
    assert j.location == "Chennai, Bengaluru" and j.min_exp == 0 and not j.external
    assert "python" in j.skills and j.description == "Work on PyTorch models"


def test_external_detection():
    assert is_external({"applyRedirectUrl": "https://careers.payu.in/x"})
    assert is_external({"companyApplyJob": True})
    assert is_external({"mode": "crawled"})
    assert not is_external({"companyApplyJob": False})


def test_contains_term_word_boundary():
    assert contains_term("AI Engineer", "ai")
    assert not contains_term("Maintenance Engineer", "ai")
    assert contains_term("Sr. Python Developer", "sr")
    assert contains_term("ASP.NET developer", ".net")
    assert contains_term("React.js Developer", "react")


def test_evaluate(cfg):
    ok, reason, score = evaluate(job(), cfg.filters, cfg.skills)
    assert ok and score >= 5, reason
    assert not evaluate(job(title="Senior ML Engineer"), cfg.filters, cfg.skills)[0]
    assert not evaluate(job(title="Java Developer"), cfg.filters, cfg.skills)[0]
    assert not evaluate(job(title="Sales Executive"), cfg.filters, cfg.skills)[0]
    assert not evaluate(job(minimumExperience="3"), cfg.filters, cfg.skills)[0]
    assert not evaluate(job(companyApplyJob=True), cfg.filters, cfg.skills)[0]
    # only "python" (from the title) matches -> score 1 < min_score 2
    assert not evaluate(job(title="Python Trainee", tagsAndSkills="Excel", jobDescription=""), cfg.filters, cfg.skills)[0]
    assert evaluate(job(title="React Developer", tagsAndSkills="React,JavaScript,HTML,CSS"), cfg.filters, cfg.skills)[0]
    assert evaluate(job(title="Python Developer", tagsAndSkills="Python,Django,SQL"), cfg.filters, cfg.skills)[0]


def test_search_url():
    assert search_url("python developer", "", 1, 1, 7) == \
        "https://www.naukri.com/python-developer-jobs?k=python%20developer&experience=1&jobAge=7"
    assert search_url("python developer", "", 2, 1, None) == \
        "https://www.naukri.com/python-developer-jobs-2?k=python%20developer&experience=1"
    assert search_url("ai ml engineer", "Bangalore", 1, 1, None) == \
        "https://www.naukri.com/ai-ml-engineer-jobs-in-bangalore?k=ai%20ml%20engineer&l=Bangalore&experience=1"


def test_storage(tmp_path):
    db = Storage(tmp_path / "t.db")
    j = job()
    assert not db.is_done(j.job_id)
    db.record(j, "skipped", "x")
    assert not db.is_done(j.job_id)
    db.record(j, "applied")
    assert db.is_done(j.job_id) and db.applied_today() == 1
    db.record_unanswered("Why us?", [], j.job_id)
    db.record_unanswered("Why us?", [], j.job_id)
    assert db.unanswered()[0][2] == 2
    assert db.export_csv(tmp_path / "o.csv") == 1
    db.close()
