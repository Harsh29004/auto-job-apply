# Job Auto Apply Bot

Finds jobs on **Naukri, LinkedIn, Indeed** and **11 remote job boards**, filters them against your resume, and applies
automatically: Naukri's chatbot, LinkedIn Easy Apply, Indeed Apply, company career sites, Google Forms, and email
(when a posting asks you to mail your resume). Every application ends up in one file, `data/applied_companies.csv`.

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

Double-click **`run_bot.bat`** (or run `python main.py`) to run everything, one step after the other:

1. **Naukri** - apply through Naukri's chatbot (daily cap `MAX_APPLIES_PER_DAY`)
2. **LinkedIn** - Easy Apply; other jobs are queued for step 5
3. **Indeed** - Indeed Apply; "apply on company site" jobs are queued for step 5
4. **Remote job boards** - collect new jobs from 11 boards (below)
5. **Company sites** - every queued job: career-site forms, Google Forms, and emailing your resume when the posting asks for it

A step that fails (login, a site change...) is logged and the next one still runs. At the end everything sent is in
`data/applied_companies.csv`, and the jobs you have to finish yourself are in `data/manual_apply.html`.

| Command | What it does |
|---|---|
| `python main.py all --dry-run` | Everything in preview mode. Nothing is submitted, no resume is uploaded. |
| `python main.py all --skip indeed,naukri` | Leave some steps out |
| `python main.py naukri --dry-run` | Naukri: search and filter only. Lists the jobs it would apply to. |
| `python main.py naukri --max 5` | Naukri: apply to at most 5 jobs |
| `python main.py naukri --job-url <url>` | Naukri: apply to one specific job |
| `python main.py report` | All reports: per-site CSVs, unanswered questions, `applied_companies.csv` |
| `--headless` (any command) | Run without showing the browser window |

**Before the first full run, log in once** to each site in the browser window it opens (the session is saved):
`python main.py naukri --login`, `python main.py linkedin --login`, `python main.py indeed --login`.

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
- LinkedIn's newer Easy Apply button ignores automated clicks, so the bot opens each job's apply page directly
  (`/jobs/view/<id>/apply/`). Jobs that failed with an error are retried on the next run.

## Indeed (`python main.py indeed`)

| Command | What it does |
|---|---|
| `python main.py indeed --login` | Log in once. Indeed usually emails a sign-in code: type it in the browser, or set `INDEED_EMAIL` to the same Gmail as `SMTP_EMAIL` and the bot reads the code itself. |
| `python main.py indeed --dry-run --max 3` | Fill the Indeed Apply steps, never submit |
| `python main.py indeed --max 5` | Apply to at most 5 jobs (default `INDEED_MAX_APPLIES`, daily cap `INDEED_MAX_APPLIES_PER_DAY`) |
| `python main.py indeed --report` | Export `data/indeed_jobs.csv` |

- Searches every site in `INDEED_DOMAINS`. Your own country's site (in.indeed.com) is searched in `INDEED_LOCATIONS`
  (`Remote`, `Anywhere` or city names); the other countries for **remote jobs only**, because on-site jobs abroad
  need a local work permit.
- Same title / experience / skill filters as the other bots. Jobs abroad that require local work rights are skipped.
- Indeed Apply steps are filled with the same answer engine. The resume already chosen on your Indeed account is kept;
  "Relevant experience" gets your `current_designation` and `current_company`.
- "Apply on company site" jobs are queued for the company-site step.
- If Indeed shows a bot check, the bot waits for you to solve it in the browser; if nobody does, it stops the Indeed step.

## Foreign / remote job boards (`foreign_jobs.py`)

Collects remote jobs from **Remotive, RemoteOK, We Work Remotely, Jobicy, Arbeitnow, Himalayas, Working Nomads,
The Muse, 4 Day Week, Landing.jobs and Arc.dev** (11 sources), then applies using the company-site bot.
Arc.dev and Landing.jobs need their own account to apply, so their jobs go on the manual list with a direct link.

| Command | What it does |
|---|---|
| `python foreign_jobs.py` | Collect new jobs from all sources (cached, safe to run often) |
| `python foreign_jobs.py --apply --no-submit` | Fill forms on pending jobs but don't submit (preview) |
| `python foreign_jobs.py --apply --max 5` | Apply to up to 5 jobs |
| `python foreign_jobs.py --apply --retry` | Retry jobs previously marked manual/failed |

