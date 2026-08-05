# Restore runbook

1. Stop the web and worker units and copy the current database/storage.
2. Create an empty PostgreSQL database with the production owner.
3. Restore with `pg_restore --clean --if-exists --no-owner --dbname="$DATABASE_URL" database.dump`.
4. Restore the asset archive beneath `$STORAGE_ROOT/assets` and restore service ownership.
5. Run `flask --app app db upgrade`, start services, and verify `/health/ready`.
6. Verify users, templates, suppressions and recent campaign totals as an administrator.
7. Perform a test send before reopening campaign sending.
