from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from assets.api.nested_serializers import NestedAssetSerializer
from assets.api.serializers import CategorySerializer, StatusLabelSerializer
from assets.models import Asset, AssetMaintenance, Category, StatusLabel
from compliance.models import AssetAudit, AuditSession, CustodyReceipt, CustodyTemplate
from compliance.registry import signature_providers
from core.managers import Scope
from extras.api.serializers import TagSerializer
from itambox.api.base import BaseModelSerializer
from organization.api.serializers import (
    AssetHolderSerializer,
    NestedLocationSerializer,
    NestedTenantGroupSerializer,
    NestedTenantSerializer,
)
from organization.models import Location, Tenant, TenantGroup

User = get_user_model()


class CustodyTemplateSerializer(BaseModelSerializer):
    # tenant + tenant_group are both nullable (allow_global_tenant = True):
    # a template with tenant=None is a valid global/shared template. Expose
    # them as optional, nullable *_id write fields so global templates can be
    # created/edited without forcing a tenant.
    tenant = NestedTenantSerializer(read_only=True)
    tenant_id = serializers.PrimaryKeyRelatedField(
        queryset=Tenant.objects, source="tenant", write_only=True, required=False, allow_null=True
    )
    tenant_group = NestedTenantGroupSerializer(read_only=True)
    tenant_group_id = serializers.PrimaryKeyRelatedField(
        queryset=TenantGroup.objects, source="tenant_group", write_only=True, required=False, allow_null=True
    )
    category = CategorySerializer(read_only=True)
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects, source="category", write_only=True, required=False, allow_null=True
    )
    tags = TagSerializer(many=True, read_only=True)

    def validate_signature_provider(self, value):
        if signature_providers.get(value) is None:
            raise serializers.ValidationError(
                _("Unknown signature provider. Choose one of: %(names)s.")
                % {"names": ", ".join(name for name, _label in signature_providers.choices())}
            )
        return value

    class Meta:
        model = CustodyTemplate
        fields = [
            "id",
            "name",
            "tenant",
            "tenant_id",
            "tenant_group",
            "tenant_group_id",
            "category",
            "category_id",
            "signature_provider",
            "logo",
            "eula_text",
            "disclaimer",
            "qms_reference",
            "is_active",
            "require_acceptance",
            "email_signature_request",
            "tags",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        brief_fields = ["id", "name", "tenant", "is_active"]


class CustodyReceiptSerializer(BaseModelSerializer):
    # Read-only representation (the viewset accepts no writes). The signing
    # ``token`` is a bearer credential and is never serialized.
    asset = NestedAssetSerializer(read_only=True)
    holder = AssetHolderSerializer(read_only=True)
    acceptance_status_display = serializers.CharField(source="get_acceptance_status_display", read_only=True)

    class Meta:
        model = CustodyReceipt
        fields = [
            "id",
            "asset",
            "holder",
            "accepted",
            "accepted_date",
            "acceptance_method",
            "acceptance_status",
            "acceptance_status_display",
            "signature_provider",
            "signature_data",
            "signature_hash",
            "verification_hash",
            "signature_canvas",
            "signed_at",
            "eula_version",
            "ip_address",
            "user_agent",
            "created_date",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
        brief_fields = ["id", "asset", "holder", "acceptance_status", "signed_at"]


class AssetMaintenanceSerializer(BaseModelSerializer):
    asset = NestedAssetSerializer(read_only=True)
    asset_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedAssetSerializer.Meta.model.objects, source="asset", write_only=True
    )
    maintenance_type_display = serializers.CharField(source="get_maintenance_type_display", read_only=True)
    downtime_days = serializers.IntegerField(read_only=True)

    class Meta:
        model = AssetMaintenance
        fields = [
            "id",
            "asset",
            "asset_id",
            "supplier",
            "maintenance_type",
            "maintenance_type_display",
            "cost",
            "currency",
            "start_date",
            "completion_date",
            "downtime_days",
            "notes",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "asset", "maintenance_type", "start_date", "cost"]


class AuditSessionSerializer(BaseModelSerializer):
    location = NestedLocationSerializer(read_only=True)
    location_id = serializers.PrimaryKeyRelatedField(
        queryset=Location.objects, source="location", write_only=True, required=False, allow_null=True
    )
    created_by = serializers.StringRelatedField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = AuditSession
        # `created_by` is forced to request.user in the viewset's perform_create;
        # there is deliberately no writable created_by_id (it would let a client
        # reattribute a session to another user on create/PATCH).
        fields = [
            "id",
            "name",
            "location",
            "location_id",
            "status",
            "status_display",
            "started_at",
            "completed_at",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["started_at", "created_at", "updated_at"]
        brief_fields = ["id", "name", "status", "started_at"]

    def validate(self, attrs):
        if "completed_at" in attrs:
            raise serializers.ValidationError(
                {"completed_at": "The completion timestamp is assigned by the close service."}
            )
        if self.instance is not None and self.instance.status == "completed":
            # unscoped: immutability check re-reads the persisted session row by pk, independent of ambient scope
            persisted = (
                AuditSession._base_manager.filter(pk=self.instance.pk).values("name", "location_id", "status").first()
            )
            immutable_changes = {
                field
                for field, persisted_value in (
                    ("name", persisted["name"] if persisted else None),
                    ("location", persisted["location_id"] if persisted else None),
                    ("status", persisted["status"] if persisted else None),
                )
                if field in attrs
                and (attrs[field].pk if field == "location" and attrs[field] is not None else attrs[field])
                != persisted_value
            }
            if immutable_changes:
                raise serializers.ValidationError("Completed audit sessions are immutable.")
        tenant = self.instance.tenant if self.instance is not None else None
        location = attrs.get("location", self.instance.location if self.instance is not None else None)
        if tenant is not None and location is not None and location.tenant_id != tenant.pk:
            raise serializers.ValidationError({"location_id": "The audit location must belong to the session tenant."})
        return attrs


class AssetAuditSerializer(serializers.ModelSerializer):
    session = serializers.PrimaryKeyRelatedField(queryset=AuditSession.objects, required=False, allow_null=True)
    asset = NestedAssetSerializer(read_only=True)
    # unscoped: asset PK field validates scan targets; tenant authorization is enforced by the audit service on write
    asset_id = serializers.PrimaryKeyRelatedField(queryset=Asset._base_manager.all(), source="asset")
    auditor = serializers.StringRelatedField(read_only=True)
    location = NestedLocationSerializer(read_only=True)
    location_id = serializers.PrimaryKeyRelatedField(queryset=Location.objects, source="location")
    status = StatusLabelSerializer(read_only=True)
    status_id = serializers.PrimaryKeyRelatedField(queryset=StatusLabel.objects.all(), source="status")
    verification_method_display = serializers.CharField(source="get_verification_method_display", read_only=True)

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.fields["session"].queryset = AuditSession.objects.for_scope(Scope.current())

    class Meta:
        model = AssetAudit
        fields = [
            "id",
            "session",
            "asset",
            "asset_id",
            "auditor",
            "timestamp",
            "location",
            "location_id",
            "status",
            "status_id",
            "notes",
            "verification_method",
            "verification_method_display",
        ]
        read_only_fields = ["auditor", "timestamp"]
