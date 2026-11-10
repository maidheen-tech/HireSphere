import csv
import io
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlsplit

from flask import (
    Blueprint, abort, current_app, flash, g, make_response, redirect, render_template,
    request, send_from_directory, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .db import get_db

main = Blueprint("main", __name__)
STATUSES = ("Applied", "Under Review", "Shortlisted", "Interview Scheduled", "Selected", "Rejected")
JOB_TYPES = ("Full-time", "Part-time", "Contract", "Internship")


def valid_email(value):
    if not value or len(value) > 254 or any(char.isspace() for char in value):
        return False
    local, separator, domain = value.partition("@")
    return bool(separator and local and "." in domain and not domain.startswith(".") and not domain.endswith("."))


def safe_csv_cell(value):
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def now():
    return datetime.now(timezone.utc).isoformat()


def log_action(action, entity_type, entity_id=None):
    db = get_db()
    db.execute(
        "INSERT INTO activity_logs (actor_id, action, entity_type, entity_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (g.user["id"] if getattr(g, "user", None) else None, action, entity_type, entity_id, now()),
    )
    db.commit()


@main.before_app_request
def load_user():
    user_id = session.get("user_id")
    g.user = get_db().execute(
        "SELECT id, email, full_name, role, is_active FROM users WHERE id=?", (user_id,)
    ).fetchone() if user_id else None
    if user_id and (g.user is None or not g.user["is_active"]):
        session.clear()
        g.user = None


@main.app_context_processor
def inject_globals():
    unread = 0
    if getattr(g, "user", None):
        unread = get_db().execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (g.user["id"],)
        ).fetchone()[0]
    return {"current_user": getattr(g, "user", None), "unread_count": unread, "job_types": JOB_TYPES}


def login_required(role=None):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.user:
                flash("Please sign in to continue.", "error")
                return redirect(url_for("main.login", next=request.path))
            if role and g.user["role"] != role:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def company_for(user_id):
    return get_db().execute("SELECT * FROM companies WHERE recruiter_id=? ORDER BY id LIMIT 1", (user_id,)).fetchone()


def notify(user_id, title, body):
    get_db().execute(
        "INSERT INTO notifications (user_id, title, body, created_at) VALUES (?, ?, ?, ?)",
        (user_id, title, body, now()),
    )
    get_db().commit()


def profile_completion(profile):
    fields = ("headline", "location", "bio", "skills", "education", "experience", "resume_filename")
    return round(sum(bool(profile and profile[field]) for field in fields) / len(fields) * 100)


@main.route("/")
def home():
    db = get_db()
    featured = db.execute(
        """SELECT j.*, c.name company_name, c.logo_filename company_logo FROM jobs j JOIN companies c ON c.id=j.company_id
           WHERE j.is_active=1 ORDER BY j.created_at DESC LIMIT 4"""
    ).fetchall()
    stats = {
        "jobs": db.execute("SELECT COUNT(*) FROM jobs WHERE is_active=1").fetchone()[0],
        "companies": db.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
        "candidates": db.execute("SELECT COUNT(*) FROM users WHERE role='candidate' AND is_active=1").fetchone()[0],
    }
    categories = db.execute(
        "SELECT category, COUNT(*) total FROM jobs WHERE is_active=1 GROUP BY category ORDER BY total DESC LIMIT 6"
    ).fetchall()
    return render_template("home.html", featured=featured, stats=stats, categories=categories)


@main.route("/about")
def about():
    return render_template("about.html")


@main.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name, email, message = (request.form.get(key, "").strip() for key in ("name", "email", "message"))
        if len(name) < 2 or len(name) > 120 or not valid_email(email) or len(message) < 10 or len(message) > 4000:
            flash("Please complete each field with a valid email and a message of at least 10 characters.", "error")
        else:
            get_db().execute(
                "INSERT INTO contact_messages (name, email, message, created_at) VALUES (?, ?, ?, ?)",
                (name, email, message, now()),
            )
            get_db().commit()
            flash("Thanks for reaching out. Our team will be in touch soon.", "success")
            return redirect(url_for("main.contact"))
    return render_template("contact.html")


@main.route("/privacy")
def privacy():
    return render_template("legal.html", title="Privacy policy", privacy=True)


@main.route("/terms")
def terms():
    return render_template("legal.html", title="Terms & conditions", privacy=False)


