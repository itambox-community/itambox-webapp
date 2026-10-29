from collections.abc import Mapping

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field, extend_schema_serializer
from rest_framework import serializers

from assets.api.nested_serializers import (
    NestedAssetRoleSerializer,
    NestedAssetSerializer,
    NestedAssetTypeSerializer,
    NestedManufacturerSerializer,
)
from assets.api.serializer_mixins import CanonicalSpecificationSerializerMixin
from assets.api.specification_api import (
    command_success_or_raise,
    create_fieldset_selection_from_values,
    if_match_revision,
    owner_detail_specification_payload,
    reject_transport_writes,
)
from assets.models import (
    Asset,
    AssetAssignment,
    AssetDisposal,
    AssetRequest,
    AssetReservation,
    AssetRole,
    AssetTagSequence,
    AssetType,
    Category,
    Depreciation,
    Manufacturer,
    StatusLabel,
    Supplier,
    Warranty,
)
from assets.services.specifications.contracts import (
    DefinitionRevision,
    DestinationAssetTypeSelectionDTO,
    ResourceRevision,
    SpecificationPatchDTO,
)
from core.managers import Scope
from core.mixins import suppress_custom_field_data_validation
from extras.api.serializers import TagSerializer
from itambox.api.base import BaseModelSerializer, reject_unknown_or_writableless
from organization.api.serializers import (
    AssetHolderSerializer,
    ContactAssignmentSerializer,
    NestedLocationSerializer,
    NestedTenantGroupSerializer,
    NestedTenantSerializer,
)
from organization.models import AssetHolder, CostCenter, Location, Tenant, TenantGroup

from ..services.specifications.commands import (
    create_asset_type,
    update_asset_specifications,
    update_asset_type_specifications,
)
from ..specification_adapters import (
    actor_context_for_user,
    authorization_for_asset,
    current_specification_plan,
    discard_staged_image,
    native_asset_type_create_input,
    owner_id_from_result,
    patch_from_mapping,
    stage_uploaded_image,
)

User = get_user_model()


class AssetRoleSerializer(BaseModelSerializer):
    asset_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = AssetRole
        fields = ["id", "name", "slug", "description", "color", "asset_count", "created_at", "updated_at"]
        brief_fields = ["id", "name", "slug", "color"]


class ManufacturerSerializer(BaseModelSerializer):
    asset_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Manufacturer
        fields = ["id", "name", "slug", "description", "asset_count", "created_at", "updated_at"]
        brief_fields = ["id", "name", "slug"]


class StatusLabelSerializer(BaseModelSerializer):
    type_display: serializers.CharField = serializers.CharField(source="get_type_display", read_only=True)
    tags: TagSerializer = TagSerializer(many=True, read_only=True)

    class Meta:
        model = StatusLabel
        fields = [
            "id",
            "name",
            "slug",
            "type",
            "type_display",
            "description",
            "color",
            "tags",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "name", "slug", "type", "color"]


class DepreciationSerializer(BaseModelSerializer):
    class Meta:
        model = Depreciation
        fields = [
            "id",
            "name",
            "months",
            "method",
            "convention",
            "immediate_expense_threshold",
            "description",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "name", "months"]


