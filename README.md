# Email Campaign Manager

A web application for sending personalized marketing emails with built-in email validation.

## Features

- 📤 **CSV Upload**: Upload contact lists with email addresses and names
- ✅ **Email Validation**: Automatic validation using DNS/MX checks and SMTP verification
- ✉️ **Email Composer**: Create HTML and plain text emails with personalization
- 📊 **Campaign Analytics**: Track sent, failed, and bounced emails
- 🎨 **Modern UI**: Clean, responsive interface with progress tracking

## Installation

1. **Clone or download this repository**

2. **Install Python dependencies**:
```bash
pip install -r requirements.txt
```

3. **Configure environment variables**:
   - Copy `.env.example` to `.env`
   - Update with your SMTP credentials

```env
SMTP_USER=your-email@gmail.com
SMTP_PASS=your-app-specific-password
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SECRET_KEY=your-random-secret-key
```

## Usage

1. **Start the application**:
```bash
python app.py
```

2. **Open your browser** to `http://localhost:5000`

3. **Follow the workflow**:
   - Upload a CSV file with email addresses
   - Configure which columns contain emails and names
   - Wait for email validation to complete
   - Compose your email (HTML + plain text)
   - Preview and send!

## CSV Format

Your CSV should include:
- **Email column** (required): e.g., `Email`, `email`, `E-mail`
- **Name column** (optional): e.g., `FirstName`, `Name`, `Contact`

Example CSV:
```csv
FirstName,Email
John,john@example.com
Jane,jane@example.com
```

## Email Personalization

Use these placeholders in your email content:
- `{{FirstName}}` or `{{Name}}` - Will be replaced with the recipient's name

## SMTP Setup (Gmail)

If using Gmail:
1. Enable 2-factor authentication
2. Generate an App Password at https://myaccount.google.com/apppasswords
3. Use the app password in your `.env` file

## Security Notes

- Never commit your `.env` file to version control
- Use app-specific passwords, not your main account password
- Consider rate limiting for large campaigns
- Always provide unsubscribe options in emails

## Technology Stack

- **Backend**: Flask (Python)
- **Email Validation**: asyncio, dnspython, email-validator
- **Email Sending**: smtplib with SSL
- **Frontend**: HTML, CSS, JavaScript
- **Data Processing**: pandas

## License

MIT License - feel free to use and modify for your needs.

## Support

For issues or questions, please create an issue in the repository.
