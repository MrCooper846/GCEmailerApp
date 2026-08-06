"""
Marketing Email Campaign Web Application
Combines CSV upload, email validation, and personalized email sending
"""
import os
import asyncio
import json
import re
import threading
import uuid
import hashlib
from io import StringIO
from pathlib import Path
from datetime import datetime
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, jsonify, send_from_directory, g)
from flask_session import Session
from redis import Redis
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
import pandas as pd
from dotenv import load_dotenv

# Load environment variables before importing services that read them at module load time.
load_dotenv()

from email_validator_service import validate_email_list
from email_sender_service import send_email_campaign, render_placeholders
from google_oauth_service import (
    generate_auth_url,
    exchange_code,
    save_credentials,
    load_credentials,
    ensure_valid_credentials,
    get_profile_email,
    get_profile,
)
from gmail_sender_service import send_email_campaign_gmail
from config import get_config
from extensions import csrf, db, limiter, migrate
from models import User, utcnow
from models import EmailTemplate, TemplateAsset
from production_services import (audit, current_user, request_id as make_request_id,
                                 sanitize_email_html, save_encrypted_credentials)
from flask_wtf.csrf import CSRFError

send_jobs = {}
send_jobs_lock = threading.Lock()

app = Flask(__name__)
config_class = get_config()
app.config.from_object(config_class)
configuration_errors = config_class.validate()
if configuration_errors:
    raise RuntimeError("Invalid production configuration: " + ", ".join(configuration_errors))
app.config['SESSION_FILE_DIR'] = str(Path(app.instance_path) / 'sessions')
if app.config['SESSION_TYPE'] == 'redis':
    app.config['SESSION_REDIS'] = Redis.from_url(app.config['REDIS_URL'])
app.config['ALLOWED_EXTENSIONS'] = {'csv'}

if app.config.get('TRUST_PROXY'):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

Path(app.config['SESSION_FILE_DIR']).mkdir(parents=True, exist_ok=True)
Session(app)
db.init_app(app)
migrate.init_app(app, db)
csrf.init_app(app)
limiter.init_app(app)
from production_api import production
app.register_blueprint(production)
from cli import register_cli
register_cli(app)


@app.before_request
def load_authenticated_user():
    """Resolve the small session identity and fail closed in hosted environments."""
    from flask import g

    g.current_user = None
    g.request_id = make_request_id()
    user_id = session.get('user_id')
    if user_id:
        user = db.session.get(User, user_id)
        if user and user.enabled:
            g.current_user = user
        else:
            session.clear()

    if not app.config.get('REQUIRE_AUTH') or g.current_user:
        return None
    if request.endpoint in {
        'static', 'login_google', 'oauth2_callback',
        'production.login_page', 'production.health_live', 'production.health_ready',
    }:
        return None
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Authentication required.'}), 401
    return redirect(url_for('production.login_page', next=request.full_path))


@app.before_request
def disable_legacy_hosted_workflow():
    """Hosted environments use persistent APIs, never session/DataFrame routes."""
    if not app.config.get('REQUIRE_AUTH'):
        return None
    legacy_endpoints = {
        'upload_csv', 'configure_columns', 'set_columns', 'validate_emails', 'api_validate',
        'review_emails', 'set_email_selection', 'compose_email', 'preview_email', 'send_emails',
        'send_progress', 'results', 'reset',
    }
    if request.endpoint not in legacy_endpoints:
        return None
    if request.path.startswith('/api/') or request.method != 'GET':
        return jsonify({'success': False, 'error': 'This legacy endpoint is disabled in hosted environments.'}), 410
    return redirect(url_for('index'))