class AssetTypeSerializer(CanonicalSpecificationSerializerMixin, BaseModelSerializer):
    manufacturer = NestedManufacturerSerializer(read_only=True)
    manufacturer_id = serializers.PrimaryKeyRelatedField(
        queryset=Manufacturer.objects.all(), source="manufacturer", write_only=True
    )
    asset_role = NestedAssetRoleSerializer(read_only=True)
    assetrole_id = serializers.PrimaryKeyRelatedField(
        queryset=AssetRole.objects.all(), source="asset_role", write_only=True, required=False, allow_null=True
    )
    tags = TagSerializer(many=True, read_only=True)
    depreciation = DepreciationSerializer(read_only=True)
    depreciation_id = serializers.PrimaryKeyRelatedField(
        queryset=Depreciation.objects.all(), source="depreciation", write_only=True, required=False, allow_null=True
    )
    fieldsets = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        write_only=True,
        required=False,
        allow_empty=True,
    )
    expected_definition_revision = serializers.CharField(write_only=True, required=False, allow_blank=False)
    preview_token = serializers.CharField(write_only=True, required=False, allow_blank=False)
    expected_category_default_snapshot_revision = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=False,
    )
    specifications = serializers.JSONField(read_only=True, default=dict)
    specification_state = serializers.JSONField(read_only=True, default=dict)
    resource_revision = serializers.CharField(read_only=True, default=None)
    definition_revision = serializers.CharField(read_only=True, default=None)

    class Meta:
        model = AssetType
        fields = [
            "id",
            "model",
            "slug",
            "manufacturer",
            "manufacturer_id",
            "part_number",
            "eol_months",
            "category",
            "asset_role",
            "assetrole_id",
            "depreciation",
            "depreciation_id",
            "fieldsets",
            "specification_patch",
            "expected_definition_revision",
            "preview_token",
            "expected_category_default_snapshot_revision",
            "specifications",
            "specification_state",
            "resource_revision",
            "definition_revision",
            "image",
            "requestable",
            "description",
            "comments",
            "tags",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "model", "slug", "manufacturer"]

    def _native_input(self, validated_data, *, staged_image_id=None):
        return native_asset_type_create_input(validated_data, staged_image_id=staged_image_id)

    def validate(self, data: dict[str, object]) -> dict[str, object]:
        if self.nested:
            return data
        initial = getattr(self, "initial_data", None)
        if isinstance(initial, Mapping):
            reject_transport_writes(initial, self.fields)
        if "fieldsets" in data:
            # Validate identity syntax while the request is still at the
            # transport boundary; resolution and availability remain command
            # responsibilities.
            create_fieldset_selection_from_values(data["fieldsets"], omitted=False)
        return data

    def _specification_payload(self, instance):
        cached = getattr(self, "_specification_payload_cache", None)
        if cached is None or cached.get("_owner_id") != instance.pk:
            cached = owner_detail_specification_payload(instance)
            cached["_owner_id"] = instance.pk
            self._specification_payload_cache = cached
        return cached

    def to_representation(self, instance):
        data = super().to_representation(instance)
        wanted = {
            "fieldsets",
            "specifications",
            "specification_state",
            "resource_revision",
            "definition_revision",
        }
        view = self.context.get("view") if hasattr(self, "context") else None
        if getattr(view, "action", None) != "list" and wanted.intersection(self.fields):
            payload = self._specification_payload(instance)
            for key in wanted:
                if key in self.fields:
                    data[key] = payload.get(key)
        return data

    def create(self, validated_data):
        patch = patch_from_mapping(validated_data.pop("specification_patch", None))
        fieldsets_marker = object()
        raw_fieldsets = validated_data.pop("fieldsets", fieldsets_marker)
        selection = create_fieldset_selection_from_values(
            None if raw_fieldsets is fieldsets_marker else raw_fieldsets,
            omitted=raw_fieldsets is fieldsets_marker,
        )
        expected_definition_revision = validated_data.pop("expected_definition_revision", None)
        preview_token = validated_data.pop("preview_token", None)
        expected_snapshot_revision = validated_data.pop("expected_category_default_snapshot_revision", None)
        actor = actor_context_for_user(self._request_user())
        uploaded = validated_data.get("image")
        stage_id = stage_uploaded_image(actor=actor, uploaded=uploaded) if uploaded else None
        succeeded = False
        try:
            native = self._native_input(validated_data, staged_image_id=stage_id)
            with transaction.atomic():
                result = create_asset_type(
                    actor=actor,
                    native=native,
                    fieldsets=selection,
                    patch=patch,
                    preview_token=preview_token,
                    expected_definition_revision=expected_definition_revision,
                    expected_category_default_snapshot_revision=expected_snapshot_revision,
                )
                command_success_or_raise(result)
                owner_id = owner_id_from_result(result)
            succeeded = True
            # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
            # ambient scope
            return AssetType.all_objects.get(pk=owner_id)
        except (PermissionDenied, DjangoValidationError) as exc:
            self._command_error(exc)
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError("The submitted specification patch is invalid.") from exc
        finally:
            if stage_id is not None and not succeeded:
                discard_staged_image(stage_id=stage_id, actor=actor)

    def update(self, instance, validated_data):
        with transaction.atomic():
            patch_marker = object()
            patch_value = validated_data.pop("specification_patch", patch_marker)
            expected_definition_revision = validated_data.pop("expected_definition_revision", None)
            # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
            # ambient scope
            current = AssetType.all_objects.get(pk=instance.pk)
            if patch_value is not patch_marker:
                try:
                    actor = actor_context_for_user(self._request_user())
                    plan = current_specification_plan(current, target_kind="asset_type")
                    expected_resource_revision = (
                        if_match_revision(self.context.get("request")) or plan.resource_revision
                    )
                    expected_definition_revision = expected_definition_revision or plan.definition_revision
                    result = update_asset_type_specifications(
                        actor=actor,
                        asset_type_id=current.pk,
                        expected_resource_revision=expected_resource_revision,
                        expected_definition_revision=expected_definition_revision,
                        patch=patch_from_mapping(patch_value),
                    )
                    command_success_or_raise(result)
                except (PermissionDenied, DjangoValidationError) as exc:
                    self._command_error(exc)
            current = self._persist_native_update(current, validated_data)
        self.instance = current
        return self.instance


