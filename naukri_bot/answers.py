"""Answer the recruiter questions Naukri's apply chatbot asks.

`Answerer.answer(question, options)` returns
  - a string to type (free-text question), or
  - one of `options` (radio / chips / checkbox question), or
  - None when it does not know; the bot then skips the job and logs the question
    so you can add an answer under `custom_answers` in config.yaml.
"""
from __future__ import annotations

import re

from .filters import contains_term

YES, NO = "Yes", "No"

# Words after "experience in/with ..." that mean "overall experience", not a specific skill.
GENERIC_EXP = {
    "total", "overall", "relevant", "it", "industry", "software", "work", "professional",
    "this", "the", "same", "similar", "field", "domain", "role", "years", "year", "development",
    "corporate", "full time", "full-time",
}

WORD_NUMBERS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def is_number(value) -> bool:
    try:
        float(str(value))
        return True
    except ValueError:
        return False


def parse_range(option: str) -> tuple[float, float] | None:
    """'0-1 years' -> (0,1); 'Less than 1' -> (0,1); '5+ yrs' -> (5,inf); '2' -> (2,2)."""
    o = norm(option)
    for w, n in WORD_NUMBERS.items():
        o = re.sub(rf"\b{w}\b", str(n), o)
    if "fresher" in o:
        return (0, 0.99)
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", o)]
    if not nums:
        return None
    if re.search(r"less than|below|under|<", o):
        return (0, nums[0] - 1e-9)
    if re.search(r"upto|up to|within|or less|or below", o):
        return (0, nums[0])
    if re.search(r"more than|above|over|\+|>|or more|and above", o):
        return (nums[0], float("inf"))
    if len(nums) >= 2:
        return (nums[0], nums[1])
    return (nums[0], nums[0])


def pick_numeric_option(value: float, options: list[str]) -> str | None:
    best, best_dist = None, float("inf")
    for opt in options:
        rng = parse_range(opt)
        if rng is None:
            continue
        lo, hi = rng
        if lo <= value <= hi:
            return opt
        dist = min(abs(value - lo), abs(value - hi))
        if dist < best_dist:
            best, best_dist = opt, dist
    return best