@app.after_request
def apply_security_headers(response):
    """Apply browser hardening without exposing recipient data through caches."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Request-ID'] = getattr(g, 'request_id', '')
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; img-src 'self' data: cid:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; frame-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self' https://accounts.google.com"
    )
    if app.config.get('SESSION_COOKIE_SECURE'):
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'The request expired. Refresh and try again.',
                        'request_id': getattr(g, 'request_id', None)}), 400
    flash('The form expired. Refresh and try again.', 'error')
    return redirect(request.referrer or url_for('index'))


@app.errorhandler(403)
def handle_forbidden(error):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'You do not have permission for this action.',
                        'request_id': getattr(g, 'request_id', None)}), 403
    return render_template('error.html', message='You do not have permission for this page.'), 403


@app.errorhandler(500)
def handle_internal_error(error):
    db.session.rollback()
    app.logger.exception('Unhandled request error [%s]', getattr(g, 'request_id', 'unknown'))
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'The request could not be completed.',
                        'request_id': getattr(g, 'request_id', None)}), 500
    return render_template('error.html', message='The request could not be completed.'), 500

# SMTP Configuration from .env
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
VALIDATION_MAIL_FROM = os.getenv("VALIDATION_MAIL_FROM", "").strip()
VALIDATION_ENABLE_SMTP = os.getenv("VALIDATION_ENABLE_SMTP", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
VALIDATION_POLICY = os.getenv("VALIDATION_POLICY", "balanced").strip().lower()
if VALIDATION_POLICY not in {"strict", "balanced", "relaxed"}:
    VALIDATION_POLICY = "balanced"

# Create uploads directory
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['EMAIL_TEMPLATE_FOLDER'], exist_ok=True)
os.makedirs(app.config['EMAIL_ASSET_FOLDER'], exist_ok=True)


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


def list_email_templates() -> list:
    """Return valid saved templates without exposing their file paths."""
    if app.config.get('REQUIRE_AUTH'):
        return [
            {'id': item.id, 'name': item.name, 'version': item.version,
             'updated_by': item.updater.email if item.updater else None}
            for item in EmailTemplate.query.filter_by(deleted_at=None).order_by(EmailTemplate.name).all()
        ]
    templates = []
    folder = Path(app.config['EMAIL_TEMPLATE_FOLDER'])
    folder.mkdir(parents=True, exist_ok=True)
    for path in sorted(folder.glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            templates.append({
                'id': path.stem,
                'name': str(data.get('name') or path.stem),
            })
        except (OSError, json.JSONDecodeError):
            app.logger.warning('Skipping invalid email template: %s', path.name)
    return templates


def template_path(template_id: str) -> Path:
    """Resolve a sanitized template identifier inside the configured folder."""
    safe_id = secure_filename(template_id or '').strip('._')
    if not safe_id or safe_id != template_id:
        raise ValueError('Invalid template identifier.')
    return Path(app.config['EMAIL_TEMPLATE_FOLDER']) / f'{safe_id}.json'


def prepare_template_payload(data: dict) -> tuple:
    """Validate template input and return its safe ID and serialized fields."""
    name = str(data.get('name') or '').strip()
    subject = str(data.get('subject') or '').strip()
    html_content = str(data.get('html_content') or '')
    if app.config.get('REQUIRE_AUTH'):
        html_content = sanitize_email_html(html_content)
    text_content = str(data.get('text_content') or '')

    if not name:
        raise ValueError('Template name is required.')
    if len(name) > 100:
        raise ValueError('Template name must be 100 characters or fewer.')
    if not subject or not html_content or not text_content:
        raise ValueError('Subject, HTML, and plain text are required.')

    template_id = secure_filename(name).strip('._')
    if not template_id:
        raise ValueError('Template name must contain letters or numbers.')

    return template_id, {
        'name': name,
        'subject': subject,
        'html_content': html_content,
        'text_content': text_content,
        'updated_at': datetime.now().isoformat(timespec='seconds'),
    }


def write_email_template(path: Path, payload: dict):
    """Atomically write one template JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix('.json.tmp')
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary_path.replace(path)


def link_template_assets(record: EmailTemplate):
    """Associate persisted CID assets so referenced files cannot be orphan-cleaned."""
    referenced = set(re.findall(r'cid:([A-Za-z0-9][A-Za-z0-9_.-]*)', record.html_content, re.IGNORECASE))
    for asset in TemplateAsset.query.filter_by(template_id=record.id).all():
        if asset.storage_name not in referenced:
            asset.template_id = None
    if referenced:
        for asset in TemplateAsset.query.filter(TemplateAsset.storage_name.in_(referenced)).all():
            if asset.template_id in {None, record.id}:
                asset.template_id = record.id


