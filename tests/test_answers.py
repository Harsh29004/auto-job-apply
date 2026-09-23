import pytest

from naukri_bot.answers import Answerer, parse_range, pick_numeric_option


@pytest.mark.parametrize("question,expected", [
    ("How many years of experience do you have in Python?", "1"),
    ("How many years of experience do you have in Machine Learning?", "1"),
    ("How many years of experience do you have in TensorFlow?", "1"),
    ("How many years of experience do you have in Kubernetes?", "0"),
    ("How many years of experience do you have in Java?", "0"),
    ("What is your total years of experience?", "1"),
    ("Total experience in years", "1"),
    ("How many years of relevant experience do you have?", "1"),
    ("Years of experience in React.js", "1"),
    ("What is your current CTC in Lakhs?", "2.4"),
    ("What is your expected CTC (LPA)?", "4"),
    ("Expected salary per month?", "33333"),
    ("Current CTC in INR", "240000"),
    ("What is your notice period?", "Immediate"),
    ("What is your notice period in days?", "0"),
    ("What is your current location?", "Surat"),
    ("Are you willing to relocate to Bangalore?", "Yes"),
    ("Are you comfortable working from office 5 days a week?", "Yes"),
    ("Can you join immediately?", "Yes"),
    ("Have you previously applied to this company?", "No"),
    ("Do you have any backlogs?", "No"),
    ("Are you currently pursuing any degree?", "No"),
    ("Do you have experience in Python?", "Yes"),
    ("Do you have hands-on experience with Kubernetes?", "No"),
    ("Do you have experience working with PyTorch?", "Yes"),
    ("What is your highest qualification?", "B.Tech"),
    ("Year of passing graduation?", "2026"),
    ("What is your 12th percentage?", "60"),
    ("10th percentage", "74.17"),
    ("What is your current company name?", "LogicGo Infotech"),
    ("Please enter your mobile number", "9727309697"),
    ("Your email id", "harshpanchal2904@gmail.com"),
    ("Total experience in months", "8"),
])
def test_text_answers(answerer, question, expected):
    assert answerer.answer(question) == expected


def test_unknown_question_returns_none(answerer):
    assert answerer.answer("What is your favourite programming paradigm?") is None
    assert answerer.answer("What is your LinkedIn profile URL?") is None  # empty in config


def test_open_ended_questions(answerer, cfg):
    p = cfg.profile
    assert answerer.answer("Briefly explain one project related to Generative models.") == p["project_summary"]
    assert answerer.answer("Describe a challenging project you worked on") == p["project_summary"]
    assert answerer.answer("Tell us about yourself") == p["about_me"]
    assert answerer.answer("Why do you want to join our company?") == p["why_join"]
    # open-ended answers are never used for option questions
    assert answerer.answer("Describe your project", ["Skip this question"]) == "Skip this question"
    # empty -> skip the job instead
    a = Answerer(dict(p, project_summary=""), cfg.skills)
    assert a.answer("Explain one project you did") is None


def test_ctc_empty_is_unanswered(cfg):
    a = Answerer(dict(cfg.profile, current_ctc_lpa="", expected_ctc_lpa=""), cfg.skills)
    assert a.answer("What is your current CTC?") is None
    assert a.answer("Expected CTC?") is None


