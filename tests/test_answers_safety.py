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


def test_statement_options_are_not_number_ranges(a):
    # "experience" -> "1 year" must not pick an option because it says "one" or mentions "n8n"
    opts = ["A. I have integrated LLM/AI APIs with Python/JavaScript or another application",
            "B. I have built workflows using tools such as n8n, Make, Zapier or similar automation platforms"]
    assert a.answer("Which of the following best describes your technical experience with AI?", opts) is None
    assert a.match_option("1", ["0-1 years", "2-4 years"]) == "0-1 years"


def test_which_frameworks_only_lists_your_skills(a):
    answer = a.answer("Which deep learning frameworks have you used?")
    assert answer and all(s.lower() in a.skills for s in answer.split(", "))
    assert "english" not in (a.answer("Which programming languages do you know?") or "").lower()
    assert a.answer("Which tools do you use for time tracking?") is None


def test_projects_question_follows_your_skills(a):
    a.p = dict(a.p, project_summary="Built an LLM chatbot")
    assert a.answer("Have you done any AI based projects?", ["Yes", "No"]) == "Yes"
    assert a.answer("Have you built any blockchain projects?", ["Yes", "No"]) == "No"


def test_np_means_notice_period(a):
    assert a.answer("What is you official NP? If Serving kindly mention your LWD .") not in (None, "No")
    assert a.answer("Are you currently serving notice period?", ["Yes", "No"]) == "No"


def test_yes_no_location_and_long_project_questions(a):
    a.p = dict(a.p, project_summary="Built an LLM chatbot", work_authorized_countries=["India"])
    a.job_location = "India (Remote)"
    assert a.answer("Are you currently located in India and legally eligible to undertake an internship in India?",
                    ["Yes", "No"]) == "Yes"
    assert a.answer("Have you built at least one software, Generative AI, LLM, AI agent, or API-based project?",
                    ["Yes", "No"]) == "Yes"
    assert a.answer("Have you built any iOS apps?", ["Yes", "No"]) == "No"


def test_name_rule_is_only_your_name(a):
    assert a.answer("7. Stream or Branch Name Single line text.") != a.p.get("name")
    assert a.answer("Project name") != a.p.get("name")
    assert a.answer("Your full name") == a.p.get("name")


def test_and_needs_every_skill(a):
    yn = ["Yes", "No"]
    assert a.answer("Do you have proven professional experience with Rust and Python?", yn) == "No"
    assert a.answer("Do you have experience with Rust or Python?", yn) == "Yes"
    assert a.answer("Do you have experience with Python and related frameworks?", yn) == "Yes"
