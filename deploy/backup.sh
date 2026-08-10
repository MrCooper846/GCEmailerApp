#!/usr/bin/env bash
set -euo pipefail
backup_root="${BACKUP_ROOT:-/var/backups/gc-emailer}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$backup_root"
pg_dump --format=custom --file="$backup_root/database-$stamp.dump" "$DATABASE_URL"
tar -C "$STORAGE_ROOT" -czf "$backup_root/assets-$stamp.tar.gz" assets
find "$backup_root" -type f -mtime +30 -delete
