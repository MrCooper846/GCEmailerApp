# Ubuntu 24.04 deployment

Install PostgreSQL, Redis, Nginx, Certbot, Python 3.12 and build tools. Create an unprivileged
`gcemailer` service account, `/opt/gc-emailer`, `/etc/gc-emailer`, `/var/lib/gc-emailer`, and
`/var/log/gc-emailer`. Keep environment files mode `0600` and owned by the service account.

For each environment:

1. Deploy the same tested revision beneath `/opt/gc-emailer/<environment>/current`.
2. Create a virtualenv and install `requirements.txt`.
3. Create a separate PostgreSQL database, Redis DB, storage root and environment file.
4. Run `flask --app app db upgrade` and `flask --app app seed-admins`.
5. Run `flask --app app import-legacy-templates --actor-email ADMIN` once in production.
6. Install and enable the corresponding web, worker and retention units.
7. Install the Nginx site, issue the certificate with Certbot, and verify renewal.
8. Permit only SSH, HTTP and HTTPS in the IONOS firewall.

Staging uses `APP_ENV=staging`, port 8001, its own OAuth client, and a restrictive
`STAGING_SEND_ALLOWLIST`. Production uses `APP_ENV=production` and port 8000.

Back up before deployment. Afterwards, check readiness, run tests, sign in with two office
accounts, perform an allowed test send, and confirm cross-user campaign access fails. Roll back
the release symlink when code fails; restore the database only for incompatible migrations.