@main.route("/jobs")
def jobs():
    db = get_db()
    query = request.args.get("q", "").strip()
    location = request.args.get("location", "").strip()
    category = request.args.get("category", "").strip()
    job_type = request.args.get("type", "").strip()
    salary = request.args.get("salary", type=int)
    sort = request.args.get("sort", "newest")
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = 8
    clauses, values = ["j.is_active=1"], []
    if query:
        clauses.append("(j.title LIKE ? OR j.description LIKE ? OR c.name LIKE ?)")
        values.extend([f"%{query}%"] * 3)
    if location:
        clauses.append("j.location LIKE ?")
        values.append(f"%{location}%")
    if category:
        clauses.append("j.category=?")
        values.append(category)
    if job_type in JOB_TYPES:
        clauses.append("j.job_type=?")
        values.append(job_type)
    if salary:
        clauses.append("COALESCE(j.salary_min, 0) >= ?")
        values.append(salary)
    where = " AND ".join(clauses)
    sort_order = {
        "newest": "j.created_at DESC",
        "oldest": "j.created_at ASC",
        "salary_high": "j.salary_max DESC, j.created_at DESC",
        "salary_low": "j.salary_min ASC, j.created_at DESC",
    }.get(sort, "j.created_at DESC")
    total = db.execute(f"SELECT COUNT(*) FROM jobs j JOIN companies c ON c.id=j.company_id WHERE {where}", values).fetchone()[0]
    rows = db.execute(
        f"""SELECT j.*, c.name company_name, c.logo_filename company_logo FROM jobs j JOIN companies c ON c.id=j.company_id
            WHERE {where} ORDER BY {sort_order} LIMIT ? OFFSET ?""",
        (*values, per_page, (page - 1) * per_page),
    ).fetchall()
    categories = db.execute("SELECT DISTINCT category FROM jobs WHERE is_active=1 ORDER BY category").fetchall()
    return render_template(
        "jobs.html", jobs=rows, total=total, page=page, pages=max((total + per_page - 1) // per_page, 1),
        categories=categories, filters={"q": query, "location": location, "category": category,
                                        "type": job_type, "salary": salary or "", "sort": sort},
    )


@main.route("/jobs/<int:job_id>")
def job_detail(job_id):
    job = get_db().execute(
        """SELECT j.*, c.name company_name, c.location company_location, c.description company_description, c.logo_filename company_logo
           FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.id=? AND j.is_active=1""", (job_id,)
    ).fetchone()
    if not job:
        abort(404)
    applied = saved = False
    if g.user and g.user["role"] == "candidate":
        db = get_db()
        applied = bool(db.execute("SELECT 1 FROM applications WHERE candidate_id=? AND job_id=?", (g.user["id"], job_id)).fetchone())
        saved = bool(db.execute("SELECT 1 FROM saved_jobs WHERE user_id=? AND job_id=?", (g.user["id"], job_id)).fetchone())
    return render_template("job_detail.html", job=job, applied=applied, saved=saved)


@main.route("/register/<role>", methods=["GET", "POST"])
def register(role):
    if role not in ("candidate", "recruiter"):
        abort(404)
    if g.user:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        company_name = request.form.get("company_name", "").strip()
        errors = []
        if len(name) < 2 or len(name) > 120:
            errors.append("Enter your full name.")
        if not valid_email(email):
            errors.append("Enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if role == "recruiter" and (len(company_name) < 2 or len(company_name) > 160):
            errors.append("Enter your company name.")
        db = get_db()
        if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            errors.append("An account already exists for that email.")
        if errors:
            for error in errors:
                flash(error, "error")
        else:
            cur = db.execute(
                "INSERT INTO users (email, full_name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (email, name, role, generate_password_hash(password), now()),
            )
            user_id = cur.lastrowid
            if role == "candidate":
                db.execute("INSERT INTO candidate_profiles (user_id) VALUES (?)", (user_id,))
            else:
                db.execute("INSERT INTO recruiter_profiles (user_id) VALUES (?)", (user_id,))
                db.execute(
                    "INSERT INTO companies (recruiter_id, name) VALUES (?, ?)", (user_id, company_name)
                )
            db.commit()
            session.clear()
            session["user_id"] = user_id
            session.permanent = True
            g.user = db.execute("SELECT id, email, full_name, role, is_active FROM users WHERE id=?", (user_id,)).fetchone()
            log_action(f"Registered {role} account", "user", user_id)
            flash("Your account is ready. Welcome to HireSphere!", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("register.html", role=role)


@main.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user or not user["is_active"] or not check_password_hash(user["password_hash"], password):
            flash("Email or password is incorrect.", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            g.user = user
            log_action("Signed in", "user", user["id"])
            destination = request.args.get("next", "")
            safe_destination = (
                destination.startswith("/") and not destination.startswith("//")
                and "\\" not in destination and not urlsplit(destination).netloc
            )
            return redirect(destination if safe_destination else url_for("main.dashboard"))
    return render_template("login.html")


@main.route("/logout", methods=["POST"])
@login_required()
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("main.home"))


@main.route("/dashboard")
@login_required()
def dashboard():
    return redirect(url_for({"candidate": "main.candidate_dashboard", "recruiter": "main.recruiter_dashboard", "admin": "main.admin_dashboard"}[g.user["role"]]))


@main.route("/candidate")
@login_required("candidate")
def candidate_dashboard():
    db = get_db()
    profile = db.execute("SELECT * FROM candidate_profiles WHERE user_id=?", (g.user["id"],)).fetchone()
    applications = db.execute(
        """SELECT a.*, j.title, j.location, c.name company_name FROM applications a JOIN jobs j ON j.id=a.job_id
           JOIN companies c ON c.id=j.company_id WHERE a.candidate_id=? ORDER BY a.created_at DESC LIMIT 5""",
        (g.user["id"],),
    ).fetchall()
    stats = {
        "applications": db.execute("SELECT COUNT(*) FROM applications WHERE candidate_id=?", (g.user["id"],)).fetchone()[0],
        "interviews": db.execute("SELECT COUNT(*) FROM interviews i JOIN applications a ON a.id=i.application_id WHERE a.candidate_id=?", (g.user["id"],)).fetchone()[0],
        "saved": db.execute("SELECT COUNT(*) FROM saved_jobs WHERE user_id=?", (g.user["id"],)).fetchone()[0],
    }
    return render_template("candidate_dashboard.html", profile=profile, completion=profile_completion(profile), applications=applications, stats=stats)


@main.route("/candidate/profile", methods=["GET", "POST"])
@login_required("candidate")
def candidate_profile():
    db = get_db()
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        values = {key: request.form.get(key, "").strip() for key in ("headline", "location", "bio", "phone", "skills", "education", "experience")}
        limits = {"headline": 160, "location": 120, "bio": 2000, "phone": 40,
                  "skills": 1000, "education": 3000, "experience": 4000}
        existing_email = db.execute(
            "SELECT id FROM users WHERE email=? AND id!=?", (email, g.user["id"])
        ).fetchone()
        if len(full_name) < 2 or len(full_name) > 120 or not valid_email(email) or existing_email:
            flash("Enter a valid name and email address that is not already in use.", "error")
            return redirect(url_for("main.candidate_profile"))
        if any(len(values[key]) > limit for key, limit in limits.items()):
            flash("One or more profile fields exceed the allowed length. Please shorten your entry and try again.", "error")
            return redirect(url_for("main.candidate_profile"))
        file = request.files.get("resume")
        filename = None
        if file and file.filename:
            original = secure_filename(file.filename)
            if not original.lower().endswith(".pdf"):
                flash("Resume must be a PDF file.", "error")
                return redirect(url_for("main.candidate_profile"))
            content = file.stream.read(current_app.config["MAX_CONTENT_LENGTH"] + 1)
            if len(content) > current_app.config["MAX_CONTENT_LENGTH"]:
                flash("Resume must be 5 MB or smaller.", "error")
                return redirect(url_for("main.candidate_profile"))
            if not content.startswith(b"%PDF-"):
                flash("The uploaded file is not a valid PDF.", "error")
                return redirect(url_for("main.candidate_profile"))
            filename = f"{uuid.uuid4().hex}.pdf"
            with open(os.path.join(current_app.config["UPLOAD_FOLDER"], filename), "wb") as saved_file:
                saved_file.write(content)
            existing = db.execute(
                "SELECT resume_filename FROM candidate_profiles WHERE user_id=?", (g.user["id"],)
            ).fetchone()
            if existing and existing["resume_filename"]:
                old_path = os.path.join(current_app.config["UPLOAD_FOLDER"], existing["resume_filename"])
                if os.path.isfile(old_path):
                    os.remove(old_path)
                db.execute("DELETE FROM resumes WHERE user_id=?", (g.user["id"],))
        assignments = ", ".join(f"{key}=?" for key in values)
        params = list(values.values())
        if filename:
            assignments += ", resume_filename=?"
            params.append(filename)
            db.execute("INSERT INTO resumes (user_id, filename, original_name, uploaded_at) VALUES (?, ?, ?, ?)",
                       (g.user["id"], filename, original, now()))
        params.append(g.user["id"])
        db.execute(f"UPDATE candidate_profiles SET {assignments} WHERE user_id=?", params)
        db.execute("UPDATE users SET full_name=?, email=? WHERE id=?", (full_name, email, g.user["id"]))
        db.commit()
        log_action("Updated candidate profile", "profile", g.user["id"])
        flash("Your profile has been updated.", "success")
        return redirect(url_for("main.candidate_profile"))
    profile = db.execute("SELECT * FROM candidate_profiles WHERE user_id=?", (g.user["id"],)).fetchone()
    return render_template("profile.html", profile=profile, completion=profile_completion(profile))


@main.route("/candidate/resume")
@login_required()
def download_own_resume():
    if g.user["role"] == "candidate":
        profile = get_db().execute("SELECT resume_filename FROM candidate_profiles WHERE user_id=?", (g.user["id"],)).fetchone()
        filename = profile["resume_filename"] if profile else None
    else:
        abort(403)
    if not filename or not os.path.isfile(os.path.join(current_app.config["UPLOAD_FOLDER"], filename)):
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename, as_attachment=True, download_name="resume.pdf")


@main.route("/candidate/resume/delete", methods=["POST"])
@login_required("candidate")
def delete_resume():
    db = get_db()
    row = db.execute("SELECT resume_filename FROM candidate_profiles WHERE user_id=?", (g.user["id"],)).fetchone()
    if row and row["resume_filename"]:
        path = os.path.join(current_app.config["UPLOAD_FOLDER"], row["resume_filename"])
        if os.path.isfile(path):
            os.remove(path)
        db.execute("UPDATE candidate_profiles SET resume_filename=NULL WHERE user_id=?", (g.user["id"],))
        db.execute("DELETE FROM resumes WHERE user_id=?", (g.user["id"],))
        db.commit()
        flash("Resume removed.", "success")
    return redirect(url_for("main.candidate_profile"))


@main.route("/jobs/<int:job_id>/apply", methods=["POST"])
@login_required("candidate")
def apply_job(job_id):
    db = get_db()
    job = db.execute("SELECT id, title FROM jobs WHERE id=? AND is_active=1", (job_id,)).fetchone()
    if not job:
        abort(404)
    cover_letter = request.form.get("cover_letter", "").strip()
    if len(cover_letter) > 4000:
        flash("Your note must be 4,000 characters or fewer.", "error")
        return redirect(url_for("main.job_detail", job_id=job_id))
    try:
        cur = db.execute(
            "INSERT INTO applications (job_id, candidate_id, cover_letter, status, created_at) VALUES (?, ?, ?, 'Applied', ?)",
            (job_id, g.user["id"], cover_letter, now()),
        )
    except sqlite3.IntegrityError as error:
        if "UNIQUE constraint failed" in str(error):
            flash("You have already applied for this role.", "error")
            return redirect(url_for("main.job_detail", job_id=job_id))
        raise
    db.execute("INSERT INTO application_history (application_id, actor_id, status, note, created_at) VALUES (?, ?, ?, ?, ?)",
               (cur.lastrowid, g.user["id"], "Applied", "Application submitted", now()))
    recruiter = db.execute("SELECT c.recruiter_id FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.id=?", (job_id,)).fetchone()
    db.commit()
    notify(recruiter["recruiter_id"], "New application", f"{g.user['full_name']} applied for {job['title']}.")
    flash("Application submitted. Good luck!", "success")
    return redirect(url_for("main.candidate_applications"))


@main.route("/jobs/<int:job_id>/save", methods=["POST"])
@login_required("candidate")
def toggle_saved_job(job_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM jobs WHERE id=? AND is_active=1", (job_id,)).fetchone():
        abort(404)
    existing = db.execute("SELECT 1 FROM saved_jobs WHERE user_id=? AND job_id=?", (g.user["id"], job_id)).fetchone()
    if existing:
        db.execute("DELETE FROM saved_jobs WHERE user_id=? AND job_id=?", (g.user["id"], job_id))
        flash("Job removed from saved jobs.", "success")
    else:
        db.execute("INSERT INTO saved_jobs (user_id, job_id, created_at) VALUES (?, ?, ?)", (g.user["id"], job_id, now()))
        flash("Job saved for later.", "success")
    db.commit()
    return redirect(request.referrer or url_for("main.job_detail", job_id=job_id))


@main.route("/candidate/applications")
@login_required("candidate")
def candidate_applications():
    rows = get_db().execute(
        """SELECT a.*, j.title, j.location, c.name company_name, i.scheduled_at, i.location interview_location, i.details interview_details
           FROM applications a JOIN jobs j ON j.id=a.job_id JOIN companies c ON c.id=j.company_id
           LEFT JOIN interviews i ON i.application_id=a.id WHERE a.candidate_id=? ORDER BY a.created_at DESC""",
        (g.user["id"],),
    ).fetchall()
    return render_template("candidate_applications.html", applications=rows)


@main.route("/candidate/saved")
@login_required("candidate")
def candidate_saved():
    rows = get_db().execute(
        """SELECT j.*, c.name company_name, c.logo_filename company_logo FROM saved_jobs s JOIN jobs j ON j.id=s.job_id
           JOIN companies c ON c.id=j.company_id WHERE s.user_id=? ORDER BY s.created_at DESC""",
        (g.user["id"],),
    ).fetchall()
    return render_template("saved_jobs.html", jobs=rows)


@main.route("/recruiter")
@login_required("recruiter")
def recruiter_dashboard():
    db = get_db()
    company = company_for(g.user["id"])
    if not company:
        abort(404)
    stats = {
        "jobs": db.execute("SELECT COUNT(*) FROM jobs WHERE company_id=?", (company["id"],)).fetchone()[0],
        "active": db.execute("SELECT COUNT(*) FROM jobs WHERE company_id=? AND is_active=1", (company["id"],)).fetchone()[0],
        "applications": db.execute("SELECT COUNT(*) FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.company_id=?", (company["id"],)).fetchone()[0],
        "interviews": db.execute("SELECT COUNT(*) FROM interviews i JOIN applications a ON a.id=i.application_id JOIN jobs j ON j.id=a.job_id WHERE j.company_id=?", (company["id"],)).fetchone()[0],
    }
    postings = db.execute(
        """SELECT j.*, (SELECT COUNT(*) FROM applications a WHERE a.job_id=j.id) applicant_count
           FROM jobs j WHERE j.company_id=? ORDER BY j.created_at DESC LIMIT 6""", (company["id"],)
    ).fetchall()
    return render_template("recruiter_dashboard.html", company=company, stats=stats, jobs=postings)


@main.route("/recruiter/company", methods=["GET", "POST"])
@login_required("recruiter")
def recruiter_company():
    db = get_db()
    company = company_for(g.user["id"])
    if request.method == "POST":
        values = [request.form.get(key, "").strip() for key in ("name", "website", "location", "description")]
        try:
            website = urlsplit(values[1]) if values[1] else None
            website_valid = not website or (website.scheme in ("http", "https") and bool(website.netloc))
        except ValueError:
            website_valid = False
        if len(values[0]) < 2 or len(values[3]) > 2000 or not website_valid:
            flash("Enter a company name, a valid HTTP(S) website, and a description under 2,000 characters.", "error")
        else:
            logo = request.files.get("logo")
            logo_name = None
            if logo and logo.filename:
                extension = os.path.splitext(secure_filename(logo.filename))[1].lower()
                signatures = {
                    ".png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
                    ".jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
                    ".jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
                    ".webp": lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP",
                }
                data = logo.stream.read(2 * 1024 * 1024 + 1)
                if extension not in signatures or len(data) > 2 * 1024 * 1024 or not signatures[extension](data):
                    flash("Company logo must be a PNG, JPG, or WebP image no larger than 2 MB.", "error")
                    return redirect(url_for("main.recruiter_company"))
                logo_name = f"{uuid.uuid4().hex}{extension}"
                with open(os.path.join(current_app.config["UPLOAD_FOLDER"], logo_name), "wb") as saved_file:
                    saved_file.write(data)
                if company["logo_filename"]:
                    old_logo = os.path.join(current_app.config["UPLOAD_FOLDER"], company["logo_filename"])
                    if os.path.isfile(old_logo):
                        os.remove(old_logo)
            db.execute(
                "UPDATE companies SET name=?, website=?, location=?, description=?, logo_filename=COALESCE(?, logo_filename) WHERE id=?",
                (*values, logo_name, company["id"]),
            )
            db.commit()
            flash("Company profile saved.", "success")
            return redirect(url_for("main.recruiter_company"))
    return render_template("company.html", company=company)


@main.route("/company-logo/<int:company_id>")
def company_logo(company_id):
    company = get_db().execute("SELECT logo_filename FROM companies WHERE id=?", (company_id,)).fetchone()
    if not company or not company["logo_filename"]:
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], company["logo_filename"], as_attachment=False)


@main.route("/recruiter/jobs/new", methods=["GET", "POST"])
@login_required("recruiter")
def recruiter_job_new():
    return job_form()


@main.route("/recruiter/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@login_required("recruiter")
def recruiter_job_edit(job_id):
    return job_form(job_id)


def job_form(job_id=None):
    db = get_db()
    company = company_for(g.user["id"])
    job = None
    if job_id:
        job = db.execute("SELECT * FROM jobs WHERE id=? AND company_id=?", (job_id, company["id"])).fetchone()
        if not job:
            abort(404)
    if request.method == "POST":
        title, category, location, job_type = (request.form.get(key, "").strip() for key in ("title", "category", "location", "job_type"))
        description = request.form.get("description", "").strip()
        try:
            low, high = (int(request.form.get(key) or 0) for key in ("salary_min", "salary_max"))
        except ValueError:
            low = high = -1
        if (not all((title, category, location, description)) or job_type not in JOB_TYPES
                or low < 0 or high < low or len(title) > 160 or len(category) > 100
                or len(location) > 160 or len(description) > 8000):
            flash("Complete all required fields and enter a valid salary range.", "error")
        else:
            values = (title, category, location, job_type, low or None, high or None, description,
                      request.form.get("experience_level", "").strip(), request.form.get("deadline", "").strip() or None)
            if job:
                db.execute(
                    """UPDATE jobs SET title=?, category=?, location=?, job_type=?, salary_min=?, salary_max=?,
                       description=?, experience_level=?, deadline=? WHERE id=? AND company_id=?""",
                    (*values, job_id, company["id"]),
                )
                action = "Updated job posting"
            else:
                db.execute(
                    """INSERT INTO jobs (title, category, location, job_type, salary_min, salary_max, description,
                       experience_level, deadline, company_id, created_at, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (*values, company["id"], now()),
                )
                action = "Created job posting"
            db.commit()
            log_action(action, "job", job_id)
            flash("Job posting saved.", "success")
            return redirect(url_for("main.recruiter_dashboard"))
    return render_template("job_form.html", job=job, job_types=JOB_TYPES, editing=bool(job))


@main.route("/recruiter/jobs/<int:job_id>/delete", methods=["POST"])
@login_required("recruiter")
def recruiter_job_delete(job_id):
    company = company_for(g.user["id"])
    db = get_db()
    db.execute("DELETE FROM jobs WHERE id=? AND company_id=?", (job_id, company["id"]))
    db.commit()
    log_action("Deleted job posting", "job", job_id)
    flash("Job posting deleted.", "success")
    return redirect(url_for("main.recruiter_dashboard"))


@main.route("/recruiter/jobs/<int:job_id>/toggle", methods=["POST"])
@login_required("recruiter")
def recruiter_job_toggle(job_id):
    company = company_for(g.user["id"])
    db = get_db()
    db.execute("UPDATE jobs SET is_active=1-is_active WHERE id=? AND company_id=?", (job_id, company["id"]))
    db.commit()
    flash("Job posting status updated.", "success")
    return redirect(url_for("main.recruiter_dashboard"))


@main.route("/recruiter/jobs/<int:job_id>/applicants")
@login_required("recruiter")
def recruiter_applicants(job_id):
    company = company_for(g.user["id"])
    db = get_db()
    job = db.execute("SELECT * FROM jobs WHERE id=? AND company_id=?", (job_id, company["id"])).fetchone()
    if not job:
        abort(404)
    query = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    clauses, args = ["a.job_id=?"], [job_id]
    if query:
        clauses.append("(u.full_name LIKE ? OR u.email LIKE ? OR p.skills LIKE ?)")
        args.extend([f"%{query}%"] * 3)
    if status in STATUSES:
        clauses.append("a.status=?")
        args.append(status)
    applicants = db.execute(
        f"""SELECT a.*, u.full_name, u.email, p.headline, p.location, p.skills, p.resume_filename,
            i.scheduled_at FROM applications a JOIN users u ON u.id=a.candidate_id
            LEFT JOIN candidate_profiles p ON p.user_id=u.id LEFT JOIN interviews i ON i.application_id=a.id
            WHERE {' AND '.join(clauses)} ORDER BY a.created_at DESC""", args
    ).fetchall()
    return render_template("applicants.html", job=job, applicants=applicants, statuses=STATUSES, filters={"q": query, "status": status})


@main.route("/recruiter/candidates/<int:candidate_id>")
@login_required("recruiter")
def recruiter_candidate_profile(candidate_id):
    company = company_for(g.user["id"])
    candidate = get_db().execute(
        """SELECT u.full_name, u.email, u.created_at, p.* FROM users u
           JOIN candidate_profiles p ON p.user_id=u.id
           WHERE u.id=? AND u.role='candidate' AND EXISTS (
             SELECT 1 FROM applications a JOIN jobs j ON j.id=a.job_id
             WHERE a.candidate_id=u.id AND j.company_id=?
           )""",
        (candidate_id, company["id"]),
    ).fetchone()
    if not candidate:
        abort(404)
    return render_template("recruiter_candidate.html", candidate=candidate)


def recruiter_application(application_id):
    company = company_for(g.user["id"])
    row = get_db().execute(
        """SELECT a.*, j.title, j.company_id, u.id candidate_id, u.full_name, u.email, p.resume_filename
           FROM applications a JOIN jobs j ON j.id=a.job_id JOIN users u ON u.id=a.candidate_id
           LEFT JOIN candidate_profiles p ON p.user_id=u.id WHERE a.id=?""", (application_id,)
    ).fetchone()
    if not row or not company or row["company_id"] != company["id"]:
        abort(404)
    return row


@main.route("/recruiter/applications/<int:application_id>/status", methods=["POST"])
@login_required("recruiter")
def update_application_status(application_id):
    row = recruiter_application(application_id)
    status = request.form.get("status", "")
    if status not in STATUSES:
        abort(400)
    note = request.form.get("note", "").strip()[:500]
    db = get_db()
    db.execute("UPDATE applications SET status=? WHERE id=?", (status, application_id))
    db.execute(
        "INSERT INTO application_history (application_id, actor_id, status, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (application_id, g.user["id"], status, note, now()),
    )
    db.commit()
    notify(row["candidate_id"], f"Application {status.lower()}", f"Your application for {row['title']} is now {status.lower()}.")
    flash(f"Application marked {status.lower()}.", "success")
    return redirect(request.referrer or url_for("main.recruiter_applicants", job_id=row["job_id"]))


@main.route("/recruiter/applications/<int:application_id>/notes", methods=["POST"])
@login_required("recruiter")
def add_recruiter_note(application_id):
    recruiter_application(application_id)
    note = request.form.get("note", "").strip()
    if not note or len(note) > 2000:
        flash("Add a note under 2,000 characters.", "error")
    else:
        get_db().execute(
            "INSERT INTO recruiter_notes (application_id, recruiter_id, note, created_at) VALUES (?, ?, ?, ?)",
            (application_id, g.user["id"], note, now()),
        )
        get_db().commit()
        flash("Private note added.", "success")
    return redirect(request.referrer or url_for("main.recruiter_dashboard"))


@main.route("/recruiter/applications/<int:application_id>/interview", methods=["POST"])
@login_required("recruiter")
def schedule_interview(application_id):
    row = recruiter_application(application_id)
    scheduled_at = request.form.get("scheduled_at", "").strip()
    details = request.form.get("details", "").strip()
    location = request.form.get("location", "").strip()
    if len(details) > 1000 or len(location) > 250:
        flash("Interview details must be under 1,000 characters and location under 250.", "error")
        return redirect(request.referrer or url_for("main.recruiter_dashboard"))
    try:
        datetime.fromisoformat(scheduled_at)
    except ValueError:
        flash("Choose a valid interview date and time.", "error")
        return redirect(request.referrer or url_for("main.recruiter_dashboard"))
    db = get_db()
    db.execute(
        """INSERT INTO interviews (application_id, scheduled_at, location, details, created_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(application_id) DO UPDATE SET scheduled_at=excluded.scheduled_at, location=excluded.location, details=excluded.details""",
        (application_id, scheduled_at, location, details, now()),
    )
    db.execute("UPDATE applications SET status='Interview Scheduled' WHERE id=?", (application_id,))
    db.execute("INSERT INTO application_history (application_id, actor_id, status, note, created_at) VALUES (?, ?, ?, ?, ?)",
               (application_id, g.user["id"], "Interview Scheduled", "Interview details updated", now()))
    db.commit()
    notify(row["candidate_id"], "Interview scheduled", f"An interview for {row['title']} has been scheduled.")
    flash("Interview details saved and candidate notified.", "success")
    return redirect(request.referrer or url_for("main.recruiter_dashboard"))


@main.route("/recruiter/resume/<int:candidate_id>")
@login_required("recruiter")
def recruiter_download_resume(candidate_id):
    company = company_for(g.user["id"])
    permitted = get_db().execute(
        """SELECT p.resume_filename FROM candidate_profiles p
           WHERE p.user_id=? AND EXISTS (SELECT 1 FROM applications a JOIN jobs j ON j.id=a.job_id
           WHERE a.candidate_id=p.user_id AND j.company_id=?)""", (candidate_id, company["id"])
    ).fetchone()
    if not permitted or not permitted["resume_filename"]:
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], permitted["resume_filename"], as_attachment=True, download_name="candidate-resume.pdf")


@main.route("/admin")
@login_required("admin")
def admin_dashboard():
    db = get_db()
    stats = {
        "users": db.execute("SELECT COUNT(*) FROM users WHERE role!='admin'").fetchone()[0],
        "candidates": db.execute("SELECT COUNT(*) FROM users WHERE role='candidate'").fetchone()[0],
        "recruiters": db.execute("SELECT COUNT(*) FROM users WHERE role='recruiter'").fetchone()[0],
        "jobs": db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "active": db.execute("SELECT COUNT(*) FROM jobs WHERE is_active=1").fetchone()[0],
        "applications": db.execute("SELECT COUNT(*) FROM applications").fetchone()[0],
    }
    users = db.execute(
        """SELECT u.*, c.id company_id, c.name company_name, c.verified FROM users u LEFT JOIN companies c ON c.recruiter_id=u.id
           ORDER BY u.created_at DESC LIMIT 12"""
    ).fetchall()
    jobs = db.execute(
        """SELECT j.*, c.name company_name FROM jobs j JOIN companies c ON c.id=j.company_id
           ORDER BY j.created_at DESC LIMIT 8"""
    ).fetchall()
    activity = db.execute(
        """SELECT l.*, u.full_name FROM activity_logs l LEFT JOIN users u ON u.id=l.actor_id
           ORDER BY l.created_at DESC LIMIT 8"""
    ).fetchall()
    chart = db.execute(
        """SELECT category, COUNT(*) total FROM jobs GROUP BY category ORDER BY total DESC LIMIT 6"""
    ).fetchall()
    return render_template("admin_dashboard.html", stats=stats, users=users, jobs=jobs, activity=activity, chart=chart)


@main.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@login_required("admin")
def admin_toggle_user(user_id):
    if user_id == g.user["id"]:
        flash("You cannot deactivate your own administrator account.", "error")
        return redirect(url_for("main.admin_dashboard"))
    db = get_db()
    db.execute("UPDATE users SET is_active=1-is_active WHERE id=? AND role!='admin'", (user_id,))
    user = db.execute("SELECT role, is_active FROM users WHERE id=?", (user_id,)).fetchone()
    if user and user["role"] == "recruiter" and not user["is_active"]:
        db.execute(
            "UPDATE jobs SET is_active=0 WHERE company_id IN (SELECT id FROM companies WHERE recruiter_id=?)",
            (user_id,),
        )
    db.commit()
    log_action("Changed user account access", "user", user_id)
    flash("User account status updated.", "success")
    return redirect(url_for("main.admin_dashboard"))


@main.route("/admin/companies/<int:company_id>/verify", methods=["POST"])
@login_required("admin")
def admin_verify_company(company_id):
    db = get_db()
    db.execute("UPDATE companies SET verified=1-verified WHERE id=?", (company_id,))
    db.commit()
    flash("Company verification status updated.", "success")
    return redirect(url_for("main.admin_dashboard"))


@main.route("/admin/jobs/<int:job_id>/delete", methods=["POST"])
@login_required("admin")
def admin_delete_job(job_id):
    db = get_db()
    db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    db.commit()
    log_action("Removed job posting", "job", job_id)
    flash("Job posting removed.", "success")
    return redirect(url_for("main.admin_dashboard"))


@main.route("/admin/export")
@login_required("admin")
def admin_export():
    rows = get_db().execute(
        """SELECT a.id, u.full_name candidate, u.email, j.title job, c.name company, a.status, a.created_at
           FROM applications a JOIN users u ON u.id=a.candidate_id JOIN jobs j ON j.id=a.job_id
           JOIN companies c ON c.id=j.company_id ORDER BY a.created_at DESC"""
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(("Application ID", "Candidate", "Email", "Job", "Company", "Status", "Applied at"))
    writer.writerows([[safe_csv_cell(value) for value in row] for row in rows])
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=hiresphere-applications.csv"
    return response


@main.route("/notifications")
@login_required()
def notifications():
    rows = get_db().execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (g.user["id"],)
    ).fetchall()
    return render_template("notifications.html", notifications=rows)


@main.route("/notifications/<int:notification_id>/read", methods=["POST"])
@login_required()
def mark_notification_read(notification_id):
    get_db().execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (notification_id, g.user["id"]))
    get_db().commit()
    return redirect(url_for("main.notifications"))


@main.route("/applications/<int:application_id>/history")
@login_required()
def application_history(application_id):
    db = get_db()
    app_row = db.execute(
        """SELECT a.*, j.title, j.company_id, c.name company_name FROM applications a
           JOIN jobs j ON j.id=a.job_id JOIN companies c ON c.id=j.company_id WHERE a.id=?""",
        (application_id,),
    ).fetchone()
    if not app_row:
        abort(404)
    if g.user["role"] == "candidate" and app_row["candidate_id"] != g.user["id"]:
        abort(404)
    if g.user["role"] == "recruiter":
        company = company_for(g.user["id"])
        if not company or company["id"] != app_row["company_id"]:
            abort(404)
    history = db.execute(
        """SELECT h.*, u.full_name actor_name FROM application_history h
           JOIN users u ON u.id=h.actor_id WHERE h.application_id=? ORDER BY h.created_at DESC""",
        (application_id,),
    ).fetchall()
    notes = []
    if g.user["role"] == "recruiter":
        notes = db.execute(
            "SELECT n.*, u.full_name FROM recruiter_notes n JOIN users u ON u.id=n.recruiter_id WHERE n.application_id=? ORDER BY n.created_at DESC",
            (application_id,),
        ).fetchall()
    return render_template("application_history.html", application=app_row, history=history, notes=notes)


@main.route("/settings", methods=["GET", "POST"])
@login_required()
def settings():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        user = get_db().execute("SELECT password_hash FROM users WHERE id=?", (g.user["id"],)).fetchone()
        if not check_password_hash(user["password_hash"], current_password):
            flash("Current password is incorrect.", "error")
        elif len(new_password) < 8:
            flash("New password must be at least 8 characters.", "error")
        else:
            get_db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password), g.user["id"]))
            get_db().commit()
            log_action("Changed account password", "user", g.user["id"])
            flash("Password changed successfully.", "success")
            return redirect(url_for("main.settings"))
    return render_template("settings.html")


@main.route("/settings/deactivate", methods=["POST"])
@login_required()
def deactivate_account():
    db = get_db()
    db.execute("UPDATE users SET is_active=0 WHERE id=?", (g.user["id"],))
    db.commit()
    log_action("Deactivated account", "user", g.user["id"])
    session.clear()
    flash("Your account has been deactivated.", "success")
    return redirect(url_for("main.home"))


@main.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403, message="You don't have permission to access this page."), 403
