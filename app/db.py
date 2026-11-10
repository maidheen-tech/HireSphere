import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import click
from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    schema = Path(current_app.root_path, "schema.sql").read_text(encoding="utf-8")
    db.executescript(schema)
    db.commit()


def seed_db():
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    from werkzeug.security import generate_password_hash

    now = datetime.now(timezone.utc).isoformat()
    accounts = [
        ("candidate@hiresphere.test", "Candidate Demo", "candidate", "demo1234"),
        ("recruiter@hiresphere.test", "Recruiter Demo", "recruiter", "demo1234"),
        ("admin@hiresphere.test", "Admin Demo", "admin", "demo1234"),
    ]
    for email, name, role, password in accounts:
        db.execute(
            "INSERT INTO users (email, full_name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (email, name, role, generate_password_hash(password), now),
        )
    recruiter_id = db.execute("SELECT id FROM users WHERE role='recruiter'").fetchone()["id"]
    candidate_id = db.execute("SELECT id FROM users WHERE role='candidate'").fetchone()["id"]
    db.execute(
        "INSERT INTO companies (recruiter_id, name, website, location, description, verified) VALUES (?, ?, ?, ?, ?, 1)",
        (recruiter_id, "Northstar Labs", "https://example.com", "New York, NY",
         "Building thoughtful software for teams that do their best work."),
    )
    company_id = db.execute("SELECT id FROM companies LIMIT 1").fetchone()["id"]
    jobs = [
        ("Senior Product Designer", "Design", "New York, NY · Hybrid", "Full-time", 125000, 155000,
         "Join a small, ambitious product team shaping a friendly and powerful platform. Partner with research and engineering from discovery to launch.", "Senior", "2026-12-31"),
        ("Backend Engineer, Python", "Engineering", "Remote · United States", "Full-time", 140000, 180000,
         "Build dependable APIs and data systems used by modern teams. We value pragmatic engineering, thoughtful reviews, and sustainable delivery.", "Mid-Senior", "2027-01-15"),
        ("People Operations Associate", "Human Resources", "Austin, TX · Hybrid", "Full-time", 72000, 88000,
         "Help create an excellent employee experience through clear processes, empathetic support, and people-first operations.", "Entry-Mid", "2027-02-01"),
        ("Growth Marketing Manager", "Marketing", "Remote · United States", "Contract", 95000, 120000,
         "Own experiments across our acquisition funnel and turn customer insight into measurable, durable growth.", "Mid-Senior", "2027-01-05"),
        ("Customer Success Lead", "Customer Success", "Chicago, IL · On-site", "Full-time", 90000, 112000,
         "Help customers realize meaningful value, build trusted relationships, and bring customer voice back to our product team.", "Senior", "2027-01-20"),
        ("Data Analyst", "Data & Analytics", "Remote · United States", "Full-time", 88000, 108000,
         "Make complex information useful through clear analysis, reliable reporting, and close collaboration with business partners.", "Mid-level", "2027-02-10"),
    ]
    for title, category, location, job_type, low, high, description, level, deadline in jobs:
        db.execute(
            """INSERT INTO jobs (company_id, title, category, location, job_type, salary_min, salary_max,
               description, experience_level, deadline, created_at, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (company_id, title, category, location, job_type, low, high, description, level, deadline, now),
        )
    first_job = db.execute("SELECT id FROM jobs ORDER BY id LIMIT 1").fetchone()["id"]
    db.execute(
        "INSERT INTO applications (job_id, candidate_id, cover_letter, status, created_at) VALUES (?, ?, ?, 'Under Review', ?)",
        (first_job, candidate_id, "I love bringing clarity to complex product challenges and would be excited to contribute.", now),
    )
    db.execute(
        "INSERT INTO candidate_profiles (user_id, headline, location, bio, skills) VALUES (?, ?, ?, ?, ?)",
        (candidate_id, "Product designer who makes complex work feel simple", "New York, NY",
         "Designer with a focus on accessible, research-led digital products.", "Product design, Figma, Research, Prototyping"),
    )
    db.execute(
        "INSERT INTO notifications (user_id, title, body, created_at) VALUES (?, ?, ?, ?)",
        (candidate_id, "Welcome to HireSphere", "Your next opportunity could be just around the corner.", now),
    )
    db.execute(
        "INSERT INTO activity_logs (actor_id, action, entity_type, entity_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (recruiter_id, "Created demo company and sample job listings", "seed", company_id, now),
    )
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)


@click.command("init-db")
def init_db_command():
    init_db()
    seed_db()
    click.echo("Database initialized with demo accounts and sample jobs.")