@extend_schema_serializer(component_name="AssetTypeCreatePreviewInput")
class AssetTypeCreatePreviewSerializer(AssetTypeSerializer):
    """Strict native create payload used by the non-persisting preview action."""

    class Meta(AssetTypeSerializer.Meta):
        fields = [
            "manufacturer_id",
            "model",
            "slug",
            "part_number",
            "eol_months",
            "category",
            "assetrole_id",
            "depreciation_id",
            "fieldsets",
            "specification_patch",
            "requestable",
            "description",
            "comments",
        ]


@extend_schema_serializer(component_name="AssetResource")
class AssetSerializer(CanonicalSpecificationSerializerMixin, BaseModelSerializer):
    asset_type = NestedAssetTypeSerializer(read_only=True)
    asset_type_id = serializers.PrimaryKeyRelatedField(
        queryset=AssetType.objects.all(), source="asset_type", write_only=True
    )
    asset_role = NestedAssetRoleSerializer(read_only=True)
    assetrole_id = serializers.PrimaryKeyRelatedField(
        queryset=AssetRole.objects.all(), source="asset_role", write_only=True, required=False, allow_null=True
    )
    location = NestedLocationSerializer(read_only=True)
    location_id: serializers.PrimaryKeyRelatedField[Location] = serializers.PrimaryKeyRelatedField(
        queryset=Location.objects, source="location", write_only=True, required=False, allow_null=True
    )
    tenant = NestedTenantSerializer(read_only=True)
    tenant_id: serializers.PrimaryKeyRelatedField[Tenant] = serializers.PrimaryKeyRelatedField(
        queryset=Tenant.objects, source="tenant", write_only=True, required=False, allow_null=True
    )
    supplier: serializers.StringRelatedField[Supplier] = serializers.StringRelatedField(read_only=True)
    supplier_id = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.all(), source="supplier", write_only=True, required=False, allow_null=True
    )
    status = StatusLabelSerializer(read_only=True)
    status_id = serializers.PrimaryKeyRelatedField(
        queryset=StatusLabel.objects.all(), source="status", write_only=True, required=False, allow_null=True
    )
    last_audited_by: serializers.StringRelatedField[models.Model] = serializers.StringRelatedField(read_only=True)
    tags = TagSerializer(many=True, read_only=True)
    assigned_to = serializers.SerializerMethodField()
    cost_center: serializers.StringRelatedField[models.Model] = serializers.StringRelatedField(read_only=True)
    expected_definition_revision = serializers.CharField(write_only=True, required=False, allow_blank=False)
    specifications = serializers.JSONField(read_only=True, default=dict)
    specification_state = serializers.JSONField(read_only=True, default=dict)
    fieldsets = serializers.ListField(read_only=True, default=list)
    resource_revision = serializers.CharField(read_only=True, default=None)
    definition_revision = serializers.CharField(read_only=True, default=None)

    class Meta:
        model = Asset
        fields = [
            "id",
            "name",
            "asset_tag",
            "serial_number",
            "status",
            "status_id",
            "asset_type",
            "asset_type_id",
            "asset_role",
            "assetrole_id",
            "location",
            "location_id",
            "tenant",
            "tenant_id",
            "purchase_date",
            "purchase_cost",
            "salvage_value",
            "currency",
            "order_number",
            "supplier",
            "supplier_id",
            "cost_center",
            "cost_center_id",
            "last_audited",
            "last_audited_by",
            "specification_patch",
            "expected_definition_revision",
            "fieldsets",
            "specifications",
            "specification_state",
            "resource_revision",
            "definition_revision",
            "requestable",
            "notes",
            "tags",
            "assigned_to",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "name", "asset_tag", "serial_number", "status"]

    def validate(self, data: dict[str, object]) -> dict[str, object]:
        if self.nested:
            return data
        initial = getattr(self, "initial_data", None)
        if isinstance(initial, Mapping):
            reject_transport_writes(initial, self.fields)
        return data

    def to_representation(self, instance: Asset) -> dict[str, object]:
        data = super().to_representation(instance)
        wanted = {"fieldsets", "specifications", "specification_state", "resource_revision", "definition_revision"}
        view = self.context.get("view") if hasattr(self, "context") else None
        if getattr(view, "action", None) != "list" and wanted.intersection(self.fields):
            payload = owner_detail_specification_payload(instance)
            for key in wanted:
                if key in self.fields:
                    data[key] = payload.get(key)
        return data

    def _apply_specification_command(
        self,
        current: Asset,
        target_type_id: int | None,
        patch: SpecificationPatchDTO,
        *,
        expected_resource_revision: ResourceRevision | None = None,
        expected_definition_revision: DefinitionRevision | None = None,
    ) -> None:
        authorization = authorization_for_asset(
            user=self._request_user(),
            tenant_id=current.tenant_id,
        )
        plan = current_specification_plan(current, target_kind="asset", asset_type_id=target_type_id)
        expected_resource_revision = expected_resource_revision or plan.resource_revision
        expected_definition_revision = expected_definition_revision or plan.definition_revision
        result = update_asset_specifications(
            authorization=authorization,
            asset_id=current.pk,
            destination=DestinationAssetTypeSelectionDTO(
                presence="replace" if target_type_id is not None else "keep_current",
                asset_type_id=target_type_id,
            ),
            expected_resource_revision=expected_resource_revision,
            expected_definition_revision=expected_definition_revision,
            patch=patch,
        )
        command_success_or_raise(result)

    def create(self, validated_data: dict[str, object]) -> Asset:
        if "specification_patch" not in validated_data:
            return super().create(validated_data)
        patch = patch_from_mapping(validated_data.pop("specification_patch", None))
        target_type = validated_data.get("asset_type")
        if target_type is None:
            raise serializers.ValidationError(
                {"asset_type_id": self.fields["asset_type_id"].error_messages["required"]}
            )
        with transaction.atomic():
            instance = Asset(**validated_data, custom_field_data={})
            with suppress_custom_field_data_validation(instance):
                instance.save()
            # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
            # ambient scope
            current = Asset._base_manager.get(pk=instance.pk)
            try:
                self._apply_specification_command(current, target_type.pk, patch)
            except (PermissionDenied, DjangoValidationError) as exc:
                self._command_error(exc)
            # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
            # ambient scope
            self.instance = Asset._base_manager.get(pk=current.pk)
        return self.instance

    def update(self, instance: Asset, validated_data: dict[str, object]) -> Asset:
        with transaction.atomic():
            patch_marker = object()
            patch_value = validated_data.pop("specification_patch", patch_marker)
            expected_definition_revision = validated_data.pop("expected_definition_revision", None)
            target_marker = object()
            target_type = validated_data.pop("asset_type", target_marker)
            # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
            # ambient scope
            current = Asset._base_manager.get(pk=instance.pk)

            type_changed = target_type is not target_marker and target_type.pk != current.asset_type_id
            if patch_value is not patch_marker or type_changed:
                try:
                    expected_resource_revision = if_match_revision(self.context.get("request"))
                    self._apply_specification_command(
                        current,
                        current.asset_type_id if target_type is target_marker else target_type.pk,
                        patch_from_mapping(None if patch_value is patch_marker else patch_value),
                        expected_resource_revision=expected_resource_revision,
                        expected_definition_revision=expected_definition_revision,
                    )
                except (PermissionDenied, DjangoValidationError) as exc:
                    self._command_error(exc)
            current = self._persist_native_update(current, validated_data)
        self.instance = current
        return self.instance

    def get_fields(self) -> dict[str, serializers.Field[object, object, object, object]]:
        fields = super().get_fields()

        fields["cost_center_id"] = serializers.PrimaryKeyRelatedField(
            source="cost_center",
            queryset=CostCenter.objects,
            allow_null=True,
            required=False,
            write_only=True,
        )
        return fields

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_assigned_to(self, obj: Asset) -> dict[str, object] | None:
        cached = getattr(obj, "_active_assignments", None)
        if cached is not None:
            active = cached[0] if cached else None
        else:
            active = obj.active_assignment
        if not active:
            return None
        target = active.assigned_to
        if target is None:
            return None
        return {
            "id": target.pk,
            "type": active.assigned_to_type,
            "name": str(target),
            "is_active": active.is_active,
            "checked_out_at": active.checked_out_at,
            "expected_checkin_date": active.expected_checkin_date,
        }


