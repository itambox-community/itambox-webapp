from django.core.management.base import CommandError
from django.db import transaction

from core.crypto import decrypt_string, encrypt_string
from core.tasks.management import SystemTaskCommand


def encrypted_field_specs():
    """Every (model, field_name) pair that stores an ``enc$``-prefixed Fernet ciphertext.

    Key rotation MUST cover every encrypted field: a field omitted here keeps its old
    ciphertext, so once the operator drops the old key it becomes permanently undecryptable
    (broken SMTP / webhook signing). When you add a new encrypted field, add it here.

    Imported lazily to avoid touching the app registry at module import time.
    """
    # inline imports: app-registry: these model modules must not load before the app registry is ready
    from compliance.models import SignatureProviderConfig
    from core.models import EmailSettings
    from extras.models import WebhookEndpoint
    from licenses.models import License

    return [
        (License, "product_key"),
        (EmailSettings, "smtp_password"),
        (WebhookEndpoint, "secret"),
        (SignatureProviderConfig, "secrets_encrypted"),
    ]


class Command(SystemTaskCommand):
    help = "Re-encrypt every encrypted field with the current primary key (key rotation)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Simulate key rotation without saving database changes",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        self.stdout.write("Scanning for encrypted fields in the database...")

        totals = {"rotated": 0, "skipped": 0, "errors": 0}
        with transaction.atomic():
            for model, field_name in encrypted_field_specs():
                result = self._rotate_field(model, field_name, dry_run)
                for key in totals:
                    totals[key] += result[key]

            if totals["errors"]:
                raise CommandError(
                    "Key rotation failed; all database changes were rolled back. "
                    f"Rotated: {totals['rotated']}, Skipped: {totals['skipped']}, "
                    f"Errors: {totals['errors']}."
                )

            prefix = "[DRY RUN] Simulation complete." if dry_run else "Rotation complete!"
            self.stdout.write(
                self.style.SUCCESS(
                    f"{prefix} Rotated: {totals['rotated']}, Skipped: {totals['skipped']}, Errors: {totals['errors']}."
                )
            )

    def _rotate_field(self, model, field_name, dry_run):
        label = f"{model.__name__}.{field_name}"

        # _base_manager: unscoped (every tenant) and INCLUDES soft-deleted rows, independent
        # of the (absent) request tenant/user context this management command runs in.
        rows = model._base_manager.all()
        self.stdout.write(f"Inspecting {rows.count()} {model.__name__} row(s) for '{field_name}'.")

        rotated = skipped = errors = 0
        for obj in rows:
            raw = getattr(obj, field_name)
            if not raw:
                skipped += 1
                continue

            if not raw.startswith("enc$"):
                # Not encrypted yet (plaintext fallback) — adopt it under the current key.
                decrypted_val = raw
            else:
                try:
                    decrypted_val = decrypt_string(raw)
                except ValueError as e:
                    self.stderr.write(self.style.ERROR(f"Failed to decrypt {label} pk={obj.pk}: {e}"))
                    errors += 1
                    continue

            # Defensive: decrypt_string raises on failure, but guard against a silent
            # cipher-passthrough to avoid re-encrypting an unrecoverable value.
            if decrypted_val == raw and raw.startswith("enc$"):
                self.stderr.write(
                    self.style.ERROR(
                        f"Decryption returned the cipher for {label} pk={obj.pk}; skipping to avoid data loss."
                    )
                )
                errors += 1
                continue

            new_encrypted = encrypt_string(decrypted_val)
            if new_encrypted == raw:
                # Already under the current primary key (rare — Fernet is non-deterministic).
                skipped += 1
                continue

            rotated += 1
            if dry_run:
                self.stdout.write(self.style.WARNING(f"[DRY RUN] Would rotate {label} pk={obj.pk}"))
            else:
                # .update() bypasses save()'s encrypt-on-save, storing the new ciphertext as-is.
                model._base_manager.filter(pk=obj.pk).update(**{field_name: new_encrypted})

        return {"rotated": rotated, "skipped": skipped, "errors": errors}
