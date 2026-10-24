import secrets
from contextlib import nullcontext
from datetime import timedelta

from django import conf
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from compliance.choices import AuditSessionStatusChoices, AuditVerificationMethodChoices
from core.managers import (
    ExplicitScopeAllObjectsManager,
    ExplicitScopeManager,
    ExplicitScopeSoftDeleteManager,
)
from core.mixins import (
    CloneableMixin,
    ExportableMixin,
    SoftDeleteMixin,
    TaggableMixin,
)
from core.models import BaseModel, ChangeLoggingMixin, StandardModel


def generate_token():
    return secrets.token_urlsafe(48)


def generate_custody_signing_session_token():
    return secrets.token_urlsafe(48)


def custody_signing_session_expiry():
    ttl = getattr(conf.settings, "CUSTODY_SIGNING_SESSION_TTL", timedelta(minutes=30))
    return timezone.now() + ttl


class CustodyTemplate(TaggableMixin, CloneableMixin, ExportableMixin, ChangeLoggingMixin, SoftDeleteMixin, BaseModel):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    allow_global_tenant = True

    tenant = models.ForeignKey(
        to="organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="custody_templates",
        verbose_name=_("Tenant"),
    )
    tenant_group = models.ForeignKey(
        to="organization.TenantGroup",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="custody_templates",
        verbose_name=_("Tenant Group"),
        help_text=_("Target Tenant Group this template applies to (conglomerate/divisional scope)."),
    )
    name = models.CharField(
        max_length=255, verbose_name=_("Name"), help_text=_("Template Name (e.g. Standard Laptop EULA)")
    )
    signature_provider = models.CharField(
        max_length=50,
        default="local",
        verbose_name=_("Signature Provider"),
        help_text=_("E-Signature workflow provider module"),
    )
    logo = models.ImageField(
        upload_to="custody_logos/",
        blank=True,
        null=True,
        verbose_name=_("Logo"),
        help_text=_("Custom EULA / signoff logo image"),
    )
    eula_text = models.TextField(verbose_name=_("EULA Text"), help_text=_("Terms of Service / EULA guidelines"))
    disclaimer = models.TextField(
        blank=True, verbose_name=_("Disclaimer"), help_text=_("Disclaimer statement printed at signoff")
    )
    qms_reference = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("QMS Reference"),
        help_text=_("Quality Management System document reference key"),
    )
    is_active = models.BooleanField(
        default=True, verbose_name=_("Is Active"), help_text=_("Deactivate to hide from choices")
    )
    require_acceptance = models.BooleanField(
        default=True,
        verbose_name=_("Require Acceptance"),
        help_text=_("Require digital signature / EULA acceptance on checkout."),
    )
    email_signature_request = models.BooleanField(
        default=True,
        verbose_name=_("Email Signature Request"),
        help_text=_("Send email signature request link to the holder on checkout."),
    )
    category = models.ForeignKey(
        to="assets.Category",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="custody_templates",
        verbose_name=_("Category"),
        help_text=_("Target Category this template overrides for the selected Tenant scope."),
    )
    tags = models.ManyToManyField(to="extras.Tag", related_name="custody_templates", blank=True, verbose_name=_("Tags"))

    class Meta:
        ordering = ("name",)
        verbose_name = _("Custody Template")
        verbose_name_plural = _("Custody Templates")

    def __str__(self):
        if self.tenant:
            return f"{self.tenant} - {self.name}"
        if self.tenant_group:
            return f"Group: {self.tenant_group} - {self.name}"
        return f"Global - {self.name}"


