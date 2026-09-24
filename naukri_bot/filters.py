"""Decide whether a job is worth applying to."""
from __future__ import annotations

import re

from .models import Job


def contains_term(text: str, term: str) -> bool:
    """Case-insensitive whole-word/phrase match ("ai" matches "AI Engineer", not "Maintenance")."""
    term = term.lower().strip()
    if not term:
        return False
    # only enforce a boundary on edges that are alphanumeric (".net" should match "asp.net")
    left = r"(?<![a-z0-9])" if term[0].isalnum() else ""
    right = r"(?![a-z0-9])" if term[-1].isalnum() else ""
    pattern = left + re.escape(term) + right
    return re.search(pattern, text.lower()) is not None


def min_years_required(text: str) -> int:
    """Smallest 'N+ years' / 'N-M years' of experience mentioned in a job description (0 if none)."""
    found = []
    for m in re.finditer(r"(\d{1,2})\s*(?:\+|-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years?|yrs?)", text or "", re.I):
        tail = text[m.end():m.end() + 40].lower()
        head = text[max(0, m.start() - 40):m.start()].lower()
        if "experience" in tail or "experience" in head or "exp" in tail:
            found.append(int(m.group(1)))
    return min(found) if found else 0


def skill_overlap(job: Job, skills: list[str]) -> list[str]:
    haystack = " | ".join(job.skills) + " | " + job.title + " | " + job.description
    return [s for s in skills if contains_term(haystack, s)]


def evaluate(job: Job, filters: dict, skills: list[str], allow_external: bool = False) -> tuple[bool, str, int]:
    """Return (ok, reason, score). allow_external=True judges relevance only (company-site jobs)."""
    if job.external and not allow_external:
        return False, "external company-site apply", 0
    if not job.url or not job.job_id:
        return False, "missing url/id", 0

    title = job.title
    bad = next((t for t in filters.get("title_exclude", []) if contains_term(title, t)), None)
    if bad:
        return False, f"title has excluded word '{bad}'", 0
    if not any(contains_term(title, t) for t in filters.get("title_include", [])):
        return False, "title not in target roles", 0

    company = job.company.lower()
    if any(c.lower() in company for c in filters.get("company_exclude", []) if c):
        return False, "company excluded", 0

    if job.min_exp > int(filters.get("max_min_experience", 1)):
        return False, f"needs {job.min_exp}+ yrs", 0

    matched = skill_overlap(job, skills)
    score = len(matched)
    if score < int(filters.get("min_score", 0)):
        return False, f"low skill match ({score})", score
    return True, "match: " + ", ".join(matched[:8]), score
