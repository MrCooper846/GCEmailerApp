"""Administrative CLI commands for migrations and maintenance."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from pathlib import Path

import click
from flask import current_app
from PIL import Image

from extensions import db
from models import EmailTemplate, TemplateAsset, User
from production_services import sanitize_email_html


def register_cli(app):
    @app.cli.command("seed-admins")
    def seed_admins():
        """Create/promote administrators listed in INITIAL_ADMIN_EMAILS."""
        for email in current_app.config["INITIAL_ADMIN_EMAILS"]:
            user = User.query.filter_by(email=email).first()
            if not user:
                user = User(email=email, display_name=email.split("@", 1)[0], role="admin")
                db.session.add(user)
            user.role = "admin"
            user.enabled = True
        db.session.commit()
        click.echo("Initial administrators are ready.")

    @app.cli.command("import-legacy-templates")
    @click.option("--template-dir", default="email_templates", type=click.Path(path_type=Path))
    @click.option("--asset-dir", default="email_assets", type=click.Path(path_type=Path))
    @click.option("--actor-email", required=True)
    def import_legacy_templates(template_dir: Path, asset_dir: Path, actor_email: str):
        """Import the local JSON/CID library once into persistent storage."""
        actor = User.query.filter_by(email=actor_email.lower()).first()
        if not actor or not actor.can_edit_templates:
            raise click.ClickException("Actor must already be an admin or template editor.")
        target_dir = Path(current_app.config["EMAIL_ASSET_FOLDER"])
        target_dir.mkdir(parents=True, exist_ok=True)
        imported_templates = imported_assets = 0
        for path in sorted(template_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            name = str(data.get("name") or path.stem)[:100]
            if EmailTemplate.query.filter_by(name=name, deleted_at=None).first():
                continue
            record = EmailTemplate(
                name=name,
                subject=str(data.get("subject") or "")[:998],
                html_content=sanitize_email_html(str(data.get("html_content") or "")),
                text_content=str(data.get("text_content") or ""),
                created_by_id=actor.id,
                updated_by_id=actor.id,
            )
            db.session.add(record)
            imported_templates += 1
        for path in sorted(asset_dir.iterdir() if asset_dir.is_dir() else []):
            if not path.is_file() or TemplateAsset.query.filter_by(storage_name=path.name).first():
                continue
            try:
                with Image.open(path) as image:
                    width, height = image.size
                    image.verify()
            except OSError:
                continue
            target = target_dir / path.name
            shutil.copy2(path, target)
            raw = target.read_bytes()
            db.session.add(TemplateAsset(
                storage_name=path.name, original_name=path.name,
                mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                width=width, height=height, byte_size=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                uploaded_by_id=actor.id,
            ))
            imported_assets += 1
        db.session.flush()
        for template in EmailTemplate.query.filter_by(deleted_at=None).all():
            references = set(re.findall(r"cid:([A-Za-z0-9][A-Za-z0-9_.-]*)", template.html_content))
            if references:
                for asset in TemplateAsset.query.filter(TemplateAsset.storage_name.in_(references)).all():
                    if asset.template_id is None:
                        asset.template_id = template.id
        db.session.commit()
        click.echo(f"Imported {imported_templates} templates and {imported_assets} assets.")

    @app.cli.command("retention-cleanup")
    def retention_cleanup():
        from tasks import retention_cleanup_task
        click.echo(json.dumps(retention_cleanup_task(), indent=2))
