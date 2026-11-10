# HireSphere

HireSphere is a responsive recruitment and applicant-tracking application built with Flask, server-rendered HTML, vanilla JavaScript, and SQLite. It includes public job discovery, candidate and recruiter workspaces, and an administrator dashboard.

## Features

- Candidate and recruiter registration, sign-in, role checks, password changes, and account deactivation.
- Job search with category, location, employment type, minimum salary, sorting, and pagination.
- Candidate profiles, PDF resume upload/download/replacement/removal, saved jobs, applications, status history, and interview details.
- Recruiter company profiles and logo uploads, job posting management, applicant search/status updates, private notes, and interview scheduling.
- Administrator platform metrics, category report, recent activity, company verification, account access controls, job moderation, and CSV export.
- SQLite foreign keys/indexes, parameterized SQL, Werkzeug password hashes, CSRF protection, secure session cookie defaults, private resume storage, and upload signature/size checks.
- Responsive layouts, accessible labels, server/client validation, confirmations, empty states, and flash feedback.

## Requirements

- Python 3.10 or later
- pip

## Install and run (Windows PowerShell)

From the project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
$secret = python -c "import secrets; print(secrets.token_hex(32))"
$env:SECRET_KEY = $secret
python run.py
```

Open `http://127.0.0.1:5000`. The app creates `instance\hiresphere.sqlite` and seeds sample accounts/jobs on first launch. The command above generates a secret for the current PowerShell session; store it in `.env` if you want it to persist across restarts. Set `COOKIE_SECURE=1` when serving over HTTPS. Do not enable Flask debug mode in production.

Generate a secret at any time with `python -c "import secrets; print(secrets.token_hex(32))"`.

The database can also be initialized explicitly with Flask:

```powershell
flask --app run init-db
```

## Demo accounts

All demo accounts use `demo1234`.

| Role | Email |
|---|---|
| Candidate | `candidate@hiresphere.test` |
| Recruiter | `recruiter@hiresphere.test` |
| Administrator | `admin@hiresphere.test` |

Change demo passwords and replace demo content before publishing a deployed instance.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The automated tests use temporary SQLite databases and upload folders; they do not modify the live demo database.

## Project layout

```text
app/
  __init__.py          Flask factory and error handlers
  db.py                SQLite connection, initialization, and seed data
  routes.py            Public and role-protected routes
  schema.sql           Relational schema, constraints, and indexes
  templates/           Server-rendered pages and role workspaces
  static/css/           Responsive visual system
  static/js/            Navigation, confirmations, and client validation
instance/               Local database and private uploaded files
tests/                  Automated route and workflow tests
config.py               Environment-based configuration
run.py                  Local application entry point
```

## Production notes

This project is suitable as a local portfolio/demo application, not a fully hardened deployment. Before production use, add managed secrets and HTTPS, rate limiting and account recovery, a production WSGI server, database backup/retention policies, malware scanning for uploads, email delivery, accessibility/browser testing, and legal review of the policy pages. The contact form stores submissions in the local database; it does not send email. Company logo uploads are private files served only through a company-specific public image route. Candidate resumes remain access-controlled.