class SupplierSerializer(BaseModelSerializer):
    tags = TagSerializer(many=True, read_only=True)
    contacts = ContactAssignmentSerializer(many=True, read_only=True)
    tenant = NestedTenantSerializer(read_only=True)
    tenant_id: serializers.PrimaryKeyRelatedField[Tenant] = serializers.PrimaryKeyRelatedField(
        queryset=Tenant.objects, source="tenant", write_only=True, required=False, allow_null=True
    )
    tenant_group = NestedTenantGroupSerializer(read_only=True)
    tenant_group_id: serializers.PrimaryKeyRelatedField[TenantGroup] = serializers.PrimaryKeyRelatedField(
        queryset=TenantGroup.objects, source="tenant_group", write_only=True, required=False, allow_null=True
    )

    def validate(self, attrs):
        # The database enforces tenant XOR tenant_group; surface a rejected
        # payload as a 400 instead of letting it hit the constraint as a 500.
        tenant = attrs.get("tenant", getattr(self.instance, "tenant", None))
        tenant_group = attrs.get("tenant_group", getattr(self.instance, "tenant_group", None))
        if tenant is not None and tenant_group is not None:
            raise serializers.ValidationError(
                {"tenant_group_id": _("A supplier is scoped to a tenant or a tenant group, not both.")}
            )
        return attrs

    class Meta:
        model = Supplier
        fields = [
            "id",
            "name",
            "slug",
            "website",
            "portal_url",
            "account_id",
            "address",
            "notes",
            "is_active",
            "tenant",
            "tenant_id",
            "tenant_group",
            "tenant_group_id",
            "tags",
            "contacts",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "name", "slug"]


