"""AssetRequest — user-facing request workflow for assets and consumables."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from assets.choices import RequestStatusChoices
from core.managers import ExplicitScopeAllObjectsManager, ExplicitScopeSoftDeleteManager, Scope
from core.mixins import JournalingMixin, SoftDeleteMixin, TaggableMixin
from core.models import BaseModel, ChangeLoggingMixin

User = get_user_model()


def reject_disposed_request(asset) -> None:
    """A disposal record owns the asset lifecycle until it is cancelled (#496).

    Module-level on purpose: an inner helper would add a decision point to
    ``AssetRequest.clean`` and a new C901 identity. The whole request surface refuses
    a disposed asset here instead of only in the create form's queryset.
    """
    if asset is not None and asset.is_disposed:
        raise ValidationError(_("The asset '%(asset)s' is disposed and cannot be requested.") % {"asset": asset})


class AssetRequest(JournalingMixin, TaggableMixin, ChangeLoggingMixin, BaseModel, SoftDeleteMixin):
    objects = ExplicitScopeSoftDeleteManager()

    all_objects = ExplicitScopeAllObjectsManager()

    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="asset_requests",
        db_index=True,
        verbose_name=_("Tenant"),
    )

    requester = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="asset_requests", db_index=True, verbose_name=_("Requester")
    )

    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requests",
        db_index=True,
        verbose_name=_("Asset"),
    )

    asset_type = models.ForeignKey(
        "assets.AssetType",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requests",
        db_index=True,
        verbose_name=_("Asset Type"),
    )

    component = models.ForeignKey(
        "inventory.Component",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requests",
        db_index=True,
        verbose_name=_("Component"),
    )

    accessory = models.ForeignKey(
        "inventory.Accessory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requests",
        db_index=True,
        verbose_name=_("Accessory"),
    )

    consumable = models.ForeignKey(
        "inventory.Consumable",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requests",
        db_index=True,
        verbose_name=_("Consumable"),
    )

    qty = models.PositiveIntegerField(default=1, verbose_name=_("Quantity"))

    source_location = models.ForeignKey(
        "organization.Location",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_requests",
        db_index=True,
        verbose_name=_("Source Location"),
    )

    status = models.CharField(
        max_length=20,
        choices=RequestStatusChoices.choices,
        default=RequestStatusChoices.PENDING,
        db_index=True,
        verbose_name=_("Status"),
    )

    request_date = models.DateTimeField(auto_now_add=True, db_index=True)

    response_date = models.DateTimeField(null=True, blank=True, verbose_name=_("Response Date"))

    responded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="asset_request_responses",
        verbose_name=_("Responded By"),
    )

    # Intended assignee target fields (delegated targets)

    assigned_user = models.ForeignKey(
        "organization.AssetHolder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="asset_requests",
        verbose_name=_("Assigned User"),
    )

    assigned_location = models.ForeignKey(
        "organization.Location",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="asset_requests",
        verbose_name=_("Assigned Location"),
    )

    assigned_asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_requests_for",
        verbose_name=_("Assigned Asset"),
    )

    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="sub_requests",
        db_index=True,
        verbose_name=_("Parent"),
    )

    is_group = models.BooleanField(default=False, db_index=True, verbose_name=_("Is Group"))

    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    response_notes = models.TextField(blank=True, verbose_name=_("Response Notes"))

    tags = models.ManyToManyField(
        "extras.Tag", related_name="asset_requests_tagged", blank=True, verbose_name=_("Tags")
    )

    @property
    def assigned_target(self):

        return self.assigned_user or self.assigned_location or self.assigned_asset

    @property
    def assigned_to(self):

        return self.assigned_target

    @property
    def assigned_to_type(self):

        if self.assigned_user:
            return "assetholder"

        if self.assigned_location:
            return "location"

        if self.assigned_asset:
            return "asset"

        return None

    def clean(self):

        super().clean()

        if self.pk:
            try:
                # _base_manager: state-machine checks must not depend on the active

                # tenant context (see Asset.clean).

                old_status = AssetRequest._base_manager.get(pk=self.pk).status

                if old_status != self.status:
                    VALID_TRANSITIONS = {
                        RequestStatusChoices.PENDING: {
                            RequestStatusChoices.APPROVED,
                            RequestStatusChoices.DENIED,
                            RequestStatusChoices.CANCELLED,
                            RequestStatusChoices.FULFILLED,
                        },
                        RequestStatusChoices.APPROVED: {
                            RequestStatusChoices.FULFILLED,
                            RequestStatusChoices.CANCELLED,
                            RequestStatusChoices.PROCUREMENT,
                        },
                        RequestStatusChoices.PROCUREMENT: {
                            RequestStatusChoices.FULFILLED,
                            RequestStatusChoices.CANCELLED,
                            RequestStatusChoices.APPROVED,
                        },
                        RequestStatusChoices.DENIED: set(),
                        RequestStatusChoices.FULFILLED: set(),
                        RequestStatusChoices.CANCELLED: set(),
                    }

                    if self.status not in VALID_TRANSITIONS.get(old_status, set()):
                        raise ValidationError(
                            _("Invalid state transition from %(old)s to %(new)s.")
                            % {"old": old_status, "new": self.status}
                        )

            except AssetRequest.DoesNotExist:
                pass

        categories_filled = []

        if self.asset is not None or self.asset_type is not None:
            categories_filled.append("asset")

        if self.component is not None:
            categories_filled.append("component")

        if self.accessory is not None:
            categories_filled.append("accessory")

        if self.consumable is not None:
            categories_filled.append("consumable")

        if len(categories_filled) == 0:
            raise ValidationError(
                _("Select the item you are requesting (Asset, Asset Type, Component, Accessory, or Consumable).")
            )

        if len(categories_filled) > 1:
            raise ValidationError(_("A request can contain only one item type."))

        if self.qty <= 0:
            raise ValidationError(_("Requested quantity must be greater than zero."))

        if not self.pk:
            if self.asset and not self.asset.is_requestable:
                raise ValidationError(_("The asset '%(asset)s' is not requestable.") % {"asset": self.asset})

            reject_disposed_request(self.asset)

            if self.asset_type and not self.asset_type.requestable:
                raise ValidationError(_("The asset type '%(type)s' is not requestable.") % {"type": self.asset_type})

            if self.asset and not self.asset.is_issuable:
                raise ValidationError(
                    _("The asset '%(asset)s' is currently not available (Status: %(status)s).")
                    % {"asset": self.asset, "status": self.asset.get_status_display()}
                )

            # Check for duplicate pending, approved or procurement requests by the same requester

            if self.requester_id and not getattr(self, "_skip_duplicate_check", False):
                duplicate_qs = AssetRequest.objects.for_scope(Scope.current()).filter(
                    requester_id=self.requester_id,
                    status__in=[
                        RequestStatusChoices.PENDING,
                        RequestStatusChoices.APPROVED,
                        RequestStatusChoices.PROCUREMENT,
                    ],
                    assigned_user_id=self.assigned_user_id,
                    assigned_location_id=self.assigned_location_id,
                    assigned_asset_id=self.assigned_asset_id,
                )

                if self.asset:
                    if duplicate_qs.filter(asset=self.asset).exists():
                        raise ValidationError(
                            _("You already have an open request for the asset '%(asset)s'.") % {"asset": self.asset}
                        )

                elif self.asset_type:
                    if duplicate_qs.filter(asset_type=self.asset_type, asset__isnull=True).exists():
                        raise ValidationError(
                            _("You already have an open request for the asset type '%(type)s'.")
                            % {"type": self.asset_type}
                        )

                elif self.component:
                    if duplicate_qs.filter(component=self.component).exists():
                        raise ValidationError(
                            _("You already have an open request for the component '%(component)s'.")
                            % {"component": self.component}
                        )

                elif self.accessory:
                    if duplicate_qs.filter(accessory=self.accessory).exists():
                        raise ValidationError(
                            _("You already have an open request for the accessory '%(accessory)s'.")
                            % {"accessory": self.accessory}
                        )

                elif self.consumable:
                    if duplicate_qs.filter(consumable=self.consumable).exists():
                        raise ValidationError(
                            _("You already have an open request for the consumable '%(consumable)s'.")
                            % {"consumable": self.consumable}
                        )

        if self.asset and self.asset_type and self.asset.asset_type != self.asset_type:
            raise ValidationError(_("The selected asset does not match the requested asset type."))

    def save(self, *args, **kwargs):

        if not self.tenant:
            from core.managers import get_current_tenant

            self.tenant = get_current_tenant()

        # Auto-approval check for Accessories and Consumables.

        # NOTE: auto-approval is advisory — it reserves no stock. Capacity is

        # enforced at fulfilment, so a generous (or malformed) threshold here can

        # only over-approve, never over-allocate.

        if not self.pk and self.status == RequestStatusChoices.PENDING:
            thresholds = getattr(settings, "ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS", None)

            if thresholds is None:
                thresholds = getattr(settings, "REQUISITION_AUTO_APPROVAL_THRESHOLDS", None)

            if thresholds and self.accessory:
                max_qty = thresholds.get("accessory", 0)

                if self.qty <= max_qty and self.accessory.available >= self.qty:
                    self.status = RequestStatusChoices.APPROVED

                    self.response_date = timezone.now()

                    self.response_notes = (
                        "Automatically approved because the quantity is within the configured threshold "
                        "and sufficient stock is available."
                    )

            elif thresholds and self.consumable:
                max_qty = thresholds.get("consumable", 0)

                if self.qty <= max_qty and self.consumable.available >= self.qty:
                    self.status = RequestStatusChoices.APPROVED

                    self.response_date = timezone.now()

                    self.response_notes = (
                        "Automatically approved because the quantity is within the configured threshold "
                        "and sufficient stock is available."
                    )

        self.full_clean()

        super().save(*args, **kwargs)

    class Meta:
        ordering = ["-request_date"]

        verbose_name = _("Asset Request")

        verbose_name_plural = _("Asset Requests")

        permissions = [
            ("add_delegated_assetrequest", _("Can request assets on behalf of others")),
            ("approve_assetrequest", _("Can approve asset requests")),
            ("fulfill_assetrequest", _("Can fulfill/claim asset requests")),
        ]

        constraints = [
            models.CheckConstraint(
                check=(
                    (
                        models.Q(assigned_user__isnull=True)
                        & models.Q(assigned_location__isnull=True)
                        & models.Q(assigned_asset__isnull=True)
                    )
                    | (
                        models.Q(assigned_user__isnull=False)
                        & models.Q(assigned_location__isnull=True)
                        & models.Q(assigned_asset__isnull=True)
                    )
                    | (
                        models.Q(assigned_user__isnull=True)
                        & models.Q(assigned_location__isnull=False)
                        & models.Q(assigned_asset__isnull=True)
                    )
                    | (
                        models.Q(assigned_user__isnull=True)
                        & models.Q(assigned_location__isnull=True)
                        & models.Q(assigned_asset__isnull=False)
                    )
                ),
                name="at_most_one_request_target",
            ),
            models.CheckConstraint(
                check=(
                    (
                        models.Q(component__isnull=True)
                        & models.Q(accessory__isnull=True)
                        & models.Q(consumable__isnull=True)
                        & (models.Q(asset__isnull=False) | models.Q(asset_type__isnull=False))
                    )
                    | (
                        models.Q(asset__isnull=True)
                        & models.Q(asset_type__isnull=True)
                        & models.Q(component__isnull=False)
                        & models.Q(accessory__isnull=True)
                        & models.Q(consumable__isnull=True)
                    )
                    | (
                        models.Q(asset__isnull=True)
                        & models.Q(asset_type__isnull=True)
                        & models.Q(component__isnull=True)
                        & models.Q(accessory__isnull=False)
                        & models.Q(consumable__isnull=True)
                    )
                    | (
                        models.Q(asset__isnull=True)
                        & models.Q(asset_type__isnull=True)
                        & models.Q(component__isnull=True)
                        & models.Q(accessory__isnull=True)
                        & models.Q(consumable__isnull=False)
                    )
                ),
                name="exactly_one_requested_category",
            ),
        ]

    def __str__(self):

        if self.asset:
            target = str(self.asset)

        elif self.asset_type:
            target = str(self.asset_type)

        elif self.component:
            target = f"{self.qty}x Component: {self.component}"

        elif self.accessory:
            target = f"{self.qty}x Accessory: {self.accessory}"

        elif self.consumable:
            target = f"{self.qty}x Consumable: {self.consumable}"

        else:
            target = "Any Asset"

        return f"Request for {target} by {self.requester} ({self.get_status_display()})"

    def get_absolute_url(self):

        return reverse("assets:assetrequest_detail", kwargs={"pk": self.pk})

    @property
    def unallocated_count(self):

        if self.is_group:
            return self.sub_requests.filter(
                asset__isnull=True, component__isnull=True, accessory__isnull=True, consumable__isnull=True
            ).count()

        return 1 if not (self.asset or self.component or self.accessory or self.consumable) else 0
