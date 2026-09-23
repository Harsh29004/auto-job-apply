"""Naukri auto apply bot.

  python main.py --dry-run          # search + filter only, shows what it would apply to
  python main.py                    # log in and apply (max_applies_per_run from config.yaml)
  python main.py --max 5            # apply to at most 5 jobs
  python main.py --login            # just log in once and save the session
  python main.py --job-url URL      # apply to one specific job
  python main.py --report           # export data/applied_jobs.csv + list unanswered questions
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from naukri_bot.answers import Answerer
from naukri_bot.bot import NaukriBot, QuotaReached
from naukri_bot.config import load_config
from naukri_bot.models import Job
from naukri_bot.storage import Storage


def setup_logging(cfg):
    log_file = cfg.data_dir / f"run_{datetime.now():%Y%m%d}.log"
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", handlers=handlers)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


def report(cfg, db):
    out = cfg.data_dir / "applied_jobs.csv"
    n = db.export_csv(out)
    print(f"\nExported {n} jobs -> {out}")
    print("Status counts:", db.counts(), "| applied today:", db.applied_today())
    ext = cfg.data_dir / "company_site_jobs.csv"
    n_ext = db.export_external_csv(ext)
    print(f"Company-site jobs ({n_ext}) -> {ext}  {db.external_counts()}  (apply with: python company_apply.py)")
    # hide questions that the current config.yaml can already answer
    answerer = Answerer(cfg.profile, cfg.skills, cfg.custom_answers)
    rows = [r for r in db.unanswered()
            if answerer.answer(r[0], [o for o in (r[1] or "").split(" | ") if o]) is None]
    if rows:
        print("\nQuestions the bot could not answer (add them under custom_answers in config.yaml):")
        for q, opts, seen in rows:
            print(f"  ({seen}x) {q}" + (f"   options: {opts}" if opts else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Naukri auto job apply bot")
    ap.add_argument("--dry-run", action="store_true", help="search and filter only, don't apply")
    ap.add_argument("--max", type=int, help="max applications this run")
    ap.add_argument("--headless", action="store_true", help="run browser hidden")
    ap.add_argument("--login", action="store_true", help="only log in and save the session")
    ap.add_argument("--job-url", help="apply to a single job URL")
    ap.add_argument("--report", action="store_true", help="export CSV and show unanswered questions")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg)
    db = Storage(cfg.data_dir / "naukri.db")
    log = logging.getLogger("naukri")
    try:
        if args.report:
            report(cfg, db)
            return 0
        with NaukriBot(cfg, db, dry_run=args.dry_run, headless=args.headless or None) as bot:
            if args.login:
                return 0 if bot.login() else 1
            if args.job_url:
                if not bot.login():
                    return 1
                job_id = args.job_url.rstrip("/").split("-")[-1].split("?")[0]
                row = db.conn.execute("SELECT title, company, location, experience FROM jobs WHERE job_id=?",
                                      (job_id,)).fetchone()
                title, company, location, experience = row or ("(direct url)", "", "", "")
                job = Job(job_id=job_id, title=title, company=company, url=args.job_url,
                          location=location, experience=experience)
                try:
                    status, detail = bot.apply(job)
                except QuotaReached as e:
                    status, detail = "quota", str(e)
                db.record(job, status, detail)
                log.info("Result: %s %s", status, detail)
                return 0 if status in ("applied", "already_applied") else 1
            stats = bot.run(args.max)
            log.info("Done: %s", stats)
        report(cfg, db)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
