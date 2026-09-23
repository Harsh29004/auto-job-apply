from naukri_bot.bot import NaukriBot, shorten
from naukri_bot.storage import Storage

QUESTIONNAIRE = [
    {"questionId": "1", "questionName": "How many years of experience do you have in Python?",
     "questionType": "Text Box", "isMandatory": True, "prefillData": ["2.0"], "answerOption": {}},
    {"questionId": "2", "questionName": "How many years of experience do you have in Deep Learning?",
     "questionType": "Text Box", "isMandatory": True, "prefillData": None, "answerOption": {}},
    {"questionId": "3", "questionName": "Are you willing to relocate to Pune?",
     "questionType": "Radio Button", "isMandatory": True, "prefillData": None, "answerOption": {"0": "Yes", "1": "No"}},
    {"questionId": "4", "questionName": "Explain your favourite algorithm in detail",
     "questionType": "Text Box", "isMandatory": True, "prefillData": None, "answerOption": {}},
    {"questionId": "5", "questionName": "Optional note", "questionType": "Text Box",
     "isMandatory": False, "prefillData": None, "answerOption": {}},
]


def make_bot(cfg, tmp_path):
    return NaukriBot(cfg, Storage(tmp_path / "t.db"))


def test_shorten():
    text = "First sentence here. Second sentence is longer and goes on. Third one."
    assert shorten(text, 200) == text
    assert shorten(text, 40) == "First sentence here."
    out = shorten("word " * 100, 50)
    assert len(out) <= 51 and out.endswith(".")


def test_fill_missing_answers(cfg, tmp_path):
    bot = make_bot(cfg, tmp_path)
    bot.questionnaires["J1"] = QUESTIONNAIRE
    data = {"applyData": {"J1": {"answers": {"2": "1"}}}}
    bot._fill_missing_answers(data)
    ans = data["applyData"]["J1"]["answers"]
    assert ans["1"] == "2.0"            # prefilled profile value used
    assert ans["2"] == "1"              # existing chatbot answer untouched
    assert ans["3"] == ["Yes"]          # radio answers are lists
    assert "4" not in ans               # unknown -> left out, logged as unanswered
    assert "5" not in ans               # optional -> not filled
    assert bot.db.unanswered()[0][0] == "Explain your favourite algorithm in detail"


def test_shorten_rejected(cfg, tmp_path):
    bot = make_bot(cfg, tmp_path)
    long = "This is a sentence. " * 30
    data = {"applyData": {"J1": {"answers": {"9": long, "3": ["Yes"]}}}}
    js = {"jobs": [{"jobId": "J1", "validationError": [
        {"field": "9", "customErrorCode": 289, "message": "The entered length exceeds the max allowed length."}]}]}
    assert bot._shorten_rejected(data, js)
    assert len(data["applyData"]["J1"]["answers"]["9"]) < len(long) * 0.65
    assert not bot._shorten_rejected(data, {"jobs": [{"jobId": "J1"}]})
