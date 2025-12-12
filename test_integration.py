"""
Integration tests for Email Campaign Manager
Tests the full workflow from upload to send
"""
import unittest
import tempfile
import shutil
import os
import pandas as pd
from pathlib import Path
from unittest.mock import patch, MagicMock

from app import app


class TestEmailCampaignWorkflow(unittest.TestCase):
    """Integration tests for complete campaign workflow"""

    def setUp(self):
        """Set up test client and temp folder"""
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['UPLOAD_FOLDER'] = tempfile.mkdtemp()
        self.client = self.app.test_client()
        
        # Create sample CSV
        self.sample_csv = pd.DataFrame({
            'FirstName': ['John', 'Jane', 'Bob', 'Alice'],
            'Email': ['john@example.com', 'jane@example.com', 'bob@example.com', 'alice@example.com']
        })

    def tearDown(self):
        """Clean up temp folder"""
        if os.path.exists(self.app.config['UPLOAD_FOLDER']):
            shutil.rmtree(self.app.config['UPLOAD_FOLDER'])

    def test_full_workflow_upload_to_configure(self):
        """Test upload and configuration flow"""
        # Create temp CSV file
        csv_file = tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False)
        self.sample_csv.to_csv(csv_file.name, index=False)
        csv_file.close()
        
        try:
            # Upload CSV
            with open(csv_file.name, 'rb') as f:
                response = self.client.post('/upload', data={'csv_file': f}, follow_redirects=True)
            
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Successfully uploaded', response.data)
            
            # Configure columns
            response = self.client.get('/configure')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Email', response.data)
            self.assertIn(b'FirstName', response.data)
        finally:
            os.unlink(csv_file.name)

    def test_validation_page_requires_upload(self):
        """Test that validation page requires prior upload"""
        response = self.client.get('/validate', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Please upload and configure a CSV file first', response.data)

    def test_review_page_requires_validation(self):
        """Test that review page requires prior validation"""
        response = self.client.get('/review', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Please validate your email list first', response.data)

    def test_compose_page_requires_validated_list(self):
        """Test that compose page requires validated list"""
        response = self.client.get('/compose', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Please validate your email list first', response.data)

    def test_send_requires_google_login(self):
        """Test that send requires Google authentication"""
        with self.client:
            # Set up session with validated file (mock)
            with self.client.session_transaction() as sess:
                sess['validated_file'] = 'test.csv'
                sess['email_col'] = 'Email'
                sess['subject'] = 'Test'
                sess['html_content'] = 'Test'
                sess['text_content'] = 'Test'
            
            # Try to send without Google login
            response = self.client.post('/send', follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Please sign in with Google', response.data)

    def test_column_validation_requires_email_column(self):
        """Test that column configuration requires email column"""
        with self.client:
            with self.client.session_transaction() as sess:
                sess['csv_file'] = 'test.csv'
                sess['columns'] = ['FirstName', 'Email']
            
            # Submit without selecting email column
            response = self.client.post('/set_columns', data={'email_col': ''}, follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Email column is required', response.data)


class TestEmailSelectionFlow(unittest.TestCase):
    """Tests for email review and selection workflow"""

    def setUp(self):
        """Set up test client"""
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['UPLOAD_FOLDER'] = tempfile.mkdtemp()
        self.client = self.app.test_client()

    def tearDown(self):
        """Clean up"""
        if os.path.exists(self.app.config['UPLOAD_FOLDER']):
            shutil.rmtree(self.app.config['UPLOAD_FOLDER'])

    def test_email_selection_endpoint(self):
        """Test email selection saving"""
        with self.client:
            # Set up validation data
            validation_df = pd.DataFrame({
                'Email': ['a@ex.com', 'b@ex.com', 'c@ex.com'],
                'bounce_risk': [False, True, False]
            })
            
            # Save test validation file
            val_path = os.path.join(self.app.config['UPLOAD_FOLDER'], 'validation_test.csv')
            validation_df.to_csv(val_path, index=False)
            
            with self.client.session_transaction() as sess:
                sess['validation_file'] = 'validation_test.csv'
                sess['csv_file'] = 'original_test.csv'  # Required for creating validated filename
                sess['email_col'] = 'Email'
            
            # Select the problematic email (index 1)
            response = self.client.post(
                '/set_email_selection',
                json={'approved_indices': [1]},
                content_type='application/json'
            )
            
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data['success'])
            # Should include 2 valid + 1 approved problematic = 3 total
            self.assertEqual(data['total'], 3)


class TestErrorHandling(unittest.TestCase):
    """Tests for error handling and edge cases"""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['UPLOAD_FOLDER'] = tempfile.mkdtemp()
        self.client = self.app.test_client()

    def tearDown(self):
        if os.path.exists(self.app.config['UPLOAD_FOLDER']):
            shutil.rmtree(self.app.config['UPLOAD_FOLDER'])

    def test_upload_invalid_file_type(self):
        """Test uploading non-CSV file"""
        txt_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        txt_file.write("Not a CSV")
        txt_file.close()
        
        try:
            with open(txt_file.name, 'rb') as f:
                response = self.client.post('/upload', data={'csv_file': f}, follow_redirects=True)
            
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Only CSV files are allowed', response.data)
        finally:
            os.unlink(txt_file.name)

    def test_empty_csv_handling(self):
        """Test handling of empty CSV"""
        empty_csv = tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False)
        empty_csv.write("Email\n")  # Header only
        empty_csv.close()
        
        try:
            with open(empty_csv.name, 'rb') as f:
                response = self.client.post('/upload', data={'csv_file': f}, follow_redirects=True)
            
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Successfully uploaded', response.data)
        finally:
            os.unlink(empty_csv.name)

    def test_malformed_csv_handling(self):
        """Test handling of malformed CSV"""
        bad_csv = tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False)
        bad_csv.write("Email\ninvalid\n")  # Missing quotes/incomplete
        bad_csv.close()
        
        try:
            with open(bad_csv.name, 'rb') as f:
                response = self.client.post('/upload', data={'csv_file': f}, follow_redirects=True)
            
            # Should handle gracefully
            self.assertEqual(response.status_code, 200)
        finally:
            os.unlink(bad_csv.name)


if __name__ == '__main__':
    unittest.main()
