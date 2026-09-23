# Naukri Auto Apply Bot

Searches Naukri for AI/ML, Python and web developer jobs, filters them against your resume,
and applies automatically. It also answers the recruiter chatbot questions.

## Setup (one time)

```
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env      # then put NAUKRI_EMAIL / NAUKRI_PASSWORD in .env
```

## Usage

| Command | What it does |
|---|---|
| `python main.py --dry-run` | Search and filter only. Lists the jobs it would apply to. |
| `python main.py` | Log in and apply (up to `max_applies_per_run`, default 25) |
| `python main.py --max 5` | Apply to at most 5 jobs |
| `python main.py --job-url <url>` | Apply to one specific job |
| `python main.py --report` | Export `data/applied_jobs.csv` and list questions the bot could not answer |
| `python main.py --headless` | Run without showing the browser window |

You can also double-click `run_bot.bat`.

## How it works

1. **Search**: runs every keyword in `config.yaml`, reading Naukri's own search API (`jobapi/v3/search`).
2. **Filter**: skips a job when
   - it has to be applied for on the company's site
   - the title has an excluded word (senior, lead, java, sales and so on)
   - it needs more experience than `max_min_experience`
   - it matches fewer of your skills than `min_score`

   The same posting listed for several cities is applied to once.
3. **Apply**: clicks Apply and answers the chatbot questions using `profile` in `config.yaml`
   (experience per skill, notice period, location, education, a short project summary and so on).
4. **Safety net**: the bot intercepts the final apply request.
   - Naukri's chatbot sometimes skips a mandatory question. The bot fills it in.
   - Naukri rejects text answers over about 100 characters. The bot shortens the answer and resends it.

   The result is read from Naukri's response, so an application counts as `applied` only when Naukri confirms it.
5. **Memory**: `data/naukri.db` records every job, so the bot never applies twice. It stops when Naukri's daily quota (50) is used up.

## When a job is skipped with `needs_answer`

Run `python main.py --report` to see the questions the bot could not answer. Add the answers to `config.yaml`:

```yaml
custom_answers:
  "current ctc": "2.4"
  "notice period": "Immediate"
```

Each key is matched as a lowercase substring of the question, and `custom_answers` are checked before the built-in rules.

**Fill in `current_ctc_lpa` and `expected_ctc_lpa` under `profile`.** Until you do, every job that asks about CTC is skipped.

## Company-site jobs ("Apply on company site")

`main.py` saves relevant company-site jobs, with their links, to `data/company_site_jobs.csv`.
`company_apply.py` then applies to them:

| Command | What it does |
|---|---|
| `python company_apply.py --no-submit` | Fill forms but don't submit (preview; screenshots in `data/company_debug/`) |
| `python company_apply.py` | Fill and submit forms, or email your resume |
| `python company_apply.py --assist 120` | Fill the form, then wait for you to check it and click Submit |
| `python company_apply.py --retry` | Also retry jobs marked manual, failed or unconfirmed |
| `python company_apply.py --list` | Write `data/manual_apply.html`, the jobs left for you, with links |

The bot takes one of three routes for each job:
- **form**: it finds the form (clicking "Apply" first if needed) and fills it from `profile`:
  - text fields, dropdowns and custom dropdowns, radio buttons and consent boxes
  - the resume PDF upload
  - a cover letter

  It only submits when every required field is filled.
- **email**: the page says "send your resume to hr@…". Put a Gmail App Password in `.env` (`SMTP_EMAIL`, `SMTP_APP_PASSWORD`) and the bot sends the email with your resume. Without it, the job goes on the manual list.
- **manual**: it leaves the job for you in these cases:
  - login portals (Workday, Oracle, Taleo)
  - WhatsApp-only jobs
  - captchas
  - pages listing many openings
  - a dropdown that has no correct option

## Files

- `config.yaml`: keywords, filters, skills and your answers
- `.env`: your Naukri login (keep it private)
- `browser_profile/`: the saved login session
- `data/`: the database, CSV export, daily logs, and `debug/` screenshots of failures
