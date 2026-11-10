import io
import os
import tempfile
import unittest

from app import create_app
from app.db import get_db, init_db, seed_db


class HireSphereWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = self.temp.name
        self.app = create_app({
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test-key-not-for-production",
            "DATABASE": os.path.join(root, "test.sqlite"),
            "UPLOAD_FOLDER": os.path.join(root, "uploads"),
        })
        with self.app.app_context():
            init_db()
            seed_db()
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def sign_in(self, email, password="demo1234"):
        return self.client.post("/login", data={"email": email, "password": password}, follow_redirects=True)

    def test_public_search_and_authentication_roles(self):
        response = self.client.get("/jobs?q=Python&location=Remote&type=Full-time")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Backend Engineer, Python", response.data)
        self.assertEqual(self.client.get("/admin").status_code, 302)
        self.sign_in("candidate@hiresphere.test")
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(self.client.get("/candidate").status_code, 200)
        self.assertEqual(self.client.get("/recruiter").status_code, 403)

    def test_public_pages_registration_and_contact(self):
        for path in ("/", "/about", "/jobs", "/contact", "/privacy", "/terms", "/login", "/register/candidate", "/register/recruiter"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        response = self.client.post("/contact", data={
            "name": "Taylor Sample", "email": "taylor@example.com", "message": "I have a question about the service.",
        }, follow_redirects=True)
        self.assertIn(b"Thanks for reaching out", response.data)
        self.client.post("/logout")
        response = self.client.post("/register/candidate", data={
            "full_name": "New Candidate", "email": "new.person@example.com", "password": "securepass123",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Candidate workspace", response.data)
        with self.app.app_context():
            self.assertIsNotNone(get_db().execute("SELECT user_id FROM candidate_profiles WHERE user_id=(SELECT id FROM users WHERE email='new.person@example.com')").fetchone())
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM contact_messages").fetchone()[0], 1)

    def test_candidate_duplicate_application_saves_and_tracks(self):
        self.sign_in("candidate@hiresphere.test")
        first = self.client.post("/jobs/2/apply", data={"cover_letter": "I enjoy building reliable APIs."}, follow_redirects=True)
        self.assertIn(b"Application submitted", first.data)
        second = self.client.post("/jobs/2/apply", data={"cover_letter": "Another note"}, follow_redirects=True)
        self.assertIn(b"already applied", second.data)
        self.client.post("/jobs/3/save", follow_redirects=True)
        self.assertIn(b"People Operations Associate", self.client.get("/candidate/saved").data)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM applications WHERE job_id=2").fetchone()[0], 1)
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=(SELECT id FROM users WHERE role='recruiter')").fetchone()[0], 1)

    def test_role_workspace_pages_render(self):
        self.sign_in("candidate@hiresphere.test")
        for path in ("/candidate", "/candidate/profile", "/candidate/applications", "/candidate/saved", "/notifications", "/settings", "/jobs/1"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        with self.app.app_context():
            application_id = get_db().execute("SELECT id FROM applications LIMIT 1").fetchone()["id"]
        self.assertEqual(self.client.get(f"/applications/{application_id}/history").status_code, 200)

        self.client.post("/logout")
        self.sign_in("recruiter@hiresphere.test")
        self.assertEqual(self.client.get("/recruiter").status_code, 200)
        self.assertEqual(self.client.get("/recruiter/company").status_code, 200)
        self.assertEqual(self.client.get("/recruiter/jobs/1/applicants").status_code, 200)
        with self.app.app_context():
            candidate_id = get_db().execute("SELECT id FROM users WHERE role='candidate'").fetchone()["id"]
        self.assertEqual(self.client.get(f"/recruiter/candidates/{candidate_id}").status_code, 200)

        self.client.post("/logout")
        self.sign_in("admin@hiresphere.test")
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_recruiter_posting_status_and_interview(self):
        self.sign_in("recruiter@hiresphere.test")
        create = self.client.post("/recruiter/jobs/new", data={
            "title": "QA Engineer", "category": "Engineering", "location": "Remote",
            "job_type": "Full-time", "salary_min": "90000", "salary_max": "110000",
            "description": "Help us make reliable software.", "experience_level": "Mid-level",
        }, follow_redirects=True)
        self.assertEqual(create.status_code, 200)
        with self.app.app_context():
            job_id = get_db().execute("SELECT id FROM jobs WHERE title='QA Engineer'").fetchone()["id"]
        self.client.post("/logout")
        self.sign_in("candidate@hiresphere.test")
        self.client.post(f"/jobs/{job_id}/apply", data={"cover_letter": "I would love to help."})
        with self.app.app_context():
            application_id = get_db().execute("SELECT id FROM applications WHERE job_id=?", (job_id,)).fetchone()["id"]
        self.client.post("/logout")
        self.sign_in("recruiter@hiresphere.test")
        self.client.post(f"/recruiter/applications/{application_id}/status", data={"status": "Shortlisted", "note": "Strong experience"})
        self.client.post(f"/recruiter/applications/{application_id}/interview", data={
            "scheduled_at": "2027-01-15T13:30", "location": "Video call", "details": "Introductory conversation",
        })
        with self.app.app_context():
            row = get_db().execute("SELECT status FROM applications WHERE id=?", (application_id,)).fetchone()
            history = get_db().execute("SELECT COUNT(*) FROM application_history WHERE application_id=?", (application_id,)).fetchone()[0]
            self.assertEqual(row["status"], "Interview Scheduled")
            self.assertEqual(history, 3)
        self.client.post(f"/recruiter/jobs/{job_id}/toggle", follow_redirects=True)
        self.assertEqual(self.client.get(f"/jobs/{job_id}").status_code, 404)
        self.client.post("/logout")
        self.sign_in("candidate@hiresphere.test")
        notices = self.client.get("/notifications")
        self.assertIn(b"Interview scheduled", notices.data)
        with self.app.app_context():
            notification_id = get_db().execute(
                "SELECT id FROM notifications WHERE title='Interview scheduled' ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
        self.client.post(f"/notifications/{notification_id}/read")
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT is_read FROM notifications WHERE id=?", (notification_id,)).fetchone()[0], 1)

    def test_resume_upload_and_private_download(self):
        self.sign_in("candidate@hiresphere.test")
        response = self.client.post("/candidate/profile", data={
            "full_name": "Candidate Demo", "email": "candidate@hiresphere.test",
            "headline": "Product designer", "skills": "Figma, Research",
            "resume": (io.BytesIO(b"%PDF-1.4\n% test document\n%%EOF"), "resume.pdf"),
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        download = self.client.get("/candidate/resume")
        self.assertEqual(download.status_code, 200)
        self.assertTrue(download.data.startswith(b"%PDF-"))
        download.close()
        with self.app.app_context():
            candidate_id = get_db().execute("SELECT id FROM users WHERE role='candidate'").fetchone()["id"]
        self.client.post("/logout")
        self.sign_in("recruiter@hiresphere.test")
        self.assertEqual(self.client.get("/candidate/resume").status_code, 403)
        recruiter_download = self.client.get(f"/recruiter/resume/{candidate_id}")
        self.assertEqual(recruiter_download.status_code, 200)
        recruiter_download.close()

    def test_profile_update_preserves_resume_and_updates_identity(self):
        self.sign_in("candidate@hiresphere.test")
        self.client.post("/candidate/profile", data={
            "full_name": "Candidate Demo", "email": "candidate@hiresphere.test",
            "headline": "Product designer", "skills": "Figma",
            "resume": (io.BytesIO(b"%PDF-1.4\n% test document\n%%EOF"), "resume.pdf"),
        }, content_type="multipart/form-data")
        response = self.client.post("/candidate/profile", data={
            "full_name": "Jordan Candidate", "email": "jordan@example.com",
            "headline": "Designer", "skills": "Research",
        }, follow_redirects=True)
        self.assertIn(b"Your profile has been updated", response.data)
        download = self.client.get("/candidate/resume")
        self.assertEqual(download.status_code, 200)
        download.close()
        with self.app.app_context():
            user = get_db().execute("SELECT full_name, email FROM users WHERE role='candidate'").fetchone()
            self.assertEqual((user["full_name"], user["email"]), ("Jordan Candidate", "jordan@example.com"))
        self.client.post("/candidate/resume/delete")
        self.assertEqual(self.client.get("/candidate/resume").status_code, 404)

    def test_password_change_and_account_deactivation(self):
        self.sign_in("candidate@hiresphere.test")
        response = self.client.post("/settings", data={
            "current_password": "demo1234", "new_password": "new-demo-password",
        }, follow_redirects=True)
        self.assertIn(b"Password changed successfully", response.data)
        self.client.post("/logout")
        self.assertIn(b"Candidate workspace", self.sign_in("candidate@hiresphere.test", "new-demo-password").data)
        self.client.post("/settings/deactivate", follow_redirects=True)
        response = self.client.post("/login", data={
            "email": "candidate@hiresphere.test", "password": "new-demo-password",
        }, follow_redirects=True)
        self.assertIn(b"Email or password is incorrect", response.data)

    def test_company_profile_logo_upload(self):
        self.sign_in("recruiter@hiresphere.test")
        response = self.client.post("/recruiter/company", data={
            "name": "Northstar Labs", "website": "https://northstar.example",
            "location": "New York, NY", "description": "A thoughtful software company.",
            "logo": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"demo-logo-payload"), "logo.png"),
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            company_id = get_db().execute("SELECT id FROM companies LIMIT 1").fetchone()["id"]
        logo_response = self.client.get(f"/company-logo/{company_id}")
        self.assertEqual(logo_response.status_code, 200)
        logo_response.close()

    def test_admin_controls_and_export(self):
        self.sign_in("admin@hiresphere.test")
        dashboard = self.client.get("/admin")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Platform overview", dashboard.data)
        report = self.client.get("/admin/export")
        self.assertEqual(report.status_code, 200)
        self.assertIn("text/csv", report.content_type)
        with self.app.app_context():
            company_id = get_db().execute("SELECT id FROM companies LIMIT 1").fetchone()["id"]
        self.client.post(f"/admin/companies/{company_id}/verify")
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT verified FROM companies WHERE id=?", (company_id,)).fetchone()[0], 0)

    def test_csrf_rejects_unprotected_post(self):
        protected = create_app({
            "TESTING": True, "SECRET_KEY": "csrf-test-key",
            "DATABASE": os.path.join(self.temp.name, "csrf.sqlite"),
            "UPLOAD_FOLDER": os.path.join(self.temp.name, "csrf-uploads"),
        })
        with protected.app_context():
            init_db()
            seed_db()
        response = protected.test_client().post("/login", data={"email": "candidate@hiresphere.test", "password": "demo1234"})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
