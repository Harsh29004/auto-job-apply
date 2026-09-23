"""Apply to the company-site jobs that main.py saved.

  python company_apply.py                 # apply to pending company-site jobs (company_apply.max_per_run)
  python company_apply.py --no-submit     # fill forms but DON'T submit (test / preview)
  python company_apply.py --assist 120    # fill forms, then you review and click submit yourself
  python company_apply.py --retry         # also retry jobs marked manual / failed / unconfirmed
  python company_apply.py --list          # show saved jobs and write data/manual_apply.html
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from naukri_bot.company import CompanyApplier, write_manual_page
from naukri_bot.config import load_config
from naukri_bot.storage import Storage


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply on company career sites")
    ap.add_argument("--max", type=int, help="max jobs this run")
    ap.add_argument("--no-submit", action="store_true", help="fill forms but do not submit")
    ap.add_argument("--assist", type=int, default=0, metavar="SECONDS",
                    help="fill the form, then wait for you to submit it yourself")
    ap.add_argument("--retry", action="store_true", help="also retry manual/failed/unconfirmed jobs")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--list", action="store_true", help="list saved jobs and write manual_apply.html")
    ap.add_argument("--config")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(cfg.data_dir / f"company_{datetime.now():%Y%m%d}.log",
                                                      encoding="utf-8")])
    db = Storage(cfg.data_dir / "naukri.db")
    try:
        if not args.list:
            with CompanyApplier(cfg, db, submit=not args.no_submit, headless=args.headless,
                                assist_seconds=args.assist) as bot:
                stats = bot.run(args.max, retry_manual=args.retry)
            logging.getLogger("company").info("Done: %s", stats)
        n = db.export_external_csv(cfg.data_dir / "company_site_jobs.csv")
        page = cfg.data_dir / "manual_apply.html"
        m = write_manual_page(db, page)
        print(f"\nCompany-site jobs: {db.external_counts()}  (all {n} -> data/company_site_jobs.csv)")
        print(f"{m} jobs still need you -> {page}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