- Himalayas pages are behind Cloudflare, so the bot tries to find the same job on the company's **Ashby, Greenhouse or Lever** board. If found, it uses the direct link; otherwise the job goes on the manual list.
- A job is kept only if you can do it from where you live. The job has to be open worldwide, to APAC, to Asia or to one of your `work_authorized_countries`.
- It is skipped if it requires US or UK work authorization, citizenship or a security clearance.
- On-site jobs abroad are kept only when they offer visa sponsorship or relocation (`FOREIGN_ALLOW_RELOCATION=true`). "We do *not* sponsor" is recognised as a no.
- Non-English ads, senior roles and roles asking for more than `FOREIGN_MAX_YEARS` years are skipped.
- All collected jobs are saved in `data/foreign_jobs.csv`.

## Resume variants (`build_resumes.py`)

Generates **6 role-tailored resumes** from a single YAML source file, each with a different summary, project selection and skill emphasis:

| Variant | Target roles |
|---|---|
| `ai_ml_engineer` | AI/ML Engineer, Computer Vision, NLP |
| `data_scientist` | Data Scientist, Data Analyst |
| `genai_llm_engineer` | GenAI, LLM, Prompt Engineering |
| `python_backend_developer` | Python, Django, FastAPI, Flask |
| `computer_vision_engineer` | CV, Image Processing, Robotics |
| `full_stack_developer` | React, MERN, Full Stack |

```bash
python build_resumes.py          # generates DOCX + PDF in resumes/
```

The bot automatically picks the best resume for each job based on the job title and description. It uploads the resume as `<Your_Name>_Resume.pdf`, never as `ai_ml_engineer.pdf`.

Setup:
- Copy `resume_content.example.yaml` to `resumes/content.yaml` and put in your own details.
- Each variant has its own `summary`, skill order, project list and `keywords`, which are used to pick the variant for a job.
- Only list skills you can talk about in an interview.
- `resumes/` is gitignored.

## LinkedIn external jobs

When `LINKEDIN_EASY_APPLY_ONLY=false` in `.env`, the LinkedIn bot also captures non-Easy-Apply jobs (company website links, Google Forms) and queues them for the company-site bot:

```bash
python linkedin_apply.py --max 5 --then-company 5   # Easy Apply 5, then 5 company-site jobs
```

Google Forms are filled by a dedicated handler. It handles:
- short and long answers
- multiple choice, checkboxes and dropdowns
- multi-page forms

For skill checklists it ticks only the skills that are in your `skills`. Forms that need a Google sign-in or a file upload are put on the manual list.

## How answers are kept honest

- **Preview runs never upload your resume.** Many sites upload a file the moment it is chosen, so `--no-submit` and `--dry-run` only log "would upload". Nothing is sent.
- **Demographic (EEO) questions are never guessed.** Race, Hispanic/Latino, veteran, disability and orientation get the "decline to answer" option, or are left empty.
- **Unknown Yes/No questions are not answered "Yes".** Only willingness, consent and flexibility questions get an automatic yes. Anything else is left empty, and if it is required the job goes to the manual list.
- **Visa questions depend on the job's country.** "Authorized to work in the US?" is answered No and "Need sponsorship?" is answered Yes for a US job, based on `profile.work_authorized_countries`.
- **"Describe your experience with X, Y and Z" only claims the skills on your list.** It says plainly which tools you haven't used yet.
- **Dropdowns only match whole words.** "India" never picks "British Indian Ocean Territory", and "Artificial Intelligence and Data Science" never picks "Science".

## Project structure

| File / Folder | Purpose |
|---|---|
| `config.example.yaml` | Template config — copy to `config.yaml` and fill in your details |
| `config.yaml` | Your personal config (gitignored, never pushed) |
| `.env.example` | Template for `.env` |
| `.env` | Your logins, SMTP credentials and search settings (gitignored, never pushed) |
| `main.py` | Entry point for every bot (`python main.py` runs them all) |
| `data/applied_companies.csv` | Every application sent, from every bot, in one file |
| `company_apply.py` | Company-site apply bot |
| `linkedin_apply.py` | LinkedIn Easy Apply bot |
| `foreign_jobs.py` | Foreign remote job collector + applier |
| `build_resumes.py` | Resume generator (6 variants from YAML) |
| `run_bot.bat` | Windows shortcut to run the bot |
| `naukri_bot/` | Core bot modules (search, filter, apply, answer engine) |
| `resumes/` | Generated resume PDFs (gitignored) |
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
