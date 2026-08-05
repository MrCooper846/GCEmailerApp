"""
Unit tests for Email Campaign Manager application
"""
import unittest
import json
import os
import io
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import pandas as pd
import dns.resolver

# Import Flask app and services
from app import app
from email_validator_service import (
    extract_first_email,
    detect_typo,
    compute_bounce_risk,
    Cache,
    TokenBucket,
    resolve_mail_route,
    batch_smtp_probe,
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

    @patch('email_validator_service.time.sleep')
    @patch('email_validator_service.dns.resolver.resolve')
    def test_dns_retries_temporary_failures(self, mock_resolve, _mock_sleep):
        mock_resolve.side_effect = dns.resolver.Timeout('timed out')

        result = resolve_mail_route('example.com', timeout=0.1, max_attempts=3)

        self.assertEqual(result['status'], 'temporary_failure')
        self.assertEqual(result['attempts'], 3)
        self.assertFalse(result['mx_ok'])
        self.assertEqual(mock_resolve.call_count, 3)

    @patch('email_validator_service.dns.resolver.resolve')
    def test_dns_uses_address_fallback_without_mx(self, mock_resolve):
        mock_resolve.side_effect = [dns.resolver.NoAnswer(), ['192.0.2.10']]

        result = resolve_mail_route('example.com', timeout=0.1)

        self.assertEqual(result['status'], 'implicit_mx')
        self.assertEqual(result['mx_host'], 'example.com')
        self.assertTrue(result['mx_ok'])

    @patch('email_validator_service.dns.resolver.resolve')
    def test_dns_nxdomain_is_definitive(self, mock_resolve):
        mock_resolve.side_effect = dns.resolver.NXDOMAIN()

        result = resolve_mail_route('missing.example', timeout=0.1)

        self.assertEqual(result['status'], 'nxdomain')
        self.assertFalse(result['mx_ok'])
        self.assertEqual(result['attempts'], 1)

    @patch('email_validator_service.time.sleep')
    @patch('email_validator_service.get_bucket')
    @patch('email_validator_service.smtp_open')
    def test_smtp_retries_temporary_recipient_failure(self, mock_open, mock_bucket, _mock_sleep):
        smtp = MagicMock()
        smtp.mail.return_value = (250, b'OK')
        smtp.rcpt.side_effect = [(451, b'Try later'), (250, b'Accepted'), (550, b'Unknown')]
        mock_open.return_value = smtp
        mock_bucket.return_value.wait.return_value = 0

        result = batch_smtp_probe(
            'mx.example.com', '[email protected]', ['person@example.com'],
            'validator.example.com', timeout=0.1, max_attempts=3,
        )['person@example.com']

        self.assertEqual(result['smtp_status'], 'valid')
        self.assertEqual(result['smtp_attempts'], 2)
        self.assertEqual(result['catch_all'], 'no')

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
                "dns_status": "mx",
                "dns_msg": "MX route found: mail.example.com",
                "dns_attempts": 1,
                "suggestion": None,
                "smtp_status": "valid",
                "smtp_code": 250,
                "smtp_msg": "OK",
                "smtp_attempts": 1,
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
        self.assertEqual(msg.get_body().get_content_type(), "text/html")

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

    def test_build_message_embeds_cid_image(self):
        """CID references become inline multipart/related image parts."""
        with tempfile.TemporaryDirectory() as asset_folder:
            image_name = 'attendee-logos.png'
            image_bytes = b'\x89PNG\r\n\x1a\n' + b'test-image-data'
            (Path(asset_folder) / image_name).write_bytes(image_bytes)

            msg = build_message(
                to_addr='recipient@example.com',
                first_name='Alice',
                subject='Conference',
                html_content=f'<p>Hello</p><img src="cid:{image_name}" alt="Logos">',
                text_content='Hello',
                email_from='sender@example.com',
                inline_image_folder=asset_folder,
            )

            image_parts = [part for part in msg.walk() if part.get_content_type() == 'image/png']
            self.assertEqual(len(image_parts), 1)
            self.assertEqual(image_parts[0]['Content-ID'], f'<{image_name}>')
            self.assertEqual(image_parts[0].get_content_disposition(), 'inline')
            self.assertEqual(image_parts[0].get_payload(decode=True), image_bytes)


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

    def test_preview_uses_first_selected_recipient(self):
        """Preview renders placeholders with the first selected recipient."""
        recipients = pd.DataFrame([
            {'Email': '[email protected]', 'FirstName': 'Alice'},
            {'Email': '[email protected]', 'FirstName': 'Bob'},
        ])
        with self.client.session_transaction() as sess:
            sess['final_df_json'] = recipients.to_json(orient='records')
            sess['email_col'] = 'Email'
            sess['name_col'] = 'FirstName'
            sess['valid_count'] = 2

        response = self.client.post('/preview', data={
            'subject': 'Hello {{ name }}',
            'html_content': '<h1>Welcome {{FirstName}}</h1>',
            'text_content': 'Welcome {{ firstname }}',
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Hello Alice', response.data)
        self.assertIn(b'Welcome Alice', response.data)
        self.assertIn(b'[email protected]', response.data)
        self.assertNotIn(b'{{FirstName}}', response.data)

    def test_save_and_load_email_template(self):
        """Templates persist as JSON and can be selected on compose."""
        original_folder = self.app.config['EMAIL_TEMPLATE_FOLDER']
        with tempfile.TemporaryDirectory() as template_folder:
            self.app.config['EMAIL_TEMPLATE_FOLDER'] = template_folder
            try:
                save_response = self.client.post('/api/templates', json={
                    'name': 'Conference Invite',
                    'subject': 'Hello {{FirstName}}',
                    'html_content': '<h1>Welcome {{FirstName}}</h1>',
                    'text_content': 'Welcome {{FirstName}}',
                })
                self.assertEqual(save_response.status_code, 200)
                template_id = save_response.json['template']['id']
                self.assertEqual(template_id, 'Conference_Invite')
                self.assertTrue((Path(template_folder) / 'Conference_Invite.json').is_file())

                load_response = self.client.get(f'/api/templates/{template_id}')
                self.assertEqual(load_response.status_code, 200)
                self.assertEqual(load_response.json['template']['subject'], 'Hello {{FirstName}}')

                list_response = self.client.get('/api/templates')
                self.assertEqual(list_response.status_code, 200)
                self.assertEqual(list_response.json['templates'][0]['name'], 'Conference Invite')

                manager_response = self.client.get('/templates')
                self.assertEqual(manager_response.status_code, 200)
                self.assertIn(b'Template Manager', manager_response.data)
                self.assertIn(b'Conference Invite', manager_response.data)

                with self.client.session_transaction() as sess:
                    sess['final_df_json'] = '[]'
                    sess['valid_count'] = 0
                compose_response = self.client.get('/compose')
                self.assertEqual(compose_response.status_code, 200)
                self.assertIn(b'Conference Invite', compose_response.data)

                update_response = self.client.put(f'/api/templates/{template_id}', json={
                    'name': 'Updated Conference Invite',
                    'subject': 'Updated subject',
                    'html_content': '<h1>Updated HTML</h1>',
                    'text_content': 'Updated text',
                })
                self.assertEqual(update_response.status_code, 200)
                updated_id = update_response.json['template']['id']
                self.assertEqual(updated_id, 'Updated_Conference_Invite')
                self.assertFalse((Path(template_folder) / 'Conference_Invite.json').exists())
                self.assertTrue((Path(template_folder) / f'{updated_id}.json').exists())

                delete_response = self.client.delete(f'/api/templates/{updated_id}')
                self.assertEqual(delete_response.status_code, 200)
                self.assertFalse((Path(template_folder) / f'{updated_id}.json').exists())
            finally:
                self.app.config['EMAIL_TEMPLATE_FOLDER'] = original_folder

    def test_template_name_cannot_escape_folder(self):
        """Unsafe names are converted to safe local identifiers."""
        original_folder = self.app.config['EMAIL_TEMPLATE_FOLDER']
        with tempfile.TemporaryDirectory() as template_folder:
            self.app.config['EMAIL_TEMPLATE_FOLDER'] = template_folder
            try:
                response = self.client.post('/api/templates', json={
                    'name': '../../Quarterly Invite',
                    'subject': 'Subject',
                    'html_content': '<p>HTML</p>',
                    'text_content': 'Text',
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['template']['id'], 'Quarterly_Invite')
                self.assertTrue((Path(template_folder) / 'Quarterly_Invite.json').is_file())
            finally:
                self.app.config['EMAIL_TEMPLATE_FOLDER'] = original_folder

    def test_upload_and_preview_inline_image(self):
        """Uploaded images are inserted by CID and rendered in browser preview."""
        original_folder = self.app.config['EMAIL_ASSET_FOLDER']
        with tempfile.TemporaryDirectory() as asset_folder:
            self.app.config['EMAIL_ASSET_FOLDER'] = asset_folder
            try:
                image_bytes = b'\x89PNG\r\n\x1a\n' + b'test-image-data'
                upload_response = self.client.post('/api/template-assets', data={
                    'image': (io.BytesIO(image_bytes), 'attendee logos.png'),
                }, content_type='multipart/form-data')
                self.assertEqual(upload_response.status_code, 200)
                asset_id = upload_response.json['asset_id']
                self.assertTrue((Path(asset_folder) / asset_id).is_file())
                self.assertIn(f'cid:{asset_id}', upload_response.json['snippet'])

                with self.client.session_transaction() as sess:
                    sess['final_df_json'] = pd.DataFrame([
                        {'Email': 'alice@example.com', 'FirstName': 'Alice'},
                    ]).to_json(orient='records')
                    sess['email_col'] = 'Email'
                    sess['name_col'] = 'FirstName'
                    sess['valid_count'] = 1

                preview_response = self.client.post('/preview', data={
                    'subject': 'Hello',
                    'html_content': f'<img src="cid:{asset_id}" alt="Logos">',
                    'text_content': 'Hello',
                })
                self.assertEqual(preview_response.status_code, 200)
                self.assertIn(f'/email-assets/{asset_id}'.encode(), preview_response.data)

                asset_response = self.client.get(f'/email-assets/{asset_id}')
                self.assertEqual(asset_response.status_code, 200)
                self.assertEqual(asset_response.data, image_bytes)
                asset_response.close()
            finally:
                self.app.config['EMAIL_ASSET_FOLDER'] = original_folder

    def test_rejects_non_image_template_asset(self):
        """Extension spoofing cannot upload arbitrary files as inline images."""
        response = self.client.post('/api/template-assets', data={
            'image': (io.BytesIO(b'<script>alert(1)</script>'), 'banner.png'),
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Only PNG, JPEG, and GIF', response.data)

    @patch('app.VALIDATION_POLICY', 'balanced')
    @patch('app.VALIDATION_ENABLE_SMTP', False)
    @patch('app.VALIDATION_MAIL_FROM', '')
    @patch('app.validate_email_list', new_callable=AsyncMock)
    def test_validation_uses_safe_defaults(self, mock_validate):
        """Default validation runs balanced DNS checks without SMTP probing."""
        csv_name = 'validation-defaults.csv'
        pd.DataFrame({'Email': ['person@example.com']}).to_csv(
            Path(self.app.config['UPLOAD_FOLDER']) / csv_name, index=False
        )
        mock_validate.return_value = pd.DataFrame({
            'Email': ['person@example.com'],
            'bounce_risk': [False],
        })
        with self.client.session_transaction() as sess:
            sess['csv_file'] = csv_name
            sess['email_col'] = 'Email'

        response = self.client.post('/api/validate', json={})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['smtp_enabled'])
        self.assertEqual(response.json['policy'], 'balanced')
        self.assertFalse(mock_validate.await_args.kwargs['do_smtp'])
        self.assertEqual(mock_validate.await_args.kwargs['policy'], 'balanced')

    @patch('app.VALIDATION_MAIL_FROM', '')
    @patch('app.validate_email_list', new_callable=AsyncMock)
    def test_smtp_validation_requires_configured_sender(self, mock_validate):
        """SMTP probing cannot silently use a placeholder envelope sender."""
        csv_name = 'smtp-validation.csv'
        pd.DataFrame({'Email': ['person@example.com']}).to_csv(
            Path(self.app.config['UPLOAD_FOLDER']) / csv_name, index=False
        )
        with self.client.session_transaction() as sess:
            sess['csv_file'] = csv_name
            sess['email_col'] = 'Email'

        response = self.client.post('/api/validate', json={
            'do_smtp': True,
            'policy': 'balanced',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn(b'VALIDATION_MAIL_FROM', response.data)
        mock_validate.assert_not_awaited()

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