def asset_path(asset_id: str) -> Path:
    """Resolve an uploaded inline image inside the configured asset folder."""
    safe_id = secure_filename(asset_id or '').strip('._')
    if not safe_id or safe_id != asset_id:
        raise ValueError('Invalid image identifier.')
    return Path(app.config['EMAIL_ASSET_FOLDER']) / safe_id


def inline_images_for_preview(html: str) -> str:
    """Replace CID image references with local URLs for browser preview."""
    def replace(match):
        asset_id = match.group(1)
        try:
            if asset_path(asset_id).is_file():
                return url_for('serve_email_asset', asset_id=asset_id)
        except ValueError:
            pass
        return match.group(0)

    return re.sub(r'cid:([A-Za-z0-9][A-Za-z0-9_.-]*)', replace, html, flags=re.IGNORECASE)


def build_review_details(row: pd.Series) -> dict:
    """Turn validator fields into a useful, human-readable review explanation."""
    raw_reasons = row.get('reasons', '')
    if pd.isna(raw_reasons):
        raw_reasons = ''
    reason_codes = [reason for reason in str(raw_reasons).split(',') if reason]

    reason_labels = {
        'invalid_syntax': 'The email address has invalid syntax.',
        'no_mx': 'No working mail server (MX record) was found for the domain.',
        'domain_not_found': 'The email domain does not exist (NXDOMAIN).',
        'null_mx': 'The domain explicitly states that it does not accept email (null MX).',
        'no_mail_route': 'No MX, A, or AAAA mail route was found for the domain.',
        'dns_temporary_failure': 'The DNS check still failed after retrying; this is inconclusive.',
        'disposable_domain': 'The domain appears to provide disposable email addresses.',
        'likely_typo_domain': 'The domain looks like a possible spelling mistake.',
        'role_address': 'This is a role-based address rather than a named mailbox.',
        'catch_all_domain': 'The domain accepts mail for unverified mailbox names.',
    }
    issues = [reason_labels.get(reason, reason.replace('_', ' ').capitalize())
              for reason in reason_codes]

    def safe_int(value) -> int:
        return 0 if value is None or pd.isna(value) else int(value)

    normalized = row.get('normalized')
    syntax_ok = normalized is not None and not pd.isna(normalized) and bool(str(normalized).strip())
    mx_value = row.get('mx_ok', False)
    mx_ok = False if mx_value is None or pd.isna(mx_value) else bool(mx_value)
    dns_status = str(row.get('dns_status', 'unknown') or 'unknown').lower()
    dns_attempts = safe_int(row.get('dns_attempts', 0))
    smtp_status = str(row.get('smtp_status', 'not_tested') or 'not_tested').lower()
    smtp_attempts = safe_int(row.get('smtp_attempts', 0))
    smtp_code = row.get('smtp_code')
    smtp_msg = row.get('smtp_msg')
    catch_all = str(row.get('catch_all', 'unknown') or 'unknown').lower()

    if not syntax_ok:
        progress = 'Syntax check failed; DNS and SMTP checks were not attempted.'
    elif dns_status == 'temporary_failure':
        progress = f'Syntax passed; DNS remained unavailable after {dns_attempts} attempts; SMTP was not attempted.'
    elif dns_status == 'implicit_mx':
        progress = 'Syntax passed; no MX existed, but an A/AAAA fallback mail route was found.'
    elif not mx_ok:
        progress = f'Syntax passed; DNS returned “{dns_status}”; SMTP was not attempted.'
    elif smtp_status == 'not_tested':
        progress = 'Syntax and MX/DNS checks passed, but the SMTP mailbox check was not completed.'
    else:
        progress = f'Syntax and MX/DNS checks passed; SMTP check returned “{smtp_status}”.'
    if smtp_attempts:
        progress += f' SMTP attempts: {smtp_attempts}.'

    smtp_labels = {
        'invalid': 'The recipient server rejected this mailbox during the SMTP check.',
        'blocked': 'The recipient server blocked the verification attempt; the address may still work.',
        'tempfail': 'The recipient server reported a temporary failure; the address may still work later.',
        'mailbox_full': 'The recipient mailbox reported that it is full.',
        'error': 'The SMTP mailbox check could not complete; this does not prove the address is invalid.',
        'unknown': 'The SMTP result was inconclusive; this does not prove the address is invalid.',
        'not_tested': 'The SMTP mailbox check was not completed.',
    }
    if smtp_status in smtp_labels:
        smtp_issue = smtp_labels[smtp_status]
        if smtp_status == 'invalid' and smtp_code is not None and not pd.isna(smtp_code):
            smtp_issue += f' (SMTP {int(smtp_code)})'
        if smtp_msg is not None and not pd.isna(smtp_msg):
            response_text = str(smtp_msg).strip().replace('\r', ' ').replace('\n', ' ')
            if response_text:
                smtp_issue += f' Last response: {response_text[:240]}'
        issues.append(smtp_issue)

    if catch_all == 'yes' and 'catch_all_domain' not in reason_codes:
        issues.append('The domain is catch-all, so the specific mailbox could not be confirmed.')

    return {
        'issues': issues or ['The checks were inconclusive; no definite fault was identified.'],
        'progress': progress,
        'dns_status': dns_status,
        'smtp_status': smtp_status,
    }


