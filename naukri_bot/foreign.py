"""Collect remote / foreign-company jobs from public job-board APIs.

Every job goes through the same title / skill filters as the Naukri bot, plus:
  * location must be open to you (worldwide / APAC / Asia / your countries), unless the job
    offers visa sponsorship / relocation and FOREIGN_ALLOW_RELOCATION is on
  * no "must be based in the US", "US citizens only", "security clearance" ...
  * not senior; mid-level only if the description asks for <= FOREIGN_MAX_YEARS years

Relevant jobs are saved to the external_jobs table (source = board name) and applied to by
company_apply.py / foreign_jobs.py --apply. Each board's original job URL is kept and opened
when applying, as the boards' API terms ask. Responses are cached so each board is called at
most every few hours (Remotive asks for <= 4 calls a day).
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .filters import evaluate, min_years_required
from .models import Job, strip_html
from .storage import Storage

log = logging.getLogger("foreign")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36", "Accept": "application/json, text/xml, */*"}
ALL_SOURCES = ["himalayas", "remotive", "remoteok", "jobicy", "weworkremotely", "arbeitnow"]

OPEN_LOCATION = re.compile(
    r"worldwide|anywhere|global|international|all countries|any country|no (location )?restriction|"
    r"work from anywhere|\bapac\b|asia|asia[- ]pacific|\bindia\b|remote$|^remote\b(?!.*\b(us|usa|uk|eu|europe|"
    r"canada|americas|latam|emea)\b)", re.I)
RESTRICTED_TEXT = re.compile(
    r"(must|should|need to|required to) (be |currently )?(be )?(based|located|living|reside|residing|live)( in| within)? "
    r"(the )?(us\b|u\.s\.|usa|united states|uk\b|united kingdom|canada|europe|eu\b|germany|australia)|"
    r"\b(us|u\.s\.) citizens?( only)?\b|green card|security clearance|\bitar\b|"
    r"(only|exclusively) (open to|accepting) (candidates|applicants) (from|in|based in) (the )?(us|usa|united states|"
    r"uk|europe|eu|canada)|\bw-?2\b only", re.I)
SPONSOR_TEXT = re.compile(r"visa sponsorship|sponsor (your |a )?visa|relocation (support|package|assistance)|"
                          r"we (can |will )?(help you )?relocate|relocation to", re.I)
SENIOR = re.compile(r"senior|lead|principal|staff|director|manager|head|expert|architect", re.I)
JUNIOR = re.compile(r"entry|junior|intern|graduate|trainee|any|fresher|associate", re.I)
GERMAN = re.compile(r"\b(und|wir|die|der|mit|für|sie|bei|deine|unser|erfahrung)\b", re.I)
# common words of Spanish / Portuguese / French / German job ads
NON_ENGLISH = re.compile(
    r"\b(desarrollador|ingeniero|desenvolvedor|engenheiro|développeur|ingénieur|entwickler|"
    r"para|con|los|las|del|nosotros|experiencia|trabajo|você|nós|equipe|vaga|avec|nous|vous|pour|"
    r"und|wir|mit|für|erfahrung)\b", re.I)
# a specific place in the title ("... (Remote @ Colombia)", "... - US only") that is not open to you
TITLE_PLACE = re.compile(
    r"\b(us|usa|u\.s\.|united states|canada|uk|united kingdom|europe|eu|emea|latam|latin america|americas|"
    r"brazil|mexico|colombia|argentina|chile|peru|germany|france|spain|portugal|poland|netherlands|ireland|"
    r"australia|new zealand|japan|singapore|philippines|vietnam|israel|south africa|nigeria|kenya|egypt|"
    r"pakistan|bangladesh|turkey|romania|ukraine)\b(?! ?(or|/) ?(remote|worldwide|india))", re.I)


@dataclass
class ForeignJob:
    source: str
    source_id: str
    title: str
    company: str
    listing_url: str
    apply_url: str
    location: str = ""
    description: str = ""
    seniority: str = ""
    tags: list[str] = field(default_factory=list)
    company_slug: str = ""

    def to_job(self) -> Job:
        jid = f"{self.source}-{self.source_id}"
        return Job(job_id=jid, title=self.title.strip(), company=self.company.strip(), url=self.listing_url,
                   skills=[t.lower() for t in self.tags], description=self.description,
                   location=self.location or "Remote", experience=self.seniority,
                   min_exp=min_years_required(self.description), external=True,
                   apply_url=self.apply_url or self.listing_url)


def text_of(value: str) -> str:
    return strip_html(html.unescape(value or ""))


# ------------------------------------------------------------------ HTTP with cache
class Fetcher:
    def __init__(self, cache_dir: Path, max_age_hours: float = 6):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_age = max_age_hours * 3600

    def get(self, url: str, as_json: bool = True, max_age: float | None = None):
        key = hashlib.sha1(url.encode()).hexdigest()[:16]
        path = self.cache_dir / f"{key}.{'json' if as_json else 'xml'}"
        age = max_age if max_age is not None else self.max_age
        if path.exists() and time.time() - path.stat().st_mtime < age:
            raw = path.read_bytes()
        else:
            req = urllib.request.Request(url, headers=UA)
            raw = urllib.request.urlopen(req, timeout=45).read()
            path.write_bytes(raw)
            time.sleep(1)  # be gentle
        return json.loads(raw) if as_json else raw

    def get_or_none(self, url: str, max_age: float = 24 * 3600):
        """Like get(), but a 404/410 is cached as None (company has no board of that kind)."""
        key = hashlib.sha1(url.encode()).hexdigest()[:16]
        miss = self.cache_dir / f"{key}.miss"
        if miss.exists() and time.time() - miss.stat().st_mtime < max_age:
            return None
        try:
            return self.get(url, max_age=max_age)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 410):
                miss.write_text(str(e.code))
                return None
            raise


# ------------------------------------------------------------------ company ATS boards
def _norm_title(t: str) -> set[str]:
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", (t or "").lower())
    t = re.sub(r"\b(remote|m/f/d|f/m/d|w/m/d|all genders|full[- ]time|part[- ]time|contract)\b", " ", t)
    return {w for w in re.findall(r"[a-z0-9+#]+", t) if w not in ("the", "and", "of", "a", "an", "-")}


def title_match(a: str, b: str) -> bool:
    x, y = _norm_title(a), _norm_title(b)
    if not x or not y:
        return False
    # strict: "Web Developer" must not match "Senior Web Developer" (a different posting)
    return x == y or len(x & y) / len(x | y) >= 0.75


def board_postings(f: Fetcher, kind: str, name: str) -> list[tuple[str, str]]:
    """(title, apply url) for every posting on a company's public ATS board ([] if it has none)."""
    try:
        if kind == "ashby":
            js = f.get_or_none(f"https://api.ashbyhq.com/posting-api/job-board/{name}")
            return [(j["title"], j.get("applyUrl") or j["jobUrl"]) for j in (js or {}).get("jobs") or []]
        if kind == "greenhouse":
            js = f.get_or_none(f"https://boards-api.greenhouse.io/v1/boards/{name}/jobs")
            return [(j["title"], j["absolute_url"]) for j in (js or {}).get("jobs") or []]
        if kind == "lever":
            js = f.get_or_none(f"https://api.lever.co/v0/postings/{name}?mode=json")
            return [(j["text"], j.get("applyUrl") or j["hostedUrl"]) for j in js or [] if isinstance(j, dict)]
    except (OSError, ValueError, KeyError):
        return []
    return []