@pytest.mark.parametrize("question,options,expected", [
    ("Are you willing to relocate?", ["Yes", "No"], "Yes"),
    ("Have you previously worked with us?", ["Yes", "No"], "No"),
    ("What is your notice period?", ["Immediate", "15 Days", "1 Month", "2 Months", "3 Months"], "Immediate"),
    ("What is your notice period?", ["15 Days or less", "1 Month", "2 Months", "3 Months"], "15 Days or less"),
    ("How many years of experience do you have in Python?", ["0-1 years", "1-3 years", "3-5 years", "5+ years"], "0-1 years"),
    ("Total experience?", ["Fresher", "Less than 1 year", "1-2 years", "2+ years"], "1-2 years"),
    ("How many years of experience in Java?", ["Fresher", "1-2 years", "3+ years"], "Fresher"),
    ("Highest qualification", ["B.E/B.Tech", "M.Tech", "MCA", "BCA"], "B.E/B.Tech"),
    ("Are you comfortable with a 2 year bond?", ["I agree", "Not comfortable"], "I agree"),
    ("Describe yourself", ["Skip this question"], "Skip this question"),
    ("Do you have experience in NLP?", ["Yes", "No"], "Yes"),
    ("Are you a 2026 passout?", ["Yes", "No"], "Yes"),
    ("Are you a 2024 passout?", ["Yes", "No"], "No"),
])
def test_option_answers(answerer, question, options, expected):
    assert answerer.answer(question, options) == expected


def test_custom_answers_override(cfg):
    a = Answerer(cfg.profile, cfg.skills, {"notice period": "30 days", "describe": "I build ML models."})
    assert a.answer("What is your notice period?") == "30 days"
    assert a.answer("Describe a project") == "I build ML models."


@pytest.mark.parametrize("opt,lo,hi", [
    ("0-1 years", 0, 1), ("5+ years", 5, float("inf")), ("2", 2, 2),
    ("15 days or less", 0, 15), ("More than 3", 3, float("inf")), ("Two years", 2, 2),
])
def test_parse_range(opt, lo, hi):
    got = parse_range(opt)
    assert got[0] == pytest.approx(lo) and (got[1] == hi or got[1] == pytest.approx(hi))


def test_pick_numeric_nearest():
    assert pick_numeric_option(1, ["2-4 years", "5+ years"]) == "2-4 years"
    assert pick_numeric_option(4, ["1", "2", "3", "4", "5"]) == "4"


@pytest.mark.parametrize("question,options,job_location,expected", [
    ("Are you legally authorized to work in the United States?", ["Yes", "No"], "", "No"),
    ("Are you legally authorized to work in India?", ["Yes", "No"], "", "Yes"),
    ("Do you require visa sponsorship to work in India?", ["Yes", "No"], "", "No"),
    ("Will you now or in the future require sponsorship for employment visa status?", ["Yes", "No"],
     "New York, United States (Remote)", "Yes"),
    ("Will you now or in the future require sponsorship for employment visa status?", ["Yes", "No"],
     "Bengaluru, Karnataka, India", "No"),
    ("Are you authorized to work in the country where this job is located?", ["Yes", "No"], "London, United Kingdom", "No"),
    ("Are you authorized to work in the country where this job is located?", ["Yes", "No"], "Pune, India", "Yes"),
    ("Please let us know if you need a visa", ["Yes", "No"], "", "No"),
])
def test_work_authorization(cfg, question, options, job_location, expected):
    a = Answerer(dict(cfg.profile, work_authorized_countries=["India"]), cfg.skills)
    a.job_location = job_location
    assert a.answer(question, options) == expected


@pytest.mark.parametrize("question,options,expected", [
    ("Are you comfortable working in night shift?", ["Yes", "No"], "Yes"),
    ("Are you okay with rotational shifts?", ["Yes", "No"], "Yes"),
    ("Which shift do you prefer?", ["Day", "Night", "Rotational"], "Rotational"),
    ("Which shift do you prefer?", ["Day", "Night", "Any"], "Any"),
    ("What is your preferred work mode?", ["Onsite", "Hybrid", "Remote"], "Remote"),
])
def test_flexible_shift_and_remote(answerer, question, options, expected):
    assert answerer.answer(question, options) == expected


def test_shift_text_answer(answerer):
    assert "flexible" in answerer.answer("Are you comfortable working in US time zone (EST)?").lower()


def test_self_rating(answerer):
    assert answerer.answer("From 1–10, how would you rate your current AI/ML knowledge?") == "7"
    assert answerer.answer("On a scale of 1 to 10, rate your Python skills", ["5", "6", "7", "8"]) == "7"
