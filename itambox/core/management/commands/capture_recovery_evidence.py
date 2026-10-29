import hashlib
import hmac
import json
import os
import re

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from core.models import EmailSettings
from extras.models import FileAttachment, WebhookEndpoint
from licenses.models import License
from organization.access import accessible_tenant_ids
from users.models import Token

_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def evidence_hmac(key, label, value):
    """Return a domain-separated HMAC for a protected evidence value."""
    return hmac.new(
        key,
        label.encode("ascii") + b"\x00" + value,
        hashlib.sha256,
    ).hexdigest()


def validate_revision(revision):
    """Require an immutable full lowercase Git object name."""
    if not _FULL_GIT_SHA_RE.fullmatch(revision):
        raise ValidationError("revision must be a full lowercase 40-character Git SHA")
    return revision


def validate_probe_context(license_obj, webhook, attachment, token):
    """Fail closed unless every tenant-bound canary is coherent and usable."""
    if token is None:
        raise ValidationError("recovery API token was not found")
    if token.is_expired:
        raise ValidationError("recovery API token is expired")
    if not token.user.is_active:
        raise ValidationError("recovery API token user is inactive")
    if token.tenant_id is None:
        raise ValidationError("recovery API token tenant is required")
    if token.tenant.deleted_at is not None:
        raise ValidationError("recovery API token tenant is inactive")
    if not token.user.is_superuser:
        if token.tenant_id not in accessible_tenant_ids(token.user):
            raise ValidationError(
                "recovery API token user cannot access its tenant",
            )

    attachment_target = attachment.content_object
    tenant_ids = (
        license_obj.tenant_id,
        webhook.tenant_id,
        getattr(attachment_target, "tenant_id", None),
        token.tenant_id,
    )
    if None in tenant_ids or len(set(tenant_ids)) != 1:
        raise ValidationError(
            "recovery canaries and API token must belong to the same tenant",
        )


def build_recovery_evidence(
    *,
    revision,
    probe_key,
    protected_values,
    api_token_verified,
    media_name,
    media_content,
    counts,
    postgresql_version_num,
    applied_migrations,
):
    """Build comparable recovery evidence without returning protected values."""
    revision = validate_revision(revision)
    ciphertext_at_rest = {}
    protected_value_hmacs = {}
    for label, (ciphertext, plaintext) in sorted(protected_values.items()):
        if not ciphertext.startswith("enc$"):
            raise ValidationError(f"{label} is not encrypted at rest")
        if not plaintext:
            raise ValidationError(f"{label} did not decrypt to a non-empty value")
        ciphertext_at_rest[label] = True
        protected_value_hmacs[label] = evidence_hmac(
            probe_key,
            label,
            plaintext.encode(),
        )

    return {
        "schema_version": 1,
        "declared_revision": revision,
        "counts": dict(sorted(counts.items())),
        "database": {
            "applied_migrations": [list(migration) for migration in sorted(applied_migrations)],
            "postgresql_version_num": postgresql_version_num,
        },
        "ciphertext_at_rest": ciphertext_at_rest,
        "protected_value_hmacs": protected_value_hmacs,
        "api_token_verified": bool(api_token_verified),
        "media": {
            "name_hmac_sha256": evidence_hmac(
                probe_key,
                "media_name",
                media_name.encode(),
            ),
            "size_bytes": len(media_content),
            "hmac_sha256": evidence_hmac(
                probe_key,
                "media_content",
                media_content,
            ),
        },
    }


def _get_probe_credentials():
    probe_key = os.environ.get("ITAMBOX_RECOVERY_PROBE_KEY", "").encode()
    if len(probe_key) < 32:
        raise CommandError(
            "ITAMBOX_RECOVERY_PROBE_KEY must contain at least 32 bytes",
        )
    api_token = os.environ.get("ITAMBOX_RECOVERY_API_TOKEN", "")
    if not api_token:
        raise CommandError("ITAMBOX_RECOVERY_API_TOKEN is required")
    return probe_key, api_token


def _get_canaries(options):
    try:
        revision = validate_revision(options["revision"])
        license_obj = License._base_manager.get(pk=options["license_pk"])
        email = EmailSettings._base_manager.get(
            pk=options["email_settings_pk"],
        )
        webhook = WebhookEndpoint._base_manager.get(
            pk=options["webhook_pk"],
        )
        attachment = FileAttachment._base_manager.get(
            pk=options["attachment_pk"],
        )
    except (ObjectDoesNotExist, ValidationError, ValueError) as exc:
        raise CommandError(str(exc)) from exc
    return revision, license_obj, email, webhook, attachment


def _read_media(attachment):
    try:
        attachment.file.open("rb")
        try:
            return attachment.file.read()
        finally:
            attachment.file.close()
    except OSError as exc:
        raise CommandError("Recovery attachment could not be read") from exc


class Command(BaseCommand):
    help = "Emit non-plaintext recovery evidence for explicit synthetic canary records and media."

    def add_arguments(self, parser):
        parser.add_argument("--revision", required=True)
        parser.add_argument("--license-pk", required=True)
        parser.add_argument("--email-settings-pk", required=True)
        parser.add_argument("--webhook-pk", required=True)
        parser.add_argument("--attachment-pk", required=True)

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("capture_recovery_evidence requires PostgreSQL")

        probe_key, api_token = _get_probe_credentials()
        canaries = _get_canaries(options)
        revision, license_obj, email, webhook, attachment = canaries

        token = Token.find_by_key(api_token)
        try:
            validate_probe_context(
                license_obj,
                webhook,
                attachment,
                token,
            )
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc

        media_content = _read_media(attachment)

        try:
            evidence = build_recovery_evidence(
                revision=revision,
                probe_key=probe_key,
                protected_values={
                    "license_product_key": (
                        license_obj.product_key,
                        license_obj.decrypted_product_key,
                    ),
                    "smtp_password": (
                        email.smtp_password,
                        email.smtp_password_decrypted,
                    ),
                    "webhook_secret": (
                        webhook.secret,
                        webhook.secret_decrypted,
                    ),
                },
                api_token_verified=True,
                media_name=attachment.name,
                media_content=media_content,
                counts={
                    "attachments": FileAttachment._base_manager.count(),
                    "email_settings": EmailSettings._base_manager.count(),
                    "licenses": License._base_manager.count(),
                    "webhooks": WebhookEndpoint._base_manager.count(),
                },
                postgresql_version_num=connection.pg_version,
                applied_migrations=MigrationRecorder(
                    connection,
                ).applied_migrations(),
            )
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            json.dumps(
                evidence,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
