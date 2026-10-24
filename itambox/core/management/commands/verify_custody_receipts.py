"""Verify the evidence seal of every decided custody receipt (read-only).

Prints how many receipts verify, are legacy (decided before sealing existed),
were sealed under a key no longer in the keyring, or no longer match their
sealed evidence. Never writes to the database.
"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from compliance.models import CustodyReceipt
from compliance.services import (
    EVIDENCE_LEGACY,
    EVIDENCE_MISMATCH,
    EVIDENCE_STATUS_LABELS,
    EVIDENCE_UNKNOWN_KEY,
    EVIDENCE_VERIFIED,
    verify_custody_evidence,
)

REPORT_ORDER = (EVIDENCE_VERIFIED, EVIDENCE_LEGACY, EVIDENCE_UNKNOWN_KEY, EVIDENCE_MISMATCH)


class Command(BaseCommand):
    help = (
        "Verify the keyed evidence seal of every accepted or declined custody receipt "
        "and report receipts that no longer match. Read-only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", metavar="SLUG", help="Only check receipts for this tenant's assets.")
        parser.add_argument(
            "--fail-on-mismatch",
            action="store_true",
            help="Exit non-zero when any receipt does not match its sealed evidence.",
        )

    def handle(self, *args, **options):
        receipts = CustodyReceipt.objects.exclude(acceptance_status=CustodyReceipt.STATUS_PENDING).exclude(
            acceptance_status=CustodyReceipt.STATUS_SUPERSEDED
        )
        if options["tenant"]:
            receipts = receipts.filter(asset__tenant__slug=options["tenant"])

        counts: Counter[str] = Counter()
        problems = []
        for receipt in receipts.order_by("pk").iterator():
            check = verify_custody_evidence(receipt)
            counts[check.status] += 1
            if check.status in (EVIDENCE_MISMATCH, EVIDENCE_UNKNOWN_KEY):
                problems.append((receipt.pk, check))

        for status in REPORT_ORDER:
            self.stdout.write(f"{EVIDENCE_STATUS_LABELS[status]}: {counts[status]}")
        for pk, check in problems:
            detail = f" ({', '.join(check.differing_fields)})" if check.differing_fields else ""
            self.stdout.write(f"  receipt {pk}: {check.status}{detail}")

        if options["fail_on_mismatch"] and counts[EVIDENCE_MISMATCH]:
            raise CommandError(f"{counts[EVIDENCE_MISMATCH]} custody receipt(s) do not match their sealed evidence.")
