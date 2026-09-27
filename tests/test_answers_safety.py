"""Answers that must never be guessed wrong on real application forms."""
import pytest

from naukri_bot.answers import Answerer


@pytest.fixture
def a(cfg):
    return Answerer(dict(cfg.profile, gender="", cgpa="", expected_salary_usd=""), cfg.skills)


@pytest.mark.parametrize("question,options,expected", [
    ("Are you Hispanic/Latino?", ["Yes", "No", "Decline To Self Identify"], "Decline To Self Identify"),
    ("Are you Hispanic/Latino?", ["Yes", "No"], None),                 # no decline option -> leave empty
    ("Veteran Status", ["I am a veteran", "I am not a veteran", "I don't wish to answer"], "I don't wish to answer"),
    ("Disability status", ["Yes, I have a disability", "No", "I do not want to answer"], "I do not want to answer"),
    ("Race", ["Asian", "White", "Prefer not to say"], "Prefer not to say"),
    ("Gender", ["Male", "Female", "Decline to self-identify"], "Decline to self-identify"),
])
def test_eeo_questions_are_declined(a, question, options, expected):
    assert a.answer(question, options) == expected


@pytest.mark.parametrize("question,options,expected", [
    ("What time zone are you in?", None, "IST (UTC+05:30)"),
    ("Which time zone are you located in?", ["PST", "EST", "IST (India)", "CET"], "IST (India)"),
    ("Country of residence", None, "India"),
    ("What is your nationality?", None, "Indian"),
    ("What was your bachelor's university degree result, and what was the grading scale?", None, None),
    ("Have you completed the following level of education: Associate's Degree?", ["Yes", "No"], "Yes"),
    ("Have you completed the following level of education: Master's Degree?", ["Yes", "No"], "No"),
    ("Are you at least 18 years of age?", ["Yes", "No"], "Yes"),
    ("What is your gross annual salary expectation (in USD)?", None, None),   # expected_salary_usd empty
    ("Do you own a car?", ["Yes", "No"], None),                              # unknown yes/no -> not guessed
    ("Have you ever been to Japan?", ["Yes", "No"], None),
    ("We require all colleagues to meet in person 2-4 times a year. Are you able to travel?", ["Yes", "No"], "Yes"),
    ("Please confirm that you have read and agree to the privacy notice", ["Yes", "No"], "Yes"),
    ("I agree to use only my own words during this application", ["Yes", "No"], "Yes"),
    ("Do you have any knowledge about AI/ML Concepts?", ["Yes", "No"], "Yes"),
])
def test_safe_answers(a, question, options, expected):
    assert a.answer(question, options) == expected


def test_what_excites_you_is_not_notice_period(a, cfg):
    ans = a.answer("What interests and excites you about joining ALX?")
    assert ans == cfg.profile["why_join"]
    assert a.answer("When would you be available to join us?") == "Immediate"


def test_describe_experience_is_a_sentence(a):
    known = a.answer("Describe your experience with Python*")
    assert known.startswith("About 1 year of hands-on experience with Python")
    unknown = a.answer("Describe your experience with Kubernetes")
    assert "not worked with Kubernetes professionally" in unknown


def test_usd_salary_when_set(cfg):
    a = Answerer(dict(cfg.profile, expected_salary_usd="12000"), cfg.skills)
    assert a.answer("What is your expected annual salary in USD?") == "12000"
    assert a.answer("Expected salary in EUR?") is None


@pytest.mark.parametrize("raw,options,expected", [
    ("India", ["British Indian Ocean Territory +246", "India +91", "Indonesia +62"], "India +91"),
    ("Artificial Intelligence and Data Science", ["Art", "Artificial Intelligence", "Data Science"],
     "Artificial Intelligence"),
    ("Surat, India", ["Surat, Gujarat, India", "Suratgarh, Rajasthan, India"], "Surat, Gujarat, India"),
    ("Yes", ["I have read and agree", "I do not agree"], "I have read and agree"),
    ("Python", ["Pythonista Club", "Java"], None),       # never match part of a word
])
def test_strict_option_matching(a, raw, options, expected):
    assert a.match_option(raw, options) == expected


def test_timezone_option_not_indian_ocean(a):
    opts = ["Indian Ocean Time Zones", "India Standard Time (IST)", "Pacific Time"]
    assert a.answer("What time zone are you in?", opts) == "India Standard Time (IST)"


def test_right_to_work_without_sponsorship_is_yes(a):
    a.job_location = "Worldwide"
    q = "Do you have the right to work in your current country of residence (without sponsorship from ALX)?"
    assert a.answer(q, ["Yes", "No"]) == "Yes"


def test_timezone_region_option(a):
    opts = ["American Time Zones", "Asia Pacific Time Zones", "Europe, Middle East or Africa Time Zones",
            "Indian Ocean Time Zones"]
    assert a.answer("What time zone are you in?", opts) == "Asia Pacific Time Zones"


def test_generic_single_word_option_not_matched(a):
    assert a.match_option("Artificial Intelligence and Data Science", ["Science", "Computer Science"]) is None
