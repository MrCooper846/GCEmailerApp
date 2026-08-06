"""Production security and multi-user workflow tests."""
import io
import unittest
from unittest.mock import patch

from app import app
from config import BaseConfig, ProductionConfig
from extensions import db
from models import Campaign, CampaignRecipient, User, ValidationJob
from production_services import sanitize_email_html


class ProductionWorkflowTests(unittest.TestCase):
    def test_production_csrf_time_limit_uses_seconds(self):
        self.assertIsInstance(ProductionConfig.WTF_CSRF_TIME_LIMIT, int)
        self.assertEqual(ProductionConfig.WTF_CSRF_TIME_LIMIT, 12 * 60 * 60)

    def test_office_default_rate_limit_allows_normal_navigation(self):
        self.assertEqual(BaseConfig.RATELIMIT_DEFAULT, "600 per hour")

    @classmethod
    def setUpClass(cls):
        cls.original_require_auth = app.config["REQUIRE_AUTH"]
        app.config.update(TESTING=True, REQUIRE_AUTH=True, WTF_CSRF_ENABLED=False)
        with app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            for email in ["owner@gulfconferences.co.uk", "other@gulfconferences.co.uk",
                          "admin@gulfconferences.co.uk", "first.login@gulfconferences.co.uk"]:
                user = User.query.filter_by(email=email).first()
                if user:
                    for campaign in user.campaigns.all():
                        db.session.delete(campaign)
                    db.session.delete(user)
            db.session.commit()
        app.config["REQUIRE_AUTH"] = cls.original_require_auth

    def setUp(self):
        self.client = app.test_client()
        with app.app_context():
            self.owner = User.query.filter_by(email="owner@gulfconferences.co.uk").first()
            if not self.owner:
                self.owner = User(email="owner@gulfconferences.co.uk", role="user")
                db.session.add(self.owner)
            self.other = User.query.filter_by(email="other@gulfconferences.co.uk").first()
            if not self.other:
                self.other = User(email="other@gulfconferences.co.uk", role="user")
                db.session.add(self.other)
            db.session.commit()
            self.owner_id, self.other_id = self.owner.id, self.other.id
        with self.client.session_transaction() as session:
            session["user_id"] = self.owner_id

    def test_anonymous_routes_fail_closed(self):
        client = app.test_client()
        self.assertEqual(client.get("/api/me").status_code, 401)
        self.assertEqual(client.get("/").status_code, 302)

    def test_first_company_login_creates_enabled_user(self):
        client = app.test_client()
        with client.session_transaction() as session:
            session["oauth_state"] = "expected-state"
        profile = {
            "email": "first.login@gulfconferences.co.uk",
            "name": "First Login",
            "verified_email": True,
        }
        with patch("app.exchange_code", return_value=object()), \
             patch("app.get_profile", return_value=profile), \
             patch("app.save_encrypted_credentials"):
            response = client.get("/oauth2/callback?code=test-code&state=expected-state")
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            user = User.query.filter_by(email=profile["email"]).one()
            self.assertTrue(user.enabled)
            self.assertEqual(user.role, "user")
        with client.session_transaction() as session:
            self.assertEqual(session["user_id"], user.id)

    def test_campaign_is_owned_and_cross_user_hidden(self):
        response = self.client.post("/api/campaigns")
        self.assertEqual(response.status_code, 201)
        campaign_id = response.json["campaign"]["id"]
        with self.client.session_transaction() as session:
            session["user_id"] = self.other_id
        self.assertEqual(self.client.get(f"/api/campaigns/{campaign_id}/recipients").status_code, 404)

    def test_authenticated_dashboard_uses_persistent_workflow(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Office campaigns", response.data)
        self.assertIn(b"Send test to me", response.data)
        self.assertIn(b"await persistVisibleContent();status('Sending test email", response.data)
        self.assertIn(b"button.disabled=true", response.data)
        self.assertIn(b"Validating\xe2\x80\xa6", response.data)
        self.assertIn(b"Uploading\xe2\x80\xa6", response.data)
        self.assertIn(b"Sending test\xe2\x80\xa6", response.data)

    def test_legacy_mutations_are_disabled_when_hosted(self):
        response = self.client.post("/api/validate", json={})
        self.assertEqual(response.status_code, 410)

    def test_csv_upload_deduplicates_and_bounds_campaign(self):
        campaign_id = self.client.post("/api/campaigns").json["campaign"]["id"]
        response = self.client.post(f"/api/campaigns/{campaign_id}/upload", data={
            "csv_file": (io.BytesIO(b"Email,FirstName\nA@example.com,A\na@example.com,Again\nb@example.com,B\n"), "people.csv")
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["campaign"]["total"], 2)

    def test_repeated_validation_reuses_active_job(self):
        with app.app_context():
            campaign = Campaign(owner_id=self.owner_id, sender_email="owner@gulfconferences.co.uk",
                                state="validating", total_count=2)
            db.session.add(campaign)
            db.session.flush()
            job = ValidationJob(campaign_id=campaign.id, owner_id=self.owner_id,
                                state="running", total=2)
            db.session.add(job)
            db.session.commit()
            campaign_id, job_id = campaign.id, job.id
        response = self.client.post(f"/api/campaigns/{campaign_id}/validate", json={"policy": "balanced"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["job_id"], job_id)
        self.assertTrue(response.json["existing"])

    def test_template_html_removes_executable_content(self):
        cleaned = sanitize_email_html('<p onclick="steal()">Hello</p><script>alert(1)</script>')
        self.assertIn("Hello", cleaned)
        self.assertNotIn("onclick", cleaned)
        self.assertNotIn("<script", cleaned)
        self.assertNotIn("alert(1)", cleaned)

    def test_template_html_does_not_render_head_css_as_email_text(self):
        cleaned = sanitize_email_html(
            '<!doctype html><html><head><title>Calendar</title>'
            '<style>@media only screen { .email { width: 100%; } }</style>'
            '</head><body><div class="email">Visible email</div></body></html>'
        )
        self.assertIn("Visible email", cleaned)
        self.assertNotIn("Calendar", cleaned)
        self.assertNotIn("@media", cleaned)

    def test_content_change_requires_new_test(self):
        with app.app_context():
            campaign = Campaign(owner_id=self.owner_id, sender_email="owner@gulfconferences.co.uk",
                                state="reviewed", selected_count=1)
            db.session.add(campaign)
            db.session.flush()
            db.session.add(CampaignRecipient(campaign_id=campaign.id, source_row=2,
                                             original_email="a@example.com", normalized_email="a@example.com",
                                             selected=True))
            db.session.commit()
            campaign_id = campaign.id
        response = self.client.put(f"/api/campaigns/{campaign_id}/content", json={
            "subject": "Hello", "html_content": "<p>Hi</p>", "text_content": "Hi"
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["campaign"]["state"], "test_required")
        queue_response = self.client.post(f"/api/campaigns/{campaign_id}/queue",
                                          json={"confirmation": "SEND 1"},
                                          headers={"Idempotency-Key": "one"})
        self.assertEqual(queue_response.status_code, 409)

    def test_content_save_returns_sanitized_preview_content(self):
        campaign_id = self.client.post("/api/campaigns").json["campaign"]["id"]
        response = self.client.put(f"/api/campaigns/{campaign_id}/content", json={
            "subject": "Hello", "html_content": '<p onclick="bad()">Hi</p>', "text_content": "Hi"
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["campaign"]["html_content"], "<p>Hi</p>")
        self.assertEqual(response.json["campaign"]["text_content"], "Hi")


if __name__ == "__main__":
    unittest.main()
