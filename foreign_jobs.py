"""Find remote jobs at foreign companies and apply on their sites.

  python foreign_jobs.py                        # collect from all boards (Himalayas, Remotive, RemoteOK,
                                                #   Jobicy, We Work Remotely, Arbeitnow) -> data/foreign_jobs.csv
  python foreign_jobs.py --sources himalayas    # only some boards
  python foreign_jobs.py --list                 # show saved foreign jobs
  python foreign_jobs.py --apply --no-submit    # fill the application forms but don't submit (preview)
  python foreign_jobs.py --apply --max 5        # apply to 5 (tailored resume picked per job)

Settings live in .env (FOREIGN_*). Jobs are saved in data/naukri.db (external_jobs, source=<board>).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from naukri_bot.company import CompanyApplier, write_manual_page
from naukri_bot.config import load_config
from naukri_bot.foreign import ALL_SOURCES, collect
from naukri_bot.storage import Storage

FOREIGN = tuple(ALL_SOURCES) + ("greenhouse", "lever", "ashby")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Remote / foreign company jobs")
    ap.add_argument("--sources", help=f"comma-separated boards (default: {','.join(ALL_SOURCES)})")
    ap.add_argument("--list", action="store_true", help="list saved foreign jobs")
    ap.add_argument("--apply", action="store_true", help="apply to pending foreign jobs")
    ap.add_argument("--no-submit", action="store_true", help="with --apply: fill forms, don't submit")
    ap.add_argument("--max", type=int, default=5, help="with --apply: max jobs (default 5)")
    ap.add_argument("--retry", action="store_true", help="with --apply: also retry manual/failed jobs")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(cfg.data_dir / f"foreign_{datetime.now():%Y%m%d}.log",
                                                      encoding="utf-8")])
    log = logging.getLogger("foreign")
    db = Storage(cfg.data_dir / "naukri.db")
    try:
        if args.apply:
            statuses = ("pending", "manual", "unconfirmed", "failed") if args.retry else ("pending",)
            with CompanyApplier(cfg, db, submit=not args.no_submit, headless=args.headless) as bot:
                stats = bot.run(args.max, retry_manual=args.retry, sources=FOREIGN, statuses=statuses)
            log.info("Done: %s", stats)
        elif not args.list:
            sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
            stats = collect(cfg, db, sources, cfg.foreign)
            log.info("fetched %(fetched)d, unique %(unique)d, NEW relevant saved: %(saved)d", stats)
            log.info("skipped: %s", stats["skipped"])

        rows = db.external_jobs(("pending", "manual", "failed", "unconfirmed", "applied", "filled"), sources=FOREIGN)
        out = cfg.data_dir / "foreign_jobs.csv"
        import csv
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["status", "source", "title", "company", "location", "score", "ats", "apply_url", "listing_url",
                        "detail"])
            for r in rows:
                w.writerow([r["status"], r["source"], r["title"], r["company"], r["location"], r["score"], r["ats"],
                            r["apply_url"], r["naukri_url"], r["detail"]])
        if args.list:
            for r in rows:
                print(f"{r['status']:<10} {r['source']:<14} {r['title'][:45]:<45} @ {r['company'][:25]:<25} "
                      f"{r['location'][:30]}")
        write_manual_page(db, cfg.data_dir / "manual_apply.html")
        print(f"\n{len(rows)} foreign jobs -> {out}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