class Answerer:
    def __init__(self, profile: dict, skills: list[str], custom_answers: dict | None = None):
        self.p = profile or {}
        self.skills = [s.lower() for s in skills]
        self.skill_exp = {str(k).lower(): v for k, v in (self.p.get("skill_experience") or {}).items()}
        self.custom = {str(k).lower(): v for k, v in (custom_answers or {}).items()}
        self.job_location = ""  # set per job by the bots (used for visa / work-authorization questions)

    # ------------------------------------------------------------------ public
    def answer(self, question: str, options: list[str] | None = None):
        options = [o.strip() for o in options or [] if o and o.strip()]
        raw = self.raw_answer(question, options)
        if not options:
            return None if raw in (None, "") else str(raw)
        if raw in (None, ""):
            return self._skip_option(options)
        return self.match_option(str(raw), options) or self._skip_option(options)

    # ------------------------------------------------------------------ rules
    def raw_answer(self, question: str, options: list[str] | None = None):
        q = norm(question)
        p = self.p

        for key, value in self.custom.items():
            if key and key in q:
                return value

        # --- voluntary self-identification (EEO): never guess ---------------
        if re.search(r"hispanic|latin[oa]\b|\brace\b|ethnicit|veteran|disabilit|sexual orientation|transgender|"
                     r"pronoun|lgbt|gender identity", q):
            return self._decline(options)
        if re.search(r"\bgender\b|\bsex\b", q):
            return p.get("gender") or self._decline(options)

        # --- open-ended text questions --------------------------------------
        if not options:
            if re.search(r"\bprojects?\b", q) and re.search(r"explain|describe|tell|share|brief|detail|mention|summar|walk", q):
                return p.get("project_summary") or None
            if re.search(r"why (do you want|should we|are you interested|this (role|company|job))|why (join|us)|"
                         r"reason for (applying|change|job change|switch)|motivat|what (interests|excites|attracts|draws) "
                         r"you|about joining|interest(ed)? in (this|the|our) (role|position|company|team|job)|"
                         r"why (are you|do you want to) (apply|join|work)", q):
                return p.get("why_join") or None
            if re.search(r"about yourself|introduce yourself|describe yourself|your (profile|background) (in brief|briefly)|brief (summary|introduction)", q):
                return p.get("about_me") or None
            if re.match(r"^(please )?(describe|tell us about|explain|elaborate on|share|walk us through)\b.*\bexperience\b", q):
                return self._experience_sentence(question)

        # --- time zone / country ---------------------------------------------
        if re.search(r"(what|which) time ?zone|your (current )?time ?zone|time ?zone (are you|do you)", q):
            tz = p.get("timezone") or "IST (UTC+05:30)"
            if options:
                return next((o for o in options if re.search(r"\bist\b|india|kolkata|calcutta|5:30|utc ?\+ ?5|gmt ?\+ ?5",
                                                             o, re.I)), None)
            return tz
        if re.search(r"country of (residence|origin|citizenship)|nationality|citizenship|(which|what) country "
                     r"(do you|are you)|country (do you|are you) (live|reside|based|located)", q):
            return p.get("country") or "India"

        # --- salary --------------------------------------------------------
        if re.search(r"\b(ctc|salary|package|compensation|remuneration|lpa|stipend|pay|rate)\b", q) and \
                re.search(r"usd|\$|dollar|eur\b|€|euro|gbp|£|pound", q):
            usd = p.get("expected_salary_usd")
            return str(usd) if usd not in (None, "") and re.search(r"usd|\$|dollar", q) else None
        if re.search(r"\b(ctc|salary|package|compensation|remuneration|lpa|stipend)\b", q):
            if re.search(r"expect|desire|looking for|require", q):
                return self._money(p.get("expected_ctc_lpa"), q)
            if re.search(r"current|present|last|existing|drawn|previous", q) or "ctc" in q:
                return self._money(p.get("current_ctc_lpa"), q)
            return self._money(p.get("expected_ctc_lpa"), q)

        # --- notice period / joining ---------------------------------------
        joining = re.search(r"\bjoin", q) and re.search(r"when|how soon|available|availability|start|date|immediate|"
                                                       r"days|weeks|earliest|notice", q)
        if "notice" in q or joining or "serving" in q:
            if re.search(r"\b(can|will|would|are) you\b.*\b(join|joining)\b.*\b(immediate|within|asap|\d+ days)", q):
                return YES
            if "serving" in q:
                return NO
            if re.search(r"how many days|in days|number of days|\(days\)|days\?", q):
                return str(p.get("notice_period_days", 0))
            if "month" in q and re.search(r"how many|in months", q):
                return "0"
            return p.get("notice_period") or "Immediate"

        # --- experience in years/months ------------------------------------
        if "experience" in q or re.search(r"\b(years|yrs|months)\b", q):
            exp = self._experience(q)
            if exp is not None:
                return exp

        # --- location ------------------------------------------------------
        if re.search(r"relocat|willing to (move|shift|work|travel)|open to (move|work|relocat)", q):
            return p.get("willing_to_relocate") or YES
        if re.search(r"current(ly)? (location|city|residing|based|located|staying|living)|where are you (located|based|staying|living)|residing|current address|hometown", q):
            return p.get("current_location")
        if re.search(r"preferred (work )?location|preferred city|location preference", q):
            return p.get("preferred_location")

        # --- personal ------------------------------------------------------
        if re.search(r"\b(full )?name\b", q) and "company" not in q and "college" not in q:
            return p.get("name")
        if re.search(r"e-?mail", q):
            return p.get("email")
        if re.search(r"phone|mobile|contact number|whatsapp", q):
            return p.get("phone")
        if "linkedin" in q:
            return p.get("linkedin") or None
        if "github" in q or "portfolio" in q:
            return p.get("github") or None
        if "gender" in q:
            return p.get("gender") or None
        if re.search(r"date of birth|\bdob\b|birth ?date", q):
            return p.get("date_of_birth") or None
        if re.search(r"languages? (do you|you) (know|speak)|which languages|languages known", q):
            return p.get("languages")

        # --- education -----------------------------------------------------
        if re.search(r"\b(12th|hsc|intermediate|higher secondary)\b", q):
            return p.get("twelfth_percentage") or None
        if re.search(r"\b(10th|ssc|matric|secondary school)\b", q):
            return p.get("tenth_percentage") or None
        if re.search(r"cgpa|\bgpa\b|percentage|aggregate|marks|degree result|\bgrades?\b|classification|honou?rs", q):
            return p.get("cgpa") or None
        edu = re.search(r"completed .*?(high school|secondary|associate|diploma|bachelor|undergraduate|b\.?tech|"
                        r"master|m\.?tech|post ?graduate|doctora|ph\.?d)", q)
        if edu:
            level = {"high school": 1, "secondary": 1, "associate": 2, "diploma": 2, "bachelor": 3, "undergraduate": 3,
                     "master": 4, "post": 4, "doctora": 5}
            key = next((k for k in level if edu.group(1).startswith(k[:4])), "bachelor")
            return YES if level[key] <= 3 else NO  # you hold a bachelor's degree (B.Tech)
        if re.search(r"18 years|over 18|at least 18|above 18|legal age", q):
            return YES
        if re.search(r"year of (passing|graduation|completion)|passing year|pass ?out|graduation year|graduated in|batch", q):
            if options and any(o.lower() in (YES.lower(), NO.lower()) for o in options):
                return YES if str(p.get("graduation_year")) in q else NO
            return p.get("graduation_year")
        if re.search(r"highest (qualification|education|degree)|qualification|which degree|your degree|educational", q):
            return p.get("highest_qualification")
        if re.search(r"specialization|stream|branch|major", q):
            return p.get("degree_branch")
        if re.search(r"college|university|institute", q):
            return p.get("college")

        # --- current job ---------------------------------------------------
        if re.search(r"current (company|employer|organi[sz]ation)|currently working (with|at|for)|which company", q):
            return p.get("current_company")
        if re.search(r"current (designation|role|job title|position)", q):
            return p.get("current_designation")
        if re.search(r"(are you )?currently (working|employed)", q):
            return YES

        # --- self ratings ("From 1-10, how would you rate ...") --------------
        if re.search(r"(1|one)\s*(-|–|to|/)\s*(10|ten)\b|scale of|out of (10|ten)|rate (your|yourself)", q):
            return str(p.get("self_rating", 7))

        # --- work authorization / visa (depends on the job's country) --------
        auth = self._work_authorization(q)
        if auth is not None:
            return auth

        # --- shifts, time zones, work mode (candidate is flexible) ---------
        if re.search(r"\bshifts?\b|time ?zones?|working hours|work(ing)? timings?|\b(est|pst|cst|gmt|uk|us) (hours|time)", q):
            if not options or not any(norm(o) in ("yes", "no") for o in options):
                if options:
                    for want in (r"\bany\b|flexible|\ball\b", r"rotational", r"night|\bus\b|\buk\b"):
                        hit = next((o for o in options if re.search(want, o, re.I)), None)
                        if hit:
                            return hit
                    return None
                return p.get("shift_preference") or "Flexible - comfortable with any shift or time zone"
        if options and re.search(r"work (mode|arrangement|model|setup|location type)|prefer(red)? (work|working)|"
                                 r"remote.*(hybrid|onsite|office)|(onsite|office|hybrid).*remote", q):
            for want in (r"remote|work from home|wfh", r"any|flexible|open"):
                hit = next((o for o in options if re.search(want, o, re.I)), None)
                if hit:
                    return hit

        # --- yes / no ------------------------------------------------------
        return self._yes_no(q, options)

    COUNTRY_RE = re.compile(
        r"\b(united states|usa|u\.s\.a?\.?|us|america|canada|united kingdom|uk|britain|england|europe|eu|germany|"
        r"netherlands|ireland|france|spain|poland|australia|new zealand|singapore|uae|dubai|saudi|qatar|japan|"
        r"switzerland|sweden|india)\b", re.I)

    def _work_authorization(self, q: str):
        """Honest answers for 'authorized to work in X?' / 'need visa sponsorship?'."""
        sponsor = re.search(r"sponsor|visa|work permit|h-?1b", q)
        authorized = re.search(r"authori[sz]ed to work|legally (eligible|authori[sz]ed|able|allowed|permitted)|"
                               r"eligible to work|right to work|work authori[sz]ation|permitted to work", q)
        if not (sponsor or authorized):
            return None
        allowed = [c.lower() for c in (self.p.get("work_authorized_countries") or ["India"])]
        found = [m.group(1).lower() for m in self.COUNTRY_RE.finditer(q)]
        if found == ["us"] and not re.search(r"\b(in|the) us\b", q):
            found = []  # "let us know" etc.
        if not found and self.job_location:  # question has no country -> use the job's location
            found = [m.group(1).lower() for m in self.COUNTRY_RE.finditer(self.job_location)
                     if m.group(1).lower() != "us" or "united states" in self.job_location.lower()]
        in_allowed = (not found) or any(c in allowed for c in found)
        if sponsor:
            return NO if in_allowed else YES
        return YES if in_allowed else NO

    # ---------------------------------------------------------------- helpers
    def _money(self, lpa, q: str):
        if lpa in (None, ""):
            return None
        lpa = float(lpa)
        if re.search(r"per month|monthly|/month|in thousands?|\bk\b", q):
            val = lpa * 100000 / 12
            return str(round(val / 1000)) if re.search(r"thousand|\bk\b", q) else str(int(round(val)))
        if re.search(r"lakh|lac|lpa|\blakhs\b", q):
            return f"{lpa:g}"
        if re.search(r"\b(inr|rupees|rs\.?|in numbers|absolute|annual|per annum|p\.a)\b", q):
            return str(int(round(lpa * 100000)))
        return f"{lpa:g}"

    def _skill_years(self, skill: str) -> float:
        if skill in self.skill_exp:
            return float(self.skill_exp[skill])
        return float(self.p.get("default_skill_experience", 1))

    def _known_skill_in(self, text: str) -> str | None:
        # longest match first so "machine learning" wins over "c"
        for s in sorted(set(self.skills) | set(self.skill_exp), key=len, reverse=True):
            if contains_term(text, s):
                return s
        return None

    def _experience(self, q: str):
        total_years = float(self.p.get("total_experience_years", 1))
        months = "month" in q and not re.search(r"\byears?\b", q)
        if months:
            return str(self.p.get("total_experience_months", int(total_years * 12)))

        # "Do you have experience in X?"  -> yes/no, handled by _yes_no
        if re.match(r"^(do|have|are|did|is) ", q) and not re.search(r"how many|how much|number of", q):
            return None

        m = re.search(r"experience (?:do you have )?(?:in|with|on|of|as|using|working (?:on|with|in))\s+(.+?)(?:\?|$| do you| you have)", q)
        subject = m.group(1).strip() if m else ""
        if not subject:
            m2 = re.search(r"(?:how many|number of) (?:years|yrs)(?: of)?(?: experience)?(?: do you have)?\s*(?:in|with|on)?\s*(.*)", q)
            subject = (m2.group(1).strip(" ?.") if m2 else "")

        if subject:
            words = set(re.findall(r"[a-z+#.]+", subject))
            skill = self._known_skill_in(subject)
            if skill:
                return self._fmt(self._skill_years(skill))
            if words and words <= GENERIC_EXP | {"of", "in", "a", "an", "as", "and", "you", "have", "do", "experience", "your"}:
                return self._fmt(total_years)
            return self._fmt(float(self.p.get("unknown_skill_experience", 0)))

        skill = self._known_skill_in(q)
        if skill and re.search(r"how many|how much|years|yrs", q):
            return self._fmt(self._skill_years(skill))
        if re.search(r"how many|how much|total|overall|relevant|years of experience|your experience", q):
            return self._fmt(total_years)
        return None

    @staticmethod
    def _fmt(v: float) -> str:
        return str(int(v)) if float(v).is_integer() else f"{v:g}"

    # Yes/No questions are only answered "Yes" when they ask about willingness / consent /
    # flexibility. Anything else unknown stays unanswered instead of guessing.
    POSITIVE = re.compile(
        r"willing|comfortable|able to|available|okay|\bok\b|open to|agree|consent|confirm|acknowledge|ready|flexible|"
        r"happy to|interested|fine with|accept|understand|certify|declare|relocat|travel|commut|background (check|"
        r"verification)|work (from|in|at) (the )?(office|onsite|on-site|remote|home|hybrid)|in[- ]person|\bmeet\b|"
        r"night|shift|weekend|bond|start (immediately|asap|soon)|laptop|internet connection|own (computer|device)")

    def _yes_no(self, q: str, options: list[str] | None):
        opts = [o.lower() for o in options or []]
        is_yn_question = re.match(
            r"^(are|do|can|will|would|have|has|is|did|could|should|shall|please confirm|confirm|kindly confirm|"
            r"please acknowledge|i agree|i confirm|i understand|i acknowledge|i consent|i certify|i declare|"
            r"by (checking|clicking|submitting))\b", q
        ) or (opts and any(o in ("yes", "no") or o.startswith(("yes", "no")) for o in opts))
        if not is_yn_question:
            return None

        negatives = [
            r"(applied|interviewed|worked) (with|for|at|in|to) (us|this|our|the company)",
            r"previously (applied|worked|employed|interviewed)",
            r"backlog|arrear|\bgap\b|criminal|convicted|disabilit",
            r"(currently )?(pursuing|a student|studying)",
            r"serving notice",
            r"any (other )?offer",
            r"related to (any|anyone)|relative",
        ]
        if any(re.search(n, q) for n in negatives):
            return NO

        # "Do you have experience in X?" -> Yes only if X is a resume skill
        m = re.search(r"(experience|hands[- ]on|worked|knowledge|familiar|proficient|expertise|skilled)\b.*?"
                      r"\b(in|with|on|of|about)\s+(.+)", q)
        if m and not re.search(r"comfortable|willing|okay|ok\b|fine|ready|open", q):
            return YES if self._known_skill_in(m.group(3)) else NO
        return YES if self.POSITIVE.search(q) else None

    @staticmethod
    def _decline(options: list[str] | None):
        """The 'prefer not to say' option of an EEO question, else None (leave it empty)."""
        return next((o for o in options or [] if re.search(
            r"decline|prefer not|don.?t (wish|want)|do not (wish|want)|not to (say|disclose|answer|identify)|"
            r"rather not|choose not|not disclose|no answer", o, re.I)), None)

    def _experience_sentence(self, question: str):
        """Honest one-liner for 'Describe your experience with X'."""
        m = re.search(r"experience\s+(?:with|in|as|of|using|working with|on)\s+(?:an?\s+)?(.+?)(?:[?.*]|$)", question, re.I)
        subject = (m.group(1).strip() if m else "").rstrip(" *?.")
        if not subject:
            return None
        skill = self._known_skill_in(subject)
        if skill:
            years = self._fmt(self._skill_years(skill))
            unit = "year" if years == "1" else "years"
            context = self.p.get("experience_context") or "academic projects and work experience"
            return f"About {years} {unit} of hands-on experience with {subject}, from {context}."
        return (f"I have not worked with {subject} professionally yet, but I pick up new tools quickly "
                f"and would be glad to build that experience.")

    # answer -> other ways a form may spell it
    SYNONYMS = {
        r"^b\.?\s?tech|^b\.?e\.?$|bachelor of (technology|engineering)": [
            "b.tech", "btech", "b.e", "be/b.tech", "bachelor", "bachelors", "graduate", "graduation", "undergraduate", "ug"],
        r"^m\.?\s?tech|master": ["m.tech", "master", "masters", "post graduate", "postgraduate", "pg"],
    }

    def match_option(self, raw: str, options: list[str]) -> str | None:
        r = norm(raw)
        low = {o: norm(o) for o in options}
        for pattern, alts in self.SYNONYMS.items():
            if re.search(pattern, r):
                for alt in alts:
                    for o, lo in low.items():
                        if re.search(rf"(?<![a-z]){re.escape(alt)}(?![a-z])", lo):
                            return o

        for o, lo in low.items():
            if lo == r:
                return o

        if r in ("yes", "no"):
            for o, lo in low.items():
                if re.match(rf"^{r}\b", lo):
                    return o
            if r == "yes":
                for o, lo in low.items():
                    if re.search(r"\b(agree|okay|ok|sure|willing|comfortable|ready|available|interested|i am|i'm|i do|i can|absolutely)\b", lo) and "not" not in lo:
                        return o
            return None

        if r in ("immediate", "immediately", "0") or "immediate" in r:
            for o, lo in low.items():
                if "immediate" in lo:
                    return o
            if r not in ("0",):
                return pick_numeric_option(0, options)

        if is_number(r):
            val = float(r)
            for o, lo in low.items():
                if lo in (r, f"{val:g}"):
                    return o
            return pick_numeric_option(val, options)

        for o, lo in low.items():
            if r and (r in lo or lo in r) and len(lo) > 1:
                return o
        # word overlap (e.g. "B.Tech" vs "B.Tech/B.E.")
        rw = set(re.findall(r"[a-z0-9]+", r))
        best, best_n = None, 0
        for o, lo in low.items():
            n = len(rw & set(re.findall(r"[a-z0-9]+", lo)))
            if n > best_n:
                best, best_n = o, n
        return best

    @staticmethod
    def _skip_option(options: list[str]) -> str | None:
        for o in options:
            if re.search(r"\bskip\b", o.lower()):
                return o
        return None
