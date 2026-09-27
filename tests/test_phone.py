"""Phone fields: which labels ask for your number, how it is written, and the country-code dropdown.
Uses a made-up number, never the one in config.yaml."""
import dataclasses

import pytest

from naukri_bot.answers import Answerer, is_phone_question
from naukri_bot.company import FieldFiller

PHONE = "9876543210"
LI = "single-line-text-form-component-formElement-urn-li-jobs-applyformcommon-easyApplyFormElement-1-2"


def make_filler(cfg, **profile):
    cfg2 = dataclasses.replace(cfg, profile={**cfg.profile, "phone": PHONE, "country": "India",
                                             "phone_country_code": None, **profile})
    return FieldFiller(cfg2, Answerer(cfg2.profile, cfg2.skills, {}))


@pytest.fixture
def filler(cfg):
    return make_filler(cfg)


def value(filler, label, name="", tag="input", **kw):
    field = {"label": label, "placeholder": "", "name": name, "tag": tag, "type": "text", "options": [], **kw}
    return filler.value_for(field, "AI Engineer", "Acme")


@pytest.mark.parametrize("label", [
    "Phone", "Phone ✱", "Mobile Number *", "Mobile phone number", "Mob No", "Mob. No.", "Ph. No.", "Cell number",
    "Contact No.", "Contact Num", "Alternate contact number", "WhatsApp number", "Phone Number (WhatsApp)",
    "Mobile / WhatsApp No.", "Your Phone Number *", "Please share your contact number", "What is your mobile number?",
    "Enter your 10 digit mobile number", "WhatsApp number for updates", "Phone number (required)", "Tel.",
    "Telefonnummer", "Mobiltelefonnummer", "Handynummer", "Número de teléfono", "Téléphone", "Celular",
])
def test_labels_that_ask_for_the_number(label):
    assert is_phone_question(label)


@pytest.mark.parametrize("label", [
    "How many years of experience do you have in mobile app development?", "Mobile app development experience",
    "Joining WhatsApp is Mandatory", "Do you have a WhatsApp number?", "Are you comfortable with telephonic interview?",
    "Describe a mobile application you have built", "Father's mobile number", "Emergency contact number",
    "Phone extension", "Phone country code", "Contact", "Contact Details", "Mobility", "Ph.D", "Telegram username",
])
def test_labels_that_only_mention_a_phone(label):
    assert not is_phone_question(label)


def test_custom_labels_get_the_number(filler):
    for label in ("Mob No", "Ph No", "Cell number", "Contact Num", "Telefonnummer", "Celular", "WhatsApp number"):
        assert value(filler, label) == PHONE, label


def test_phone_never_typed_into_other_questions(filler):
    assert value(filler, "How many years of experience do you have in mobile app development?") != PHONE
    assert value(filler, "Describe a mobile application you have built") != PHONE
    assert value(filler, "Joining WhatsApp is Mandatory") == "Yes"  # acknowledgement, not the number
    assert value(filler, "Father's mobile number") is None
    assert value(filler, "Emergency contact number") != PHONE


def test_linkedin_phone_box_is_national_in_any_language(filler):
    filler.answerer.job_location = "United States (Remote)"  # even for a job abroad: the code has its own dropdown
    for label in ("Mobile phone number", "Mobiltelefonnummer", "携帯電話番号"):
        assert value(filler, label, name=f"{LI}-phoneNumber-nationalNumber") == PHONE, label


def test_number_format_follows_the_box(filler):
    assert value(filler, "Phone number (with country code)") == "+91 " + PHONE
    assert value(filler, "Mobile number (10 digits)") == PHONE
    assert value(filler, "Phone", placeholder="+1 555 555 5555") == "+91 " + PHONE
    assert value(filler, "Mobile Number", name=f"{LI}-numeric") == PHONE           # LinkedIn numeric question
    filler.answerer.job_location = "Berlin, Germany"                                 # abroad: +91, or it reads as local
    assert value(filler, "Phone") == "+91 " + PHONE
    assert value(filler, "Phone", maxlength=10) == PHONE                             # ... unless the box can't take it
    assert value(filler, "Phone", pattern="[0-9]{10}") == PHONE
    assert value(filler, "Phone", type="number") == "91" + PHONE
    filler.separate_country_code = True                                              # form has its own code dropdown
    assert value(filler, "Phone") == PHONE


def test_country_code_dropdown(filler, cfg):
    opts = ["Select an option", "Afghanistan (+93)", "American Samoa (+1)", "India (+91)", "United States (+1)"]
    assert value(filler, "Phone country code", tag="select", options=opts) == "India (+91)"
    assert value(filler, "Telefonnumrets landskod", tag="select", options=["Välj", "Amerikanska Samoa (+1)",
                                                                          "Indien (+91)"]) == "Indien (+91)"
    assert value(filler, "Phone", tag="select", options=["+1", "+44", "+91"]) == "+91"  # shares the number's label
    assert value(filler, "Country code") == "+91"
    us = make_filler(cfg, phone="+1 415 555 0100", country="United States")
    assert value(us, "Phone country code", tag="select", options=opts) == "United States (+1)"


def test_phone_numbers_parsing(cfg):
    def parts(phone, **kw):
        return Answerer(dict(cfg.profile, phone=phone, phone_country_code=None, country="India", **kw),
                        cfg.skills).phone_numbers()[1:]
    assert parts("+91 98765 43210") == (PHONE, "+91 " + PHONE)
    assert parts("919876543210") == (PHONE, "+91 " + PHONE)
    assert parts(PHONE) == (PHONE, "+91 " + PHONE)
    assert parts("+44 7911 123456") == ("7911123456", "+44 7911123456")  # its own code wins over country
    a = Answerer(dict(cfg.profile, phone=PHONE, phone_country_code=None, country="India"), cfg.skills)
    assert a.answer("Please share your mobile number with country code") == "+91 " + PHONE
    assert a.answer("Please enter your mobile number") == PHONE
