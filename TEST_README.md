# Test Configuration and Running

## Running Tests

### Run all tests:
```bash
python -m unittest discover -s . -p "test_*.py" -v
```

### Run specific test file:
```bash
python -m unittest test_app.py -v
python -m unittest test_integration.py -v
```

### Run specific test class:
```bash
python -m unittest test_app.TestEmailValidationService -v
```

### Run specific test method:
```bash
python -m unittest test_app.TestEmailValidationService.test_extract_first_email -v
```

### Run with coverage:
```bash
pip install coverage
coverage run -m unittest discover
coverage report
coverage html  # generates htmlcov/index.html
```

## Test Structure

### `test_app.py` - Unit Tests
- **TestEmailValidationService**: Email validation functions
  - Email extraction, typo detection, bounce risk computation
  - Token bucket rate limiting
  - SQLite cache operations (email and MX records)

- **TestEmailSenderService**: Email message building
  - Message creation with correct headers
  - Personalization with placeholders

- **TestFlaskApp**: Flask routes
  - Home page, file upload, configuration
  - Google OAuth login/logout
  - Session management

- **TestGoogleOAuth**: OAuth credential handling
  - Client config structure
  - Credential storage/retrieval

- **TestDataProcessing**: Data manipulation
  - DataFrame creation, filtering by bounce risk

### `test_integration.py` - Integration Tests
- **TestEmailCampaignWorkflow**: Full workflow tests
  - Upload → Configure → Validate → Review → Compose → Send
  - Page access requirements and redirects

- **TestEmailSelectionFlow**: Email selection logic
  - Approving/rejecting problematic emails

- **TestErrorHandling**: Edge cases and error scenarios
  - Invalid file types, empty files, malformed CSVs
  - Missing required fields

## Coverage Goals

- **Services** (validators, senders, OAuth): 85%+
- **Routes** (Flask app): 70%+
- **Templates** (HTML): Manual testing only
- **Integration**: Key workflows covered

## CI/CD Integration

For GitHub Actions, add `.github/workflows/tests.yml`:
```yaml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: 3.12
      - run: pip install -r requirements.txt
      - run: python -m unittest discover
```

## Notes

- Tests use `tempfile` for isolated file operations
- Mocking is used for Google OAuth and SMTP calls
- Database tests use temporary SQLite files
- Flask tests run with `TESTING=True` (disables error catching during request handling)