@app.route('/')
def index():
    """Home page - upload CSV"""
    if app.config.get('REQUIRE_AUTH'):
        return render_template('dashboard.html', saved_templates=list_email_templates(),
                               max_recipients=app.config['MAX_CAMPAIGN_RECIPIENTS'])
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
        df.columns = df.columns.str.strip()  # remove leading/trailing whitespace and newlines

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
    
    return render_template(
        'validate.html',
        total_rows=session.get('total_rows'),
        smtp_available=bool(VALIDATION_MAIL_FROM),
        smtp_default=bool(VALIDATION_ENABLE_SMTP and VALIDATION_MAIL_FROM),
        validation_policy=VALIDATION_POLICY,
    )


@app.route('/api/validate', methods=['POST'])
def api_validate():
    """API endpoint for email validation - returns full results for review"""
    import sys
    try:
        app.logger.info('Starting email validation...')
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], session['csv_file'])
        df = pd.read_csv(filepath)
        df.columns = df.columns.str.strip()  # remove leading/trailing whitespace and newlines
        email_col = session['email_col']
        
        app.logger.info(f'Loaded {len(df)} rows from CSV')
        
        options = request.get_json(silent=True) or {}
        do_smtp = bool(options.get('do_smtp', VALIDATION_ENABLE_SMTP))
        policy = str(options.get('policy') or VALIDATION_POLICY).lower()
        if policy not in {'strict', 'balanced', 'relaxed'}:
            return jsonify({'success': False, 'error': 'Invalid validation policy.'}), 400
        if do_smtp and not VALIDATION_MAIL_FROM:
            return jsonify({
                'success': False,
                'error': 'SMTP verification requires VALIDATION_MAIL_FROM in .env.',
            }), 400
        
        app.logger.info(f'Validation policy: {policy}; SMTP validation: {do_smtp}')
        
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
                validate_email_list(
                    df,
                    email_col,
                    do_smtp=do_smtp,
                    mail_from=VALIDATION_MAIL_FROM,
                    policy=policy,
                )
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
            'problematic': len(problematic_df),
            'smtp_enabled': do_smtp,
            'policy': policy,
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
    validated_df = pd.read_json(StringIO(session['validated_df_json']), orient='records')
    
    # Separate valid and problematic
    valid_df = validated_df[validated_df['bounce_risk'] == False]
    problematic_df = validated_df[validated_df['bounce_risk'] == True]
    
    # Convert problematic emails to list for template
    email_col = session['email_col']
    problematic_emails = []
    for idx, row in problematic_df.iterrows():
        review_details = build_review_details(row)

        problematic_emails.append({
            'idx': idx,
            'email': row[email_col],
            'issues': review_details['issues'],
            'progress': review_details['progress'],
            'smtp_status': review_details['smtp_status'],
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
        if 'validated_df_json' in session:
            validated_df = pd.read_json(StringIO(session['validated_df_json']), orient='records')
        elif session.get('validation_file'):
            validated_df = pd.read_csv(os.path.join(app.config['UPLOAD_FOLDER'], session['validation_file']))
        else:
            return jsonify({'success': False, 'error': 'Validation results are missing.'}), 400
        
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
@limiter.limit("30 per hour")
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

        expected_state = session.pop('oauth_state', None)
        returned_state = request.args.get('state')
        if not expected_state or returned_state != expected_state:
            raise ValueError('The Google sign-in state was invalid or expired. Please try again.')

        creds = exchange_code(
            code,
            state=expected_state,
            authorization_response=request.url,
        )
        if not app.config.get('REQUIRE_AUTH'):
            email = get_profile_email(creds)
            save_credentials(email, creds)
            session['google_email'] = email
            flash(f'Connected to Google as {email}', 'success')
            return redirect(url_for('compose_email'))
        profile = get_profile(creds)
        email = profile['email']
        if not profile['verified_email']:
            raise ValueError('Google did not return a verified email address.')
        allowed_domain = app.config['ALLOWED_GOOGLE_DOMAIN']
        if email.rsplit('@', 1)[-1] != allowed_domain:
            raise ValueError(f'Only @{allowed_domain} accounts may use this application.')

        user = db.session.scalar(db.select(User).where(User.email == email))
        if not user:
            role = 'admin' if email in app.config['INITIAL_ADMIN_EMAILS'] else 'user'
            # Column defaults are applied during INSERT; before the first flush
            # a new model instance still has enabled=None, which the disabled
            # account check below would incorrectly reject.
            user = User(email=email, display_name=profile['name'], role=role, enabled=True)
            db.session.add(user)
        if not user.enabled:
            raise ValueError('This office account has been disabled by an administrator.')
        user.display_name = profile['name'] or user.display_name
        user.last_login_at = utcnow()
        save_encrypted_credentials(user, creds)
        db.session.commit()

        session.clear()
        session['user_id'] = user.id
        session['google_email'] = email
        session.permanent = True
        flash(f'Connected to Google as {email}', 'success')
    except Exception as e:
        flash(f'Google OAuth error: {e}', 'error')
    return redirect(url_for('compose_email'))


@app.route('/logout/google', methods=['GET', 'POST'])
def logout_google():
    if app.config.get('REQUIRE_AUTH') and request.method != 'POST':
        return jsonify({'success': False, 'error': 'Method not allowed.'}), 405
    session.clear()
    flash('Disconnected Google account.' if not app.config.get('REQUIRE_AUTH') else 'Signed out.', 'info')
    return redirect(url_for('production.login_page') if app.config.get('REQUIRE_AUTH') else url_for('index'))


@app.route('/compose')
def compose_email():
    """Email composition page"""
    if 'final_df_json' not in session:
        flash('Please validate your email list first', 'warning')
        return redirect(url_for('index'))
    
    return render_template('compose.html',
                         valid_count=session.get('valid_count'),
                         name_col=session.get('name_col'),
                         saved_templates=list_email_templates())


@app.route('/templates')
def template_manager():
    """Manage reusable templates independently of a campaign session."""
    return render_template('template_manager.html', saved_templates=list_email_templates(),
                           can_edit=(not app.config.get('REQUIRE_AUTH') or current_user().can_edit_templates))


@app.route('/api/templates', methods=['GET', 'POST'])
def save_email_template():
    """List templates or save a new/replacement template on disk."""
    if request.method == 'GET':
        return jsonify({'success': True, 'templates': list_email_templates()})

    if app.config.get('REQUIRE_AUTH') and not current_user().can_edit_templates:
        return jsonify({'success': False, 'error': 'Template editor access is required.'}), 403

    data = request.get_json(silent=True) or {}
    try:
        template_id, payload = prepare_template_payload(data)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    if app.config.get('REQUIRE_AUTH'):
        existing = EmailTemplate.query.filter_by(name=payload['name'], deleted_at=None).first()
        if existing:
            return jsonify({'success': False, 'error': 'A template with that name already exists.'}), 409
        record = EmailTemplate(
            name=payload['name'], subject=payload['subject'], html_content=payload['html_content'],
            text_content=payload['text_content'], created_by_id=current_user().id,
            updated_by_id=current_user().id,
        )
        db.session.add(record)
        db.session.flush()
        link_template_assets(record)
        audit('template.created', 'template', record.id)
        db.session.commit()
        template_id = record.id
        template_version = record.version
    else:
        path = template_path(template_id)
        write_email_template(path, payload)

    return jsonify({
        'success': True,
        'template': {'id': template_id, 'name': payload['name'],
                     'version': template_version if app.config.get('REQUIRE_AUTH') else None},
        'message': f'Saved template “{payload["name"]}”.',
    })


@app.route('/api/templates/<template_id>', methods=['GET', 'PUT', 'DELETE'])
def load_email_template(template_id):
    """Load, update/rename, or delete one reusable email template."""
    if app.config.get('REQUIRE_AUTH'):
        record = db.session.get(EmailTemplate, template_id)
        if not record or record.deleted_at:
            return jsonify({'success': False, 'error': 'Template not found.'}), 404
        if request.method in {'PUT', 'DELETE'} and not current_user().can_edit_templates:
            return jsonify({'success': False, 'error': 'Template editor access is required.'}), 403
        if request.method == 'DELETE':
            record.deleted_at = utcnow()
            audit('template.deleted', 'template', record.id)
            db.session.commit()
            return jsonify({'success': True, 'message': 'Template deleted.'})
        if request.method == 'PUT':
            data = request.get_json(silent=True) or {}
            expected_version = data.get('version')
            if expected_version is None or int(expected_version) != record.version:
                return jsonify({'success': False, 'error': 'Template changed since it was loaded. Refresh and try again.'}), 409
            try:
                _, payload = prepare_template_payload(data)
            except ValueError as exc:
                return jsonify({'success': False, 'error': str(exc)}), 400
            duplicate = EmailTemplate.query.filter(
                EmailTemplate.name == payload['name'], EmailTemplate.id != record.id,
                EmailTemplate.deleted_at.is_(None),
            ).first()
            if duplicate:
                return jsonify({'success': False, 'error': 'A template with that name already exists.'}), 409
            record.name = payload['name']
            record.subject = payload['subject']
            record.html_content = payload['html_content']
            record.text_content = payload['text_content']
            record.version += 1
            record.updated_by_id = current_user().id
            link_template_assets(record)
            audit('template.updated', 'template', record.id, {'version': record.version})
            db.session.commit()
            return jsonify({'success': True, 'template': {'id': record.id, 'name': record.name,
                                                           'version': record.version},
                            'message': f'Updated template “{record.name}”.'})
        return jsonify({'success': True, 'template': {
            'id': record.id, 'name': record.name, 'subject': record.subject,
            'html_content': record.html_content, 'text_content': record.text_content,
            'version': record.version,
        }})

    try:
        path = template_path(template_id)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    if not path.is_file():
        return jsonify({'success': False, 'error': 'Template not found.'}), 404

    if request.method == 'DELETE':
        path.unlink()
        return jsonify({'success': True, 'message': 'Template deleted.'})

    if request.method == 'PUT':
        try:
            new_id, payload = prepare_template_payload(request.get_json(silent=True) or {})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400

        new_path = template_path(new_id)
        if new_path != path and new_path.exists():
            return jsonify({'success': False, 'error': 'A template with that name already exists.'}), 409
        write_email_template(new_path, payload)
        if new_path != path:
            path.unlink()
        return jsonify({
            'success': True,
            'template': {'id': new_id, 'name': payload['name']},
            'message': f'Updated template “{payload["name"]}”.',
        })

    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return jsonify({'success': False, 'error': 'Template file is invalid.'}), 500

    return jsonify({'success': True, 'template': data})


@app.route('/api/template-assets', methods=['POST'])
def upload_email_asset():
    """Upload a PNG, JPEG, or GIF for CID embedding in email HTML."""
    uploaded = request.files.get('image')
    if not uploaded or not uploaded.filename:
        return jsonify({'success': False, 'error': 'Choose an image to upload.'}), 400

    if app.config.get('REQUIRE_AUTH') and not current_user().can_edit_templates:
        return jsonify({'success': False, 'error': 'Template editor access is required.'}), 403

    image_data = uploaded.read(5 * 1024 * 1024 + 1)
    if len(image_data) > 5 * 1024 * 1024:
        return jsonify({'success': False, 'error': 'Image must be 5 MB or smaller.'}), 400

    if image_data.startswith(b'\x89PNG\r\n\x1a\n'):
        extension = 'png'
    elif image_data.startswith(b'\xff\xd8\xff'):
        extension = 'jpg'
    elif image_data.startswith((b'GIF87a', b'GIF89a')):
        extension = 'gif'
    else:
        return jsonify({'success': False, 'error': 'Only PNG, JPEG, and GIF images are supported.'}), 400

    if app.config.get('REQUIRE_AUTH'):
        from PIL import Image, UnidentifiedImageError
        import io
        try:
            with Image.open(io.BytesIO(image_data)) as source:
                source.verify()
            with Image.open(io.BytesIO(image_data)) as source:
                width, height = source.size
                if width * height > 25_000_000:
                    return jsonify({'success': False, 'error': 'Image dimensions are too large.'}), 400
                clean = io.BytesIO()
                save_format = {'png': 'PNG', 'jpg': 'JPEG', 'gif': 'GIF'}[extension]
                source.save(clean, format=save_format)
                image_data = clean.getvalue()
        except (UnidentifiedImageError, OSError):
            return jsonify({'success': False, 'error': 'The uploaded image is invalid.'}), 400

    original_stem = secure_filename(Path(uploaded.filename).stem).strip('._') or 'image'
    asset_id = f'{uuid.uuid4().hex[:12]}_{original_stem}.{extension}'
    path = asset_path(asset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image_data)

    if app.config.get('REQUIRE_AUTH'):
        db.session.add(TemplateAsset(
            storage_name=asset_id, original_name=Path(uploaded.filename).name[:255],
            mime_type=f'image/{"jpeg" if extension == "jpg" else extension}',
            width=width, height=height, byte_size=len(image_data),
            sha256=hashlib.sha256(image_data).hexdigest(), uploaded_by_id=current_user().id,
        ))
        audit('template_asset.uploaded', 'template_asset', asset_id,
              {'mime_type': extension, 'bytes': len(image_data)})
        db.session.commit()

    snippet = (
        f'<img src="cid:{asset_id}" alt="{original_stem.replace("_", " ")}" '
        'style="display:block; max-width:100%; height:auto;">'
    )
    return jsonify({
        'success': True,
        'asset_id': asset_id,
        'snippet': snippet,
        'message': f'Uploaded {uploaded.filename}.',
    })


@app.route('/email-assets/<asset_id>')
def serve_email_asset(asset_id):
    """Serve an uploaded CID image in the browser preview only."""
    try:
        path = asset_path(asset_id)
    except ValueError:
        return 'Invalid image identifier.', 400
    if not path.is_file():
        return 'Image not found.', 404
    return send_from_directory(str(path.parent.resolve()), path.name, max_age=3600)


@app.route('/preview', methods=['POST'])
def preview_email():
    """Preview email before sending"""
    if 'final_df_json' not in session:
        flash('Please validate your email list first', 'warning')
        return redirect(url_for('index'))

    subject = request.form.get('subject')
    html_content = request.form.get('html_content')
    text_content = request.form.get('text_content')
    
    # Store in session
    session['subject'] = subject
    session['html_content'] = html_content
    session['text_content'] = text_content
    
    # Preview the first selected recipient using the same renderer as sending.
    recipients = pd.read_json(StringIO(session['final_df_json']), orient='records')
    first_recipient = recipients.iloc[0] if not recipients.empty else pd.Series(dtype=object)
    email_col = session.get('email_col')
    name_col = session.get('name_col')

    sample_email = ''
    if email_col and email_col in recipients.columns:
        email_value = first_recipient.get(email_col)
        if email_value is not None and not pd.isna(email_value):
            sample_email = str(email_value).strip()

    sample_name = ''
    if name_col and name_col in recipients.columns:
        name_value = first_recipient.get(name_col)
        if name_value is not None and not pd.isna(name_value):
            sample_name = str(name_value).strip()

    preview_subject = render_placeholders(subject, sample_name)
    preview_html = inline_images_for_preview(render_placeholders(html_content, sample_name))
    preview_text = render_placeholders(text_content, sample_name)

    return render_template('preview.html',
                         subject=preview_subject,
                         html_preview=preview_html,
                         text_preview=preview_text,
                         sample_email=sample_email,
                         sample_name=sample_name,
                         valid_count=session.get('valid_count'))


@app.route('/send', methods=['POST'])
def send_emails():
    """Start a Gmail campaign in the background and return its job ID."""
    is_async_request = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    google_email = session.get('google_email')
    if not google_email:
        message = 'Please sign in with Google to send emails.'
        if is_async_request:
            return jsonify({'success': False, 'error': message}), 401
        flash(message, 'error')
        return redirect(url_for('compose_email'))

    creds = load_credentials(google_email)
    if not creds:
        message = 'Google session expired. Please sign in again.'
        if is_async_request:
            return jsonify({'success': False, 'error': message}), 401
        flash(message, 'error')
        return redirect(url_for('compose_email'))

    try:
        creds = ensure_valid_credentials(creds)
        save_credentials(google_email, creds)  # persist refreshed token

        # Load final email list from session
        df = pd.read_json(StringIO(session['final_df_json']), orient='records')
        
        email_col = session['email_col']
        name_col = session.get('name_col')
        subject = session['subject']
        html_content = session['html_content']
        text_content = session['text_content']

        job_id = uuid.uuid4().hex
        with send_jobs_lock:
            send_jobs[job_id] = {
                'status': 'queued',
                'current': 0,
                'total': len(df),
                'message': 'Preparing campaign...',
                'results': None,
                'error': None,
                'sender_email': google_email,
            }
        session['send_job_id'] = job_id

        worker = threading.Thread(
            target=run_send_job,
            args=(job_id, df, email_col, name_col, subject, html_content,
                  text_content, creds, google_email, app.config['EMAIL_ASSET_FOLDER']),
            daemon=True,
        )
        worker.start()

        return jsonify({'success': True, 'job_id': job_id})
    except Exception as e:
        app.logger.error(f'Error starting email campaign: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def run_send_job(job_id, df, email_col, name_col, subject, html_content,
                 text_content, creds, google_email, inline_image_folder):
    """Send a campaign and record progress for the polling endpoint."""
    def update_progress(current, total, message):
        with send_jobs_lock:
            job = send_jobs.get(job_id)
            if job:
                job.update({
                    'status': 'running',
                    'current': current,
                    'total': total,
                    'message': message,
                })

    try:
        results = send_email_campaign_gmail(
            df=df,
            email_col=email_col,
            name_col=name_col,
            subject=subject,
            html_content=html_content,
            text_content=text_content,
            credentials=creds,
            inline_image_folder=inline_image_folder,
            progress_callback=update_progress,
        )
        with send_jobs_lock:
            job = send_jobs.get(job_id)
            if job:
                job.update({
                    'status': 'complete',
                    'current': job.get('total', len(df)),
                    'message': 'Campaign complete.',
                    'results': results,
                    'sender_email': google_email,
                })
    except Exception as e:
        app.logger.error(f'Campaign {job_id} failed: {e}', exc_info=True)
        with send_jobs_lock:
            job = send_jobs.get(job_id)
            if job:
                job.update({
                    'status': 'failed',
                    'message': 'Campaign stopped because of an error.',
                    'error': str(e),
                })


@app.route('/api/send-progress/<job_id>')
def send_progress(job_id):
    """Return progress for the current browser session's send job."""
    if session.get('send_job_id') != job_id:
        return jsonify({'success': False, 'error': 'Send job not found.'}), 404

    with send_jobs_lock:
        job = send_jobs.get(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Send job not found.'}), 404
        payload = {
            'success': True,
            'status': job['status'],
            'current': job['current'],
            'total': job['total'],
            'message': job['message'],
            'error': job['error'],
        }
    return jsonify(payload)


@app.route('/results')
def results():
    """Display campaign results"""
    job_id = request.args.get('job_id') or session.get('send_job_id')
    if not job_id or session.get('send_job_id') != job_id:
        flash('No campaign results available', 'warning')
        return redirect(url_for('index'))

    with send_jobs_lock:
        job = send_jobs.get(job_id)
        if not job or job.get('status') != 'complete' or not job.get('results'):
            flash('Campaign results are not ready yet', 'warning')
            return redirect(url_for('compose_email'))
        results_data = dict(job['results'])

    return render_template('results.html', results=results_data)


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


def create_app():
    """WSGI/application-factory entry point configured by APP_ENV at process start."""
    return app


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO)
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)
