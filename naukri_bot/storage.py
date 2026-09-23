"""SQLite log of every job seen / applied, so the bot never applies twice."""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

from .ats import detect_ats
from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    title TEXT, company TEXT, url TEXT, location TEXT, experience TEXT,
    status TEXT, reason TEXT, score INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS external_jobs (
    job_id TEXT PRIMARY KEY,
    title TEXT, company TEXT, location TEXT, experience TEXT, score INTEGER,
    naukri_url TEXT, apply_url TEXT, ats TEXT,
    status TEXT DEFAULT 'pending', detail TEXT, added_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS unanswered (
    question TEXT PRIMARY KEY, options TEXT, job_id TEXT, seen INTEGER DEFAULT 1, updated_at TEXT
);
"""

# Statuses after which we never touch the job again.
FINAL = {"applied", "already_applied"}


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def status(self, job_id: str) -> str | None:
        row = self.conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return row[0] if row else None

    def is_done(self, job_id: str) -> bool:
        return self.status(job_id) in FINAL

    def record(self, job: Job, status: str, reason: str = "", score: int = 0):
        self.conn.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job.job_id, job.title, job.company, job.url, job.location, job.experience,
             status, reason, score, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def record_unanswered(self, question: str, options: list[str], job_id: str):
        self.conn.execute(
            """INSERT INTO unanswered VALUES (?,?,?,1,?)
               ON CONFLICT(question) DO UPDATE SET seen=seen+1, options=excluded.options,
               job_id=excluded.job_id, updated_at=excluded.updated_at""",
            (question.strip(), " | ".join(options), job_id, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def applied_today(self) -> int:
        today = datetime.now().date().isoformat()
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='applied' AND updated_at LIKE ?", (today + "%",)
        ).fetchone()[0]

    def counts(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())

    def export_csv(self, path: str | Path):
        rows = self.conn.execute(
            "SELECT updated_at,status,title,company,location,experience,score,reason,url "
            "FROM jobs ORDER BY updated_at DESC"
        ).fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["time", "status", "title", "company", "location", "experience", "score", "reason", "url"])
            w.writerows(rows)
        return len(rows)

    def unanswered(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT question, options, seen FROM unanswered ORDER BY seen DESC"
        ).fetchall()

    # ------------------------------------------------ company-site (external) jobs
    def save_external(self, job: Job, score: int = 0) -> bool:
        """Remember a relevant company-site job. Returns True if it is new."""
        now = datetime.now().isoformat(timespec="seconds")
        if job.apply_url and self.conn.execute(
                "SELECT 1 FROM external_jobs WHERE apply_url=? AND job_id<>?", (job.apply_url, job.job_id)).fetchone():
            return False  # same company link already saved (posting repeated for another city)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO external_jobs "
            "(job_id,title,company,location,experience,score,naukri_url,apply_url,ats,status,detail,added_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'pending','',?,?)",
            (job.job_id, job.title, job.company, job.location, job.experience, score,
             job.url, job.apply_url, detect_ats(job.apply_url), now, now),
        )
        if not cur.rowcount and job.apply_url:  # fill in a link we didn't have before
            self.conn.execute(
                "UPDATE external_jobs SET apply_url=?, ats=? WHERE job_id=? AND (apply_url IS NULL OR apply_url='')",
                (job.apply_url, detect_ats(job.apply_url), job.job_id))
        self.conn.commit()
        return bool(cur.rowcount)

    def external_jobs(self, statuses: tuple[str, ...] = ("pending",), limit: int | None = None) -> list[dict]:
        q = f"SELECT * FROM external_jobs WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY score DESC, added_at"
        if limit:
            q += f" LIMIT {int(limit)}"
        cur = self.conn.execute(q, statuses)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def update_external(self, job_id: str, status: str, detail: str = "", apply_url: str | None = None,
                        ats: str | None = None):
        sets, args = ["status=?", "detail=?", "updated_at=?"], [status, detail, datetime.now().isoformat(timespec="seconds")]
        if apply_url:
            sets.append("apply_url=?")
            args.append(apply_url)
        if ats:
            sets.append("ats=?")
            args.append(ats)
        self.conn.execute(f"UPDATE external_jobs SET {', '.join(sets)} WHERE job_id=?", (*args, job_id))
        self.conn.commit()

    def external_counts(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT status, COUNT(*) FROM external_jobs GROUP BY status").fetchall())

    def export_external_csv(self, path: str | Path) -> int:
        rows = self.conn.execute(
            "SELECT status,title,company,location,experience,score,ats,apply_url,naukri_url,detail,added_at,updated_at "
            "FROM external_jobs ORDER BY status, score DESC").fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["status", "title", "company", "location", "experience", "score", "ats", "apply_url",
                        "naukri_url", "detail", "added_at", "updated_at"])
            w.writerows(rows)
        return len(rows)