class CategorySerializer(BaseModelSerializer):
    tags = TagSerializer(many=True, read_only=True)

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "color", "description", "applies_to", "tags", "created_at", "updated_at"]
        brief_fields = ["id", "name", "slug", "color"]


@extend_schema_serializer(component_name="AssetRequest")
class AssetRequestSerializer(BaseModelSerializer):
    requester = serializers.StringRelatedField(read_only=True)
    requester_id = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), source="requester", write_only=True)
    asset = NestedAssetSerializer(read_only=True)
    asset_id = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects, source="asset", write_only=True, required=False, allow_null=True
    )
    asset_type = NestedAssetTypeSerializer(read_only=True)
    asset_type_id = serializers.PrimaryKeyRelatedField(
        queryset=AssetType.objects.all(), source="asset_type", write_only=True, required=False, allow_null=True
    )
    responded_by = serializers.StringRelatedField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    tags = TagSerializer(many=True, read_only=True)

    class Meta:
        model = AssetRequest
        fields = [
            "id",
            "requester",
            "requester_id",
            "asset",
            "asset_id",
            "asset_type",
            "asset_type_id",
            "status",
            "status_display",
            "request_date",
            "response_date",
            "responded_by",
            "notes",
            "response_notes",
            "tags",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "requester", "asset", "status", "request_date"]


class AssetTagSequenceBulkSerializer(serializers.ListSerializer):
    def validate(self, attrs: list[dict[str, object]]) -> list[dict[str, object]]:
        initial = getattr(self, "initial_data", None)
        if not isinstance(initial, list):
            return attrs

        for item in initial:
            if isinstance(item, Mapping):
                reject_unknown_or_writableless(item, self.child.fields)
        return attrs


