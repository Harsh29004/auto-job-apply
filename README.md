# Naukri Auto Apply Bot

Searches Naukri for jobs, filters them against your resume, and applies automatically.
It also answers the recruiter chatbot questions.

> **This is a generic tool — you need to add your own resume and fill in your personal details before using it.**

## Setup (one time)

### 1. Install dependencies

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Add your resume

Place your resume PDF in the project root folder (same folder as `main.py`).

```
your_resume.pdf      ← your actual resume file
```

### 3. Set up config

```bash
copy config.example.yaml config.yaml
```

Open `config.yaml` and fill in your details. Update the resume filename:

```yaml
company_apply:
  resume_pdf: your_resume.pdf    # ← change this to your resume filename
```

> ⚠️ `config.yaml` is gitignored — your personal data will **not** be pushed to GitHub.

### 4. Set up credentials

```bash
copy .env.example .env
```

Open `.env` and fill in:

```env
NAUKRI_EMAIL=your_naukri_email@example.com
NAUKRI_PASSWORD=your_naukri_password
```

For company-site email applications (optional):

```env
SMTP_EMAIL=your_gmail@gmail.com
SMTP_APP_PASSWORD=xxxx xxxx xxxx xxxx   # Google Account > Security > App passwords
```

### 5. Fill in your profile in `config.yaml`

Open `config.yaml` and update the **`profile`** section with your own details:

```yaml
profile:
  name: Your Full Name
  email: your_email@example.com
  phone: "9999999999"
  current_location: Your City
  preferred_location: Anywhere in India / Remote
  total_experience_years: 0
  total_experience_months: 0
  current_company: ""
  current_designation: ""
  notice_period: Immediate
  current_ctc_lpa: ""          # e.g. 2.4 (CTC questions are skipped while empty)
  expected_ctc_lpa: ""         # e.g. 4
  highest_qualification: B.Tech
  degree_branch: Your Branch
  college: Your College
  graduation_year: "2025"
  about_me: A short one-line summary about yourself (max ~100 characters)
  project_summary: A short description of your best project
  why_join: Why you want to join (max ~100 characters)
```

Also update the **`cover_letter`** under `company_apply` with your own details.

### 6. Customize job search (optional)

Edit the following sections in `config.yaml` or override them in `.env`:

- **`search.keywords`** — job search terms
- **`skills`** — your skills from your resume (used for job scoring and answering skill questions)
- **`filters.title_include`** — job titles you're interested in
- **`filters.title_exclude`** — job titles to skip (senior, lead, java, etc.)
- **`filters.max_min_experience`** — skip jobs requiring more experience than this
- **`custom_answers`** — extra answers for recruiter questions

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

## LinkedIn Easy Apply (`linkedin_apply.py`)

Add your LinkedIn login to `.env` (`LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD`). All search settings are also in `.env` (`LINKEDIN_*`).

| Command | What it does |
|---|---|
| `python linkedin_apply.py --login` | Log in once. If LinkedIn asks for a code or captcha, complete it in the browser window. |
| `python linkedin_apply.py --dry-run --max 3` | Fill the Easy Apply dialogs, then discard them. Nothing is submitted. |
| `python linkedin_apply.py --max 5` | Apply to at most 5 jobs (default `LINKEDIN_MAX_APPLIES`) |
| `python linkedin_apply.py --report` | Export `data/linkedin_jobs.csv` and list the questions the bot could not answer |

- The bot uses the same title, experience and skill filters as the Naukri bot. It also skips jobs whose description says unpaid or no stipend.
- The Easy Apply dialog is filled with the same answer engine, and your resume already saved on LinkedIn is reused.
- An application counts only when LinkedIn shows "application was sent". If Submit was clicked but no confirmation appeared, the job is marked `unconfirmed`. That still counts toward the limit and is never retried.
- Keep `LINKEDIN_MAX_APPLIES` low (about 15). LinkedIn restricts accounts that apply too fast.

## Remote and foreign jobs

| `.env` setting | Effect |
|---|---|
| `JOB_REMOTE_FIRST=true` | Naukri searches remote / work-from-home jobs first (`wfhType=2`) and applies to remote jobs before others |
| `LINKEDIN_WORK_TYPE=2` | LinkedIn remote-only filter (`1` on-site, `2` remote, `3` hybrid; comma-separated, empty = all) |
| `LINKEDIN_LOCATIONS=Worldwide, United States, …` | Search foreign markets as well as India |

The answer engine handles foreign jobs as follows:
- It answers "Are you authorized to work in X?" and "Do you need visa sponsorship?" from `profile.work_authorized_countries`. If the question names no country, it uses the job's country.
  - For a US job it answers "authorized: No" and "needs sponsorship: Yes".
  - It never claims work rights you don't have.
- For shift, time-zone and work-mode questions, it answers from `profile.shift_preference`. By default that is flexible (any shift), with remote preferred.
- It answers "Rate yourself 1–10" questions with `profile.self_rating`.

## Project structure

| File / Folder | Purpose |
|---|---|
| `config.example.yaml` | Template config — copy to `config.yaml` and fill in your details |
| `config.yaml` | Your personal config (gitignored, never pushed) |
| `.env.example` | Template for `.env` |
| `.env` | Your Naukri login & SMTP credentials (gitignored, never pushed) |
| `main.py` | Main bot entry point |
| `company_apply.py` | Company-site apply bot |
| `linkedin_apply.py` | LinkedIn Easy Apply bot |
| `run_bot.bat` | Windows shortcut to run the bot |
| `naukri_bot/` | Core bot modules (search, filter, apply, answer engine) |
| `tests/` | Test suite |
| `browser_profile/` | Saved login session (auto-created, gitignored) |
| `data/` | Database, CSV exports, logs, debug screenshots (gitignored) |

## Important notes

- **Copy `config.example.yaml` to `config.yaml`** and fill in your own details before running.
- **Your resume (`.pdf` / `.docx`) must be placed in the project root.** It is gitignored and will not be pushed to GitHub.
- **Your `.env` file contains passwords.** It is gitignored. Never commit it.
- **`config.yaml` contains your personal profile.** It is gitignored. Never commit it.
- Run `--dry-run` first to verify your filters before actually applying.

## License

MIT
