from naukri_bot.linkedin import min_years_required, search_url


def test_search_url():
    url = search_url("python developer", "India", ["1", "2"], "r604800", 25)
    assert url.startswith("https://www.linkedin.com/jobs/search/?")
    assert "keywords=python+developer" in url and "location=India" in url
    assert "f_AL=true" in url and "f_E=1%2C2" in url and "f_TPR=r604800" in url and "start=25" in url


def test_min_years_required():
    assert min_years_required("We need 5+ years of experience in Python.") == 5
    assert min_years_required("Experience: 0-2 years") == 0
    assert min_years_required("1 to 3 years of relevant experience; 10 years company history") == 1
    assert min_years_required("Freshers welcome. Team of 30 people.") == 0
    assert min_years_required("Minimum 3 yrs exp in ML") == 3


def test_search_url_work_type():
    assert "f_WT=2" in search_url("ml engineer", "Worldwide", ["2"], "r604800", 0, ["2"])
    assert "f_WT" not in search_url("ml engineer", "India", ["2"], "r604800", 0, [])
