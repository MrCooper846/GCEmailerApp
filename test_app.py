"""
Unit tests for Email Campaign Manager application
"""
import unittest
import json
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
import pandas as pd

# Import Flask app and services
from app import app
from email_validator_service import (
    extract_first_email,
    detect_typo,
    compute_bounce_risk,
    Cache,
    TokenBucket,
)
from email_sender_service import build_message
from google_oauth_service import (
    _client_config,
    load_credentials,
    save_credentials,
)


class TestEmailValidationService(unittest.TestCase):
    """Tests for email validation service"""

    def test_extract_first_email(self):
        """Test email extraction from text"""
        self.assertEqual(
            extract_first_email("contact: john@example.com"),
            "john@example.com"
        )
        self.assertEqual(
            extract_first_email("john@example.com, jane@example.com"),
            "john@example.com"
        )
        self.assertIsNone(extract_first_email("no email here"))
        self.assertIsNone(extract_first_email(None))

    def test_detect_typo(self):
        """Test typo detection in domains"""
        # Common typo (1 char diff)
        self.assertEqual(detect_typo("gmai.com"), "gmail.com")    # missing 'l'
        self.assertEqual(detect_typo("gomail.com"), "gmail.com")  # 'o' instead of 'a'
        self.assertEqual(detect_typo("gmil.com"), "gmail.com")    # missing 'a'
        
        # No typo (too different or exact match)
        self.assertIsNone(detect_typo("example.com"))

    def test_compute_bounce_risk_strict(self):
        """Test bounce risk computation (strict policy)"""
        # Hard flags trigger risk in strict mode
        self.assertTrue(
            compute_bounce_risk("strict", ["invalid_syntax"], "valid", "no", False)
        )
        self.assertTrue(
            compute_bounce_risk("strict", ["no_mx"], "valid", "no", False)
        )
        # Valid email is safe
        self.assertFalse(
            compute_bounce_risk("strict", [], "valid", "no", False)
        )

    def test_compute_bounce_risk_balanced(self):
        """Test bounce risk computation (balanced policy)"""
        # Syntax errors are risky
        self.assertTrue(
            compute_bounce_risk("balanced", ["invalid_syntax"], "valid", "no", False)
        )
        # SMTP hard failures are risky
        self.assertTrue(
            compute_bounce_risk("balanced", [], "invalid", "no", False)
        )
        # Valid email is safe
        self.assertFalse(
            compute_bounce_risk("balanced", [], "valid", "no", False)
        )

    def test_token_bucket(self):
        """Test rate limiter token bucket"""
        bucket = TokenBucket(tokens=2, period=1.0)
        
        # Should allow first token immediately
        wait1 = bucket.wait()
        self.assertEqual(wait1, 0.0)
        
        # Should allow second token immediately
        wait2 = bucket.wait()
        self.assertEqual(wait2, 0.0)
        
        # Third token should require waiting
        wait3 = bucket.wait()
        self.assertGreater(wait3, 0.0)

    def test_cache_email_operations(self):
        """Test email cache operations"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        
        try:
            cache = Cache(db_path, ttl_valid_days=1, ttl_soft_days=1, ttl_mx_days=1)
            
            # Test put and get
            email_data = {
                "email": "test@example.com",
                "normalized": "test@example.com",
                "bounce_risk": False,
                "reasons": "none",
                "mx_ok": True,
                "suggestion": None,
                "smtp_status": "valid",
                "smtp_code": 250,
                "smtp_msg": "OK",
                "catch_all": "no",
                "mailbox_full": False,
            }
            cache.put_email(email_data)
            
            # Retrieve and verify
            retrieved = cache.get_email("test@example.com")
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved["email"], "test@example.com")
            self.assertFalse(retrieved["bounce_risk"])
            
            cache.close()
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_cache_mx_operations(self):
        """Test MX record cache operations"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        
        try:
            cache = Cache(db_path)
            
            # Test put and get
            cache.put_mx("example.com", True, "mail.example.com", None)
            
            # Retrieve and verify
            result = cache.get_mx("example.com")
            self.assertIsNotNone(result)
            mx_ok, err, mx_host = result
            self.assertTrue(mx_ok)
            self.assertEqual(mx_host, "mail.example.com")
            
            cache.close()
        finally:
            Path(db_path).unlink(missing_ok=True)


class TestEmailSenderService(unittest.TestCase):
    """Tests for email sender service"""

    def test_build_message(self):
        """Test email message building"""
        msg = build_message(
            to_addr="test@example.com",
            first_name="John",
            subject="Test Subject",
            html_content="<h1>Hello {{FirstName}}</h1>",
            text_content="Hello {{FirstName}}",
            email_from="sender@example.com"
        )
        
        self.assertEqual(msg["To"], "test@example.com")
        self.assertEqual(msg["From"], "sender@example.com")
        self.assertEqual(msg["Subject"], "Test Subject")
        self.assertIn("Hello John", msg.as_string())

    def test_build_message_personalization(self):
        """Test email personalization with placeholders"""
        msg = build_message(
            to_addr="jane@example.com",
            first_name="Jane",
            subject="Hello {{FirstName}}",
            html_content="<p>Hi {{FirstName}}, welcome!</p>",
            text_content="Hi {{FirstName}}, welcome!",
            email_from="sender@example.com"
        )
        
        msg_str = msg.as_string()
        self.assertIn("Hi Jane", msg_str)
        self.assertNotIn("{{FirstName}}", msg_str)


