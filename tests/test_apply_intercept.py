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


def test_disabled_send_after_skip_chip(cfg, tmp_path):
    """'Skip this question' sends the answer itself; Naukri's Send button stays disabled (empty text box).
    Clicking Send must not hang for 20 s and fail the job."""
    import time
    from playwright.sync_api import sync_playwright
    bot = make_bot(cfg, tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        bot.page = browser.new_page()
        bot.page.set_content('<div class="chatbot_Drawer"><div class="chatbot_Chip">Skip this question</div>'
                             '<button class="sendMsg" disabled>Save</button></div>')
        start = time.time()
        bot._click_save(bot.page.locator(".chatbot_Drawer"), chip=True)
        assert time.time() - start < 6
        browser.close()


def test_text_question_with_skip_chip_gets_typed_answer(cfg, tmp_path):
    """Naukri shows a text box plus a 'Skip this question' chip: type the CTC when it is known."""
    import dataclasses
    from playwright.sync_api import sync_playwright
    from naukri_bot.models import Job
    bot = make_bot(dataclasses.replace(cfg, profile=dict(cfg.profile, current_ctc_lpa=3)), tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        bot.page = browser.new_page()
        bot.page.set_content("""
          <div class="chatbot_Drawer"><ul><li class="botItem">What is your current CTC in Lacs per annum?</li></ul>
            <div class="chatbot_Chip">Skip this question</div>
            <div contenteditable="true" class="textArea"></div><button class="sendMsg">Save</button></div>
          <script>document.querySelector('.sendMsg').onclick = () => {
            document.body.innerHTML = '<p>You have successfully applied</p>'; window.typed = true; };
            document.querySelector('.chatbot_Chip').onclick = () => { window.skipped = true; };</script>""")
        bot._baseline = ""
        status, _ = bot._finish_apply(Job(job_id="1", title="x", company="y", url="u"))
        assert status == "applied"
        assert bot.page.evaluate("window.typed === true && !window.skipped")
        browser.close()
