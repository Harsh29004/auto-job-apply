"""One CSV with every application the bots sent: data/applied_companies.csv (Naukri, LinkedIn, Indeed,
company sites, Google Forms and emailed resumes)."""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

COLUMNS = ["applied_at", "platform", "company", "job_title", "location", "status", "applied_via", "apply_link",
           "job_link", "detail"]
BOARDS = (("naukri.db", "Naukri", "Naukri apply"), ("linkedin.db", "LinkedIn", "LinkedIn Easy Apply"),
          ("indeed.db", "Indeed", "Indeed Apply"))
# external_jobs.source -> where the job was found
SOURCES = {"naukri": "Naukri", "linkedin": "LinkedIn", "indeed": "Indeed", "himalayas": "Himalayas",
           "remotive": "Remotive", "remoteok": "Remote OK", "jobicy": "Jobicy", "weworkremotely": "We Work Remotely",
           "arbeitnow": "Arbeitnow", "workingnomads": "Working Nomads", "themuse": "The Muse",
           "4dayweek": "4 Day Week", "landingjobs": "Landing.jobs", "arc": "Arc",
           "greenhouse": "Company board", "lever": "Company board", "ashby": "Company board"}
SENT = ("applied", "unconfirmed")  # unconfirmed = Submit was clicked, confirmation not seen


def applied_rows(data_dir: Path) -> list[dict]:
    rows = []
    for db_name, platform, via in BOARDS:
        path = Path(data_dir) / db_name
        if not path.exists():
            continue
        conn = sqlite3.connect(path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "jobs" in tables:
                for at, status, title, company, location, url, reason in conn.execute(
                        "SELECT updated_at, status, title, company, location, url, reason FROM jobs "
                        "WHERE status IN (?, ?)", SENT):
                    rows.append(dict(applied_at=at, platform=platform, company=company, job_title=title,
                                     location=location, status=status, applied_via=via, apply_link="", job_link=url,
                                     detail=reason))
            if "external_jobs" in tables:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(external_jobs)")}
                source = "source" if "source" in cols else "'naukri'"
                for at, status, title, company, location, apply_url, listing, src, ats, detail in conn.execute(
                        f"SELECT updated_at, status, title, company, location, apply_url, naukri_url, {source}, ats, "
                        "detail FROM external_jobs WHERE status IN (?, ?)", SENT):
                    detail = detail or ""
                    how = "email" if detail.startswith("emailed") else \
                        "Google Form" if ats == "googleforms" or "Google Form" in detail else \
                        f"company site ({ats or 'website'})"
                    rows.append(dict(applied_at=at, platform=SOURCES.get(src or "naukri", (src or "").title()),
                                     company=company, job_title=title, location=location, status=status,
                                     applied_via=how, apply_link=apply_url, job_link=listing, detail=detail))
        finally:
            conn.close()
    rows.sort(key=lambda r: r["applied_at"] or "", reverse=True)
    return rows


def export_applied(data_dir: Path) -> tuple[Path, int]:
    """Write data/applied_companies.csv (UTF-8 with BOM so Excel shows names like 'Café' correctly)."""
    path = Path(data_dir) / "applied_companies.csv"
    rows = applied_rows(data_dir)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return path, len(rows)