class TestFlaskApp(unittest.TestCase):
    """Tests for Flask application routes"""

    def setUp(self):
        """Set up test client and temp folder"""
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['UPLOAD_FOLDER'] = tempfile.mkdtemp()
        self.client = self.app.test_client()

    def tearDown(self):
        """Clean up temp folder"""
        if os.path.exists(self.app.config['UPLOAD_FOLDER']):
            shutil.rmtree(self.app.config['UPLOAD_FOLDER'])

    def test_index_route(self):
        """Test home page loads"""
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Email Campaign Manager', response.data)

    def test_upload_csv_no_file(self):
        """Test CSV upload without file"""
        response = self.client.post('/upload', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'No file uploaded', response.data)

    def test_upload_csv_valid(self):
        """Test valid CSV upload"""
        csv_data = "FirstName,Email\nJohn,john@example.com\nJane,jane@example.com"
        data = {
            'csv_file': (open(tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False).name, 'w'), csv_data)
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_data)
            f.flush()
            with open(f.name, 'rb') as csv_file:
                response = self.client.post('/upload', data={'csv_file': csv_file}, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'Successfully uploaded', response.data)

    def test_configure_columns_no_session(self):
        """Test configure page without uploaded CSV"""
        response = self.client.get('/configure', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Please upload a CSV file first', response.data)

    def test_reset_clears_session(self):
        """Test reset route clears session"""
        # Set some session data first
        with self.client.session_transaction() as sess:
            sess['test_key'] = 'test_value'
        
        # Call reset
        self.client.get('/reset', follow_redirects=True)
        
        # Verify session is cleared
        with self.client.session_transaction() as sess:
            self.assertEqual(len(sess), 0)

    def test_login_google_no_credentials(self):
        """Test Google login without credentials configured"""
        with patch.dict(os.environ, {'GOOGLE_CLIENT_ID': '', 'GOOGLE_CLIENT_SECRET': ''}):
            response = self.client.get('/login/google', follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Google login setup error', response.data)

    def test_logout_google(self):
        """Test Google logout"""
        with self.client.session_transaction() as sess:
            sess['google_email'] = 'test@example.com'
        
        response = self.client.get('/logout/google', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Disconnected Google account', response.data)


class TestGoogleOAuth(unittest.TestCase):
    """Tests for Google OAuth service"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.token_store = Path(self.temp_dir) / "tokens.json"

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_client_config_structure(self):
        """Test OAuth client config structure"""
        with patch.dict(os.environ, {
            'GOOGLE_CLIENT_ID': 'test-id',
            'GOOGLE_CLIENT_SECRET': 'test-secret',
            'GOOGLE_REDIRECT_URI': 'http://localhost:5000/callback'
        }):
            # Import to get updated env vars
            import importlib
            import google_oauth_service
            importlib.reload(google_oauth_service)
            
            config = google_oauth_service._client_config()
            self.assertIn('web', config)
            self.assertEqual(config['web']['client_id'], 'test-id')
            self.assertEqual(config['web']['client_secret'], 'test-secret')

    def test_save_and_load_credentials(self):
        """Test saving and loading credentials"""
        with patch('google_oauth_service.TOKEN_STORE', self.token_store):
            # Create mock credentials
            mock_creds = MagicMock()
            mock_creds.to_json.return_value = json.dumps({
                'token': 'test_token',
                'refresh_token': 'test_refresh',
                'token_uri': 'https://oauth2.googleapis.com/token',
                'client_id': 'test_id',
                'client_secret': 'test_secret',
                'scopes': ['https://www.googleapis.com/auth/gmail.send']
            })
            
            # Import and patch
            import google_oauth_service
            google_oauth_service.save_credentials('test@example.com', mock_creds)
            
            # Verify file was created
            self.assertTrue(self.token_store.exists())

    def test_credentials_encryption_simple(self):
        """Test that credentials are stored"""
        with patch('google_oauth_service.TOKEN_STORE', self.token_store):
            mock_creds = MagicMock()
            mock_creds.to_json.return_value = json.dumps({'token': 'secret123'})
            
            import google_oauth_service
            google_oauth_service.save_credentials('user@workspace.com', mock_creds)
            
            # Verify data is written
            self.assertTrue(self.token_store.exists())
            content = self.token_store.read_text()
            self.assertIn('user@workspace.com', content)


class TestDataProcessing(unittest.TestCase):
    """Tests for data processing functions"""

    def test_csv_dataframe_creation(self):
        """Test creating DataFrame from CSV-like data"""
        data = {
            'FirstName': ['John', 'Jane', 'Bob'],
            'Email': ['john@example.com', 'jane@example.com', 'bob@example.com']
        }
        df = pd.DataFrame(data)
        
        self.assertEqual(len(df), 3)
        self.assertIn('FirstName', df.columns)
        self.assertIn('Email', df.columns)

    def test_bounce_risk_filtering(self):
        """Test filtering by bounce risk"""
        data = {
            'Email': ['a@ex.com', 'b@ex.com', 'c@ex.com'],
            'bounce_risk': [False, True, False]
        }
        df = pd.DataFrame(data)
        
        valid = df[df['bounce_risk'] == False]
        self.assertEqual(len(valid), 2)
        
        risky = df[df['bounce_risk'] == True]
        self.assertEqual(len(risky), 1)


if __name__ == '__main__':
    unittest.main()
