from django.contrib.auth import get_user_model
from django.db import models
from rest_framework import serializers

from assets.api.nested_serializers import NestedSupplierSerializer
from assets.models import Supplier
from core.managers import Scope, get_current_tenant
from extras.api.serializers import TagSerializer
from itambox.api.base import BaseModelSerializer
from itambox.api.fields import validate_gfk_target_tenant
from organization.api.serializers import NestedTenantSerializer
from organization.models import CostCenter, Membership, Tenant
from procurement.models import Contract
from subscriptions.models import Subscription, SubscriptionAssignment, SubscriptionStatusChoices

User = get_user_model()


def _tenant_member_user_queryset():
    """Users that are members of the active tenant (else, for the
    superuser/global context where no tenant is bound, all users).

    Used to scope ``owner_id`` / ``assigned_by_id`` so a tenant-scoped request
    cannot assign or attribute records to users from another tenant.
    """
    from core.managers import get_current_tenant

    tenant = get_current_tenant()
    if tenant is None:
        return User.objects.all()
    member_ids = Membership.objects.filter(tenant=tenant).values_list("user_id", flat=True)
    return User.objects.filter(pk__in=member_ids)


class SubscriptionSerializer(BaseModelSerializer):
    auto_renewal = serializers.BooleanField(required=False)
    supplier = NestedSupplierSerializer(read_only=True)
    supplier_id: serializers.PrimaryKeyRelatedField[Supplier] = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.filter(is_active=True), source="supplier", write_only=True
    )
    linked_contract: serializers.StringRelatedField[Contract] = serializers.StringRelatedField(read_only=True)
    linked_contract_id: serializers.PrimaryKeyRelatedField[Contract] = serializers.PrimaryKeyRelatedField(
        queryset=Contract.objects, source="linked_contract", write_only=True, required=False, allow_null=True
    )
    tenant = NestedTenantSerializer(read_only=True)
    tenant_id: serializers.PrimaryKeyRelatedField[Tenant] = serializers.PrimaryKeyRelatedField(
        queryset=Tenant.objects, source="tenant", write_only=True, required=False, allow_null=True
    )
    owner: serializers.StringRelatedField[models.Model] = serializers.StringRelatedField(read_only=True)
    owner_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source="owner", write_only=True, required=False, allow_null=True
    )
    tags = TagSerializer(many=True, read_only=True)
    type_display = serializers.CharField(source="get_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    billing_cycle_display = serializers.CharField(source="get_billing_cycle_display", read_only=True)
    days_until_renewal = serializers.IntegerField(read_only=True)
    annual_cost = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    cost_center: serializers.StringRelatedField[CostCenter] = serializers.StringRelatedField(read_only=True)
    cost_center_id: serializers.PrimaryKeyRelatedField[CostCenter] = serializers.PrimaryKeyRelatedField(
        source="cost_center",
        queryset=CostCenter.objects,
        write_only=True,
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Subscription
        fields = (
            "id",
            "name",
            "slug",
            "supplier",
            "supplier_id",
            "type",
            "type_display",
            "status",
            "status_display",
            "tenant",
            "tenant_id",
            "owner",
            "owner_id",
            "start_date",
            "renewal_date",
            "renewal_cost",
            "currency",
            "billing_cycle",
            "billing_cycle_display",
            "term_months",
            "vendor_contract_auto_renews",
            "auto_renewal",
            "licensed_quantity",
            "contract_reference",
            "linked_contract",
            "linked_contract_id",
            "cost_center",
            "cost_center_id",
            "cancellation_date",
            "days_until_renewal",
            "annual_cost",
            "description",
            "notes",
            "tags",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "status",
            "cancellation_date",
            "created_at",
            "updated_at",
            "days_until_renewal",
            "annual_cost",
        )
        brief_fields = ("id", "name", "slug", "supplier", "status", "status_display", "days_until_renewal")

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.fields["supplier_id"].queryset = Supplier.objects.for_scope(Scope.current()).filter(is_active=True)
        self.fields["linked_contract_id"].queryset = Contract.objects.for_scope(Scope.current())
        if "owner_id" in self.fields:
            self.fields["owner_id"].queryset = _tenant_member_user_queryset()

    def _effective_tenant(self, validated: dict[str, object]) -> Tenant | None:
        if "tenant" in validated:
            tenant = validated["tenant"]
            return tenant if isinstance(tenant, Tenant) else None
        if self.instance is not None:
            instance_tenant = self.instance.tenant
            return instance_tenant if isinstance(instance_tenant, Tenant) else None
        # On create, the active tenant is injected after serializer validation
        # in perform_create(); the relation checks must already see it so the
        # tenant's own suppliers and contracts stay selectable when the
        # optional tenant_id is omitted.
        current = get_current_tenant()
        return current if isinstance(current, Tenant) else None

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        lifecycle_errors = {}
        current_status = self.instance.status if self.instance else SubscriptionStatusChoices.ACTIVE
        if "status" in self.initial_data and self.initial_data["status"] != current_status:
            lifecycle_errors["status"] = "Use the explicit subscription lifecycle actions."
        current_cancellation_date = self.instance.cancellation_date if self.instance else None
        if "cancellation_date" in self.initial_data:
            supplied_date = self.initial_data["cancellation_date"] or None
            if supplied_date != (str(current_cancellation_date) if current_cancellation_date else None):
                lifecycle_errors["cancellation_date"] = "Use the explicit cancel action."
        if lifecycle_errors:
            raise serializers.ValidationError(lifecycle_errors)

        legacy_value = attrs.pop("auto_renewal", serializers.empty)
        canonical_value = attrs.get("vendor_contract_auto_renews", serializers.empty)
        if legacy_value is not serializers.empty:
            if canonical_value is not serializers.empty and canonical_value != legacy_value:
                raise serializers.ValidationError({"auto_renewal": "Conflicts with vendor_contract_auto_renews."})
            attrs["vendor_contract_auto_renews"] = legacy_value
        validated = super().validate(attrs)
        effective_tenant = self._effective_tenant(validated)
        tenant_id = effective_tenant.pk if effective_tenant else None
        supplier = validated.get("supplier", self.instance.supplier if self.instance else None)
        if supplier and supplier.tenant_id is not None and supplier.tenant_id != tenant_id:
            raise serializers.ValidationError({"supplier_id": "Selected supplier belongs to a different tenant."})
        linked_contract = validated.get("linked_contract", self.instance.linked_contract if self.instance else None)
        if linked_contract and linked_contract.tenant_id is not None and linked_contract.tenant_id != tenant_id:
            raise serializers.ValidationError(
                {"linked_contract_id": "Selected contract belongs to a different tenant."}
            )
        return validated

    def to_representation(self, instance: Subscription) -> dict[str, object]:
        data = super().to_representation(instance)
        data["auto_renewal"] = instance.vendor_contract_auto_renews
        return data

    def update(self, instance: Subscription, validated_data: dict[str, object]) -> Subscription:
        if not validated_data:
            return instance
        return super().update(instance, validated_data)


class SubscriptionAssignmentSerializer(BaseModelSerializer):
    subscription = SubscriptionSerializer(read_only=True)
    subscription_id: serializers.PrimaryKeyRelatedField[Subscription] = serializers.PrimaryKeyRelatedField(
        queryset=Subscription.objects, source="subscription", write_only=True
    )
    assigned_object = serializers.SerializerMethodField(read_only=True)
    assigned_by: serializers.StringRelatedField[models.Model] = serializers.StringRelatedField(read_only=True)
    assigned_by_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source="assigned_by", write_only=True, required=False, allow_null=True
    )

    class Meta:
        model = SubscriptionAssignment
        fields = (
            "id",
            "subscription",
            "subscription_id",
            "content_type",
            "object_id",
            "assigned_object",
            "assigned_by",
            "assigned_by_id",
            "assigned_date",
            "notes",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("created_at", "updated_at", "assigned_date")
        brief_fields = ("id", "subscription", "assigned_object")

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        if "assigned_by_id" in self.fields:
            self.fields["assigned_by_id"].queryset = _tenant_member_user_queryset()

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        attrs = super().validate(attrs)
        content_type = attrs.get("content_type")
        object_id = attrs.get("object_id")
        if content_type is None and self.instance is not None:
            content_type = getattr(self.instance, "content_type", None)
        if object_id is None and self.instance is not None:
            object_id = getattr(self.instance, "object_id", None)
        validate_gfk_target_tenant(content_type, object_id)
        return attrs

    def get_assigned_object(self, obj: SubscriptionAssignment) -> dict[str, object] | None:
        target = obj.tenant_safe_assigned_object
        if target:
            return {
                "id": obj.object_id,
                "type": obj.content_type.model,
                "name": str(target),
            }
        return None
