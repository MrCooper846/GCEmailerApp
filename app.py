"""
Marketing Email Campaign Web Application
Combines CSV upload, email validation, and personalized email sending
"""
import os
import asyncio
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from werkzeug.utils import secure_filename
import pandas as pd
from dotenv import load_dotenv

from email_validator_service import validate_email_list
from email_sender_service import send_email_campaign
from google_oauth_service import (
    generate_auth_url,
    exchange_code,
    save_credentials,
    load_credentials,
    ensure_valid_credentials,
    get_profile_email,
)
from gmail_sender_service import send_email_campaign_gmail

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24))
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'csv'}

# SMTP Configuration from .env
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))

# Create uploads directory
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


def guess_email_column(df: pd.DataFrame) -> str:
    """Auto-detect email column"""
    import re
    candidates = [c for c in df.columns if re.search(r'(^|_)e?-?mail(s)?(|_)$', c.strip().lower())]
    return candidates[0] if candidates else None


def guess_name_column(df: pd.DataFrame) -> str:
    """Auto-detect name column"""
    import re
    for pattern in [r'first.*name', r'name', r'contact', r'recipient']:
        candidates = [c for c in df.columns if re.search(pattern, c.strip().lower())]
        if candidates:
            return candidates[0]
    return None


@app.route('/')
def index():
    """Home page - upload CSV"""
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_csv():
    """Handle CSV upload"""
    if 'csv_file' not in request.files:
        flash('No file uploaded', 'error')
        return redirect(url_for('index'))
    
    file = request.files['csv_file']
    
    if file.filename == '':
        flash('No file selected', 'error')
        return redirect(url_for('index'))
    
    if not allowed_file(file.filename):
        flash('Only CSV files are allowed', 'error')
        return redirect(url_for('index'))
    
    try:
        # Save file
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_filename = f"{timestamp}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        file.save(filepath)
        
        # Load and inspect CSV
        df = pd.read_csv(filepath)
        
        # Auto-detect columns
        email_col = guess_email_column(df)
        name_col = guess_name_column(df)
        
        # Store in session
        session['csv_file'] = unique_filename
        session['total_rows'] = len(df)
        session['email_col'] = email_col
        session['name_col'] = name_col
        session['columns'] = df.columns.tolist()
        
        flash(f'Successfully uploaded {len(df)} contacts', 'success')
        return redirect(url_for('configure_columns'))
        
    except Exception as e:
        flash(f'Error processing CSV: {str(e)}', 'error')
        return redirect(url_for('index'))


@app.route('/configure')
def configure_columns():
    """Configure email and name columns"""
    if 'csv_file' not in session:
        flash('Please upload a CSV file first', 'warning')
        return redirect(url_for('index'))
    
    return render_template('configure.html',
                         columns=session.get('columns', []),
                         email_col=session.get('email_col'),
                         name_col=session.get('name_col'),
                         total_rows=session.get('total_rows'))


@app.route('/set_columns', methods=['POST'])
def set_columns():
    """Save column configuration and start validation"""
    session['email_col'] = request.form.get('email_col')
    session['name_col'] = request.form.get('name_col')
    
    if not session.get('email_col'):
        flash('Email column is required', 'error')
        return redirect(url_for('configure_columns'))
    
    return redirect(url_for('validate_emails'))


@app.route('/validate')
def validate_emails():
    """Email validation page"""
    if 'csv_file' not in session or 'email_col' not in session:
        flash('Please upload and configure a CSV file first', 'warning')
        return redirect(url_for('index'))
    
    return render_template('validate.html', total_rows=session.get('total_rows'))


@app.route('/api/validate', methods=['POST'])
def api_validate():
    """API endpoint for email validation - returns full results for review"""
    import sys
    try:
        app.logger.info('Starting email validation...')
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], session['csv_file'])
        df = pd.read_csv(filepath)
        email_col = session['email_col']
        
        app.logger.info(f'Loaded {len(df)} rows from CSV')
        
        # For testing, you can disable SMTP checks (faster)
        # Change to do_smtp=False to skip SMTP verification
        do_smtp = True  # Set to True to enable full SMTP validation
        
        app.logger.info(f'SMTP validation: {do_smtp}')
        
        # Set Windows event loop policy if needed
        if sys.platform.startswith('win'):
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            except Exception:
                pass
        
        # Run async validation
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            app.logger.info('Running validation...')
            validated_df = loop.run_until_complete(
                validate_email_list(df, email_col, do_smtp=do_smtp, policy='strict')
            )
            app.logger.info('Validation complete')
        finally:
            loop.close()
        
        # Separate valid and problematic emails
        valid_df = validated_df[validated_df['bounce_risk'] == False].copy()
        problematic_df = validated_df[validated_df['bounce_risk'] == True].copy()
        
        # Store validation results in session (not files) for multi-user safety
        session['validated_df_json'] = validated_df.to_json(orient='records')
        session['valid_count'] = len(valid_df)
        session['problematic_count'] = len(problematic_df)
        
        app.logger.info(f'Results: {len(valid_df)} valid, {len(problematic_df)} problematic')
        
        # Return summary
        return jsonify({
            'success': True,
            'total': len(df),
            'valid': len(valid_df),
            'problematic': len(problematic_df)
        })
        
    except Exception as e:
        app.logger.error(f'Validation error: {str(e)}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/review')