def find_on_company_board(f: Fetcher, company: str, slug: str, title: str) -> str:
    """The same job on the company's own Ashby / Greenhouse / Lever board -> direct apply link."""
    names = []
    for n in (slug, re.sub(r"[^a-z0-9]", "", company.lower()), re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")):
        if n and n not in names:
            names.append(n)
    for name in names[:3]:
        for kind in ("ashby", "greenhouse", "lever"):
            for t, url in board_postings(f, kind, name):
                if title_match(title, t):
                    return url
    return ""


# ------------------------------------------------------------------ sources
def fetch_himalayas(f: Fetcher, keywords: list[str], pages: int = 2) -> list[ForeignJob]:
    out = []
    for kw in keywords:
        for extra in ("&worldwide=true", "&seniority=Entry-level"):
            cursor = ""
            for _ in range(pages):
                url = f"https://himalayas.app/jobs/api/search?q={urllib.parse.quote(kw)}{extra}{cursor}"
                js = f.get(url)
                for j in js.get("jobs") or []:
                    restr = j.get("locationRestrictions") or []
                    out.append(ForeignJob(
                        "himalayas", str(j.get("guid") or j.get("applicationLink"))[-60:], j.get("title", ""),
                        j.get("companyName", ""), j.get("guid") or j.get("applicationLink") or "",
                        j.get("applicationLink") or "", ", ".join(restr) if restr else "Worldwide",
                        text_of(j.get("description") or j.get("excerpt") or ""),
                        ", ".join(j.get("seniority") or []), list(j.get("categories") or []),
                        j.get("companySlug") or ""))
                nxt = js.get("nextCursor")
                if not nxt:
                    break
                cursor = f"&cursor={urllib.parse.quote(str(nxt))}"
    return out


def fetch_remotive(f: Fetcher) -> list[ForeignJob]:
    js = f.get("https://remotive.com/api/remote-jobs", max_age=12 * 3600)
    return [ForeignJob("remotive", str(j["id"]), j["title"], j["company_name"], j["url"], j["url"],
                       j.get("candidate_required_location", ""), text_of(j.get("description", "")),
                       j.get("job_type", ""), list(j.get("tags") or []))
            for j in js.get("jobs") or []]


def fetch_remoteok(f: Fetcher) -> list[ForeignJob]:
    js = f.get("https://remoteok.com/api")
    out = []
    for j in js[1:] if isinstance(js, list) else []:
        if not j.get("id"):
            continue
        out.append(ForeignJob("remoteok", str(j["id"]), j.get("position", ""), j.get("company", ""),
                              j.get("url", ""), j.get("apply_url") or j.get("url", ""), j.get("location", ""),
                              text_of(j.get("description", "")), "", list(j.get("tags") or [])))
    return out


def fetch_jobicy(f: Fetcher) -> list[ForeignJob]:
    out = []
    for industry in ("engineering", "data-science"):
        js = f.get(f"https://jobicy.com/api/v2/remote-jobs?count=100&industry={industry}")
        for j in js.get("jobs") or []:
            out.append(ForeignJob("jobicy", str(j["id"]), html.unescape(j.get("jobTitle", "")),
                                  html.unescape(j.get("companyName", "")), j.get("url", ""), j.get("url", ""),
                                  j.get("jobGeo", ""), text_of(j.get("jobDescription", "")),
                                  j.get("jobLevel", ""), []))
    return out


def fetch_weworkremotely(f: Fetcher) -> list[ForeignJob]:
    out = []
    for cat in ("remote-programming-jobs", "remote-full-stack-programming-jobs", "remote-back-end-programming-jobs",
                "remote-front-end-programming-jobs"):
        try:
            raw = f.get(f"https://weworkremotely.com/categories/{cat}.rss", as_json=False)
            root = ET.fromstring(raw)
        except (ET.ParseError, OSError):
            continue
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            company, _, role = title.partition(":")
            link = item.findtext("link") or ""
            out.append(ForeignJob("weworkremotely", link.rstrip("/").split("/")[-1], role.strip() or title,
                                  company.strip(), link, link, item.findtext("region") or "",
                                  text_of(item.findtext("description") or ""), item.findtext("type") or "", []))
    return out


def fetch_arbeitnow(f: Fetcher, pages: int = 2) -> list[ForeignJob]:
    out = []
    for page in range(1, pages + 1):
        js = f.get(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
        for j in js.get("data") or []:
            desc = text_of(j.get("description", ""))
            loc = j.get("location", "") + (" (remote)" if j.get("remote") else "")
            out.append(ForeignJob("arbeitnow", j["slug"], j.get("title", ""), j.get("company_name", ""),
                                  j.get("url", ""), j.get("url", ""), loc, desc,
                                  ", ".join(j.get("job_types") or []), list(j.get("tags") or [])))
    return out


def fetch_ats_boards(f: Fetcher, boards: list[str]) -> list[ForeignJob]:
    """Company career boards: 'greenhouse:<token>', 'lever:<company>', 'ashby:<org>'."""
    out = []
    for spec in boards:
        kind, _, name = spec.partition(":")
        kind, name = kind.strip().lower(), name.strip()
        try:
            if kind == "greenhouse":
                js = f.get(f"https://boards-api.greenhouse.io/v1/boards/{name}/jobs?content=true")
                for j in js.get("jobs") or []:
                    out.append(ForeignJob("greenhouse", f"{name}-{j['id']}", j["title"], j.get("company_name") or name,
                                          j["absolute_url"], j["absolute_url"], (j.get("location") or {}).get("name", ""),
                                          text_of(j.get("content", ""))))
            elif kind == "lever":
                js = f.get(f"https://api.lever.co/v0/postings/{name}?mode=json")
                for j in js if isinstance(js, list) else []:
                    cat = j.get("categories") or {}
                    out.append(ForeignJob("lever", j["id"], j["text"], name, j["hostedUrl"], j.get("applyUrl") or j["hostedUrl"],
                                          f"{cat.get('location', '')} {j.get('workplaceType', '')}".strip(),
                                          j.get("descriptionPlain", "")))
            elif kind == "ashby":
                js = f.get(f"https://api.ashbyhq.com/posting-api/job-board/{name}")
                for j in js.get("jobs") or []:
                    loc = j.get("location", "") + (" (remote)" if j.get("isRemote") else "")
                    out.append(ForeignJob("ashby", j["id"], j["title"], name, j["jobUrl"], j.get("applyUrl") or j["jobUrl"],
                                          loc, j.get("descriptionPlain", "")))
        except (OSError, ValueError, KeyError) as e:
            log.warning("board %s failed: %s", spec, e)
    return out


# ------------------------------------------------------------------ eligibility
NEGATION = re.compile(r"\b(no|not|unable|cannot|can't|can not|won't|will not|don't|do not|does not|doesn't|without|"
                      r"isn't|is not|neither|nor)\b", re.I)


NEGATION_AFTER = re.compile(r"^[^.]{0,40}?\b(not (available|provided|offered|possible|supported)|unavailable|"
                            r"n't (available|provided|offered))|^\W{0,3}(provided|offered|available)?\W{0,3}:?\s*no\b",
                            re.I)
# hard limits: relocation help doesn't change these
HARD_RESTRICTION = re.compile(
    r"(legally )?authori[sz]ed to work in (the )?(us\b|u\.s\.|usa|united states|uk\b|united kingdom|canada|"
    r"europe|eu\b|australia|germany)|\b(us|u\.s\.) citizens?( only)?\b|citizenship (is )?required|green card|"
    r"security clearance|\bitar\b", re.I)


def offers_sponsorship(text: str) -> bool:
    """True when the ad offers visa sponsorship / relocation (ignores 'we do NOT sponsor visas' and
    'Relocation support is not available')."""
    for m in SPONSOR_TEXT.finditer(text or ""):
        before = text[max(0, m.start() - 60):m.start()]
        after = text[m.end():m.end() + 60]
        if not NEGATION.search(before) and not NEGATION_AFTER.search(after):
            return True
    return False


def location_open(location: str, allowed_countries: list[str]) -> bool:
    loc = (location or "").strip()
    if not loc or OPEN_LOCATION.search(loc):
        return True
    return any(re.search(rf"\b{re.escape(c)}\b", loc, re.I) for c in allowed_countries)


def eligible(fj: ForeignJob, cfg: Config, opts: dict) -> tuple[bool, str, int]:
    job = fj.to_job()
    filters = dict(cfg.filters, max_min_experience=opts.get("max_years", 2))
    ok, reason, score = evaluate(job, filters, cfg.skills, allow_external=True)
    if not ok:
        return False, reason, score
    desc = fj.description or ""
    words = max(len(desc.split()), 1)
    if NON_ENGLISH.search(fj.title) or len(NON_ENGLISH.findall(desc)) / words > 0.02:
        return False, "not in English", score
    place = TITLE_PLACE.search(fj.title)
    allowed = opts.get("countries") or ["india"]
    if place and place.group(1).lower() not in allowed:
        return False, f"title limited to {place.group(1)}", score
    if SENIOR.search(fj.seniority or "") and not JUNIOR.search(fj.seniority or ""):
        return False, f"seniority {fj.seniority}", score
    hard = HARD_RESTRICTION.search(desc)
    if hard:
        return False, f"restricted: {hard.group(0)[:50]}", score
    sponsor = offers_sponsorship(desc)
    if not location_open(fj.location, allowed) and not (opts.get("allow_relocation") and sponsor):
        return False, f"location restricted: {fj.location[:60]}", score
    restricted = RESTRICTED_TEXT.search(desc)
    if restricted and not (opts.get("allow_relocation") and sponsor and "sponsorship" not in restricted.group(0).lower()):
        return False, f"restricted: {restricted.group(0)[:50]}", score
    return True, reason + (" | visa/relocation" if sponsor else ""), score


def collect(cfg: Config, db: Storage, sources: list[str] | None = None, opts: dict | None = None) -> dict:
    opts = opts or {}
    fetcher = Fetcher(cfg.data_dir / "cache", opts.get("cache_hours", 6))
    sources = sources or opts.get("sources") or ALL_SOURCES
    keywords = opts.get("keywords") or cfg.linkedin.get("keywords") or cfg.search["keywords"]
    fetched: list[ForeignJob] = []
    stats = {}
    for src in sources:
        try:
            if src == "himalayas":
                jobs = fetch_himalayas(fetcher, keywords, opts.get("pages", 1))
            elif src == "remotive":
                jobs = fetch_remotive(fetcher)
            elif src == "remoteok":
                jobs = fetch_remoteok(fetcher)
            elif src == "jobicy":
                jobs = fetch_jobicy(fetcher)
            elif src == "weworkremotely":
                jobs = fetch_weworkremotely(fetcher)
            elif src == "arbeitnow":
                jobs = fetch_arbeitnow(fetcher)
            else:
                log.warning("unknown source %s", src)
                continue
        except (OSError, ValueError) as e:
            log.warning("%s: fetch failed (%s)", src, str(e)[:120])
            continue
        log.info("%-15s %4d jobs fetched", src, len(jobs))
        fetched += jobs
    if opts.get("boards"):
        board_jobs = fetch_ats_boards(fetcher, opts["boards"])
        log.info("%-15s %4d jobs fetched", "company boards", len(board_jobs))
        fetched += board_jobs

    seen, saved, reasons = set(), 0, {}
    for fj in fetched:
        key = (fj.title.lower().strip(), fj.company.lower().strip())
        if key in seen or not fj.title:
            continue
        seen.add(key)
        ok, reason, score = eligible(fj, cfg, opts)
        if not ok:
            bucket = reason.split(":")[0].split("(")[0].strip()
            reasons[bucket] = reasons.get(bucket, 0) + 1
            continue
        note = ""
        if "himalayas.app" in (fj.apply_url or fj.listing_url):
            # Himalayas job pages sit behind a Cloudflare bot check -> look for the job on the
            # company's own ATS board instead
            direct = find_on_company_board(fetcher, fj.company, fj.company_slug, fj.title)
            if direct:
                fj.apply_url, note = direct, " | direct company link"
            else:
                note = " | manual (Himalayas page has a bot check)"
        job = fj.to_job()
        if db.save_external(job, score, source=fj.source):
            saved += 1
            if note.startswith(" | manual"):
                db.update_external(job.job_id, "manual",
                                   "Himalayas page has a Cloudflare bot check - open the link and apply yourself")
            log.info("  + [%s] %s @ %s (%s) -- %s%s", fj.source, fj.title, fj.company, fj.location[:40],
                     reason[:70], note)
    stats.update(fetched=len(fetched), unique=len(seen), saved=saved, skipped=reasons)
    return stats
