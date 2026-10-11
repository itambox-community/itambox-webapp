import re

from django.utils.translation import gettext as _
from rest_framework import serializers

from assets.api.nested_serializers import NestedAssetSerializer, NestedAssetTypeSerializer, NestedSupplierSerializer
from assets.models import Asset, AssetType, Supplier
from core.managers import Scope
from inventory.api.serializers import (
    NestedAccessorySerializer,
    NestedComponentSerializer,
    NestedConsumableSerializer,
)
from inventory.models import Accessory, Component, Consumable
from itambox.api.base import BaseModelSerializer
from licenses.models import License
from organization.api.serializers import NestedLocationSerializer, NestedTenantSerializer
from organization.models import CostCenter, Location, Tenant
from procurement.models import Contract, PurchaseOrder, PurchaseOrderLine


class ContractSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:procurement_api:contract-detail")

    tenant = NestedTenantSerializer(read_only=True)
    tenant_id: serializers.PrimaryKeyRelatedField[Tenant] = serializers.PrimaryKeyRelatedField(
        source="tenant",
        write_only=True,
        required=False,
        allow_null=True,
        queryset=Tenant.objects,
    )

    supplier = NestedSupplierSerializer(read_only=True)
    supplier_id: serializers.PrimaryKeyRelatedField[Supplier] = serializers.PrimaryKeyRelatedField(
        source="supplier",
        write_only=True,
        required=False,
        allow_null=True,
        queryset=Supplier.objects.all(),
    )

    cost_center_display = serializers.SerializerMethodField(read_only=True)
    cost_center_id: serializers.PrimaryKeyRelatedField[CostCenter] = serializers.PrimaryKeyRelatedField(
        source="cost_center",
        queryset=CostCenter.objects,
        write_only=True,
        required=False,
        allow_null=True,
    )

    assets = NestedAssetSerializer(many=True, read_only=True)
    assets_ids: serializers.PrimaryKeyRelatedField[Asset] = serializers.PrimaryKeyRelatedField(
        source="assets",
        many=True,
        write_only=True,
        required=False,
        queryset=Asset.objects,
    )

    contract_type_display = serializers.CharField(source="get_contract_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    billing_cycle_display = serializers.CharField(source="get_billing_cycle_display", read_only=True)

    days_until_expiry = serializers.IntegerField(read_only=True)

    class Meta:
        model = Contract
        fields = [
            "id",
            "url",
            "display",
            "name",
            "contract_number",
            "contract_type",
            "contract_type_display",
            "status",
            "status_display",
            "tenant",
            "tenant_id",
            "supplier",
            "supplier_id",
            "cost",
            "currency",
            "billing_cycle",
            "billing_cycle_display",
            "start_date",
            "end_date",
            "renewal_date",
            "auto_renew",
            "sla_response_time",
            "sla_resolution_time",
            "coverage_hours",
            "sla_terms",
            "assets",
            "assets_ids",
            "purchase_order",
            "cost_center_display",
            "cost_center_id",
            "notes",
            "days_until_expiry",
            "created_at",
            "updated_at",
        ]
        brief_fields = [
            "id",
            "url",
            "display",
            "name",
            "contract_number",
            "status",
            "end_date",
        ]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        attrs = super().validate(attrs)
        purchase_order = attrs.get("purchase_order", self.instance.purchase_order if self.instance else None)
        tenant = attrs["tenant"] if "tenant" in attrs else (self.instance.tenant if self.instance else None)
        tenant_id = tenant.pk if tenant else None
        if purchase_order is not None and purchase_order.tenant_id != tenant_id:
            raise serializers.ValidationError(
                {"purchase_order": _("The purchase order must belong to the same tenant as the contract.")}
            )
        return attrs

    def get_cost_center_display(self, obj: Contract) -> dict[str, object] | None:
        cc = obj.cost_center
        if cc is None:
            return None
        return {"id": cc.pk, "name": str(cc)}


class PurchaseOrderLineSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:procurement_api:purchaseorderline-detail")
    qty_received = serializers.IntegerField(read_only=True)

    tenant = NestedTenantSerializer(read_only=True)
    tenant_id: serializers.PrimaryKeyRelatedField[Tenant] = serializers.PrimaryKeyRelatedField(
        source="tenant",
        write_only=True,
        required=False,
        allow_null=True,
        queryset=Tenant.objects,
    )

    purchase_order: serializers.PrimaryKeyRelatedField[PurchaseOrder] = serializers.PrimaryKeyRelatedField(
        read_only=True
    )
    purchase_order_id: serializers.PrimaryKeyRelatedField[PurchaseOrder] = serializers.PrimaryKeyRelatedField(
        source="purchase_order",
        write_only=True,
        queryset=PurchaseOrder.objects,
    )

    asset_type = NestedAssetTypeSerializer(read_only=True)
    asset_type_id: serializers.PrimaryKeyRelatedField[AssetType] = serializers.PrimaryKeyRelatedField(
        source="asset_type",
        queryset=AssetType.objects,
        write_only=True,
        required=False,
        allow_null=True,
    )

    component = NestedComponentSerializer(read_only=True)
    component_id: serializers.PrimaryKeyRelatedField[Component] = serializers.PrimaryKeyRelatedField(
        source="component",
        queryset=Component.objects,
        write_only=True,
        required=False,
        allow_null=True,
    )

    accessory = NestedAccessorySerializer(read_only=True)
    accessory_id: serializers.PrimaryKeyRelatedField[Accessory] = serializers.PrimaryKeyRelatedField(
        source="accessory",
        queryset=Accessory.objects,
        write_only=True,
        required=False,
        allow_null=True,
    )

    consumable = NestedConsumableSerializer(read_only=True)
    consumable_id: serializers.PrimaryKeyRelatedField[Consumable] = serializers.PrimaryKeyRelatedField(
        source="consumable",
        queryset=Consumable.objects,
        write_only=True,
        required=False,
        allow_null=True,
    )

    license_display = serializers.SerializerMethodField(read_only=True)
    license_id: serializers.PrimaryKeyRelatedField[License] = serializers.PrimaryKeyRelatedField(
        source="license",
        queryset=License.objects,
        write_only=True,
        required=False,
        allow_null=True,
    )

    qty_outstanding = serializers.IntegerField(read_only=True)
    total_cost = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    currency = serializers.CharField(read_only=True)

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.fields["purchase_order_id"].queryset = PurchaseOrder.objects.for_scope(Scope.current())

    class Meta:
        model = PurchaseOrderLine
        fields = [
            "id",
            "url",
            "display",
            "tenant",
            "tenant_id",
            "purchase_order",
            "purchase_order_id",
            "asset_type",
            "asset_type_id",
            "component",
            "component_id",
            "accessory",
            "accessory_id",
            "consumable",
            "consumable_id",
            "license_display",
            "license_id",
            "qty_ordered",
            "qty_received",
            "qty_outstanding",
            "unit_price",
            "total_cost",
            "currency",
            "created_at",
            "updated_at",
        ]
        brief_fields = [
            "id",
            "url",
            "display",
            "qty_ordered",
            "qty_received",
        ]

    def get_license_display(self, obj: PurchaseOrderLine) -> dict[str, object] | None:
        lic = obj.license
        if lic is None:
            return None
        return {"id": lic.pk, "name": str(lic)}

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        attrs = super().validate(attrs)
        if "qty_received" not in self.initial_data:
            return attrs

        message = "Use the purchase order /receive/ action to change qty_received."
        try:
            submitted_quantity = serializers.IntegerField(min_value=0).run_validation(self.initial_data["qty_received"])
        except serializers.ValidationError as exc:
            raise serializers.ValidationError({"qty_received": message}) from exc

        current_quantity = self.instance.qty_received if self.instance else 0
        if submitted_quantity != current_quantity:
            raise serializers.ValidationError({"qty_received": message})
        return attrs


class PurchaseOrderReceiveSerializer(serializers.Serializer[object]):
    line_quantities = serializers.DictField(child=serializers.IntegerField(min_value=0))
    expected_received = serializers.DictField(
        child=serializers.IntegerField(min_value=0),
        help_text=(
            "The recorded received quantity per line id this submission was prepared against. "
            "Stale submissions, replays, and parallel duplicates whose lines have moved on are refused."
        ),
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        # Sparse-field controls apply to model response serializers, not this
        # action's fixed request payload.
        kwargs.pop("fields", None)
        kwargs.pop("omit", None)
        super().__init__(*args, **kwargs)

    def validate_line_quantities(self, value: dict[str, int]) -> dict[int, int]:
        if any(not re.fullmatch(r"[1-9][0-9]*", line_id) for line_id in value):
            raise serializers.ValidationError("Line IDs must be canonical positive decimal integers.")
        quantities = {int(line_id): quantity for line_id, quantity in value.items()}

        if not any(quantity > 0 for quantity in quantities.values()):
            raise serializers.ValidationError("At least one line quantity must be positive.")

        purchase_order = self.context.get("purchase_order")
        if purchase_order is None:
            raise serializers.ValidationError("Purchase order context is required.")

        owned_line_ids = set(purchase_order.lines.filter(pk__in=quantities).values_list("pk", flat=True))
        if owned_line_ids != set(quantities):
            raise serializers.ValidationError("Every line must belong to this purchase order.")
        return quantities

    def validate_expected_received(self, value: dict[str, int]) -> dict[int, int]:
        if any(not re.fullmatch(r"[1-9][0-9]*", line_id) for line_id in value):
            raise serializers.ValidationError("Line IDs must be canonical positive decimal integers.")
        expected = {int(line_id): quantity for line_id, quantity in value.items()}

        purchase_order = self.context.get("purchase_order")
        if purchase_order is None:
            raise serializers.ValidationError("Purchase order context is required.")

        owned_line_ids = set(purchase_order.lines.filter(pk__in=expected).values_list("pk", flat=True))
        if owned_line_ids != set(expected):
            raise serializers.ValidationError("Every line must belong to this purchase order.")
        return expected

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        missing = sorted(
            line_id
            for line_id, quantity in attrs["line_quantities"].items()
            if quantity > 0 and line_id not in attrs["expected_received"]
        )
        if missing:
            raise serializers.ValidationError(
                {
                    "expected_received": (
                        "Every line with a positive quantity must state the recorded received "
                        f"quantity it was prepared against (missing: {missing})."
                    )
                }
            )
        return attrs


class PurchaseOrderActionResponseSerializer(serializers.Serializer):
    message = serializers.CharField()


class PurchaseOrderSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:procurement_api:purchaseorder-detail")
    status = serializers.ChoiceField(choices=PurchaseOrder.STATUS_CHOICES, read_only=True)

    tenant = NestedTenantSerializer(read_only=True)
    tenant_id: serializers.PrimaryKeyRelatedField[Tenant] = serializers.PrimaryKeyRelatedField(
        source="tenant",
        write_only=True,
        required=False,
        allow_null=True,
        queryset=Tenant.objects,
    )

    supplier = NestedSupplierSerializer(read_only=True)
    supplier_id = serializers.PrimaryKeyRelatedField(
        source="supplier",
        write_only=True,
        queryset=Supplier.objects.all(),
    )

    destination_location = NestedLocationSerializer(read_only=True)
    destination_location_id: serializers.PrimaryKeyRelatedField[Location] = serializers.PrimaryKeyRelatedField(
        source="destination_location",
        write_only=True,
        queryset=Location.objects,
    )

    created_by_display = serializers.SerializerMethodField(read_only=True)

    status_display = serializers.CharField(source="get_status_display", read_only=True)

    lines = PurchaseOrderLineSerializer(many=True, read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = [
            "id",
            "url",
            "display",
            "order_number",
            "status",
            "status_display",
            "tenant",
            "tenant_id",
            "supplier",
            "supplier_id",
            "currency",
            "order_date",
            "expected_delivery_date",
            "destination_location",
            "destination_location_id",
            "notes",
            "created_by_display",
            "lines",
            "created_at",
            "updated_at",
        ]
        brief_fields = [
            "id",
            "url",
            "display",
            "order_number",
            "status",
        ]

    def get_created_by_display(self, obj: PurchaseOrder) -> dict[str, object] | None:
        user = obj.created_by
        if user is None:
            return None
        return {"id": user.pk, "name": str(user)}

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        attrs = super().validate(attrs)
        if "status" not in self.initial_data:
            return attrs

        current_status = self.instance.status if self.instance else PurchaseOrder.STATUS_DRAFT
        requested_status = self.initial_data["status"]
        if requested_status != current_status:
            action_by_status = {
                PurchaseOrder.STATUS_DRAFT: "reopen",
                PurchaseOrder.STATUS_APPROVED: "approve",
                PurchaseOrder.STATUS_ORDERED: "order",
                PurchaseOrder.STATUS_PARTIAL: "receive",
                PurchaseOrder.STATUS_RECEIVED: "receive",
                PurchaseOrder.STATUS_CANCELLED: "cancel",
            }
            action = action_by_status.get(requested_status)
            if action is None:
                raise serializers.ValidationError(
                    {
                        "status": "Purchase order status changes must use a lifecycle action "
                        "(/approve/, /order/, /receive/, /cancel/, /reopen/)."
                    }
                )
            raise serializers.ValidationError({"status": f"Use the /{action}/ action to change purchase order status."})
        return attrs