class AssetTagSequenceSerializer(BaseModelSerializer):
    class Meta:
        model = AssetTagSequence
        fields = [
            "id",
            "prefix",
            "next_value",
            "zero_padding",
            "tenant",
            "category",
            "is_active",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "prefix", "next_value"]
        list_serializer_class = AssetTagSequenceBulkSerializer

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        initial = getattr(self, "initial_data", None)
        # nested=True uses the base class's attribute-lookup shortcut and bulk
        # (many=True) children carry no initial_data; neither path has a
        # submitted-field mapping to check.
        if self.nested or not isinstance(initial, Mapping):
            return attrs

        reject_unknown_or_writableless(initial, self.fields)
        return attrs


class AssetAssignmentSerializer(BaseModelSerializer):
    asset = NestedAssetSerializer(read_only=True)
    asset_id: serializers.PrimaryKeyRelatedField[Asset] = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects, source="asset", write_only=True
    )
    assigned_to_type = serializers.CharField(read_only=True)
    assigned_to_name = serializers.SerializerMethodField()
    checked_out_by_name = serializers.CharField(source="checked_out_by.username", read_only=True)

    class Meta:
        model = AssetAssignment
        fields = [
            "id",
            "asset",
            "asset_id",
            "assigned_user",
            "assigned_location",
            "assigned_asset",
            "assigned_to_type",
            "assigned_to_name",
            "checked_out_by",
            "checked_out_by_name",
            "checked_out_at",
            "expected_checkin_date",
            "is_active",
            "checked_in_at",
            "checked_in_by",
            "notes",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "asset", "assigned_to_name", "is_active"]

    @extend_schema_field(OpenApiTypes.STR)
    def get_assigned_to_name(self, obj: AssetAssignment) -> str | None:
        try:
            return str(obj.assigned_to)
        except Exception:
            return None


class AssetDisposalSerializer(BaseModelSerializer):
    asset = NestedAssetSerializer(read_only=True)
    asset_id = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects,
        source="asset",
        write_only=True,
    )
    disposal_method_display = serializers.CharField(source="get_disposal_method_display", read_only=True)
    data_sanitization_method_display = serializers.CharField(
        source="get_data_sanitization_method_display", read_only=True
    )
    # Cancellation is owned by the service: read-only over the API.
    is_active = serializers.BooleanField(read_only=True)
    cancelled_at = serializers.DateTimeField(read_only=True)
    cancelled_by_name = serializers.SerializerMethodField()
    cancellation_reason = serializers.CharField(read_only=True)

    @extend_schema_field(OpenApiTypes.STR)
    def get_cancelled_by_name(self, obj: AssetDisposal) -> str | None:
        return str(obj.cancelled_by) if obj.cancelled_by_id else None

    def validate(self, attrs):
        if self.instance is not None and "asset" in attrs and attrs["asset"] != self.instance.asset:
            raise serializers.ValidationError({"asset_id": _("The asset of a disposal record cannot be changed.")})
        return attrs

    class Meta:
        model = AssetDisposal
        fields = [
            "id",
            "asset",
            "asset_id",
            "is_active",
            "disposal_method",
            "disposal_method_display",
            "disposal_date",
            "data_sanitization_method",
            "data_sanitization_method_display",
            "sanitization_certificate",
            "sanitized_by",
            "recipient",
            "proceeds",
            "currency",
            "weee_compliant",
            "notes",
            "cancelled_at",
            "cancelled_by_name",
            "cancellation_reason",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "asset", "disposal_method", "disposal_date", "is_active"]