class CustodyReceipt(ChangeLoggingMixin, BaseModel):
    # Tenant scoping is enforced at the API layer (CustodyReceiptViewSet uses
    # _scope_by_asset_tenant). The default manager is intentionally unscoped so
    # the token-based public sign view (custody_eula_sign) can resolve a receipt
    # by its secret token regardless of the requester's tenant context.

    # No tenant FK of its own: attribute the changelog to the asset's owning
    # tenant (mirrors AssetAudit) so receipts signed in the unauthenticated
    # public sign flow are not written tenant=None and lost to the owner.
    changelog_tenant_lookup = "asset__tenant"
    # Keep the public sign token and the signature artifacts out of the
    # changelog JSON — they are bearer credentials / signature evidence.
    _change_logging_excluded_fields = [
        "updated_at",
        "token",
        "signature_data",
        "signature_hash",
        "verification_hash",
        "signature_canvas",
        "evidence_snapshot",
    ]

    # Version label stamped on every signed receipt. The EULA text is snapshotted per receipt
    # (eula_text); custody templates carry no version, so the label is deliberately constant.
    EULA_VERSION = "1.0"
    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_DECLINED = "declined"
    # Administrative terminal state: the custody period the receipt evidences ended
    # (check-in or reassignment) before the holder decided. Not signer-authored.
    STATUS_SUPERSEDED = "superseded"
    ACCEPTANCE_STATUS_CHOICES = [
        (STATUS_PENDING, _("Pending")),
        (STATUS_ACCEPTED, _("Accepted")),
        (STATUS_DECLINED, _("Declined")),
        (STATUS_SUPERSEDED, _("Superseded")),
    ]

    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.PROTECT,
        related_name="custody_receipts",
        db_index=True,
        verbose_name=_("Asset"),
    )
    holder = models.ForeignKey(
        "organization.AssetHolder", on_delete=models.PROTECT, related_name="custody_receipts", verbose_name=_("Holder")
    )
    assignment = models.ForeignKey(
        "assets.AssetAssignment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="custody_receipts",
        verbose_name=_("Assignment"),
    )
    token = models.CharField(max_length=64, unique=True, default=generate_token)
    custody_template = models.ForeignKey(
        to="compliance.CustodyTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receipts",
        verbose_name=_("Custody Template"),
    )
    signature_provider = models.CharField(max_length=50, default="local", verbose_name=_("Signature Provider"))
    eula_text = models.TextField(blank=True, verbose_name=_("EULA Text"))
    disclaimer = models.TextField(blank=True, verbose_name=_("Disclaimer"))
    qms_reference = models.CharField(max_length=100, blank=True, verbose_name=_("QMS Reference"))
    accepted_date = models.DateTimeField(null=True, blank=True, verbose_name=_("Accepted Date"))
    acceptance_method = models.CharField(max_length=50, default="link", verbose_name=_("Acceptance Method"))
    acceptance_status = models.CharField(
        max_length=20,
        choices=ACCEPTANCE_STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
        verbose_name=_("Acceptance Status"),
    )
    signature_data = models.TextField(blank=True, verbose_name=_("Signature Data"))
    signature_hash = models.CharField(max_length=64, blank=True)
    verification_hash = models.CharField(
        max_length=64, unique=True, blank=True, null=True
    )  # null=True intentional: unique constraint allows multiple unsigned (NULL) receipts
    signature_canvas = models.TextField(
        blank=True, verbose_name=_("Signature Canvas"), help_text=_("Base64 canvas stroke vector string representation")
    )
    signed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Signed At"))
    eula_version = models.CharField(max_length=10, default=EULA_VERSION, verbose_name=_("EULA Version"))
    created_date = models.DateTimeField(auto_now_add=True, null=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name=_("IP Address"))
    user_agent = models.TextField(blank=True, verbose_name=_("User Agent"))
    # Keyed evidence seal (compliance.services.seal_custody_evidence). Version 0
    # marks a receipt decided before sealing existed: its verification_hash is
    # an unkeyed digest that cannot be verified.
    evidence_version = models.PositiveSmallIntegerField(default=0, editable=False, verbose_name=_("Evidence Version"))
    evidence_key_id = models.CharField(max_length=16, blank=True, editable=False, verbose_name=_("Evidence Key ID"))
    evidence_snapshot = models.JSONField(
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Evidence Snapshot"),
        help_text=_("What the holder attested to, frozen at the decision and covered by the evidence seal."),
    )

    @property
    def accepted(self) -> bool:
        # Derived from acceptance_status (the single source of truth); a comment, not a docstring, keeps it out of the OpenAPI text.
        return self.acceptance_status == self.STATUS_ACCEPTED

    class Meta:
        ordering = ("-signed_at",)
        verbose_name = _("Custody Receipt")
        verbose_name_plural = _("Custody Receipts")
        permissions = (
            ("prepare_custodyreceipt", _("Can prepare custody receipt signing sessions")),
            ("export_custodyreceipt", _("Can export custody receipts")),
        )

    def __str__(self):
        return f"Custody Receipt for {self.asset} assigned to {self.holder} ({self.get_acceptance_status_display()})"

    def get_absolute_url(self):
        return reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.pk})