def review_emails():
    """Review and manually approve/reject problematic emails"""
    if 'validated_df_json' not in session:
        flash('Please validate your email list first', 'warning')
        return redirect(url_for('index'))
    
    # Load validation results from session
    validated_df = pd.read_json(session['validated_df_json'], orient='records')
    
    # Separate valid and problematic
    valid_df = validated_df[validated_df['bounce_risk'] == False]
    problematic_df = validated_df[validated_df['bounce_risk'] == True]
    
    # Convert problematic emails to list for template
    email_col = session['email_col']
    problematic_emails = []
    for idx, row in problematic_df.iterrows():
        reasons = row.get('reasons', '')
        # Handle NaN values from pandas
        if pd.isna(reasons) or reasons == '':
            reasons = 'No specific issues detected'
        
        problematic_emails.append({
            'idx': idx,
            'email': row[email_col],
            'reasons': str(reasons),
            'suggestion': row.get('suggestion') if not pd.isna(row.get('suggestion')) else None,
            'catch_all': row.get('catch_all', 'unknown'),
        })
    
    return render_template('review.html',
                         valid_count=len(valid_df),
                         problematic_count=len(problematic_df),
                         problematic_emails=problematic_emails)


@app.route('/set_email_selection', methods=['POST'])
def set_email_selection():
    """Save selected emails (valid + manually approved problematic ones)"""
    try:
        # Get which problematic emails to include (sent as JSON)
        data = request.get_json()
        approved_indices = data.get('approved_indices', [])
        
        # Load validation results from session
        validated_df = pd.read_json(session['validated_df_json'], orient='records')
        
        # Start with valid emails
        valid_df = validated_df[validated_df['bounce_risk'] == False].copy()
        
        # Add manually approved problematic emails
        if approved_indices:
            approved_df = validated_df.iloc[approved_indices]
            final_df = pd.concat([valid_df, approved_df], ignore_index=True)
        else:
            final_df = valid_df
        
        # Store final selected list in session
        session['final_df_json'] = final_df.to_json(orient='records')
        session['valid_count'] = len(final_df)
        
        return jsonify({
            'success': True,
            'total': len(final_df),
            'message': f'Selected {len(final_df)} emails for campaign'
        })
    except Exception as e:
        app.logger.error(f'Selection error: {str(e)}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/login/google')
def login_google():
    try:
        auth_url, state = generate_auth_url()
        session['oauth_state'] = state
        return redirect(auth_url)
    except Exception as e:
        flash(f'Google login setup error: {e}', 'error')
        return redirect(url_for('compose_email'))


@app.route('/oauth2/callback')
def oauth2_callback():
    try:
        code = request.args.get('code')
        if not code:
            flash('Missing authorization code.', 'error')
            return redirect(url_for('compose_email'))

        creds = exchange_code(code)
        email = get_profile_email(creds)
        save_credentials(email, creds)
        session['google_email'] = email
        flash(f'Connected to Google as {email}', 'success')
    except Exception as e:
        flash(f'Google OAuth error: {e}', 'error')
    return redirect(url_for('compose_email'))


@app.route('/logout/google')
def logout_google():
    session.pop('google_email', None)
    flash('Disconnected Google account.', 'info')
    return redirect(url_for('compose_email'))


@app.route('/compose')
def compose_email():
    """Email composition page"""
    if 'final_df_json' not in session:
        flash('Please select emails first', 'warning')
        return redirect(url_for('index'))
    
    return render_template('compose.html',
                         valid_count=session.get('valid_count'),
                         name_col=session.get('name_col'))


@app.route('/preview', methods=['POST'])
def preview_email():
    """Preview email before sending"""
    subject = request.form.get('subject')
    html_content = request.form.get('html_content')
    text_content = request.form.get('text_content')
    
    # Store in session
    session['subject'] = subject
    session['html_content'] = html_content
    session['text_content'] = text_content
    
    # Generate preview with sample name
    preview_html = html_content.replace('{{FirstName}}', 'John').replace('{{Name}}', 'John')
    preview_text = text_content.replace('{{FirstName}}', 'John').replace('{{Name}}', 'John')
    
    return render_template('preview.html',
                         subject=subject,
                         html_preview=preview_html,
                         text_preview=preview_text,
                         valid_count=session.get('valid_count'))


@app.route('/send', methods=['POST'])
def send_emails():
    """Send email campaign via Gmail API (Google OAuth)"""
    google_email = session.get('google_email')
    if not google_email:
        flash('Please sign in with Google to send emails.', 'error')
        return redirect(url_for('compose_email'))

    creds = load_credentials(google_email)
    if not creds:
        flash('Google session expired. Please sign in again.', 'error')
        return redirect(url_for('compose_email'))

    try:
        creds = ensure_valid_credentials(creds)
        save_credentials(google_email, creds)  # persist refreshed token

        # Load final email list from session
        df = pd.read_json(session['final_df_json'], orient='records')
        
        email_col = session['email_col']
        name_col = session.get('name_col')
        subject = session['subject']
        html_content = session['html_content']
        text_content = session['text_content']
        
        # Send campaign through Gmail API
        results = send_email_campaign_gmail(
            df=df,
            email_col=email_col,
            name_col=name_col,
            subject=subject,
            html_content=html_content,
            text_content=text_content,
            credentials=creds,
        )
        
        session['send_results'] = results
        session['sender_email'] = google_email
        
        return redirect(url_for('results'))
        
    except Exception as e:
        flash(f'Error sending emails: {str(e)}', 'error')
        return redirect(url_for('compose_email'))


@app.route('/results')
def results():
    """Display campaign results"""
    if 'send_results' not in session:
        flash('No campaign results available', 'warning')
        return redirect(url_for('index'))
    
    return render_template('results.html', results=session['send_results'])


@app.route('/reset')
def reset():
    """Clear session and start over"""
    session.clear()
    flash('Session cleared. You can start a new campaign.', 'info')
    return redirect(url_for('index'))


@app.template_filter('datetime')
def format_datetime(value):
    """Template filter for datetime formatting"""
    if isinstance(value, str):
        return value
    return value.strftime('%Y-%m-%d %H:%M:%S')


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO)
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)
