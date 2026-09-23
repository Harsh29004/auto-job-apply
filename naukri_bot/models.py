"""Job model built from Naukri's jobapi/v3/search response."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

BASE_URL = "https://www.naukri.com"


def _to_int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


@dataclass
class Job:
    job_id: str
    title: str
    company: str
    url: str
    skills: list[str] = field(default_factory=list)
    description: str = ""
    location: str = ""
    experience: str = ""
    salary: str = ""
    min_exp: int = 0
    max_exp: int = 0
    external: bool = False
    apply_url: str = ""      # company-site apply link (external jobs)
    posted: str = ""

    @classmethod
    def from_api(cls, d: dict) -> "Job":
        placeholders = {p.get("type"): p.get("label", "") for p in d.get("placeholders") or []}
        jd_url = d.get("jdURL") or ""
        if jd_url and not jd_url.startswith("http"):
            jd_url = BASE_URL + jd_url
        skills = [s.strip().lower() for s in (d.get("tagsAndSkills") or "").split(",") if s.strip()]
        return cls(
            job_id=str(d.get("jobId", "")),
            title=(d.get("title") or "").strip(),
            company=(d.get("companyName") or "").strip(),
            url=jd_url,
            skills=skills,
            description=strip_html(d.get("jobDescription", "")),
            location=placeholders.get("location", ""),
            experience=placeholders.get("experience", d.get("experienceText", "")),
            salary=placeholders.get("salary", ""),
            min_exp=_to_int(d.get("minimumExperience")),
            max_exp=_to_int(d.get("maximumExperience")),
            external=is_external(d),
            apply_url=d.get("applyRedirectUrl") or "",
            posted=d.get("footerPlaceholderLabel", ""),
        )


def is_external(d: dict) -> bool:
    """True when applying redirects to the company's own site instead of Naukri."""
    redirect = d.get("applyRedirectUrl") or ""
    if redirect:
        host = urlparse(redirect).netloc.lower()
        if host and not host.endswith("naukri.com"):
            return True
    return bool(d.get("companyApplyJob")) or d.get("mode") == "crawled"
