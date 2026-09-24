import pytest

from naukri_bot.foreign import ForeignJob, eligible, location_open, offers_sponsorship

OPTS = {"countries": ["india"], "max_years": 2, "allow_relocation": True}


def fj(**kw):
    base = dict(source="himalayas", source_id="1", title="Machine Learning Engineer", company="Acme",
                listing_url="https://example.com/job", apply_url="https://example.com/apply", location="Worldwide",
                description="We use Python, PyTorch and machine learning. 1+ years of experience.",
                seniority="Entry-level")
    base.update(kw)
    return ForeignJob(**base)


@pytest.mark.parametrize("loc,ok", [
    ("Worldwide", True), ("Anywhere in the World", True), ("APAC, EMEA", True), ("India", True), ("", True),
    ("Remote", True), ("USA Only", False), ("United States", False), ("Remote - US", False),
    ("Americas, Europe, Israel", False), ("EMEA", False),
])
def test_location_open(loc, ok):
    assert location_open(loc, ["india"]) is ok


@pytest.mark.parametrize("text,ok", [
    ("We offer visa sponsorship and relocation support.", True),
    ("Relocation package available", True),
    ("We are unable to provide visa sponsorship for this role.", False),
    ("Relocation support is not available for this position", False),
    ("Relocation Assistance Provided: No - This is a remote position", False),
])
def test_offers_sponsorship(text, ok):
    assert offers_sponsorship(text) is ok


def test_eligible_worldwide_junior(cfg):
    ok, reason, score = eligible(fj(), cfg, OPTS)
    assert ok, reason
    assert score >= 2


@pytest.mark.parametrize("kw,why", [
    ({"title": "Senior Machine Learning Engineer"}, "senior"),
    ({"location": "USA Only"}, "location"),
    ({"description": "Python ML role. Must be based in the United States."}, "restricted"),
    ({"description": "Python machine learning. Only those legally authorized to work in the United States."},
     "restricted"),
    ({"title": "Desarrollador Python"}, "English"),
    ({"title": "Machine Learning Engineer (Remote @ Colombia)"}, "Colombia"),
    ({"description": "Python and machine learning. 5+ years of experience required."}, "yrs"),
    ({"seniority": "Senior"}, "seniority"),
])
def test_not_eligible(cfg, kw, why):
    ok, reason, _ = eligible(fj(**kw), cfg, OPTS)
    assert not ok and why.lower() in reason.lower(), reason


def test_relocation_job_abroad(cfg):
    job = fj(location="Paris", description="Python, PyTorch, machine learning. We offer visa sponsorship and relocation.")
    assert eligible(job, cfg, OPTS)[0]
    assert not eligible(job, cfg, dict(OPTS, allow_relocation=False))[0]
    no_sponsor = fj(location="Paris", description="Python, PyTorch, machine learning. Relocation support is not available.")
    assert not eligible(no_sponsor, cfg, OPTS)[0]