class CustodySigningSession(BaseModel):
    """Short-lived, operator-prepared handoff for recipient custody consent."""

    objects = ExplicitScopeManager()
    tenant_lookup = "receipt__asset__tenant"
    deny_global_tenant = True

    OUTCOME_ACCEPTED = CustodyReceipt.STATUS_ACCEPTED
    OUTCOME_DECLINED = CustodyReceipt.STATUS_DECLINED
    OUTCOME_CHOICES = (
        (OUTCOME_ACCEPTED, _("Accepted")),
        (OUTCOME_DECLINED, _("Declined")),
    )

    receipt = models.ForeignKey(
        CustodyReceipt,
        on_delete=models.PROTECT,
        related_name="signing_sessions",
        editable=False,
        verbose_name=_("Custody Receipt"),
    )
    operator = models.ForeignKey(
        conf.settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="prepared_custody_signing_sessions",
        editable=False,
        verbose_name=_("Operator"),
    )
    intended_holder = models.ForeignKey(
        "organization.AssetHolder",
        on_delete=models.PROTECT,
        related_name="custody_signing_sessions",
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Intended Holder"),
    )
    token = models.CharField(
        max_length=64,
        unique=True,
        default=generate_custody_signing_session_token,
        editable=False,
    )
    expires_at = models.DateTimeField(default=custody_signing_session_expiry, editable=False)
    consumed_at = models.DateTimeField(null=True, blank=True, editable=False)
    canceled_at = models.DateTimeField(null=True, blank=True, editable=False)
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, blank=True, editable=False)

    class Meta:
        ordering = ("-created_at", "-pk")
        default_permissions = ()
        verbose_name = _("Custody Signing Session")
        verbose_name_plural = _("Custody Signing Sessions")

    def __str__(self):
        return f"Custody signing session {self.pk} for receipt {self.receipt_id}"

    @property
    def is_active(self):
        return self.canceled_at is None and self.consumed_at is None and self.expires_at > timezone.now()

    @property
    def status(self):
        if self.canceled_at is not None:
            return "canceled"
        if self.consumed_at is not None:
            return "consumed"
        if self.expires_at <= timezone.now():
            return "expired"
        return "active"


