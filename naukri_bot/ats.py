"""Identify which applicant-tracking system (ATS) a company apply link uses."""
from __future__ import annotations

from urllib.parse import urlparse

# host substring -> ATS name
ATS_HOSTS = {
    "myworkdayjobs.com": "workday",
    "myworkdaysite.com": "workday",
    "oraclecloud.com": "oracle",
    "taleo.net": "taleo",
    "successfactors": "successfactors",
    "sapsf.": "successfactors",
    "icims.com": "icims",
    "brassring.com": "brassring",
    "accenture.com": "accenture",
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "ashbyhq.com": "ashby",
    "workable.com": "workable",
    "smartrecruiters.com": "smartrecruiters",
    "zohorecruit": "zoho",
    "freshteam.com": "freshteam",
    "keka.com": "keka",
    "darwinbox": "darwinbox",
    "recruitee.com": "recruitee",
    "bamboohr.com": "bamboohr",
    "breezy.hr": "breezy",
    "jobvite.com": "jobvite",
    "applytojob.com": "jazzhr",
    "superset.com": "superset",
    "hirist": "hirist",
    "instahyre.com": "instahyre",
    "linkedin.com": "linkedin",
    "google.com/forms": "googleforms",
    "forms.gle": "googleforms",
    "docs.google.com": "googleforms",
    "doubleclick.net": "redirect",
}

# These need an account (sign-up + email verification) before applying,
# so the bot hands them to you as "manual" instead of guessing.
LOGIN_REQUIRED = {"workday", "oracle", "taleo", "successfactors", "icims", "brassring",
                  "accenture", "darwinbox", "linkedin", "instahyre", "hirist", "superset"}


def detect_ats(url: str) -> str:
    if not url:
        return ""
    u = url.lower()
    host = urlparse(u).netloc
    for key, name in ATS_HOSTS.items():
        if "/" in key:
            if key in u:
                return name
        elif key in host:
            return name
    return "company-site"


def needs_login(ats: str) -> bool:
    return ats in LOGIN_REQUIRED
