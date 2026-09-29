"""Unified auto job apply bot -- Naukri, LinkedIn, Indeed, Company sites, Foreign jobs.

  python main.py                         # run EVERYTHING (see `all` below) - this is what run_bot.bat does
  python main.py naukri --dry-run        # Naukri: search + filter only
  python main.py naukri --max 5          # Naukri: apply to at most 5
  python main.py naukri --login          # Naukri: log in once, save session
  python main.py naukri --job-url URL    # Naukri: apply to one URL
  python main.py naukri --report         # Naukri: export CSV + unanswered Qs

  python main.py linkedin --login       # LinkedIn: log in once
  python main.py linkedin --dry-run     # LinkedIn: fill but don't submit
  python main.py linkedin --max 3       # LinkedIn: apply to 3 jobs
  python main.py linkedin --report      # LinkedIn: export CSV
  python main.py linkedin --then-company 5   # after Easy Apply, do 5 company-site jobs

  python main.py indeed --login         # Indeed: log in once (emailed code / captcha in the browser)
  python main.py indeed --dry-run       # Indeed: fill the apply steps but don't submit
  python main.py indeed --max 5         # Indeed: apply to 5 jobs
  python main.py indeed --report        # Indeed: export CSV

  python main.py company                # apply to pending company-site jobs
  python main.py company --no-submit    # fill but don't submit
  python main.py company --assist 120   # fill, then you review + submit
  python main.py company --retry        # also retry manual/failed
  python main.py company --list         # list saved jobs

  python main.py foreign                # collect from all boards
  python main.py foreign --sources himalayas   # only specific boards
  python main.py foreign --apply --max 5       # apply to 5 foreign jobs
  python main.py foreign --list         # list saved foreign jobs

  python main.py all                    # Naukri -> LinkedIn -> Indeed -> remote job boards -> every pending
                                        #   company-site job -> data/applied_companies.csv
  python main.py all --dry-run          # everything in preview mode (nothing is submitted)
  python main.py all --skip indeed,naukri   # leave some steps out
  python main.py report                 # combined report for all platforms
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from naukri_bot.answers import Answerer
from naukri_bot.applied import export_applied
from naukri_bot.bot import NaukriBot, QuotaReached
from naukri_bot.config import load_config
from naukri_bot.models import Job
from naukri_bot.storage import Storage


def setup_logging(cfg, prefix="run"):
    log_file = cfg.data_dir / f"{prefix}_{datetime.now():%Y%m%d}.log"
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", handlers=handlers)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


# ──────────────────────────── report helpers ────────────────────────────

def naukri_report(cfg, db):
    out = cfg.data_dir / "applied_jobs.csv"
    n = db.export_csv(out)
    print(f"\n[Naukri] Exported {n} jobs -> {out}")
    print("Status counts:", db.counts(), "| applied today:", db.applied_today())
    ext = cfg.data_dir / "company_site_jobs.csv"
    n_ext = db.export_external_csv(ext)
    print(f"Company-site jobs ({n_ext}) -> {ext}  {db.external_counts()}  (apply with: python main.py company)")
    # hide questions that the current config.yaml can already answer
    answerer = Answerer(cfg.profile, cfg.skills, cfg.custom_answers)
    rows = [r for r in db.unanswered()
            if answerer.answer(r[0], [o for o in (r[1] or "").split(" | ") if o]) is None]
    if rows:
        print("\nQuestions the bot could not answer (add them under custom_answers in config.yaml):")
        for q, opts, seen in rows:
            print(f"  ({seen}x) {q}" + (f"   options: {opts}" if opts else ""))


def applied_summary(cfg):
    path, n = export_applied(cfg.data_dir)
    print(f"\nAll applications so far ({n}) -> {path}")


def linkedin_report(cfg, db):
    board_report(cfg, db, "LinkedIn", "linkedin_jobs.csv")


def indeed_report(cfg, db):
    board_report(cfg, db, "Indeed", "indeed_jobs.csv")


def board_report(cfg, db, name, csv_name):
    out = cfg.data_dir / csv_name
    n = db.export_csv(out)
    print(f"\n[{name}] Jobs ({n}) -> {out}")
    print("Status counts:", db.counts(), "| applied today:", db.applied_today())
    answerer = Answerer(cfg.profile, cfg.skills, cfg.custom_answers)
    rows = [r for r in db.unanswered() if answerer.answer(r[0]) is None]
    if rows:
        print("\nQuestions/fields the bot could not fill (add under custom_answers in config.yaml):")
        for q, _, seen in rows:
            print(f"  ({seen}x) {q}")


# ──────────────────────────── subcommands ───────────────────────────────

def cmd_naukri(args, cfg):
    """Run the Naukri auto-apply bot."""
    log = logging.getLogger("naukri")
    db = Storage(cfg.data_dir / "naukri.db")
    try:
        if args.report:
            naukri_report(cfg, db)
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
        naukri_report(cfg, db)
        return 0
    finally:
        db.close()


def cmd_linkedin(args, cfg):
    """Run the LinkedIn Easy Apply bot."""
    from naukri_bot.linkedin import LinkedInBot

    log = logging.getLogger("linkedin")
    db = Storage(cfg.data_dir / "linkedin.db")
    try:
        if args.report:
            linkedin_report(cfg, db)
            return 0
        with LinkedInBot(cfg, db, dry_run=args.dry_run, headless=args.headless) as bot:
            if args.login:
                return 0 if bot.login() else 1
            stats = bot.run(args.max)
            log.info("Done: %s", stats)
        if args.then_company:
            from naukri_bot.company import CompanyApplier
            queue = Storage(cfg.data_dir / "naukri.db")
            try:
                with CompanyApplier(cfg, queue, submit=not args.dry_run, headless=args.headless) as company:
                    cstats = company.run(args.then_company, sources=("linkedin",))
                log.info("Company-site applications: %s", cstats)
            finally:
                queue.close()
        linkedin_report(cfg, db)
        return 0
    finally:
        db.close()


def cmd_indeed(args, cfg):
    """Run the Indeed apply bot."""
    from naukri_bot.indeed import IndeedBot

    log = logging.getLogger("indeed")
    db = Storage(cfg.data_dir / "indeed.db")
    try:
        if args.report:
            indeed_report(cfg, db)
            return 0
        with IndeedBot(cfg, db, dry_run=args.dry_run, headless=args.headless) as bot:
            if args.login:
                return 0 if bot.login() else 1
            stats = bot.run(args.max)
            log.info("Done: %s", stats)
        if args.then_company:
            from naukri_bot.company import CompanyApplier
            queue = Storage(cfg.data_dir / "naukri.db")
            try:
                with CompanyApplier(cfg, queue, submit=not args.dry_run, headless=args.headless) as company:
                    cstats = company.run(args.then_company, sources=("indeed",))
                log.info("Company-site applications: %s", cstats)
            finally:
                queue.close()
        indeed_report(cfg, db)
        applied_summary(cfg)
        return 0
    finally:
        db.close()


def cmd_company(args, cfg):
    """Apply to company-site jobs saved by Naukri/LinkedIn bots."""
    from naukri_bot.company import CompanyApplier, write_manual_page

    log = logging.getLogger("company")
    db = Storage(cfg.data_dir / "naukri.db")
    try:
        if not args.list:
            with CompanyApplier(cfg, db, submit=not args.no_submit, headless=args.headless,
                                assist_seconds=args.assist) as bot:
                stats = bot.run(args.max, retry_manual=args.retry,
                                statuses=("pending", "failed") if args.retry_failed and not args.retry else None)
            log.info("Done: %s", stats)
        n = db.export_external_csv(cfg.data_dir / "company_site_jobs.csv")
        page = cfg.data_dir / "manual_apply.html"
        m = write_manual_page(db, page)
        print(f"\n[Company] Company-site jobs: {db.external_counts()}  (all {n} -> data/company_site_jobs.csv)")
        print(f"{m} jobs still need you -> {page}")
        applied_summary(cfg)
        return 0
    finally:
        db.close()


def cmd_foreign(args, cfg):
    """Find remote foreign jobs and optionally apply."""
    import csv
    from naukri_bot.company import CompanyApplier, write_manual_page
    from naukri_bot.foreign import ALL_SOURCES, collect

    FOREIGN = tuple(ALL_SOURCES) + ("greenhouse", "lever", "ashby")
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
        print(f"\n[Foreign] {len(rows)} foreign jobs -> {out}")
        return 0
    finally:
        db.close()


STEPS = ("naukri", "linkedin", "indeed", "foreign", "company")


def banner(n, title):
    print("\n" + "=" * 60)
    print(f"  STEP {n}/{len(STEPS)} - {title}")
    print("=" * 60)


def cmd_all(args, cfg):
    """Every bot, one after the other: Naukri -> LinkedIn -> Indeed -> remote job boards -> all pending
    company-site jobs (the ones every board found, incl. emailing resumes where a posting asks for it).
    A step that fails (login, site change ...) is logged and the next one still runs."""
    log = logging.getLogger("all")
    skip = {s.strip().lower() for s in (args.skip or "").split(",") if s.strip()}

    def step(n, name, title, fn):
        if name in skip:
            log.info("[%s] skipped (--skip)", title)
            return
        banner(n, title)
        try:
            fn()
        except KeyboardInterrupt:
            raise
        except (Exception, SystemExit) as e:  # noqa: BLE001 - one bot failing must not stop the others
            log.error("[%s] stopped: %s", title, e)

    def naukri():
        db = Storage(cfg.data_dir / "naukri.db")
        try:
            with NaukriBot(cfg, db, dry_run=args.dry_run, headless=args.headless or None) as bot:
                if args.dry_run or bot.login():
                    log.info("[Naukri] %s", bot.run(args.max))
                else:
                    log.warning("[Naukri] login failed, skipping")
            naukri_report(cfg, db)
        finally:
            db.close()

    def linkedin():
        from naukri_bot.linkedin import LinkedInBot
        db = Storage(cfg.data_dir / "linkedin.db")
        try:
            with LinkedInBot(cfg, db, dry_run=args.dry_run, headless=args.headless) as bot:
                if bot.login():
                    log.info("[LinkedIn] %s", bot.run(args.max))
                else:
                    log.warning("[LinkedIn] login failed, skipping")
            linkedin_report(cfg, db)
        finally:
            db.close()

    def indeed():
        from naukri_bot.indeed import IndeedBot
        db = Storage(cfg.data_dir / "indeed.db")
        try:
            with IndeedBot(cfg, db, dry_run=args.dry_run, headless=args.headless) as bot:
                if bot.login():
                    log.info("[Indeed] %s", bot.run(args.max))
                else:
                    log.warning("[Indeed] not logged in, skipping (run: python main.py indeed --login)")
            indeed_report(cfg, db)
        finally:
            db.close()

    def foreign():
        from naukri_bot.foreign import collect
        db = Storage(cfg.data_dir / "naukri.db")
        try:
            stats = collect(cfg, db, None, cfg.foreign)
            log.info("[Job boards] fetched %(fetched)d, unique %(unique)d, NEW relevant saved: %(saved)d", stats)
        finally:
            db.close()

    def company():
        from naukri_bot.company import CompanyApplier, write_manual_page
        db = Storage(cfg.data_dir / "naukri.db")
        try:
            # every pending job the boards found (or --max), not just company_apply.max_per_run
            with CompanyApplier(cfg, db, submit=not args.dry_run, headless=args.headless) as bot:
                log.info("[Company] %s", bot.run(args.max or 1_000_000))
            n = db.export_external_csv(cfg.data_dir / "company_site_jobs.csv")
            m = write_manual_page(db, cfg.data_dir / "manual_apply.html")
            print(f"[Company] {n} company-site jobs exported, {m} need you -> data/manual_apply.html")
        finally:
            db.close()

    for n, (name, title, fn) in enumerate([("naukri", "NAUKRI", naukri), ("linkedin", "LINKEDIN", linkedin),
                                           ("indeed", "INDEED", indeed), ("foreign", "REMOTE JOB BOARDS", foreign),
                                           ("company", "COMPANY SITES / GOOGLE FORMS / EMAIL", company)], 1):
        step(n, name, title, fn)

    applied_summary(cfg)
    print("\n" + "=" * 60)
    print("  ALL DONE!")
    print("=" * 60)
    return 0


def cmd_report(args, cfg):
    """Combined report for all platforms."""
    print("=" * 60)
    print("  NAUKRI REPORT")
    print("=" * 60)
    db_naukri = Storage(cfg.data_dir / "naukri.db")
    try:
        naukri_report(cfg, db_naukri)
    finally:
        db_naukri.close()

    print("\n" + "=" * 60)
    print("  LINKEDIN REPORT")
    print("=" * 60)
    db_li = Storage(cfg.data_dir / "linkedin.db")
    try:
        linkedin_report(cfg, db_li)
    finally:
        db_li.close()

    print("\n" + "=" * 60)
    print("  INDEED REPORT")
    print("=" * 60)
    db_in = Storage(cfg.data_dir / "indeed.db")
    try:
        indeed_report(cfg, db_in)
    finally:
        db_in.close()

    print("\n" + "=" * 60)
    print("  COMPANY SITES REPORT")
    print("=" * 60)
    from naukri_bot.company import write_manual_page
    db_ext = Storage(cfg.data_dir / "naukri.db")
    try:
        n = db_ext.export_external_csv(cfg.data_dir / "company_site_jobs.csv")
        page = cfg.data_dir / "manual_apply.html"
        m = write_manual_page(db_ext, page)
        print(f"Company-site jobs: {db_ext.external_counts()}  (all {n} -> data/company_site_jobs.csv)")
        print(f"{m} jobs still need you -> {page}")
    finally:
        db_ext.close()

    applied_summary(cfg)
    return 0


# ──────────────────────────── CLI parser ────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Unified auto job apply bot — Naukri, LinkedIn, Company sites, Foreign jobs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command")

    # ── naukri ──
    p_naukri = sub.add_parser("naukri", help="Naukri.com auto apply")
    p_naukri.add_argument("--dry-run", action="store_true", help="search and filter only, don't apply")
    p_naukri.add_argument("--max", type=int, help="max applications this run")
    p_naukri.add_argument("--headless", action="store_true", help="run browser hidden")
    p_naukri.add_argument("--login", action="store_true", help="only log in and save the session")
    p_naukri.add_argument("--job-url", help="apply to a single job URL")
    p_naukri.add_argument("--report", action="store_true", help="export CSV and show unanswered questions")

    # ── linkedin ──
    p_linkedin = sub.add_parser("linkedin", help="LinkedIn Easy Apply bot")
    p_linkedin.add_argument("--dry-run", action="store_true", help="fill dialogs but discard, never submit")
    p_linkedin.add_argument("--max", type=int, help="max applications this run")
    p_linkedin.add_argument("--login", action="store_true", help="only log in and save the session")
    p_linkedin.add_argument("--headless", action="store_true")
    p_linkedin.add_argument("--report", action="store_true")
    p_linkedin.add_argument("--then-company", type=int, default=0, metavar="N",
                            help="afterwards, apply to N queued company-site jobs from LinkedIn")

    # ── indeed ──
    p_indeed = sub.add_parser("indeed", help="Indeed apply bot (Easily apply + company-site jobs)")
    p_indeed.add_argument("--dry-run", action="store_true", help="fill the apply steps but never submit")
    p_indeed.add_argument("--max", type=int, help="max applications this run")
    p_indeed.add_argument("--login", action="store_true", help="only log in and save the session")
    p_indeed.add_argument("--headless", action="store_true")
    p_indeed.add_argument("--report", action="store_true")
    p_indeed.add_argument("--then-company", type=int, default=0, metavar="N",
                          help="afterwards, apply to N queued company-site jobs from Indeed")

    # ── company ──
    p_company = sub.add_parser("company", help="Apply on company career sites")
    p_company.add_argument("--max", type=int, help="max jobs this run")
    p_company.add_argument("--no-submit", action="store_true", help="fill forms but do not submit")
    p_company.add_argument("--assist", type=int, default=0, metavar="SECONDS",
                           help="fill the form, then wait for you to submit it yourself")
    p_company.add_argument("--retry", action="store_true", help="also retry manual/failed/unconfirmed jobs")
    p_company.add_argument("--retry-failed", action="store_true",
                           help="also retry jobs whose form showed errors (never re-sends unconfirmed ones)")
    p_company.add_argument("--headless", action="store_true")
    p_company.add_argument("--list", action="store_true", help="list saved jobs and write manual_apply.html")

    # ── foreign ──
    p_foreign = sub.add_parser("foreign", help="Remote / foreign company jobs")
    p_foreign.add_argument("--sources", help="comma-separated boards (himalayas,remotive,...)")
    p_foreign.add_argument("--list", action="store_true", help="list saved foreign jobs")
    p_foreign.add_argument("--apply", action="store_true", help="apply to pending foreign jobs")
    p_foreign.add_argument("--no-submit", action="store_true", help="with --apply: fill forms, don't submit")
    p_foreign.add_argument("--max", type=int, default=5, help="with --apply: max jobs (default 5)")
    p_foreign.add_argument("--retry", action="store_true", help="with --apply: also retry manual/failed")
    p_foreign.add_argument("--headless", action="store_true")

    # ── all ──
    p_all = sub.add_parser("all", help="Run every bot: Naukri -> LinkedIn -> Indeed -> job boards -> company sites")
    p_all.add_argument("--dry-run", action="store_true", help="dry-run all bots")
    p_all.add_argument("--max", type=int, help="max applies per bot (company sites: default all pending)")
    p_all.add_argument("--headless", action="store_true")
    p_all.add_argument("--skip", help=f"comma-separated steps to leave out: {', '.join(STEPS)}")

    # ── report ──
    sub.add_parser("report", help="Combined report for all platforms")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg, prefix=args.command or "all")

    # default to 'all' if no subcommand given — run every bot
    cmd = args.command or "all"

    dispatch = {
        "naukri": cmd_naukri,
        "linkedin": cmd_linkedin,
        "indeed": cmd_indeed,
        "company": cmd_company,
        "foreign": cmd_foreign,
        "all": cmd_all,
        "report": cmd_report,
    }

    # when no subcommand is given, add missing attrs for cmd_all
    if not args.command:
        for attr in ("dry_run", "headless"):
            if not hasattr(args, attr):
                setattr(args, attr, False)
        if not hasattr(args, "max"):
            args.max = None
        args.skip = getattr(args, "skip", None)

    return dispatch[cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
