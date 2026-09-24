"""Load config.yaml + .env into a single object."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    search: dict
    filters: dict
    skills: list[str]
    apply: dict
    profile: dict
    custom_answers: dict = field(default_factory=dict)
    company_apply: dict = field(default_factory=dict)
    linkedin: dict = field(default_factory=dict)
    foreign: dict = field(default_factory=dict)
    email: str = ""
    password: str = ""
    smtp_email: str = ""
    smtp_password: str = ""
    root: Path = ROOT

    @property
    def data_dir(self) -> Path:
        d = self.root / "data"
        d.mkdir(exist_ok=True)
        return d

    @property
    def profile_dir(self) -> Path:
        return self.root / "browser_profile"


def env_list(name: str) -> list[str] | None:
    """Comma-separated .env value -> list (None when the variable is not set)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None if raw is None else []
    return [x.strip() for x in raw.split(",") if x.strip()]


def env_int(name: str) -> int | None:
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


# .env variable -> (config section, key). Values in .env win over config.yaml.
ENV_LISTS = {
    "JOB_KEYWORDS": ("search", "keywords"),
    "JOB_LOCATIONS": ("search", "locations"),
    "JOB_TITLE_INCLUDE": ("filters", "title_include"),
    "JOB_TITLE_EXCLUDE": ("filters", "title_exclude"),
    "JOB_COMPANY_EXCLUDE": ("filters", "company_exclude"),
}
ENV_INTS = {
    "JOB_EXPERIENCE_YEARS": ("search", "experience_years"),
    "JOB_AGE_DAYS": ("search", "job_age_days"),
    "JOB_PAGES_PER_SEARCH": ("search", "pages_per_search"),
    "JOB_MAX_MIN_EXPERIENCE": ("filters", "max_min_experience"),
    "JOB_MIN_SCORE": ("filters", "min_score"),
    "MAX_APPLIES_PER_RUN": ("apply", "max_applies_per_run"),
}


def apply_env_overrides(sections: dict[str, dict]):
    for var, (section, key) in ENV_LISTS.items():
        value = env_list(var)
        if value is not None:
            sections[section][key] = value
    for var, (section, key) in ENV_INTS.items():
        value = env_int(var)
        if value is not None:
            sections[section][key] = value


def load_config(path: str | Path | None = None, env_path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.yaml"
    load_dotenv(env_path or ROOT / ".env")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    search = raw.get("search") or {}
    search.setdefault("keywords", [])
    search.setdefault("locations", [])
    search.setdefault("experience_years", 1)
    search.setdefault("job_age_days", 7)
    search.setdefault("pages_per_search", 2)

    filters = raw.get("filters") or {}
    filters.setdefault("max_min_experience", 1)
    filters.setdefault("min_score", 2)
    filters.setdefault("title_include", [])
    filters.setdefault("title_exclude", [])
    filters.setdefault("company_exclude", [])

    apply = raw.get("apply") or {}
    apply.setdefault("max_applies_per_run", 25)
    apply.setdefault("delay_seconds", [4, 9])
    apply.setdefault("headless", False)

    search.setdefault("remote_first", False)
    apply_env_overrides({"search": search, "filters": filters, "apply": apply})
    if os.getenv("JOB_REMOTE_FIRST") is not None:
        search["remote_first"] = os.getenv("JOB_REMOTE_FIRST", "").strip().lower() in ("1", "true", "yes")

    company = raw.get("company_apply") or {}
    company.setdefault("resume_pdf", "")
    company.setdefault("max_per_run", 15)
    company.setdefault("heard_about_us", "Naukri.com")
    company.setdefault("cover_letter", "")
    company.setdefault("send_emails", True)

    linkedin = {
        "email": os.getenv("LINKEDIN_EMAIL", "").strip(),
        "password": os.getenv("LINKEDIN_PASSWORD", "").strip(),
        "keywords": env_list("LINKEDIN_KEYWORDS") or search["keywords"],
        "locations": env_list("LINKEDIN_LOCATIONS") or ["India"],
        "experience_levels": env_list("LINKEDIN_EXPERIENCE_LEVELS") or ["1", "2"],
        "date_posted": (os.getenv("LINKEDIN_DATE_POSTED") or "r604800").strip(),
        "work_types": env_list("LINKEDIN_WORK_TYPE") or [],
        # false = also collect jobs that apply on the company site / Google Form (queued for company_apply.py)
        "easy_apply_only": (os.getenv("LINKEDIN_EASY_APPLY_ONLY") or "false").strip().lower() in ("1", "true", "yes"),
        "pages_per_search": env_int("LINKEDIN_PAGES_PER_SEARCH") or 2,
        "max_applies": env_int("LINKEDIN_MAX_APPLIES") or 15,
    }

    truthy = lambda name, default: (os.getenv(name) or str(default)).strip().lower() in ("1", "true", "yes")  # noqa: E731
    foreign = {
        "sources": env_list("FOREIGN_SOURCES") or [],
        "keywords": env_list("FOREIGN_KEYWORDS") or [],
        "max_years": env_int("FOREIGN_MAX_YEARS") if env_int("FOREIGN_MAX_YEARS") is not None else 2,
        "allow_relocation": truthy("FOREIGN_ALLOW_RELOCATION", True),
        "boards": env_list("FOREIGN_BOARDS") or [],
        "countries": [c.lower() for c in (raw.get("profile") or {}).get("work_authorized_countries") or ["India"]],
        "pages": env_int("FOREIGN_PAGES") or 1,
        "cache_hours": env_int("FOREIGN_CACHE_HOURS") or 6,
    }

    return Config(
        search=search,
        filters=filters,
        skills=[s.lower().strip() for s in raw.get("skills") or []],
        apply=apply,
        profile=raw.get("profile") or {},
        custom_answers={str(k).lower(): v for k, v in (raw.get("custom_answers") or {}).items()},
        company_apply=company,
        linkedin=linkedin,
        foreign=foreign,
        email=os.getenv("NAUKRI_EMAIL", "").strip(),
        smtp_email=os.getenv("SMTP_EMAIL", "").strip(),
        smtp_password=os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "").strip(),
        password=os.getenv("NAUKRI_PASSWORD", "").strip(),
        root=path.resolve().parent,
    )