class AssetDisposalCancelSerializer(serializers.Serializer):
    """Reason-only payload for cancelling a disposal (#496).

    Cancellation state itself is never writable: ``cancel_asset_disposal`` owns
    the actor, the timestamp and the reason validation.
    """

    reason = serializers.CharField(trim_whitespace=True)

    def validate_reason(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError(_("A cancellation reason is required."))
        return value.strip()


class WarrantySerializer(BaseModelSerializer):
    asset = NestedAssetSerializer(read_only=True)
    asset_id = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects,
        source="asset",
        write_only=True,
    )
    # Tenant is derived from the parent asset (no direct field); expose it
    # read-only via the model property. There is deliberately no writable
    # tenant_id — the boundary follows asset.tenant.
    tenant = NestedTenantSerializer(read_only=True)
    supplier: serializers.StringRelatedField[Supplier] = serializers.StringRelatedField(read_only=True)
    supplier_id = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.all(), source="supplier", write_only=True, required=False, allow_null=True
    )
    warranty_type_display = serializers.CharField(source="get_warranty_type_display", read_only=True)
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = Warranty
        fields = [
            "id",
            "asset",
            "asset_id",
            "tenant",
            "warranty_type",
            "warranty_type_display",
            "supplier",
            "supplier_id",
            "start_date",
            "end_date",
            "is_active",
            "cost",
            "currency",
            "reference",
            "terms",
            "notes",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "asset", "warranty_type", "start_date", "end_date"]


class AssetReservationSerializer(BaseModelSerializer):
    asset = NestedAssetSerializer(read_only=True)
    asset_id = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects,
        source="asset",
        write_only=True,
    )
    # Tenant is derived from the parent asset (no direct field); expose it
    # read-only via the model property. There is deliberately no writable
    # tenant_id — the boundary follows asset.tenant.
    tenant = NestedTenantSerializer(read_only=True)
    reserved_for = AssetHolderSerializer(read_only=True)
    reserved_for_id = serializers.PrimaryKeyRelatedField(
        queryset=AssetHolderSerializer.Meta.model.objects,
        source="reserved_for",
        write_only=True,
        required=False,
        allow_null=True,
    )
    # `created_by` is forced to request.user in the viewset's perform_create;
    # there is deliberately no writable created_by_id (it would let a client
    # reattribute a reservation to another user on create/PATCH).
    created_by = serializers.StringRelatedField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = AssetReservation
        fields = [
            "id",
            "asset",
            "asset_id",
            "tenant",
            "reserved_for",
            "reserved_for_id",
            "start_date",
            "end_date",
            "status",
            "status_display",
            "created_by",
            "purpose",
            "notes",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "asset", "reserved_for", "status", "start_date", "end_date"]


class AssetCheckOutAPISerializer(serializers.Serializer[object]):
    holder_id = serializers.IntegerField(required=False, allow_null=True)
    location_id = serializers.IntegerField(required=False, allow_null=True)
    asset_target_id = serializers.IntegerField(required=False, allow_null=True)
    status_id = serializers.PrimaryKeyRelatedField(
        queryset=StatusLabel.objects.filter(type="deployed"), required=False, allow_null=True
    )
    expected_checkin = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, default="", allow_blank=True)

    def validate(self, data: dict[str, object]) -> dict[str, object]:
        holder_id = data.get("holder_id")
        location_id = data.get("location_id")
        asset_target_id = data.get("asset_target_id")

        # Validate that exactly one assignment target is specified
        targets = [holder_id, location_id, asset_target_id]
        filled = [t for t in targets if t is not None]

        if not filled:
            raise serializers.ValidationError(_("Either holder_id, location_id, or asset_target_id must be provided."))
        if len(filled) > 1:
            raise serializers.ValidationError(_("You can only check out an asset to ONE target."))

        from assets.models import Asset

        # Resolve entity references
        if holder_id:
            try:
                data["holder"] = AssetHolder.objects.get(pk=holder_id)
            except AssetHolder.DoesNotExist:
                raise serializers.ValidationError({"holder_id": _("Specified holder does not exist.")}) from None
        elif location_id:
            try:
                data["location"] = Location.objects.get(pk=location_id)
            except Location.DoesNotExist:
                raise serializers.ValidationError({"location_id": _("Specified location does not exist.")}) from None
        elif asset_target_id:
            try:
                data["asset_target"] = Asset.objects.for_scope(Scope.current()).get(pk=asset_target_id)
            except Asset.DoesNotExist:
                raise serializers.ValidationError(
                    {"asset_target_id": _("Specified target asset does not exist.")}
                ) from None

        return data


class AssetCheckInAPISerializer(serializers.Serializer):
    status_id = serializers.PrimaryKeyRelatedField(
        queryset=StatusLabel.objects.exclude(type="deployed"), required=False, allow_null=True
    )
    location_id = serializers.PrimaryKeyRelatedField(queryset=Location.objects, required=False, allow_null=True)
    checkin_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, default="", allow_blank=True)