class CustodyHandoffDelivery(BaseModel):
    """Durable, token-free audit record for a handoff e-mail attempt."""

    objects = ExplicitScopeManager()
    tenant_lookup = "signing_session__receipt__asset__tenant"
    deny_global_tenant = True

    STATUS_REQUESTED = "requested"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_TERMINAL_FAILED = "terminal_failed"
    STATUS_CHOICES = (
        (STATUS_REQUESTED, _("Requested")),
        (STATUS_SUCCEEDED, _("Succeeded")),
        (STATUS_TERMINAL_FAILED, _("Terminal failure")),
    )

    receipt = models.ForeignKey(
        CustodyReceipt,
        on_delete=models.PROTECT,
        related_name="handoff_deliveries",
        editable=False,
        verbose_name=_("Custody Receipt"),
    )
    signing_session = models.ForeignKey(
        CustodySigningSession,
        on_delete=models.PROTECT,
        related_name="handoff_deliveries",
        editable=False,
        verbose_name=_("Custody Signing Session"),
    )
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="custody_handoff_deliveries",
        editable=False,
        verbose_name=_("Operator"),
    )
    attempt = models.PositiveIntegerField(verbose_name=_("Attempt"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, verbose_name=_("Status"))
    error_class = models.CharField(max_length=100, blank=True, null=True, verbose_name=_("Error class"))
    delivered_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Delivered at"))

    class Meta:
        ordering = ("-created_at", "-pk")
        unique_together = (("signing_session", "attempt"),)
        verbose_name = _("Custody Handoff Delivery")
        verbose_name_plural = _("Custody Handoff Deliveries")

    def __str__(self):
        return f"Custody handoff delivery {self.pk} (attempt {self.attempt})"


User = get_user_model()


class AuditSession(StandardModel, SoftDeleteMixin):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    allow_global_tenant = True
    # reconciliation_report is a large frozen JSON snapshot; keep it out of the
    # changelog so each session UPDATE doesn't store the whole blob twice.
    _change_logging_excluded_fields = ["updated_at", "reconciliation_report"]

    name = models.CharField(max_length=200, verbose_name=_("Name"))
    tenant = models.ForeignKey(
        to="organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="audit_sessions",
        verbose_name=_("Tenant"),
        help_text=_("Tenant this campaign belongs to. Leave blank for MSP-wide / global sessions."),
    )
    location = models.ForeignKey(
        "organization.Location",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Location"),
        help_text=_("Target location expected to be audited. If omitted, applies globally."),
    )
    status = models.CharField(
        max_length=20,
        choices=AuditSessionStatusChoices.choices,
        default=AuditSessionStatusChoices.PLANNED,
        verbose_name=_("Status"),
    )
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Completed At"))
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="audit_sessions", verbose_name=_("Created By")
    )
    reconciliation_report = models.JSONField(
        null=True,
        blank=True,
        editable=False,
        help_text=_("Frozen reconciliation report written at close time. Denormalized for long-term readability."),
    )

    class Meta:
        ordering = ["-started_at"]
        verbose_name = _("Audit Session")
        verbose_name_plural = _("Audit Sessions")

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"

    def validate_location_integrity(self, allowed_tenant_ids=None, *, using=None):
        if self.location_id is None:
            return
        location = self._state.fields_cache.get("location")
        if location is None:
            location_model = self._meta.get_field("location").remote_field.model
            location = (
                location_model._base_manager.using(using)
                .filter(pk=self.location_id)
                .only("tenant_id", "deleted_at")
                .first()
            )
        if location is None or location.deleted_at is not None or location.tenant_id is None:
            raise ValidationError({"location": _("Audit sessions require a live tenant-owned location.")})
        if self.tenant_id is not None and location.tenant_id != self.tenant_id:
            raise ValidationError({"location": _("The audit location must belong to the session tenant.")})
        if allowed_tenant_ids is not None and location.tenant_id not in allowed_tenant_ids:
            raise ValidationError({"location": _("The audit location is outside the authorized tenant scope.")})

    def _persisted_mutation_fields(self, *, lock=False, using=None):
        fields = tuple(
            field.attname
            for field in self._meta.concrete_fields
            if not field.primary_key and field.attname != "updated_at"
        )
        queryset = type(self)._base_manager.using(using)
        if lock:
            queryset = queryset.select_for_update(of=("self",))
        return fields, queryset.filter(pk=self.pk).values(*fields).first()

    def _reject_completed_mutation(self, *, persisted_state=None):
        if self._state.adding or self.pk is None:
            return
        fields, persisted = persisted_state or self._persisted_mutation_fields()
        if persisted is None or persisted["status"] != AuditSessionStatusChoices.COMPLETED:
            return
        if any(getattr(self, field) != persisted[field] for field in fields):
            raise ValidationError(_("Completed audit sessions are immutable."))

    def save(self, *args, **kwargs):
        using = kwargs.get("using") or self._state.db or "default"
        if self._state.adding or self.pk is None:
            self.validate_location_integrity(using=using)
            return super().save(*args, **kwargs)
        write_transaction = (
            nullcontext() if transaction.get_connection(using).in_atomic_block else transaction.atomic(using=using)
        )
        with write_transaction:
            persisted_state = self._persisted_mutation_fields(lock=True, using=using)
            self.validate_location_integrity(using=using)
            self._reject_completed_mutation(persisted_state=persisted_state)
            return super().save(*args, **kwargs)

    def _reject_completed_delete(self, *, persisted_status=None, using=None):
        if self.pk is None:
            return
        status = persisted_status
        if status is None:
            status = type(self)._base_manager.using(using).filter(pk=self.pk).values_list("status", flat=True).first()
        if status == AuditSessionStatusChoices.COMPLETED:
            raise ValidationError(_("Completed audit sessions and their evidence cannot be deleted."))

    def soft_delete(self):
        return super().soft_delete()

    def delete(self, *args, **kwargs):
        if self.pk is None:
            return super().delete(*args, **kwargs)
        using = kwargs.get("using") or self._state.db or "default"
        write_transaction = (
            nullcontext() if transaction.get_connection(using).in_atomic_block else transaction.atomic(using=using)
        )
        with write_transaction:
            persisted_status = (
                type(self)
                ._base_manager.using(using)
                .select_for_update(of=("self",))
                .filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )
            self._reject_completed_delete(persisted_status=persisted_status, using=using)
            return super().delete(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("compliance:auditsession_detail", kwargs={"pk": self.pk})


class AssetAudit(ChangeLoggingMixin, models.Model):
    # Tenant scoping is enforced at the API layer (AssetAuditViewSet uses
    # _scope_by_asset_tenant). The default manager is intentionally unscoped:
    # audit classification/reconciliation reads `session.audits` and must see
    # every audit in a session regardless of the viewer's tenant context.

    # AssetAudit carries no tenant of its own. Its owning tenant is the audited
    # asset's tenant (`asset.tenant`), NOT the session's: a session may be a
    # global / MSP-wide record (AuditSession.tenant is null for those), whereas
    # the asset always belongs to exactly one tenant. Attributing the changelog
    # to asset.tenant keeps each audit change visible to the asset's owner even
    # in no-ambient-tenant flows (superuser global session, service audit_asset).
    changelog_tenant_lookup = "asset__tenant"

    session = models.ForeignKey(
        AuditSession, on_delete=models.CASCADE, related_name="audits", null=True, blank=True, verbose_name=_("Session")
    )
    # SET_NULL preserves audit history as orphaned records when an asset is
    # hard-purged via purge_deleted, rather than destroying compliance evidence.
    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audits",
        verbose_name=_("Asset"),
    )
    auditor = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="audits_performed", verbose_name=_("Auditor")
    )
    timestamp = models.DateTimeField(auto_now_add=True)
    location = models.ForeignKey(
        "organization.Location",
        on_delete=models.PROTECT,
        verbose_name=_("Location"),
        help_text=_("The observed physical location of the asset during audit."),
    )
    status = models.ForeignKey(
        "assets.StatusLabel",
        on_delete=models.PROTECT,
        verbose_name=_("Status"),
        help_text=_("The observed physical status of the asset during audit."),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    verification_method = models.CharField(
        max_length=30,
        choices=AuditVerificationMethodChoices.choices,
        default=AuditVerificationMethodChoices.MANUAL,
        verbose_name=_("Verification Method"),
    )

    class Meta:
        ordering = ["-timestamp"]
        constraints = [models.UniqueConstraint(fields=["session", "asset"], name="unique_session_asset")]
        verbose_name = _("Asset Audit")
        verbose_name_plural = _("Asset Audits")
