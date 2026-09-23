"""LinkedIn Easy Apply bot.

  python linkedin_apply.py --login        # log in once (session is saved)
  python linkedin_apply.py --dry-run      # fill Easy Apply dialogs but discard instead of submitting
  python linkedin_apply.py                # apply (LINKEDIN_MAX_APPLIES from .env)
  python linkedin_apply.py --max 3        # apply to at most 3 jobs
  python linkedin_apply.py --report       # export data/linkedin_jobs.csv + unanswered questions

Search settings (keywords, location, experience level ...) are in .env (LINKEDIN_*).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from naukri_bot.answers import Answerer
from naukri_bot.config import load_config
from naukri_bot.linkedin import LinkedInBot
from naukri_bot.storage import Storage


def report(cfg, db):
    out = cfg.data_dir / "linkedin_jobs.csv"
    n = db.export_csv(out)
    print(f"\nLinkedIn jobs ({n}) -> {out}")
    print("Status counts:", db.counts(), "| applied today:", db.applied_today())
    answerer = Answerer(cfg.profile, cfg.skills, cfg.custom_answers)
    rows = [r for r in db.unanswered() if answerer.answer(r[0]) is None]
    if rows:
        print("\nQuestions/fields the bot could not fill (add under custom_answers in config.yaml):")
        for q, _, seen in rows:
            print(f"  ({seen}x) {q}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="LinkedIn Easy Apply bot")
    ap.add_argument("--dry-run", action="store_true", help="fill dialogs but discard, never submit")
    ap.add_argument("--max", type=int, help="max applications this run")
    ap.add_argument("--login", action="store_true", help="only log in and save the session")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--config")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(cfg.data_dir / f"linkedin_{datetime.now():%Y%m%d}.log",
                                                      encoding="utf-8")])
    db = Storage(cfg.data_dir / "linkedin.db")
    try:
        if args.report:
            report(cfg, db)
            return 0
        with LinkedInBot(cfg, db, dry_run=args.dry_run, headless=args.headless) as bot:
            if args.login:
                return 0 if bot.login() else 1
            stats = bot.run(args.max)
            logging.getLogger("linkedin").info("Done: %s", stats)
        report(cfg, db)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
